from pathlib import Path
import uuid
import uvicorn
from fastapi import FastAPI, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from contextlib import asynccontextmanager
from app.observability.rag_observability import score_query_result

from app.core.logger import logger
from app.observability.langfuse_monitor import flush_langfuse

from app.utils.task_utils import *
from app.utils.sse_utils import create_sse_queue, SSEEvent, sse_generator
from app.clients.mongo_history_utils import *
from app.clients.minio_utils import resolve_object_urls
from app.runtime.config import load_runtime_config
from app.runtime.run_store import RunStatus, get_run_store, run_owner
from app.security.auth import Principal, require_role
from app.security.http import configure_http_security
from app.security.tenancy import scoped_session_id

# Literal用于限制反馈值只能是0或1。
from typing import Literal, Optional

# Langfuse监控和反馈相关工具。
from app.observability.langfuse_monitor import (
    create_query_trace_id,
    submit_trace_feedback,
    trace_query
)

# 后续导入启动图对象
# 如需直接复用预编译图，可从 app.query_process.main_graph 导入 query_app。


# 定义fastapi对象
@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    flush_langfuse()


app = FastAPI(title="query service", description="设备文档Agent查询服务", lifespan=lifespan)# 跨域问题解决
configure_http_security(app)

# 返回chat.html页面
@app.get("/chat.html")  # 对外访问地址
async def chat():
    # 从 api -> query_process
    current_dir_parent_path = Path(__file__).absolute().parent.parent
    # 定义chat.html位置
    chat_html_path = current_dir_parent_path / "page" / "chat.html"
    # 如果不存在，抛出404异常
    if not chat_html_path.exists():
        raise HTTPException(status_code=404, detail=f"没有查询到页面，地址为：{chat_html_path}！")
    return FileResponse(chat_html_path)

# 定义接口接收的数据结构
class QueryRequest(BaseModel):
    """查询请求数据结构"""
    query: str = Field(..., description="查询内容")  # ...必须填写
    session_id: str = Field(None, description="会话ID")
    is_stream: bool = Field(False, description="是否流式返回")

class FeedbackRequest(BaseModel):
    """
    用户反馈请求数据结构。

    前端点赞时发送value=1；
    前端点踩时发送value=0。
    """

    # 本轮回答对应的Langfuse Trace ID。
    trace_id: str = Field(
        ...,
        min_length=32,
        max_length=32,
        description="Langfuse Trace ID"
    )

    # Literal限制JSON中的value只能是0或者1。
    value: Literal[0, 1] = Field(
        ...,
        description="1表示点赞，0表示点踩"
    )

    # 用户可选的补充说明。
    comment: Optional[str] = Field(
        default=None,
        max_length=500,
        description="用户反馈说明"
    )


# 证明服务器启动即可
@app.get("/health")
async def health():
    """
    检查服务是否正常
    """
    return {"ok": True}


