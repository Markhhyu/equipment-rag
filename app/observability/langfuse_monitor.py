import os
import re
import uuid
from contextlib import contextmanager
from typing import Any, Iterator, Optional, Tuple

from dotenv import find_dotenv, load_dotenv
from langfuse import get_client, propagate_attributes
from langfuse.langchain import CallbackHandler

from app.conf.rag_tuning_config import rag_tuning_config


load_dotenv(find_dotenv())

LANGFUSE_ENABLED = os.getenv("LANGFUSE_TRACING_ENABLED", "false").lower() == "true"
langfuse = get_client() if LANGFUSE_ENABLED else None
MONITOR_VERSION = "2.1"


def create_query_trace_id() -> str:
    """
    为一次独立问答生成Langfuse Trace ID。

    Langfuse关闭时使用本地UUID，保证可观测平台不可用时问答业务仍可正常执行。
    """
    if not LANGFUSE_ENABLED or langfuse is None:
        return uuid.uuid4().hex

    trace_id = langfuse.create_trace_id()
    if not re.fullmatch(r"[0-9a-fA-F]{32}", trace_id or ""):
        raise RuntimeError(f"Langfuse生成的trace_id无效：{trace_id!r}")
    return trace_id


@contextmanager
def trace_import(
    task_id: str,
    tenant_id: str,
    file_name: str,
) -> Iterator[Tuple[Optional[Any], Optional[CallbackHandler]]]:
    """为一次文件导入建立Langfuse根Trace。"""
    if not LANGFUSE_ENABLED or langfuse is None:
        yield None, None
        return

    handler = CallbackHandler()
    with langfuse.start_as_current_observation(
        as_type="agent",
        name="equipment-import-agent",
        input={"task_id": task_id, "file_name": file_name},
    ) as observation:
        with propagate_attributes(
            trace_name="设备文档导入",
            session_id=task_id,
            tags=["equipment-rag", "langgraph", "import"],
            metadata={
                "service": "import-service",
                "tenant_id": tenant_id,
                "monitor_version": MONITOR_VERSION,
                "rag_tuning": rag_tuning_config.to_dict(),
            },
        ):
            yield observation, handler


@contextmanager
def trace_query(
    session_id: str,
    user_query: str,
    is_stream: bool,
    trace_id: str,
) -> Iterator[Tuple[Optional[Any], Optional[CallbackHandler]]]:
    """为一次完整问答创建Langfuse根Observation。"""
    if not LANGFUSE_ENABLED or langfuse is None:
        yield None, None
        return

    actual_trace_id = trace_id or langfuse.create_trace_id()
    handler = CallbackHandler()
    with langfuse.start_as_current_observation(
        as_type="agent",
        name="equipment-query-agent",
        trace_context={"trace_id": actual_trace_id},
        input={
            "query": user_query,
            "session_id": session_id,
            "is_stream": is_stream,
        },
    ) as observation:
        with propagate_attributes(
            trace_name="设备文档Agent问答",
            session_id=session_id,
            tags=["equipment-rag", "langgraph", "query"],
            metadata={
                "service": "query-service",
                "is_stream": is_stream,
                "monitor_version": MONITOR_VERSION,
                "rag_tuning": rag_tuning_config.to_dict(),
            },
        ):
            yield observation, handler


@contextmanager
def trace_image_enrichment(
    *,
    worker_id: str,
    tenant_id: str,
    image_id: str,
    document_id: str,
    document_name: str,
    page_number: Any,
    retry_count: int,
) -> Iterator[Optional[Any]]:
    """
    为一张后台图片增强任务建立独立Langfuse根Trace。

    后台Worker不处于文件导入或用户问答上下文中，因此不能只创建孤立Span。这里统一补充Trace名称、
    文档会话、租户、Worker和重试属性，使单张图片的下载、视觉调用、结果写回和失败恢复可以完整追踪。
    """
    if not LANGFUSE_ENABLED or langfuse is None:
        yield None
        return

    safe_session_id = document_id or image_id
    with langfuse.start_as_current_observation(
        as_type="agent",
        name="equipment-image-enrichment-agent",
        input={
            "image_id": image_id,
            "document_id": document_id,
            "document_name": document_name,
            "page_number": page_number,
            "retry_count": retry_count,
        },
    ) as observation:
        with propagate_attributes(
            trace_name="设备手册图片异步增强",
            session_id=safe_session_id,
            tags=["equipment-rag", "image-enrichment", "vision"],
            metadata={
                "service": "image-enrichment-worker",
                "tenant_id": tenant_id,
                "worker_id": worker_id,
                "document_id": document_id,
                "monitor_version": MONITOR_VERSION,
            },
        ):
            yield observation


def submit_trace_feedback(
    trace_id: str,
    value: int,
    comment: str = "",
) -> None:
    """将用户点赞或点踩写入Langfuse。"""
    if not LANGFUSE_ENABLED or langfuse is None:
        return
    if not re.fullmatch(r"[0-9a-fA-F]{32}", trace_id or ""):
        raise ValueError("trace_id格式不正确")
    if value not in (0, 1):
        raise ValueError("反馈值只能是0或1")

    safe_comment = (comment or "").strip()[:500]
    score_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"equipment-rag:user-feedback:{trace_id}",
        )
    )
    if not safe_comment:
        safe_comment = "用户点赞" if value == 1 else "用户点踩"

    langfuse.create_score(
        trace_id=trace_id,
        score_id=score_id,
        name="user_feedback",
        value=float(value),
        data_type="BOOLEAN",
        comment=safe_comment,
    )
    langfuse.flush()


def flush_langfuse() -> None:
    """将SDK缓存中尚未上传的数据发送到Langfuse。"""
    if langfuse is not None:
        langfuse.flush()
