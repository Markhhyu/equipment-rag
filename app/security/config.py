from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any


_TENANT_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}$")
_KNOWN_ROLES = frozenset({"admin", "query", "import"})


def _as_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _positive_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _csv(name: str, default: str) -> tuple[str, ...]:
    return tuple(value.strip() for value in (os.getenv(name) or default).split(",") if value.strip())


@dataclass(frozen=True)
class ApiKeyIdentity:
    """一把 API Key 对应的调用方、租户和角色；密钥不会出现在 repr 日志中。"""

    key_id: str
    secret: str = field(repr=False)
    tenant_id: str
    roles: frozenset[str]


@dataclass(frozen=True)
class SecurityConfig:
    """应用启动时使用的安全配置快照。"""

    environment: str
    auth_mode: str
    api_keys: tuple[ApiKeyIdentity, ...]
    cors_allowed_origins: tuple[str, ...]
    minio_public_read: bool
    minio_presigned_url_ttl_seconds: int
    max_upload_bytes: int
    allowed_upload_extensions: frozenset[str]

    @property
    def production(self) -> bool:
        return self.environment in {"prod", "production"}


def _parse_api_keys(raw_value: str) -> tuple[ApiKeyIdentity, ...]:
    """解析并严格校验 AUTH_API_KEYS_JSON，配置错误时直接阻止启动。"""
    if not raw_value.strip():
        return ()
    try:
        payload = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise ValueError("AUTH_API_KEYS_JSON must be valid JSON") from exc
    if not isinstance(payload, list):
        raise ValueError("AUTH_API_KEYS_JSON must be a JSON array")

    identities: list[ApiKeyIdentity] = []
    seen_ids: set[str] = set()
    for entry in payload:
        if not isinstance(entry, dict):
            raise ValueError("Each AUTH_API_KEYS_JSON entry must be an object")
        identity = _parse_identity(entry)
        if identity.key_id in seen_ids:
            raise ValueError(f"Duplicate API key id: {identity.key_id!r}")
        seen_ids.add(identity.key_id)
        identities.append(identity)
    return tuple(identities)


def _parse_identity(entry: dict[str, Any]) -> ApiKeyIdentity:
    key_id = str(entry.get("id") or "").strip()
    secret = str(entry.get("key") or "")
    tenant_id = str(entry.get("tenant_id") or "").strip()
    roles_value = entry.get("roles") or []
    if not key_id or not secret:
        raise ValueError("Each API key needs non-empty 'id' and 'key' values")
    if len(secret) < 24:
        raise ValueError(f"API key {key_id!r} must contain at least 24 characters")
    if not _TENANT_PATTERN.fullmatch(tenant_id):
        raise ValueError(f"Invalid tenant_id for API key {key_id!r}")
    if not isinstance(roles_value, list):
        raise ValueError(f"API key {key_id!r} roles must be an array")
    roles = frozenset(str(role).strip().lower() for role in roles_value if str(role).strip())
    unknown_roles = roles - _KNOWN_ROLES
    if unknown_roles:
        raise ValueError(f"API key {key_id!r} has unknown roles: {sorted(unknown_roles)}")
    if not roles:
        raise ValueError(f"API key {key_id!r} must have at least one role")
    return ApiKeyIdentity(key_id=key_id, secret=secret, tenant_id=tenant_id, roles=roles)


@lru_cache(maxsize=1)
def load_security_config() -> SecurityConfig:
    """加载一次安全配置并缓存，避免每个请求重复解析密钥 JSON。"""
    environment = (os.getenv("APP_ENVIRONMENT") or "development").strip().lower()
    auth_mode = (os.getenv("AUTH_MODE") or "disabled").strip().lower()
    if auth_mode not in {"disabled", "api_key"}:
        raise ValueError("AUTH_MODE must be 'disabled' or 'api_key'")

    api_keys = _parse_api_keys(os.getenv("AUTH_API_KEYS_JSON") or "")
    cors_origins = _csv("CORS_ALLOWED_ORIGINS", "http://localhost:8000,http://localhost:8001")
    if "*" in cors_origins:
        raise ValueError("CORS_ALLOWED_ORIGINS cannot contain '*'")

    allowed_extensions = frozenset(
        extension if extension.startswith(".") else f".{extension}"
        for extension in _csv("ALLOWED_UPLOAD_EXTENSIONS", ".pdf,.md")
    )
    config = SecurityConfig(
        environment=environment,
        auth_mode=auth_mode,
        api_keys=api_keys,
        cors_allowed_origins=cors_origins,
        minio_public_read=_as_bool("MINIO_PUBLIC_READ", False),
        minio_presigned_url_ttl_seconds=_positive_int("MINIO_PRESIGNED_URL_TTL_SECONDS", 3600),
        max_upload_bytes=_positive_int("MAX_UPLOAD_BYTES", 100 * 1024 * 1024),
        allowed_upload_extensions=allowed_extensions,
    )

    # 安全配置采用“失败即关闭”策略：生产环境不满足要求时拒绝启动。
    if config.auth_mode == "api_key" and not config.api_keys:
        raise ValueError("AUTH_MODE=api_key requires at least one AUTH_API_KEYS_JSON entry")
    if config.production and config.auth_mode != "api_key":
        raise ValueError("Production startup requires AUTH_MODE=api_key")
    if config.production and config.minio_public_read:
        raise ValueError("Production startup requires MINIO_PUBLIC_READ=false")
    return config


def reset_security_config_for_tests() -> None:
    load_security_config.cache_clear()