# 定义查询接口
def run_query_graph(
        session_id: str,
        user_query: str,
        is_stream: bool,
        trace_id: str,
        resume: bool = False,
        tenant_id: str = "local",
):
    """
    执行一次完整问答流程。

    :param session_id: 多轮会话ID。
    :param user_query: 用户原始问题。
    :param is_stream: 是否流式返回。
    :param trace_id: 当前这轮问答对应的Langfuse Trace ID。
    """

    internal_session_id = scoped_session_id(tenant_id, session_id)
    logger.info(
        f"开始执行问答流程，"
        f"tenant_id={tenant_id}，"
        f"session_id={session_id}，"
        f"trace_id={trace_id}，"
        f"is_stream={is_stream}"
    )

    runtime_config = load_runtime_config()
    run_store = get_run_store()
    run_input = {
        "session_id": session_id,
        "user_query": user_query,
        "is_stream": is_stream,
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

    # 构造LangGraph初始状态。
    # trace_id放入State后，后续回答节点可以将它保存到MongoDB。
    default_state = {
        "original_query": user_query,
        "session_id": internal_session_id,
        "tenant_id": tenant_id,
        "trace_id": trace_id,
        "is_stream": is_stream
    }

    try:
        from app.query_process.agent.main_graph import query_app

        # 创建本轮问答的Langfuse根Trace。
        with trace_query(
                session_id=internal_session_id,
                user_query=user_query,
                is_stream=is_stream,
                trace_id=trace_id
        ) as (observation, handler):

            # LangGraph运行配置。
            config = {
                "run_name": "equipment-query-graph",
                "configurable": {
                    "thread_id": trace_id,
                },
                "tags": [
                    "equipment-rag",
                    "query"
                ],
                "metadata": {
                    "session_id": internal_session_id,
                    "tenant_id": tenant_id,
                    "trace_id": trace_id,
                    "is_stream": is_stream
                }
            }

            # Langfuse开启时添加自动追踪回调。
            if handler is not None:
                config["callbacks"] = [handler]

            # 按节点边界流式执行并刷新租约。恢复时传入None，
            # LangGraph会从该thread最后成功的checkpoint继续。
            graph_input = None if resume else default_state
            final_state = None
            for state_snapshot in query_app.stream(
                graph_input,
                config=config,
                stream_mode="values",
            ):
                final_state = state_snapshot
                run_store.heartbeat(trace_id, owner, runtime_config.lease_seconds)

            if final_state is None:
                final_state = query_app.get_state(config).values

            # 写入自动基础评分。
            score_query_result(final_state)

            # 更新根Observation最终输出。
            if observation is not None:
                observation.update(
                    output={
                        "status": "completed",
                        "answer": final_state.get(
                            "answer",
                            ""
                        ),
                        "rewritten_query": final_state.get(
                            "rewritten_query",
                            ""
                        ),
                        "item_names": final_state.get(
                            "item_names",
                            []
                        ),
                        "retrieved_count": len(
                            final_state.get(
                                "reranked_docs"
                            ) or []
                        )
                    }
                )

        # 将Trace ID保存到当前任务结果。
        # 后面扩展/status接口时也可以直接读取。
        set_task_result(
            internal_session_id,
            "trace_id",
            trace_id
        )

        # 更新任务状态为完成。
        update_task_status(
            internal_session_id,
            TASK_STATUS_COMPLETED,
            is_stream
        )

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
            },
        )

        logger.info(
            f"问答流程执行完成，"
            f"session_id={session_id}，"
            f"trace_id={trace_id}"
        )
        return final_state

    except Exception as e:
        logger.exception(
            f"问答流程执行异常，"
            f"session_id={session_id}，"
            f"trace_id={trace_id}，"
            f"错误={e}"
        )

        # 更新任务状态为失败。
        update_task_status(
            internal_session_id,
            TASK_STATUS_FAILED,
            is_stream
        )
        try:
            run_store.fail(trace_id, owner, str(e))
        except RuntimeError:
            logger.exception("持久化问答运行失败状态时发生异常")

        # 流式问答发生异常时，将错误推送给页面。
        if is_stream:
            push_to_session(
                internal_session_id,
                SSEEvent.ERROR,
                {
                    "error": str(e),
                    "trace_id": trace_id
                }
            )
        return None


