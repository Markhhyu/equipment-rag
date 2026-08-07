from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections import defaultdict, deque
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app.platform.observability.logging import logger
from app.platform.security import email_sender, oauth, user_store
from app.platform.security.auth import SESSION_COOKIE_NAME, Principal, authenticate
from app.platform.security.config import load_security_config
from app.platform.security.passwords import hash_password, normalize_email, validate_password, verify_password
from app.platform.security.user_store import DuplicateEmailError, OAuthIdentityConflictError


router = APIRouter(prefix="/auth", tags=["auth"])
_rate_limit_lock = threading.Lock()
_rate_limit_buckets: dict[str, deque[float]] = defaultdict(deque)
_DUMMY_PASSWORD_HASH = hash_password("not-a-real-user-password")
_OAUTH_STATE_COOKIE_NAME = "equipment_oauth_state"
_OAUTH_REDIRECT_COOKIE_NAME = "equipment_oauth_redirect"
_OAUTH_COOKIE_MAX_AGE = 10 * 60


class CredentialsRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=128)


class EmailRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)


class VerificationRequest(BaseModel):
    token: str = Field(min_length=32, max_length=512)


def _principal_payload(principal: Principal) -> dict[str, object]:
    return {
        "key_id": principal.key_id,
        "tenant_id": principal.tenant_id,
        "roles": sorted(principal.roles),
        "authenticated": principal.authenticated,
        "email": principal.email,
        "auth_type": principal.auth_type,
    }


def _user_principal(user: dict[str, object]) -> Principal:
    return Principal(
        key_id=str(user["user_id"]),
        tenant_id=str(user["tenant_id"]),
        roles=frozenset(str(role) for role in user["roles"]),
        authenticated=True,
        email=str(user["email"]),
        auth_type=str(user.get("auth_type") or "password"),
    )


def _set_session_cookie(response: Response, token: str) -> None:
    config = load_security_config()
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=config.session_ttl_seconds,
        httponly=True,
        secure=config.session_cookie_secure,
        samesite="lax",
        path="/",
    )


def _safe_browser_redirect(value: str | None) -> str:
    if not value or not value.startswith("/") or value.startswith("//"):
        return "/apps"
    if value.startswith(("/login", "/register", "/auth/")):
        return "/apps"
    return value


def _oauth_callback_uri(provider: str) -> str:
    return f"{load_security_config().public_base_url}/auth/oauth/{provider}/callback"


def _set_oauth_cookies(response: Response, *, state_value: str, redirect_path: str) -> None:
    config = load_security_config()
    cookie_options = {
        "max_age": _OAUTH_COOKIE_MAX_AGE,
        "httponly": True,
        "secure": config.session_cookie_secure,
        "samesite": "lax",
        "path": "/auth/oauth",
    }
    response.set_cookie(_OAUTH_STATE_COOKIE_NAME, state_value, **cookie_options)
    response.set_cookie(_OAUTH_REDIRECT_COOKIE_NAME, redirect_path, **cookie_options)


def _clear_oauth_cookies(response: Response) -> None:
    secure = load_security_config().session_cookie_secure
    response.delete_cookie(_OAUTH_STATE_COOKIE_NAME, path="/auth/oauth", secure=secure, samesite="lax")
    response.delete_cookie(_OAUTH_REDIRECT_COOKIE_NAME, path="/auth/oauth", secure=secure, samesite="lax")


def _oauth_frontend_redirect(path: str, *, error: str | None = None) -> RedirectResponse:
    config = load_security_config()
    if error:
        query = urlencode({"oauth_error": error, "redirect": path})
        location = f"{config.public_base_url}/login?{query}"
    else:
        location = f"{config.public_base_url}{path}"
    response = RedirectResponse(location, status_code=status.HTTP_302_FOUND)
    _clear_oauth_cookies(response)
    return response


async def _send_verification_email(user: dict[str, object]) -> None:
    config = load_security_config()
    store = user_store.get_user_store()
    token = store.create_email_verification(str(user["user_id"]), config.email_verification_ttl_seconds)
    verification_url = f"{config.public_base_url}/verify-email?{urlencode({'token': token})}"
    expires_minutes = max(1, config.email_verification_ttl_seconds // 60)
    try:
        await run_in_threadpool(
            email_sender.get_verification_email_sender().send_verification,
            str(user["email"]),
            verification_url,
            expires_minutes,
        )
    except Exception as exc:
        logger.exception(f"验证邮件发送失败，user_id={user['user_id']}，错误={exc}")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="验证邮件发送失败，请稍后重试") from exc


