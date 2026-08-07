from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Annotated, Callable

from fastapi import Cookie, Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader

from app.platform.security.config import load_security_config
from app.platform.security import user_store


api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
SESSION_COOKIE_NAME = "equipment_session"


@dataclass(frozen=True)
class Principal:
    """认证后的调用方身份，包含租户和角色信息。"""

    key_id: str
    tenant_id: str
    roles: frozenset[str]
    authenticated: bool
    email: str = ""
    auth_type: str = "api_key"

    def has_role(self, role: str) -> bool:
        return "admin" in self.roles or role in self.roles


async def authenticate(
    supplied_key: Annotated[str | None, Security(api_key_header)] = None,
    session_token: Annotated[str | None, Cookie(alias=SESSION_COOKIE_NAME)] = None,
) -> Principal:
    """验证用户会话或 API Key；开发模式可显式关闭认证。"""
    config = load_security_config()
    if config.auth_mode == "disabled":
        # 该分支便于本地上手；生产环境会在加载配置时强制启用 API Key。
        return Principal(
            key_id="local-development",
            tenant_id="local",
            roles=frozenset({"admin", "query", "import", "workflow"}),
            authenticated=False,
            auth_type="development",
        )

    if supplied_key:
        matched = None
        # 使用恒定时间比较降低根据响应耗时推测密钥内容的风险。
        for identity in config.api_keys:
            if hmac.compare_digest(supplied_key, identity.secret):
                matched = identity
        if matched is not None:
            return Principal(
                key_id=matched.key_id,
                tenant_id=matched.tenant_id,
                roles=matched.roles,
                authenticated=True,
                auth_type="api_key",
            )

    if config.auth_mode == "password" and session_token:
        user = user_store.get_user_store().find_user_by_session(session_token)
        if user:
            return Principal(
                key_id=str(user["user_id"]),
                tenant_id=str(user["tenant_id"]),
                roles=frozenset(str(role) for role in user["roles"]),
                authenticated=True,
                email=str(user["email"]),
                auth_type="password",
            )

    challenge = "ApiKey" if config.auth_mode == "api_key" else "Session"
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing credentials",
        headers={"WWW-Authenticate": challenge},
    )


def require_role(role: str) -> Callable[..., Principal]:
    """生成 FastAPI 依赖，在进入接口逻辑前完成角色授权。"""

    async def dependency(principal: Annotated[Principal, Depends(authenticate)]) -> Principal:
        if not principal.has_role(role):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Role {role!r} is required")
        return principal

    return dependency
