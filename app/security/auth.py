from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Annotated, Callable

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader

from app.security.config import load_security_config


api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


@dataclass(frozen=True)
class Principal:
    """认证后的调用方身份，包含租户和角色信息。"""

    key_id: str
    tenant_id: str
    roles: frozenset[str]
    authenticated: bool

    def has_role(self, role: str) -> bool:
        return "admin" in self.roles or role in self.roles


async def authenticate(
    supplied_key: Annotated[str | None, Security(api_key_header)] = None,
) -> Principal:
    """验证 API Key；仅在显式关闭认证的开发环境中返回本地管理员。"""
    config = load_security_config()
    if config.auth_mode == "disabled":
        # 该分支便于本地上手；生产环境会在加载配置时强制启用 API Key。
        return Principal(
            key_id="local-development",
            tenant_id="local",
            roles=frozenset({"admin", "query", "import"}),
            authenticated=False,
        )

    if not supplied_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )

    matched = None
    # 使用恒定时间比较降低根据响应耗时推测密钥内容的风险。
    for identity in config.api_keys:
        if hmac.compare_digest(supplied_key, identity.secret):
            matched = identity
    if matched is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    return Principal(
        key_id=matched.key_id,
        tenant_id=matched.tenant_id,
        roles=matched.roles,
        authenticated=True,
    )


def require_role(role: str) -> Callable[..., Principal]:
    """生成 FastAPI 依赖，在进入接口逻辑前完成角色授权。"""

    async def dependency(principal: Annotated[Principal, Depends(authenticate)]) -> Principal:
        if not principal.has_role(role):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Role {role!r} is required")
        return principal

    return dependency
