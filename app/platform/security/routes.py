from __future__ import annotations

import hashlib
import threading
import time
from collections import defaultdict, deque
from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from app.platform.observability.logging import logger
from app.platform.security import user_store
from app.platform.security.auth import SESSION_COOKIE_NAME, Principal, authenticate
from app.platform.security.config import load_security_config
from app.platform.security.passwords import hash_password, normalize_email, validate_password, verify_password
from app.platform.security.user_store import DuplicateEmailError


router = APIRouter(prefix="/auth", tags=["auth"])
_rate_limit_lock = threading.Lock()
_rate_limit_buckets: dict[str, deque[float]] = defaultdict(deque)
_DUMMY_PASSWORD_HASH = hash_password("not-a-real-user-password")


class CredentialsRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=128)


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
        auth_type="password",
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
    }


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
        )
    except DuplicateEmailError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    token = store.create_session(str(user["user_id"]), config.session_ttl_seconds)
    _set_session_cookie(response, token)
    logger.info(f"audit auth_event=register user_id={user['user_id']} tenant_id={user['tenant_id']}")
    return _principal_payload(_user_principal(user))


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
    encoded_password = str(user.get("password_hash") or "") if user else _DUMMY_PASSWORD_HASH
    password_valid = verify_password(credentials.password, encoded_password)
    if not user or user.get("status") != "active" or not password_valid:
        logger.warning("audit auth_event=login_failed")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="邮箱或密码错误")
    store.record_login(str(user["user_id"]))
    token = store.create_session(str(user["user_id"]), config.session_ttl_seconds)
    _set_session_cookie(response, token)
    logger.info(f"audit auth_event=login user_id={user['user_id']} tenant_id={user['tenant_id']}")
    return _principal_payload(_user_principal(user))


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
