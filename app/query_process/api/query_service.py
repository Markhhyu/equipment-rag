from pathlib import Path
import uuid
import uvicorn
from fastapi import FastAPI, BackgroundTasks, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from app.observability.rag_observability import score_query_result

from app.core.logger import logger
from app.observability.langfuse_monitor import flush_langfuse, trace_query

from app.utils.task_utils import *
from app.utils.sse_utils import create_sse_queue, SSEEvent, sse_generator
from app.clients.mongo_history_utils import *
from app.query_process.agent.main_graph import query_app

# Literal用于限制反馈值只能是0或1。
from typing import Literal, Optional

# Langfuse监控和反馈相关工具。
from app.observability.langfuse_monitor import (
    create_query_trace_id,
    submit_trace_feedback,
    trace_query
)

# 后续导入启动图对象
#from app.query_process.main_graph import query_app


# 定义fastapi对象
@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    flush_langfuse()


app = FastAPI(title="query service", description="设备文档Agent查询服务", lifespan=lifespan)# 跨域问题解决
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

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
        trace_id: str
):
    """
    执行一次完整问答流程。

    :param session_id: 多轮会话ID。
    :param user_query: 用户原始问题。
    :param is_stream: 是否流式返回。
    :param trace_id: 当前这轮问答对应的Langfuse Trace ID。
    """

    logger.info(
        f"开始执行问答流程，"
        f"session_id={session_id}，"
        f"trace_id={trace_id}，"
        f"is_stream={is_stream}"
    )

    # 构造LangGraph初始状态。
    default_state = {
        "original_query": user_query,
        "session_id": session_id,
        "is_stream": is_stream
    }

    try:
        # 创建本轮问答的Langfuse根Trace。
        with trace_query(
                session_id=session_id,
                user_query=user_query,
                is_stream=is_stream,
                trace_id=trace_id
        ) as (observation, handler):

            # LangGraph运行配置。
            config = {
                "run_name": "equipment-query-graph",
                "tags": [
                    "equipment-rag",
                    "query"
                ],
                "metadata": {
                    "session_id": session_id,
                    "trace_id": trace_id,
                    "is_stream": is_stream
                }
            }

            # Langfuse开启时添加自动追踪回调。
            if handler is not None:
                config["callbacks"] = [handler]

            # 执行完整LangGraph。
            final_state = query_app.invoke(
                default_state,
                config=config
            )

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
            session_id,
            "trace_id",
            trace_id
        )

        # 更新任务状态为完成。
        update_task_status(
            session_id,
            TASK_STATUS_COMPLETED,
            is_stream
        )

        logger.info(
            f"问答流程执行完成，"
            f"session_id={session_id}，"
            f"trace_id={trace_id}"
        )

    except Exception as e:
        logger.exception(
            f"问答流程执行异常，"
            f"session_id={session_id}，"
            f"trace_id={trace_id}，"
            f"错误={e}"
        )

        # 更新任务状态为失败。
        update_task_status(
            session_id,
            TASK_STATUS_FAILED,
            is_stream
        )

        # 流式问答发生异常时，将错误推送给页面。
        if is_stream:
            push_to_session(
                session_id,
                SSEEvent.ERROR,
                {
                    "error": str(e),
                    "trace_id": trace_id
                }
            )


@app.post("/query")
async def query(background_tasks: BackgroundTasks, request: QueryRequest):
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
    # 每一次提问都生成独立Trace ID。
    # session_id代表整个对话，trace_id代表当前这一轮问答。
    trace_id = create_query_trace_id()

    # 处理是不是流式返回结果
    is_stream = request.is_stream
    if is_stream:
        # 创建一个字典 存储对一个session_id : queue 结果队列
        create_sse_queue(session_id)
    # 更新任务状态
    # 当前会话id作为key! 整体装填处于运行中！
    update_task_status(session_id, TASK_STATUS_PROCESSING,is_stream)

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
            trace_id
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
        run_query_graph(
            session_id,
            user_query,
            is_stream,
            trace_id
        )
        answer = get_task_result(session_id,"answer","")
        return {
            "message": "处理完成！",
            "session_id": session_id,
            "trace_id": trace_id,
            "answer": answer,
            "done_list": []
        }

@app.post("/feedback")
async def submit_feedback(request: FeedbackRequest):
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
        # 将反馈写入Langfuse Score。
        submit_trace_feedback(
            trace_id=request.trace_id,
            value=request.value,
            comment=request.comment or ""
        )

        logger.info(
            f"用户反馈提交成功，"
            f"trace_id={request.trace_id}，"
            f"value={request.value}"
        )

        return {
            "message": "反馈已记录",
            "trace_id": request.trace_id,
            "value": request.value
        }

    except ValueError as e:
        # 参数格式错误时返回400。
        raise HTTPException(
            status_code=400,
            detail=str(e)
        )

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
async def stream(session_id: str, request: Request):
    print("调用流式/stream...")
    """
    sse 实时返回结果
    """
    return StreamingResponse(
        sse_generator(session_id, request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


@app.get("/history/{session_id}")
async def history(session_id: str, limit: int = 50):
    """
    查询当前会话历史记录
    """
    try:
        records = get_recent_messages(session_id, limit=limit)
        items = []
        for r in records:
            items.append({
                "_id": str(r.get("_id")) if r.get("_id") is not None else "",
                "session_id": r.get("session_id", ""),
                "role": r.get("role", ""),
                "text": r.get("text", ""),
                "rewritten_query": r.get("rewritten_query", ""),
                "item_names": r.get("item_names", []),
                "ts": r.get("ts")
            })
        return {"session_id": session_id, "items": items}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"history error: {e}")


@app.delete("/history/{session_id}")
async def clear_chat_history(session_id: str):
    count = clear_history(session_id)
    return {"message": "History cleared", "deleted_count": count}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8001)
