from contextlib import asynccontextmanager
import time
import uuid
from typing import Optional

import uvicorn
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.modules.qa.infrastructure.attachments import (
    validate_session_attachment_refs,
)
from app.platform.observability.logging import logger
from app.platform.observability.langfuse_monitor import (
    create_query_trace_id,
    flush_langfuse,
    trace_query,
)
from app.platform.observability.prometheus_metrics import install_prometheus, observe_run
from app.platform.observability.quality_metrics import analyze_query_state
from app.platform.observability.rag_observability import score_query_result, summarize_image_reasoning
from app.modules.analytics.infrastructure.store import get_query_analytics_store
from app.modules.qa.api.feedback_routes import router as feedback_router
from app.modules.qa.api.schemas import QueryRequest
from app.modules.qa.api.session_routes import router as session_router
from app.platform.runtime.config import load_runtime_config
from app.platform.runtime.run_store import RunStatus, get_run_store, run_owner
from app.platform.security.auth import Principal, require_role
from app.platform.security.http import configure_http_security
from app.platform.security.tenancy import scoped_session_id
from app.platform.runtime.sse import SSEEvent, create_sse_queue, push_to_session
from app.platform.runtime.task_progress import (
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_PROCESSING,
    clear_task,
    get_done_task_list,
    get_running_task_list,
    get_task_result,
    get_task_status,
    set_task_result,
    update_task_status,
)
from app.shared.paths import PROJECT_ROOT


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    flush_langfuse()


app = FastAPI(title="query service", description="设备文档Agent查询服务", lifespan=lifespan)
configure_http_security(app)
install_prometheus(app, "query-api")

FRONTEND_DIST_DIR = PROJECT_ROOT / "frontend" / "dist"
FRONTEND_ASSETS_DIR = FRONTEND_DIST_DIR / "assets"
if FRONTEND_ASSETS_DIR.exists():
    # Vue构建产物由查询服务同源提供，避免API Key与SSE请求受到跨域限制。
    app.mount("/assets", StaticFiles(directory=FRONTEND_ASSETS_DIR), name="frontend-assets")


@app.get("/", response_class=FileResponse, include_in_schema=False)
@app.get("/apps.html", response_class=FileResponse)
async def apps_page():
    """返回业务应用与系统组件的统一入口。"""
    built_path = FRONTEND_DIST_DIR / "apps.html"
    if not built_path.exists():
        raise HTTPException(status_code=404, detail="应用中心尚未构建，请先执行前端构建")
    return FileResponse(built_path)


@app.get("/chat.html")
async def chat():
    """优先返回Vue构建页面；本地尚未构建时保留旧页面作为降级入口。"""
    built_chat_path = FRONTEND_DIST_DIR / "chat.html"
    if built_chat_path.exists():
        return FileResponse(built_chat_path)
    chat_html_path = PROJECT_ROOT / "app" / "query_process" / "page" / "chat.html"
    if not chat_html_path.exists():
        raise HTTPException(status_code=404, detail=f"没有查询到页面，地址为：{chat_html_path}！")
    return FileResponse(chat_html_path)


@app.get("/analytics.html")
async def analytics_page():
    """返回问答运营看板。"""
    built_path = FRONTEND_DIST_DIR / "analytics.html"
    if not built_path.exists():
        raise HTTPException(status_code=404, detail="运营看板尚未构建，请先执行前端构建")
    return FileResponse(built_path)


def _record_analytics(method: str, *args) -> None:
    """统计写入失败不能影响主问答链路，错误保留在日志中供运维修复。"""
    try:
        getattr(get_query_analytics_store(), method)(*args)
    except Exception as exc:
        logger.exception(f"问答统计写入失败，method={method}，错误={exc}")


@app.get("/health")
async def health():
    """检查服务是否正常。"""
    return {"ok": True}


def run_query_graph(
    session_id: str,
    user_query: str,
    is_stream: bool,
    trace_id: str,
    resume: bool = False,
    tenant_id: str = "local",
    user_image_refs: Optional[list[str]] = None,
    version_scope_id: str = "",
    reset_version_context: bool = False,
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
        "version_scope_id": version_scope_id,
        "reset_version_context": reset_version_context,
    }
    run_store.create(
        trace_id,
        "query",
        run_input,
        max_attempts=runtime_config.max_attempts,
        tenant_id=tenant_id,
    )
    _record_analytics("record_started", tenant_id, trace_id, session_id, user_query)
    owner = run_owner()
    run_store.claim(trace_id, owner, runtime_config.lease_seconds)

    default_state = {
        "original_query": user_query,
        "session_id": internal_session_id,
        "tenant_id": tenant_id,
        "trace_id": trace_id,
        "is_stream": is_stream,
        "user_image_refs": user_image_refs or [],
        "selected_version_scope_id": version_scope_id,
        "reset_version_context": reset_version_context,
    }

    try:
        from app.modules.qa.graph.main_graph import query_app

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
                "version_scope_options": final_state.get("version_scope_options") or [],
                "selected_version_context": final_state.get("selected_version_context") or [],
                "clarified": quality_report["response"]["clarified"],
                "visual": visual_summary,
            },
        )
        _record_analytics(
            "record_completed",
            tenant_id,
            trace_id,
            {
                "answer_policy": final_state.get("answer_policy") or "answer",
                "requires_human_review": bool(final_state.get("requires_human_review")),
                "review_reason": final_state.get("review_reason") or "",
                "device_names": final_state.get("item_names") or [],
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
                    "version_scope_options": final_state.get("version_scope_options") or [],
                    "selected_version_context": final_state.get("selected_version_context") or [],
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
        _record_analytics("record_failed", tenant_id, trace_id, str(exc))

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
            "version_scope_id": request.version_scope_id,
            "reset_version_context": request.reset_version_context,
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
            request.version_scope_id,
            request.reset_version_context,
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
        request.version_scope_id,
        request.reset_version_context,
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
        "version_scope_options": run_record.result.get("version_scope_options", []),
        "selected_version_context": run_record.result.get("selected_version_context", []),
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
        str(run.input.get("version_scope_id") or ""),
        bool(run.input.get("reset_version_context", False)),
    )
    return pending.to_public_dict()


app.include_router(feedback_router)
app.include_router(session_router)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8001)