def _rate_limit(request: Request, scope: str, identity: str, limit: int, window_seconds: int) -> None:
    client = request.client.host if request.client else "unknown"
    identity_hash = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    key = f"{scope}:{client}:{identity_hash}"
    now = time.monotonic()
    with _rate_limit_lock:
        if len(_rate_limit_buckets) > 10_000:
            stale_keys = [
                bucket_key
                for bucket_key, timestamps in _rate_limit_buckets.items()
                if not timestamps or now - timestamps[-1] >= window_seconds
            ]
            for stale_key in stale_keys:
                _rate_limit_buckets.pop(stale_key, None)
        bucket = _rate_limit_buckets[key]
        while bucket and now - bucket[0] >= window_seconds:
            bucket.popleft()
        if len(bucket) >= limit:
            retry_after = max(1, int(window_seconds - (now - bucket[0])))
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="请求过于频繁，请稍后重试",
                headers={"Retry-After": str(retry_after)},
            )
        bucket.append(now)


@router.get("/config")
async def auth_config() -> dict[str, object]:
    config = load_security_config()
    return {
        "auth_mode": config.auth_mode,
        "password_login_enabled": config.auth_mode == "password",
        "registration_enabled": config.registration_enabled,
        "email_verification_required": config.email_verification_required,
        "oauth_providers": ["github"] if config.github_oauth_enabled else [],
    }


@router.get("/oauth/{provider}/start")
async def start_oauth(provider: str, request: Request, redirect: str | None = None) -> RedirectResponse:
    config = load_security_config()
    oauth_provider = oauth.get_oauth_provider(provider)
    if config.auth_mode != "password" or not oauth_provider:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="OAuth 登录尚未启用")
    _rate_limit(request, "oauth-start-ip", provider, limit=30, window_seconds=15 * 60)
    state_value = secrets.token_urlsafe(32)
    redirect_path = _safe_browser_redirect(redirect)
    authorization_url = oauth_provider.authorization_url(
        state=state_value,
        redirect_uri=_oauth_callback_uri(provider),
    )
    response = RedirectResponse(authorization_url, status_code=status.HTTP_302_FOUND)
    _set_oauth_cookies(response, state_value=state_value, redirect_path=redirect_path)
    return response


@router.get("/oauth/{provider}/callback")
async def oauth_callback(
    provider: str,
    request: Request,
    oauth_state: Annotated[str | None, Cookie(alias=_OAUTH_STATE_COOKIE_NAME)] = None,
    oauth_redirect: Annotated[str | None, Cookie(alias=_OAUTH_REDIRECT_COOKIE_NAME)] = None,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    config = load_security_config()
    oauth_provider = oauth.get_oauth_provider(provider)
    redirect_path = _safe_browser_redirect(oauth_redirect)
    if config.auth_mode != "password" or not oauth_provider:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="OAuth 登录尚未启用")
    _rate_limit(request, "oauth-callback-ip", provider, limit=60, window_seconds=15 * 60)
    if not oauth_state or not state or not hmac.compare_digest(oauth_state, state):
        return _oauth_frontend_redirect(redirect_path, error="invalid_state")
    if error or not code:
        return _oauth_frontend_redirect(redirect_path, error="access_denied")

    try:
        identity = await run_in_threadpool(
            oauth_provider.exchange_code,
            code=code,
            redirect_uri=_oauth_callback_uri(provider),
        )
        if identity.provider != provider:
            raise oauth.OAuthProviderError("OAuth provider identity mismatch")
        email = normalize_email(identity.email)
        user = user_store.get_user_store().find_or_create_oauth_user(
            provider=identity.provider,
            subject=identity.subject,
            email=email,
            tenant_id=config.registration_tenant_id,
            roles=frozenset({"query"}),
        )
    except OAuthIdentityConflictError:
        logger.warning(f"audit auth_event=oauth_identity_conflict provider={provider}")
        return _oauth_frontend_redirect(redirect_path, error="identity_conflict")
    except (oauth.OAuthProviderError, ValueError) as exc:
        logger.warning(f"audit auth_event=oauth_failed provider={provider} error={type(exc).__name__}")
        return _oauth_frontend_redirect(redirect_path, error="provider_failed")
    if not user:
        return _oauth_frontend_redirect(redirect_path, error="account_unavailable")

    store = user_store.get_user_store()
    store.record_login(str(user["user_id"]))
    token = store.create_session(str(user["user_id"]), config.session_ttl_seconds, auth_type="oauth")
    response = _oauth_frontend_redirect(redirect_path)
    _set_session_cookie(response, token)
    logger.info(
        f"audit auth_event=oauth_login provider={provider} user_id={user['user_id']} tenant_id={user['tenant_id']}"
    )
    return response


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(request: Request, response: Response, credentials: CredentialsRequest) -> dict[str, object]:
    config = load_security_config()
    if config.auth_mode != "password" or not config.registration_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="邮箱注册尚未开放")
    try:
        email = normalize_email(credentials.email)
        validate_password(credentials.password)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    _rate_limit(request, "register-ip", "*", limit=20, window_seconds=15 * 60)
    _rate_limit(request, "register-email", email, limit=5, window_seconds=15 * 60)
    store = user_store.get_user_store()
    try:
        user = store.create_user(
            email=email,
            password_hash=hash_password(credentials.password),
            tenant_id=config.registration_tenant_id,
            roles=frozenset({"query"}),
            status="pending_verification" if config.email_verification_required else "active",
        )
    except DuplicateEmailError as exc:
        existing_user = store.find_user_by_email(email)
        can_retry_verification = (
            config.email_verification_required
            and existing_user
            and existing_user.get("status") == "pending_verification"
            and verify_password(credentials.password, str(existing_user.get("password_hash") or ""))
        )
        if not can_retry_verification:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        user = existing_user
    logger.info(f"audit auth_event=register user_id={user['user_id']} tenant_id={user['tenant_id']}")
    if config.email_verification_required:
        await _send_verification_email(user)
        return {
            "verification_required": True,
            "email": user["email"],
            "expires_in": config.email_verification_ttl_seconds,
        }
    token = store.create_session(str(user["user_id"]), config.session_ttl_seconds)
    _set_session_cookie(response, token)
    return {**_principal_payload(_user_principal(user)), "verification_required": False}


