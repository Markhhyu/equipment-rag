from pathlib import Path
import uuid
import uvicorn
from fastapi import FastAPI, BackgroundTasks, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from app.core.logger import logger
from app.observability.langfuse_monitor import trace_query
from app.observability.rag_observability import score_query_result

from app.core.logger import logger
from app.observability.langfuse_monitor import flush_langfuse, trace_query

from app.utils.task_utils import *
from app.utils.sse_utils import create_sse_queue, SSEEvent, sse_generator
from app.clients.mongo_history_utils import *
from app.query_process.agent.main_graph import query_app

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
        is_stream: bool = True
):
    """
    执行一次完整的LangGraph问答流程。

    该方法负责：
    1. 创建LangGraph初始状态；
    2. 创建Langfuse根Trace；
    3. 挂载LangGraph CallbackHandler；
    4. 执行完整问答图；
    5. 写入确定性Score；
    6. 更新任务成功或失败状态。

    :param session_id: 当前会话ID。
    :param user_query: 用户原始问题。
    :param is_stream: 是否流式输出。
    """

    logger.info(
        f"开始执行问答流程，"
        f"session_id={session_id}，"
        f"is_stream={is_stream}"
    )

    # 构造LangGraph初始状态。
    default_state = {
        "original_query": user_query,
        "session_id": session_id,
        "is_stream": is_stream
    }

    try:
        # 为当前问答创建Langfuse根Observation。
        with trace_query(
                session_id=session_id,
                user_query=user_query,
                is_stream=is_stream
        ) as (observation, handler):

            # LangGraph运行配置。
            config = {
                # Langfuse页面中显示的图运行名称。
                "run_name": "equipment-query-graph",

                # 给LangGraph运行增加标签。
                "tags": [
                    "equipment-rag",
                    "query"
                ],

                # LangGraph本次运行的业务元数据。
                "metadata": {
                    "session_id": session_id,
                    "is_stream": is_stream
                }
            }

            # 只有启用Langfuse时才添加CallbackHandler。
            # 关闭监控后，LangGraph仍然能够正常执行。
            if handler is not None:
                config["callbacks"] = [handler]

            # 执行完整LangGraph问答流程。
            final_state = query_app.invoke(
                default_state,
                config=config
            )

            # 为本次问答写入确定性评分。
            # 必须在trace_query的with上下文内部调用，
            # 否则无法获取当前Trace ID。
            score_query_result(final_state)

            # 更新根Observation的最终输出。
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

        # 更新项目内部任务状态。
        update_task_status(
            session_id,
            TASK_STATUS_COMPLETED,
            is_stream
        )

        logger.info(
            f"问答流程执行完成，session_id={session_id}"
        )

    except Exception as e:
        # logger.exception会同时打印错误信息和完整异常堆栈。
        logger.exception(
            f"问答流程执行异常，"
            f"session_id={session_id}，"
            f"错误={e}"
        )

        # 更新任务为失败状态。
        update_task_status(
            session_id,
            TASK_STATUS_FAILED,
            is_stream
        )

        # 流式模式下，将异常事件推送给前端。
        if is_stream:
            push_to_session(
                session_id,
                SSEEvent.ERROR,
                {
                    "error": str(e)
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
        background_tasks.add_task(run_query_graph, session_id,user_query,is_stream)
        # 返回结果
        print("开始处理结果....")
        return {
            "message":"结果正在处理中...",
            "session_id":session_id
        }
    else:
        # 同步运行
        run_query_graph(session_id, user_query, is_stream)
        answer = get_task_result(session_id,"answer","")
        return {
            "message":"处理完成！",
            "session_id":session_id,
            "answer":answer,
            "done_list":[]
        }



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
