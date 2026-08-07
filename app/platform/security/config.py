from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any
from urllib.parse import urlparse


_TENANT_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}$")
_KNOWN_ROLES = frozenset({"admin", "query", "import", "workflow"})


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
    """平台启动时使用的安全配置快照。"""

    environment: str
    auth_mode: str
    api_keys: tuple[ApiKeyIdentity, ...]
    registration_enabled: bool
    registration_tenant_id: str
    email_verification_required: bool
    email_verification_ttl_seconds: int
    public_base_url: str
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str = field(repr=False)
    smtp_from_address: str
    smtp_security: str
    smtp_timeout_seconds: int
    github_oauth_enabled: bool
    github_oauth_client_id: str
    github_oauth_client_secret: str = field(repr=False)
    github_oauth_timeout_seconds: int
    session_ttl_seconds: int
    session_cookie_secure: bool
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
    if auth_mode not in {"disabled", "api_key", "password"}:
        raise ValueError("AUTH_MODE must be 'disabled', 'api_key', or 'password'")

    api_keys = _parse_api_keys(os.getenv("AUTH_API_KEYS_JSON") or "")
    cors_origins = _csv(
        "CORS_ALLOWED_ORIGINS",
        (
            "http://localhost:8000,http://localhost:8001,http://localhost:8002,"
            "http://127.0.0.1:8000,http://127.0.0.1:8001,http://127.0.0.1:8002"
        ),
    )
    if "*" in cors_origins:
        raise ValueError("CORS_ALLOWED_ORIGINS cannot contain '*'")

    allowed_extensions = frozenset(
        extension if extension.startswith(".") else f".{extension}"
        for extension in _csv("ALLOWED_UPLOAD_EXTENSIONS", ".pdf,.md")
    )
    registration_tenant_id = (os.getenv("AUTH_REGISTRATION_TENANT_ID") or "public").strip()
    if not _TENANT_PATTERN.fullmatch(registration_tenant_id):
        raise ValueError("Invalid AUTH_REGISTRATION_TENANT_ID")
    registration_enabled = _as_bool("AUTH_REGISTRATION_ENABLED", False)
    if registration_enabled and auth_mode != "password":
        raise ValueError("AUTH_REGISTRATION_ENABLED requires AUTH_MODE=password")
    email_verification_required = _as_bool("AUTH_EMAIL_VERIFICATION_REQUIRED", False)
    if email_verification_required and not registration_enabled:
        raise ValueError("AUTH_EMAIL_VERIFICATION_REQUIRED requires AUTH_REGISTRATION_ENABLED=true")
    public_base_url = (os.getenv("AUTH_PUBLIC_BASE_URL") or "http://127.0.0.1:8080").strip().rstrip("/")
    parsed_public_url = urlparse(public_base_url)
    if parsed_public_url.scheme not in {"http", "https"} or not parsed_public_url.netloc:
        raise ValueError("AUTH_PUBLIC_BASE_URL must be an absolute HTTP(S) URL")
    smtp_host = (os.getenv("SMTP_HOST") or "").strip()
    smtp_from_address = (os.getenv("SMTP_FROM_ADDRESS") or "").strip()
    smtp_security = (os.getenv("SMTP_SECURITY") or "starttls").strip().lower()
    if smtp_security not in {"none", "starttls", "ssl"}:
        raise ValueError("SMTP_SECURITY must be 'none', 'starttls', or 'ssl'")
    if email_verification_required and (not smtp_host or not smtp_from_address):
        raise ValueError("Email verification requires SMTP_HOST and SMTP_FROM_ADDRESS")
    github_oauth_enabled = _as_bool("AUTH_GITHUB_OAUTH_ENABLED", False)
    github_oauth_client_id = (os.getenv("AUTH_GITHUB_CLIENT_ID") or "").strip()
    github_oauth_client_secret = os.getenv("AUTH_GITHUB_CLIENT_SECRET") or ""
    if github_oauth_enabled and auth_mode != "password":
        raise ValueError("AUTH_GITHUB_OAUTH_ENABLED requires AUTH_MODE=password")
    if github_oauth_enabled and (not github_oauth_client_id or not github_oauth_client_secret):
        raise ValueError("GitHub OAuth requires AUTH_GITHUB_CLIENT_ID and AUTH_GITHUB_CLIENT_SECRET")

    config = SecurityConfig(
        environment=environment,
        auth_mode=auth_mode,
        api_keys=api_keys,
        registration_enabled=registration_enabled,
        registration_tenant_id=registration_tenant_id,
        email_verification_required=email_verification_required,
        email_verification_ttl_seconds=_positive_int("AUTH_EMAIL_VERIFICATION_TTL_SECONDS", 30 * 60),
        public_base_url=public_base_url,
        smtp_host=smtp_host,
        smtp_port=_positive_int("SMTP_PORT", 465 if smtp_security == "ssl" else 587),
        smtp_username=(os.getenv("SMTP_USERNAME") or "").strip(),
        smtp_password=os.getenv("SMTP_PASSWORD") or "",
        smtp_from_address=smtp_from_address,
        smtp_security=smtp_security,
        smtp_timeout_seconds=_positive_int("SMTP_TIMEOUT_SECONDS", 10),
        github_oauth_enabled=github_oauth_enabled,
        github_oauth_client_id=github_oauth_client_id,
        github_oauth_client_secret=github_oauth_client_secret,
        github_oauth_timeout_seconds=_positive_int("AUTH_GITHUB_OAUTH_TIMEOUT_SECONDS", 10),
        session_ttl_seconds=_positive_int("AUTH_SESSION_TTL_SECONDS", 7 * 24 * 60 * 60),
        session_cookie_secure=_as_bool("AUTH_COOKIE_SECURE", environment in {"prod", "production"}),
        cors_allowed_origins=cors_origins,
        minio_public_read=_as_bool("MINIO_PUBLIC_READ", False),
        minio_presigned_url_ttl_seconds=_positive_int("MINIO_PRESIGNED_URL_TTL_SECONDS", 3600),
        max_upload_bytes=_positive_int("MAX_UPLOAD_BYTES", 100 * 1024 * 1024),
        allowed_upload_extensions=allowed_extensions,
    )

    # 安全配置采用“失败即关闭”策略：生产环境不满足要求时拒绝启动。
    if config.auth_mode == "api_key" and not config.api_keys:
        raise ValueError("AUTH_MODE=api_key requires at least one AUTH_API_KEYS_JSON entry")
    if config.production and config.auth_mode == "disabled":
        raise ValueError("Production startup requires AUTH_MODE=api_key or AUTH_MODE=password")
    if config.production and config.auth_mode == "password" and not config.session_cookie_secure:
        raise ValueError("Production password authentication requires AUTH_COOKIE_SECURE=true")
    if config.production and config.registration_enabled and not config.email_verification_required:
        raise ValueError("Production public registration requires AUTH_EMAIL_VERIFICATION_REQUIRED=true")
    if config.production and config.registration_enabled and parsed_public_url.scheme != "https":
        raise ValueError("Production public registration requires an HTTPS AUTH_PUBLIC_BASE_URL")
    if config.production and config.github_oauth_enabled and parsed_public_url.scheme != "https":
        raise ValueError("Production GitHub OAuth requires an HTTPS AUTH_PUBLIC_BASE_URL")
    if config.production and config.minio_public_read:
        raise ValueError("Production startup requires MINIO_PUBLIC_READ=false")
    return config


def reset_security_config_for_tests() -> None:
    load_security_config.cache_clear()
