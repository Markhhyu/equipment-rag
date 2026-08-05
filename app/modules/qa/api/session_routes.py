"""Query session attachments, event stream, and history routes."""

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse

from app.modules.qa.infrastructure.attachments import delete_session_attachments, store_session_attachment
from app.modules.qa.infrastructure.history import clear_history, get_recent_messages
from app.platform.config.chat_attachment_config import chat_attachment_config
from app.platform.observability.logging import logger
from app.platform.runtime.sse import sse_generator
from app.platform.security.auth import Principal, require_role
from app.platform.security.tenancy import safe_upload_filename, scoped_session_id
from app.platform.storage.minio import resolve_object_urls


router = APIRouter()


@router.get("/attachments/config")
async def attachment_config(principal: Principal = Depends(require_role("query"))):
    """返回聊天附件限制，供前端在上传前完成友好校验。"""
    return {
        "max_files": chat_attachment_config.max_files,
        "max_bytes": chat_attachment_config.max_bytes,
        "allowed_extensions": sorted(chat_attachment_config.allowed_extensions),
        "allowed_content_types": sorted(chat_attachment_config.allowed_content_types),
    }


@router.post("/attachments/{session_id}")
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


@router.get("/stream/{session_id}")
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


@router.get("/history/{session_id}")
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
                    "image_refs": record.get("image_urls", []),
                    "image_urls": resolve_object_urls(record.get("image_urls", [])),
                    "sources": record.get("sources", []),
                    "requires_human_review": bool(record.get("requires_human_review")),
                    "review_reason": record.get("review_reason", ""),
                    "version_scope_options": record.get("version_scope_options", []),
                    "selected_version_context": record.get("selected_version_context", []),
                    "trace_id": record.get("trace_id", ""),
                    "feedback_value": record.get("feedback_value"),
                    "feedback_comment": record.get("feedback_comment", ""),
                    "resolution_status": record.get("resolution_status"),
                    "resolution_comment": record.get("resolution_comment", ""),
                    "ts": record.get("ts"),
                }
            )
        return {"session_id": session_id, "items": items}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"history error: {exc}") from exc


@router.delete("/history/{session_id}")
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
