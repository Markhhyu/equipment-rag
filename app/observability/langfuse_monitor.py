import os
import re
import uuid
from contextlib import contextmanager
from typing import Any, Iterator, Optional, Tuple

from dotenv import find_dotenv, load_dotenv
from langfuse import get_client, propagate_attributes
from langfuse.langchain import CallbackHandler


# 加载项目根目录中的.env配置。
# find_dotenv()会从当前目录向上查找.env文件。
load_dotenv(find_dotenv())


# 是否开启Langfuse链路监控。
# 在.env中配置LANGFUSE_TRACING_ENABLED=false即可临时关闭。
LANGFUSE_ENABLED = os.getenv(
    "LANGFUSE_TRACING_ENABLED",
    "true"
).lower() == "true"


# 只有启用监控时才初始化Langfuse客户端。
# 关闭监控后使用None，避免影响正常问答业务。
langfuse = get_client() if LANGFUSE_ENABLED else None


def create_query_trace_id() -> str:
    """
    为一次独立问答生成Langfuse Trace ID。

    注意：
    session_id表示整个多轮会话；
    trace_id表示当前这一轮问答。

    同一个session_id下面可以有多个trace_id。

    :return:
        Langfuse开启时返回32位十六进制Trace ID；
        Langfuse关闭时返回空字符串。
    """

    # 监控关闭时不生成Trace ID。
    if not LANGFUSE_ENABLED or langfuse is None:
        return ""

    # Langfuse会生成符合OpenTelemetry规范的32位Trace ID。
    return langfuse.create_trace_id()


@contextmanager
def trace_query(
        session_id: str,
        user_query: str,
        is_stream: bool,
        trace_id: str
) -> Iterator[Tuple[Optional[Any], Optional[CallbackHandler]]]:
    """
    为一次完整问答创建Langfuse根Observation。

    该Observation内部会包含：
    1. LangGraph完整执行链路；
    2. 每个LangGraph节点；
    3. BGE-M3向量生成；
    4. Milvus检索；
    5. BGE Reranker；
    6. 大模型调用和Token统计。

    :param session_id: 当前多轮对话的会话ID。
    :param user_query: 用户本轮原始问题。
    :param is_stream: 是否使用SSE流式响应。
    :param trace_id: 本轮问答预先生成的Trace ID。
    :return:
        observation：本轮问答根Observation；
        handler：LangGraph使用的Langfuse回调处理器。
    """

    # 监控关闭时返回空上下文。
    # 调用方仍然可以正常执行LangGraph。
    if not LANGFUSE_ENABLED or langfuse is None:
        yield None, None
        return

    # 如果调用方没有传入Trace ID，则在这里重新生成。
    actual_trace_id = trace_id or langfuse.create_trace_id()

    # LangGraph和LangChain使用的自动追踪回调。
    handler = CallbackHandler()

    # 使用预先生成的Trace ID创建根Observation。
    # 这样/query接口可以在LangGraph执行前就把Trace ID返回给前端。
    with langfuse.start_as_current_observation(
            as_type="agent",
            name="equipment-query-agent",
            trace_context={
                "trace_id": actual_trace_id
            },
            input={
                "query": user_query,
                "session_id": session_id,
                "is_stream": is_stream
            }
    ) as observation:

        # 给当前Trace及其所有子Observation传播公共属性。
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
                    "monitor_version": "1.1"
                }
        ):
            yield observation, handler


def submit_trace_feedback(
        trace_id: str,
        value: int,
        comment: str = ""
) -> None:
    """
    将用户点赞或点踩写入Langfuse。

    value含义：
    1：点赞；
    0：点踩。

    使用固定score_id的原因：
    用户第一次点踩、后面改成点赞时，
    Langfuse会更新同一条反馈，而不是不断新增重复Score。

    :param trace_id: 当前回答对应的Langfuse Trace ID。
    :param value: 1表示点赞，0表示点踩。
    :param comment: 用户补充说明，最多保留500个字符。
    """

    # 监控关闭时不能提交反馈。
    if not LANGFUSE_ENABLED or langfuse is None:
        raise RuntimeError("Langfuse监控未开启，无法提交反馈")

    # Trace ID必须是32位十六进制字符串。
    # 这可以阻止前端提交明显无效的数据。
    if not re.fullmatch(r"[0-9a-fA-F]{32}", trace_id or ""):
        raise ValueError("trace_id格式不正确")

    # 当前只允许点赞和点踩两个值。
    if value not in (0, 1):
        raise ValueError("反馈值只能是0或1")

    # 去除首尾空格，并限制最大长度。
    safe_comment = (comment or "").strip()[:500]

    # 基于Trace ID生成固定UUID。
    # 同一个Trace永远得到相同的score_id。
    score_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"equipment-rag:user-feedback:{trace_id}"
        )
    )

    # 当用户未填写说明时，自动补充基础说明。
    if not safe_comment:
        safe_comment = "用户点赞" if value == 1 else "用户点踩"

    # 将反馈绑定到本轮问答Trace。
    langfuse.create_score(
        trace_id=trace_id,
        score_id=score_id,
        name="user_feedback",
        value=float(value),
        data_type="BOOLEAN",
        comment=safe_comment
    )

    # 用户反馈量通常远低于Trace量。
    # 这里主动flush，使反馈可以较快显示在Langfuse页面。
    langfuse.flush()


def flush_langfuse() -> None:
    """
    将SDK缓存中尚未上传的数据发送到Langfuse。

    正常问答时不需要频繁调用；
    建议在FastAPI服务关闭时统一调用。
    """

    if langfuse is not None:
        langfuse.flush()