@router.post("/login")
async def login(request: Request, response: Response, credentials: CredentialsRequest) -> dict[str, object]:
    config = load_security_config()
    if config.auth_mode != "password":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="邮箱登录尚未启用")
    try:
        email = normalize_email(credentials.email)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="邮箱或密码错误") from exc
    _rate_limit(request, "login-ip", "*", limit=50, window_seconds=15 * 60)
    _rate_limit(request, "login-email", email, limit=10, window_seconds=15 * 60)
    store = user_store.get_user_store()
    user = store.find_user_by_email(email)
    encoded_password = str(user.get("password_hash") or _DUMMY_PASSWORD_HASH) if user else _DUMMY_PASSWORD_HASH
    password_valid = verify_password(credentials.password, encoded_password)
    if user and password_valid and user.get("status") == "pending_verification":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="请先完成邮箱验证")
    if not user or user.get("status") != "active" or not password_valid:
        logger.warning("audit auth_event=login_failed")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="邮箱或密码错误")
    store.record_login(str(user["user_id"]))
    token = store.create_session(str(user["user_id"]), config.session_ttl_seconds)
    _set_session_cookie(response, token)
    logger.info(f"audit auth_event=login user_id={user['user_id']} tenant_id={user['tenant_id']}")
    return _principal_payload(_user_principal(user))


@router.post("/verify-email")
async def verify_email(request: Request, response: Response, verification: VerificationRequest) -> dict[str, object]:
    config = load_security_config()
    if config.auth_mode != "password":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="邮箱验证尚未启用")
    _rate_limit(request, "verify-email-ip", "*", limit=30, window_seconds=15 * 60)
    store = user_store.get_user_store()
    user = store.verify_email(verification.token)
    if not user:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="验证链接无效或已过期")
    token = store.create_session(str(user["user_id"]), config.session_ttl_seconds)
    _set_session_cookie(response, token)
    logger.info(f"audit auth_event=email_verified user_id={user['user_id']} tenant_id={user['tenant_id']}")
    return _principal_payload(_user_principal(user))


@router.post("/resend-verification", status_code=status.HTTP_202_ACCEPTED)
async def resend_verification(request: Request, payload: EmailRequest) -> dict[str, str]:
    config = load_security_config()
    if config.auth_mode != "password" or not config.email_verification_required:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="邮箱验证尚未启用")
    try:
        email = normalize_email(payload.email)
    except ValueError:
        return {"message": "如果该邮箱存在待验证账号，验证邮件将会重新发送"}
    _rate_limit(request, "resend-ip", "*", limit=20, window_seconds=60 * 60)
    _rate_limit(request, "resend-email", email, limit=3, window_seconds=60 * 60)
    user = user_store.get_user_store().find_user_by_email(email)
    if user and user.get("status") == "pending_verification":
        await _send_verification_email(user)
    return {"message": "如果该邮箱存在待验证账号，验证邮件将会重新发送"}


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    response: Response,
    session_token: Annotated[str | None, Cookie(alias=SESSION_COOKIE_NAME)] = None,
) -> None:
    config = load_security_config()
    if config.auth_mode == "password" and session_token:
        user_store.get_user_store().revoke_session(session_token)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/", secure=config.session_cookie_secure, samesite="lax")


@router.get("/me")
async def current_principal(principal: Principal = Depends(authenticate)) -> dict[str, object]:
    return _principal_payload(principal)


def reset_auth_rate_limits_for_tests() -> None:
    with _rate_limit_lock:
        _rate_limit_buckets.clear()
