from __future__ import annotations

import re
from pathlib import Path


_SAFE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}$")


def scoped_session_id(tenant_id: str, session_id: str) -> str:
    """给公开会话 ID 加上租户前缀，形成内部存储键。"""
    if not _SAFE_ID_PATTERN.fullmatch(session_id):
        raise ValueError("session_id contains unsupported characters")
    return f"{tenant_id}:{session_id}"


def public_session_id(tenant_id: str, stored_session_id: str) -> str:
    """移除当前租户前缀；发现跨租户数据时拒绝返回。"""
    prefix = f"{tenant_id}:"
    if not stored_session_id.startswith(prefix):
        raise ValueError("Session does not belong to this tenant")
    return stored_session_id[len(prefix) :]


def safe_upload_filename(raw_name: str | None, allowed_extensions: frozenset[str]) -> str:
    """移除目录部分并校验扩展名，防止上传路径穿越。"""
    filename = Path((raw_name or "").replace("\\", "/")).name.strip()
    if not filename or filename in {".", ".."}:
        raise ValueError("Upload filename is empty")
    if any(character in filename for character in "\x00\r\n"):
        raise ValueError("Upload filename contains unsupported characters")
    extension = Path(filename).suffix.lower()
    if extension not in allowed_extensions:
        allowed = ", ".join(sorted(allowed_extensions))
        raise ValueError(f"Unsupported file extension; allowed: {allowed}")
    return filename


def tenant_object_prefix(tenant_id: str, *parts: str) -> str:
    """生成租户隔离的对象存储路径前缀。"""
    clean_parts = [str(part).strip("/\\").replace("\\", "/") for part in parts if str(part).strip("/\\")]
    return "/".join(["tenants", tenant_id, *clean_parts])


def escape_milvus_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def tenant_filter(tenant_id: str, expression: str | None = None) -> str:
    """把租户条件强制合并进 Milvus 查询表达式。"""
    tenant_expression = f'tenant_id == "{escape_milvus_literal(tenant_id)}"'
    return f"({tenant_expression}) and ({expression})" if expression else tenant_expression
