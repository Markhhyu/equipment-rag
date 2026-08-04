from __future__ import annotations

import uuid
from pathlib import Path
from typing import BinaryIO
from urllib.parse import unquote, urlparse

from app.clients.minio_utils import get_minio_client, minio_object_uri, resolve_object_url
from app.conf.chat_attachment_config import chat_attachment_config
from app.conf.minio_config import minio_config
from app.security.tenancy import tenant_object_prefix


_IMAGE_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
)


def _detect_image_type(header: bytes) -> tuple[str, str] | None:
    for signature, content_type, extension in _IMAGE_SIGNATURES:
        if header.startswith(signature):
            return content_type, extension
    if len(header) >= 12 and header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "image/webp", ".webp"
    return None


def inspect_image_stream(stream: BinaryIO) -> tuple[int, str, str]:
    """读取文件大小与真实图片签名，随后恢复文件指针供MinIO上传。"""
    stream.seek(0, 2)
    size = stream.tell()
    stream.seek(0)
    header = stream.read(16)
    stream.seek(0)

    if size <= 0:
        raise ValueError("图片文件为空")
    if size > chat_attachment_config.max_bytes:
        raise ValueError(f"单张图片不能超过 {chat_attachment_config.max_bytes // (1024 * 1024)} MB")

    detected = _detect_image_type(header)
    if detected is None:
        raise ValueError("无法识别图片内容，仅支持 JPG、PNG 和 WebP")
    content_type, canonical_extension = detected
    if content_type not in chat_attachment_config.allowed_content_types:
        raise ValueError(f"不支持的图片类型：{content_type}")
    return size, content_type, canonical_extension


def store_session_attachment(
    *,
    tenant_id: str,
    session_id: str,
    original_filename: str,
    stream: BinaryIO,
) -> dict:
    """把用户图片保存到当前租户/会话的私有目录，不创建图片资产或知识库记录。"""
    size, content_type, canonical_extension = inspect_image_stream(stream)
    client = get_minio_client()
    if client is None:
        raise RuntimeError("MinIO客户端初始化失败，无法保存会话图片")

    attachment_id = uuid.uuid4().hex
    object_name = tenant_object_prefix(
        tenant_id,
        "chat_attachments",
        session_id,
        f"{attachment_id}{canonical_extension}",
    )
    client.put_object(
        minio_config.bucket_name,
        object_name,
        stream,
        length=size,
        content_type=content_type,
    )
    object_ref = minio_object_uri(minio_config.bucket_name, object_name)
    return {
        "id": attachment_id,
        "name": Path(original_filename).name,
        "content_type": content_type,
        "size": size,
        "object_ref": object_ref,
        "preview_url": resolve_object_url(object_ref),
    }


def _parse_object_ref(object_ref: str) -> tuple[str, str]:
    if not object_ref.startswith("minio://"):
        raise ValueError("附件引用格式不正确")
    parsed = urlparse(object_ref)
    bucket_name = parsed.netloc
    object_name = unquote(parsed.path.lstrip("/"))
    if not bucket_name or not object_name:
        raise ValueError("附件引用格式不正确")
    return bucket_name, object_name


def validate_session_attachment_refs(tenant_id: str, session_id: str, object_refs: list[str]) -> list[str]:
    """验证附件属于当前租户和会话，并确认对象仍存在且大小安全。"""
    unique_refs = list(dict.fromkeys(str(value or "").strip() for value in object_refs if str(value or "").strip()))
    if len(unique_refs) > chat_attachment_config.max_files:
        raise ValueError(f"每轮最多上传 {chat_attachment_config.max_files} 张图片")
    if not unique_refs:
        return []

    expected_prefix = tenant_object_prefix(tenant_id, "chat_attachments", session_id) + "/"
    client = get_minio_client()
    if client is None:
        raise RuntimeError("MinIO客户端初始化失败，无法验证会话图片")

    for object_ref in unique_refs:
        bucket_name, object_name = _parse_object_ref(object_ref)
        if bucket_name != minio_config.bucket_name or not object_name.startswith(expected_prefix):
            raise ValueError("附件不属于当前租户或会话")
        stat = client.stat_object(bucket_name, object_name)
        if stat.size <= 0 or stat.size > chat_attachment_config.max_bytes:
            raise ValueError("附件大小不符合当前会话限制")
        if str(stat.content_type or "").lower() not in chat_attachment_config.allowed_content_types:
            raise ValueError("附件类型不符合当前会话限制")
    return unique_refs


def delete_session_attachments(tenant_id: str, session_id: str) -> int:
    """删除当前会话上传的全部图片；不会触碰知识库文档图片。"""
    client = get_minio_client()
    if client is None:
        raise RuntimeError("MinIO客户端初始化失败，无法清理会话图片")

    prefix = tenant_object_prefix(tenant_id, "chat_attachments", session_id) + "/"
    deleted_count = 0
    for item in client.list_objects(minio_config.bucket_name, prefix=prefix, recursive=True):
        client.remove_object(minio_config.bucket_name, item.object_name)
        deleted_count += 1
    return deleted_count
