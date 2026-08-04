from contextlib import asynccontextmanager
from pathlib import Path
import time
import uuid
from typing import Literal, Optional

import uvicorn
from fastapi import BackgroundTasks, Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.clients.minio_utils import resolve_object_urls
from app.clients.mongo_history_utils import *
from app.clients.session_attachment_utils import (
    delete_session_attachments,
    store_session_attachment,
    validate_session_attachment_refs,
)
from app.conf.chat_attachment_config import chat_attachment_config
from app.core.logger import logger
from app.observability.langfuse_monitor import (
    create_query_trace_id,
    flush_langfuse,
    submit_trace_feedback,
    trace_query,
)
from app.observability.prometheus_metrics import install_prometheus, observe_feedback, observe_run
from app.observability.quality_metrics import analyze_query_state
from app.observability.rag_observability import score_query_result, summarize_image_reasoning
from app.runtime.config import load_runtime_config
from app.runtime.run_store import RunStatus, get_run_store, run_owner
from app.security.auth import Principal, require_role
from app.security.http import configure_http_security
from app.security.tenancy import safe_upload_filename, scoped_session_id
from app.utils.sse_utils import SSEEvent, create_sse_queue, push_to_session, sse_generator
from app.utils.task_utils import *


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    flush_langfuse()


app = FastAPI(title="query service", description="设备文档Agent查询服务", lifespan=lifespan)
configure_http_security(app)
install_prometheus(app, "query-api")

FRONTEND_DIST_DIR = Path(__file__).resolve().parents[3] / "frontend" / "dist"
FRONTEND_ASSETS_DIR = FRONTEND_DIST_DIR / "assets"
if FRONTEND_ASSETS_DIR.exists():
    # Vue构建产物由查询服务同源提供，避免API Key与SSE请求受到跨域限制。
    app.mount("/assets", StaticFiles(directory=FRONTEND_ASSETS_DIR), name="frontend-assets")


@app.get("/chat.html")
async def chat():
    """优先返回Vue构建页面；本地尚未构建时保留旧页面作为降级入口。"""
    built_chat_path = FRONTEND_DIST_DIR / "chat.html"
    if built_chat_path.exists():
        return FileResponse(built_chat_path)
    current_dir_parent_path = Path(__file__).absolute().parent.parent
    chat_html_path = current_dir_parent_path / "page" / "chat.html"
    if not chat_html_path.exists():
        raise HTTPException(status_code=404, detail=f"没有查询到页面，地址为：{chat_html_path}！")
    return FileResponse(chat_html_path)


class QueryRequest(BaseModel):
    """查询请求数据结构。"""

    query: str = Field(default="", max_length=10000, description="查询内容；上传图片时可以留空")
    session_id: str = Field(None, description="会话ID")
    is_stream: bool = Field(False, description="是否流式返回")
    image_refs: list[str] = Field(default_factory=list, description="当前租户和会话已上传的图片对象引用")


class FeedbackRequest(BaseModel):
    """用户反馈请求数据结构。"""

    trace_id: str = Field(..., min_length=32, max_length=32, description="Langfuse Trace ID")
    value: Literal[0, 1] = Field(..., description="1表示点赞，0表示点踩")
    comment: Optional[str] = Field(default=None, max_length=500, description="用户反馈说明")


@app.get("/health")
async def health():
    """检查服务是否正常。"""
    return {"ok": True}


@app.get("/attachments/config")
async def attachment_config(principal: Principal = Depends(require_role("query"))):
    """返回聊天附件限制，供前端在上传前完成友好校验。"""
    return {
        "max_files": chat_attachment_config.max_files,
        "max_bytes": chat_attachment_config.max_bytes,
        "allowed_extensions": sorted(chat_attachment_config.allowed_extensions),
        "allowed_content_types": sorted(chat_attachment_config.allowed_content_types),
    }