@app.post("/query")
async def query(
    background_tasks: BackgroundTasks,
    request: QueryRequest,
    principal: Principal = Depends(require_role("query")),
):
    """
    1 解析参数
    2 更新任务状态
    3 调用处理流程图
    4 返回结果
    :param background_tasks:
    :param request:
    :return:
    """
    user_query = request.query
    session_id = request.session_id if request.session_id else str(uuid.uuid4())
    try:
        internal_session_id = scoped_session_id(principal.tenant_id, session_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # 每一次提问都生成独立Trace ID。
    # session_id代表整个对话，trace_id代表当前这一轮问答。
    trace_id = create_query_trace_id()
    runtime_config = load_runtime_config()
    run_store = get_run_store()
    run_store.create(
        trace_id,
        "query",
        {
            "session_id": session_id,
            "user_query": user_query,
            "is_stream": request.is_stream,
        },
        max_attempts=runtime_config.max_attempts,
        tenant_id=principal.tenant_id,
    )

    # 处理是不是流式返回结果
    is_stream = request.is_stream
    if is_stream:
        # 创建一个字典 存储对一个session_id : queue 结果队列
        create_sse_queue(internal_session_id)
    # 更新任务状态
    # 当前会话id作为key! 整体装填处于运行中！
    update_task_status(internal_session_id, TASK_STATUS_PROCESSING, is_stream)

    print("开始处理流程... 是否流式:", is_stream, f"其他参数:{user_query}, session_id:{session_id}")

    if is_stream:
        # 如果是流式，则返回一个流式响应，过程不断地推送
        # 运行执行图对象方法
        # 后台执行LangGraph时，把预先生成的Trace ID一起传入。
        background_tasks.add_task(
            run_query_graph,
            session_id,
            user_query,
            is_stream,
            trace_id,
            False,
            principal.tenant_id,
        )        # 返回结果
        print("开始处理结果....")
        return {
            "message": "结果正在处理中...",
            "session_id": session_id,

            # 前端收到Trace ID后，在回答完成时显示反馈按钮。
            "trace_id": trace_id
        }
    else:
        # 同步运行
        final_state = run_query_graph(
            session_id,
            user_query,
            is_stream,
            trace_id,
            False,
            principal.tenant_id,
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
            "done_list": []
        }


@app.get("/runs/{run_id}", tags=["runtime"])
async def get_run(run_id: str, principal: Principal = Depends(require_role("query"))):
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
    if is_stream:
        create_sse_queue(internal_session_id)
    background_tasks.add_task(
        run_query_graph,
        session_id,
        str(run.input["user_query"]),
        is_stream,
        run_id,
        True,
        principal.tenant_id,
    )
    return pending.to_public_dict()


@app.post("/feedback")
async def submit_feedback(
    request: FeedbackRequest,
    principal: Principal = Depends(require_role("query")),
):
    """
    接收聊天页面的点赞或点踩。

    请求示例：
    {
        "trace_id": "32位Trace ID",
        "value": 1,
        "comment": "回答很有帮助"
    }
    """

    try:
        run = get_run_store().get_for_tenant(request.trace_id, principal.tenant_id)
        if run is None or run.kind != "query":
            raise HTTPException(status_code=404, detail="Query run not found")
        # 将反馈写入Langfuse Score。
        # 第一份反馈写入Langfuse，用于质量统计和筛选。
        submit_trace_feedback(request.trace_id, request.value, request.comment or "")

        # 第二份反馈写入MongoDB，用于页面刷新后恢复按钮状态。
        matched_count = update_message_feedback(request.trace_id, request.value, request.comment or "")

        if matched_count == 0:
            logger.warning(f"Langfuse反馈已保存，但MongoDB未找到对应回答，trace_id={request.trace_id}")

        logger.info(
            f"用户反馈提交成功，"
            f"trace_id={request.trace_id}，"
            f"value={request.value}"
        )

        return {
            "message": "反馈已记录",
            "trace_id": request.trace_id,
            "value": request.value,
            "history_updated": matched_count > 0
        }

    except ValueError as e:
        # 参数格式错误时返回400。
        raise HTTPException(
            status_code=400,
            detail=str(e)
        )

    except HTTPException:
        raise

    except RuntimeError as e:
        # Langfuse未启用或不可用时返回503。
        raise HTTPException(
            status_code=503,
            detail=str(e)
        )

    except Exception as e:
        logger.exception(
            f"用户反馈提交失败，"
            f"trace_id={request.trace_id}，"
            f"错误={e}"
        )

        raise HTTPException(
            status_code=500,
            detail="用户反馈提交失败"
        )


@app.get("/stream/{session_id}")
async def stream(
    session_id: str,
    request: Request,
    principal: Principal = Depends(require_role("query")),
):
    print("调用流式/stream...")
    """
    sse 实时返回结果
    """
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
            "X-Accel-Buffering": "no"
        }
    )


@app.get("/history/{session_id}")
async def history(
    session_id: str,
    limit: int = 50,
    principal: Principal = Depends(require_role("query")),
):
    """
    查询当前会话历史记录
    """
    try:
        internal_session_id = scoped_session_id(principal.tenant_id, session_id)
        records = get_recent_messages(internal_session_id, limit=limit)
        items = []
        for r in records:
            items.append({
                "_id": str(r.get("_id")) if r.get("_id") is not None else "",
                "session_id": session_id,
                "role": r.get("role", ""),
                "text": r.get("text", ""),
                "rewritten_query": r.get("rewritten_query", ""),
                "item_names": r.get("item_names", []),
                "image_urls": resolve_object_urls(r.get("image_urls", [])),
                "trace_id": r.get("trace_id", ""),
                "feedback_value": r.get("feedback_value"),
                "feedback_comment": r.get("feedback_comment", ""),
                "ts": r.get("ts")
            })
        return {"session_id": session_id, "items": items}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"history error: {e}")


@app.delete("/history/{session_id}")
async def clear_chat_history(
    session_id: str,
    principal: Principal = Depends(require_role("query")),
):
    try:
        internal_session_id = scoped_session_id(principal.tenant_id, session_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    count = clear_history(internal_session_id)
    return {"message": "History cleared", "deleted_count": count}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8001)
