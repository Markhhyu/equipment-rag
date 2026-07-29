import os
from contextlib import contextmanager
from typing import Iterator, Optional, Tuple, Any

from dotenv import load_dotenv
from langfuse import get_client, propagate_attributes
from langfuse.langchain import CallbackHandler

# 加载项目根目录中的.env配置文件。
# 必须在读取LANGFUSE相关环境变量之前执行。
load_dotenv()


# 是否启用Langfuse监控。
# 默认为true；需要临时关闭监控时，可以在.env中配置为false。
LANGFUSE_ENABLED = os.getenv(
    "LANGFUSE_TRACING_ENABLED",
    "true"
).lower() == "true"


# 当监控开启时，初始化Langfuse客户端。
# 当监控关闭时使用None，业务流程仍然可以正常执行。
langfuse = get_client() if LANGFUSE_ENABLED else None


@contextmanager
def trace_query(
        session_id: str,
        user_query: str,
        is_stream: bool
) -> Iterator[Tuple[Optional[Any], Optional[CallbackHandler]]]:
    """
    为一次完整问答创建Langfuse根Observation。

    一次用户问题对应一个根Observation，内部包含：
    1. LangGraph完整执行流程；
    2. 各个LangGraph节点；
    3. LangChain大模型调用；
    4. 自定义检索、向量化和重排Observation。

    :param session_id: 当前会话ID，用于关联同一个用户的多轮问答。
    :param user_query: 用户本次提交的原始问题。
    :param is_stream: 是否使用SSE流式输出。
    :return:
        observation：当前问答根Observation；
        handler：传递给LangGraph的Langfuse CallbackHandler。
    """

    # 未启用Langfuse时，返回两个None。
    # 调用方不需要修改业务流程，只是不再上传监控数据。
    if not LANGFUSE_ENABLED or langfuse is None:
        yield None, None
        return

    # LangChain/LangGraph使用的回调处理器。
    # 将它传给query_app.invoke之后，LangGraph节点和LLM调用会自动被追踪。
    handler = CallbackHandler()

    # propagate_attributes用于给当前Trace统一设置公共属性。
    # 内部创建的LangGraph节点和LLM调用会继承这些属性。
    with propagate_attributes(
            trace_name="设备文档Agent问答",
            session_id=session_id,
            tags=[
                "equipment-rag",
                "langgraph",
                "query"
            ],
            metadata={
                "service": "query-service",
                "is_stream": is_stream,
                "monitor_version": "1.0"
            }
    ):

        # 创建整次问答的根Observation。
        # agent类型表示这是一次Agent业务执行。
        with langfuse.start_as_current_observation(
                as_type="agent",
                name="equipment-query-agent",
                input={
                    "query": user_query,
                    "session_id": session_id,
                    "is_stream": is_stream
                }
        ) as observation:

            # 将根Observation和CallbackHandler交给调用方。
            yield observation, handler


def flush_langfuse() -> None:
    """
    将SDK缓存中尚未上传的监控数据立即发送到Langfuse。

    FastAPI是长时间运行的服务，不需要每次问答都调用flush。
    建议只在服务关闭时调用，避免增加正常问答的响应时间。
    """

    if langfuse is not None:
        langfuse.flush()