"""Shared identity endpoints used by every browser-facing service."""

from fastapi import APIRouter, Depends

from app.platform.security.auth import Principal, authenticate


router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/me")
async def current_principal(principal: Principal = Depends(authenticate)) -> dict[str, object]:
    return {
        "key_id": principal.key_id,
        "tenant_id": principal.tenant_id,
        "roles": sorted(principal.roles),
        "authenticated": principal.authenticated,
    }