@app.post("/attachments/{session_id}")
async def upload_session_attachments(
    session_id: str,
    files: list[UploadFile] = File(...),
    principal: Principal = Depends(require_role("query")),
):
    """上传只属于当前会话的图片；不会写入图片资产集合、Milvus或知识库。"""
    try:
        scoped_session_id(principal.tenant_id, session_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not files:
        raise HTTPException(status_code=422, detail="请至少选择一张图片")
    if len(files) > chat_attachment_config.max_files:
        raise HTTPException(status_code=422, detail=f"每轮最多上传 {chat_attachment_config.max_files} 张图片")

    attachments = []
    try:
        for upload in files:
            filename = safe_upload_filename(upload.filename, chat_attachment_config.allowed_extensions)
            attachments.append(
                store_session_attachment(
                    tenant_id=principal.tenant_id,
                    session_id=session_id,
                    original_filename=filename,
                    stream=upload.file,
                )
            )
        logger.info(
            f"会话图片上传完成，tenant_id={principal.tenant_id}，session_id={session_id}，"
            f"图片数={len(attachments)}"
        )
        return {"session_id": session_id, "attachments": attachments}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception(f"上传会话图片失败，session_id={session_id}，错误={exc}")
        raise HTTPException(status_code=500, detail="上传会话图片失败") from exc
    finally:
        for upload in files:
            await upload.close()


def run_query_graph(
    session_id: str,
    user_query: str,
    is_stream: bool,
    trace_id: str,
    resume: bool = False,
    tenant_id: str = "local",
    user_image_refs: Optional[list[str]] = None,
):
    """执行一次完整问答流程。"""
    run_started = time.perf_counter()
    internal_session_id = scoped_session_id(tenant_id, session_id)
    logger.info(
        f"开始执行问答流程，tenant_id={tenant_id}，session_id={session_id}，"
        f"trace_id={trace_id}，is_stream={is_stream}"
    )

    runtime_config = load_runtime_config()
    run_store = get_run_store()
    run_input = {
        "session_id": session_id,
        "user_query": user_query,
        "is_stream": is_stream,
        "image_refs": user_image_refs or [],
    }
    run_store.create(
        trace_id,
        "query",
        run_input,
        max_attempts=runtime_config.max_attempts,
        tenant_id=tenant_id,
    )
    owner = run_owner()
    run_store.claim(trace_id, owner, runtime_config.lease_seconds)

    default_state = {
        "original_query": user_query,
        "session_id": internal_session_id,
        "tenant_id": tenant_id,
        "trace_id": trace_id,
        "is_stream": is_stream,
        "user_image_refs": user_image_refs or [],
    }

    try:
        from app.query_process.agent.main_graph import query_app

        with trace_query(
            session_id=internal_session_id,
            user_query=user_query,
            is_stream=is_stream,
            trace_id=trace_id,
        ) as (observation, handler):
            config = {
                "run_name": "equipment-query-graph",
                "configurable": {"thread_id": trace_id},
                "tags": ["equipment-rag", "query"],
                "metadata": {
                    "session_id": internal_session_id,
                    "tenant_id": tenant_id,
                    "trace_id": trace_id,
                    "is_stream": is_stream,
                },
            }
            if handler is not None:
                config["callbacks"] = [handler]

            graph_input = None if resume else default_state
            final_state = None
            for state_snapshot in query_app.stream(graph_input, config=config, stream_mode="values"):
                final_state = state_snapshot
                run_store.heartbeat(trace_id, owner, runtime_config.lease_seconds)

            if final_state is None:
                final_state = query_app.get_state(config).values

            score_query_result(final_state)
            quality_report = analyze_query_state(final_state)
            visual_summary = summarize_image_reasoning(final_state)

            if observation is not None:
                observation.update(
                    output={
                        "status": "completed",
                        "answer": final_state.get("answer", ""),
                        "rewritten_query": final_state.get("rewritten_query", ""),
                        "item_names": final_state.get("item_names", []),
                        "retrieved_count": len(final_state.get("reranked_docs") or []),
                        "visual": visual_summary,
                    }
                )

        set_task_result(internal_session_id, "trace_id", trace_id)

        retrieved_source_ids = []
        for doc in final_state.get("reranked_docs") or []:
            source_id = doc.get("chunk_id") or doc.get("url")
            if source_id is not None:
                retrieved_source_ids.append(str(source_id))

        run_store.complete(
            trace_id,
            owner,
            {
                "session_id": session_id,
                "tenant_id": tenant_id,
                "trace_id": trace_id,
                "answer": final_state.get("answer", ""),
                "retrieved_source_ids": retrieved_source_ids,
                "sources": final_state.get("sources") or [],
                "answer_policy": final_state.get("answer_policy") or "answer",
                "requires_human_review": bool(final_state.get("requires_human_review")),
                "review_reason": final_state.get("review_reason") or "",
                "clarified": quality_report["response"]["clarified"],
                "visual": visual_summary,
            },
        )

        # 必须等LangGraph全部节点（包括node_answer_output的finally）完成后，才能把本轮标记为完成。
        # 先推送completed进度，再推送final；前端收到final后会主动关闭SSE连接，如果顺序相反，
        # 最后一个完成进度就会丢失并永久显示“进行中1”。
        update_task_status(internal_session_id, TASK_STATUS_COMPLETED, is_stream)
        if is_stream:
            push_to_session(
                internal_session_id,
                SSEEvent.FINAL,
                {
                    "answer": final_state.get("answer") or "",
                    "status": TASK_STATUS_COMPLETED,
                    "done_list": get_done_task_list(internal_session_id),
                    "running_list": get_running_task_list(internal_session_id),
                    "image_urls": final_state.get("image_urls") or [],
                    "sources": final_state.get("sources") or [],
                    "answer_policy": final_state.get("answer_policy") or "answer",
                    "requires_human_review": bool(final_state.get("requires_human_review")),
                    "review_reason": final_state.get("review_reason") or "",
                    "trace_id": trace_id,
                    "need_visual_reasoning": bool(final_state.get("need_visual_reasoning")),
                    "image_reasoning_status": final_state.get("image_reasoning_status") or "not_required",
                },
            )

        logger.info(
            f"问答流程执行完成，session_id={session_id}，trace_id={trace_id}，"
            f"视觉状态={visual_summary.get('status')}，图片数={visual_summary.get('selected_image_count')}"
        )
        observe_run("query", time.perf_counter() - run_started, "completed", quality_report)
        return final_state
    except Exception as exc:
        logger.exception(
            f"问答流程执行异常，session_id={session_id}，trace_id={trace_id}，错误={exc}"
        )
        update_task_status(internal_session_id, TASK_STATUS_FAILED, is_stream)
        try:
            run_store.fail(trace_id, owner, str(exc))
        except RuntimeError:
            logger.exception("持久化问答运行失败状态时发生异常")

        if is_stream:
            push_to_session(
                internal_session_id,
                SSEEvent.ERROR,
                {"error": str(exc), "trace_id": trace_id},
            )
        observe_run("query", time.perf_counter() - run_started, "failed")
        return None


@app.post("/query")
async def query(
    background_tasks: BackgroundTasks,
    request: QueryRequest,
    principal: Principal = Depends(require_role("query")),
):
    """提交设备知识问答请求。"""
    user_query = request.query.strip()
    session_id = request.session_id if request.session_id else str(uuid.uuid4())
    try:
        internal_session_id = scoped_session_id(principal.tenant_id, session_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        user_image_refs = validate_session_attachment_refs(
            principal.tenant_id,
            session_id,
            request.image_refs,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception(f"验证会话图片失败，session_id={session_id}，错误={exc}")
        raise HTTPException(status_code=422, detail="会话图片不存在或已失效") from exc
    if not user_query and not user_image_refs:
        raise HTTPException(status_code=422, detail="问题和图片不能同时为空")
    if not user_query:
        user_query = "请分析我上传的设备图片，并说明可以确认的信息。"

    try:
        trace_id = create_query_trace_id()
    except RuntimeError as exc:
        logger.exception("创建Langfuse Trace ID失败")
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    runtime_config = load_runtime_config()
    run_store = get_run_store()
    run_store.create(
        trace_id,
        "query",
        {
            "session_id": session_id,
            "user_query": user_query,
            "is_stream": request.is_stream,
            "image_refs": user_image_refs,
        },
        max_attempts=runtime_config.max_attempts,
        tenant_id=principal.tenant_id,
    )

    is_stream = request.is_stream
    # 任务追踪以session_id为键，因此同一会话每发起一个新问题都必须先清空上一轮节点列表。
    # 这只清理进程内的“进度显示”，不会删除MongoDB聊天记录、向量数据或运行审计记录。
    clear_task(internal_session_id)
    if is_stream:
        create_sse_queue(internal_session_id)
    update_task_status(internal_session_id, TASK_STATUS_PROCESSING, is_stream)

    if is_stream:
        background_tasks.add_task(
            run_query_graph,
            session_id,
            user_query,
            is_stream,
            trace_id,
            False,
            principal.tenant_id,
            user_image_refs,
        )
        return {
            "message": "结果正在处理中...",
            "session_id": session_id,
            "trace_id": trace_id,
        }

    final_state = run_query_graph(
        session_id,
        user_query,
        is_stream,
        trace_id,
        False,
        principal.tenant_id,
        user_image_refs,
    )
    run_record = run_store.get_for_tenant(trace_id, principal.tenant_id)
    if final_state is None or run_record is None or run_record.status == RunStatus.FAILED:
        detail = run_record.error if run_record else "Agent run failed"
        raise HTTPException(status_code=500, detail=detail)

    answer = run_record.result.get("answer") or get_task_result(internal_session_id, "answer", "")
    return {
        "message": "处理完成！",
        "session_id": session_id,
        "trace_id": trace_id,
        "answer": answer,
        "retrieved_source_ids": run_record.result.get("retrieved_source_ids", []),
        "sources": run_record.result.get("sources", []),
        "answer_policy": run_record.result.get("answer_policy", "answer"),
        "requires_human_review": run_record.result.get("requires_human_review", False),
        "review_reason": run_record.result.get("review_reason", ""),
        "clarified": run_record.result.get("clarified", False),
        "visual": run_record.result.get("visual", {}),
        "image_urls": final_state.get("image_urls") or [],
        "status": get_task_status(internal_session_id),
        "done_list": get_done_task_list(internal_session_id),
        "running_list": get_running_task_list(internal_session_id),
    }


@app.get("/runs/{run_id}", tags=["runtime"])
async def get_run(run_id: str, principal: Principal = Depends(require_role("query"))):
    """读取指定问答运行记录。"""
    run = get_run_store().get_for_tenant(run_id, principal.tenant_id)
    if run is None or run.kind != "query":
        raise HTTPException(status_code=404, detail="Query run not found")
    return run.to_public_dict()


@app.post("/runs/{run_id}/retry", status_code=202, tags=["runtime"])
async def retry_run(
    run_id: str,
    background_tasks: BackgroundTasks,
    principal: Principal = Depends(require_role("query")),
):
    """从最近一次成功检查点重试问答流程。"""
    run_store = get_run_store()
    run = run_store.get_for_tenant(run_id, principal.tenant_id)
    if run is None or run.kind != "query":
        raise HTTPException(status_code=404, detail="Query run not found")
    try:
        pending = run_store.request_retry(run_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    session_id = str(run.input["session_id"])
    internal_session_id = scoped_session_id(principal.tenant_id, session_id)
    is_stream = bool(run.input.get("is_stream", False))
    # 重试也是一轮新的执行进度，不能继承失败前或更早轮次的节点列表。
    clear_task(internal_session_id)
    if is_stream:
        create_sse_queue(internal_session_id)
    update_task_status(internal_session_id, TASK_STATUS_PROCESSING, is_stream)
    background_tasks.add_task(
        run_query_graph,
        session_id,
        str(run.input["user_query"]),
        is_stream,
        run_id,
        True,
        principal.tenant_id,
        list(run.input.get("image_refs") or []),
    )
    return pending.to_public_dict()


@app.post("/feedback")
async def submit_feedback(
    request: FeedbackRequest,
    principal: Principal = Depends(require_role("query")),
):
    """接收聊天页面点赞或点踩，并同步写入Langfuse和MongoDB。"""
    try:
        run = get_run_store().get_for_tenant(request.trace_id, principal.tenant_id)
        if run is None or run.kind != "query":
            raise HTTPException(status_code=404, detail="Query run not found")

        submit_trace_feedback(request.trace_id, request.value, request.comment or "")
        matched_count = update_message_feedback(
            request.trace_id,
            request.value,
            request.comment or "",
        )
        observe_feedback(request.value)

        if matched_count == 0:
            logger.warning(f"反馈已处理，但MongoDB未找到对应回答，trace_id={request.trace_id}")

        logger.info(
            f"用户反馈提交成功，trace_id={request.trace_id}，value={request.value}"
        )
        return {
            "message": "反馈已记录",
            "trace_id": request.trace_id,
            "value": request.value,
            "history_updated": matched_count > 0,
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException:
        raise
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception(
            f"用户反馈提交失败，trace_id={request.trace_id}，错误={exc}"
        )
        raise HTTPException(status_code=500, detail="用户反馈提交失败") from exc


@app.get("/stream/{session_id}")
async def stream(
    session_id: str,
    request: Request,
    principal: Principal = Depends(require_role("query")),
):
    """通过SSE实时返回问答结果。"""
    try:
        internal_session_id = scoped_session_id(principal.tenant_id, session_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return StreamingResponse(
        sse_generator(internal_session_id, request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/history/{session_id}")
async def history(
    session_id: str,
    limit: int = 50,
    principal: Principal = Depends(require_role("query")),
):
    """查询当前会话历史记录。"""
    try:
        internal_session_id = scoped_session_id(principal.tenant_id, session_id)
        records = get_recent_messages(internal_session_id, limit=limit)
        items = []
        for record in records:
            items.append(
                {
                    "_id": str(record.get("_id")) if record.get("_id") is not None else "",
                    "session_id": session_id,
                    "role": record.get("role", ""),
                    "text": record.get("text", ""),
                    "rewritten_query": record.get("rewritten_query", ""),
                    "item_names": record.get("item_names", []),
                    "image_urls": resolve_object_urls(record.get("image_urls", [])),
                    "sources": record.get("sources", []),
                    "requires_human_review": bool(record.get("requires_human_review")),
                    "review_reason": record.get("review_reason", ""),
                    "trace_id": record.get("trace_id", ""),
                    "feedback_value": record.get("feedback_value"),
                    "feedback_comment": record.get("feedback_comment", ""),
                    "ts": record.get("ts"),
                }
            )
        return {"session_id": session_id, "items": items}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"history error: {exc}") from exc


@app.delete("/history/{session_id}")
async def clear_chat_history(
    session_id: str,
    principal: Principal = Depends(require_role("query")),
):
    """清空当前租户和会话的历史记录。"""
    try:
        internal_session_id = scoped_session_id(principal.tenant_id, session_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    count = clear_history(internal_session_id)
    deleted_attachment_count = 0
    attachment_cleanup_error = ""
    try:
        deleted_attachment_count = delete_session_attachments(principal.tenant_id, session_id)
    except Exception as exc:
        attachment_cleanup_error = str(exc)[:300]
        logger.exception(f"清理会话图片失败，session_id={session_id}，错误={exc}")
    return {
        "message": "History cleared",
        "deleted_count": count,
        "deleted_attachment_count": deleted_attachment_count,
        "attachment_cleanup_error": attachment_cleanup_error,
    }


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8001)
