"""Administrative API for external workflow connector configuration."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.modules.workflow.application.connector_config_service import get_connector_config_service
from app.modules.workflow.connectors.base import WorkflowConnectorError
from app.platform.security.auth import Principal, require_role
from app.platform.security.secrets import SecretEncryptionError


class FormFieldSettings(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    type: str = Field(default="input", min_length=1, max_length=64)
    max_length: int = Field(default=2000, ge=1, le=10000)


class FeishuConnectorSettingsRequest(BaseModel):
    enabled: bool = False
    app_id: str = Field(default="", max_length=128)
    app_secret: str = Field(default="", max_length=512)
    approval_code: str = Field(default="", max_length=256)
    initiator_user_id: str = Field(default="", max_length=256)
    user_id_type: Literal["open_id", "user_id", "union_id"] = "open_id"
    form_fields: dict[str, FormFieldSettings] | None = None
    base_url: str = Field(default="https://open.feishu.cn", max_length=512)
    timeout_seconds: int = Field(default=10, ge=1, le=120)


router = APIRouter(prefix="/workflow/connectors/feishu", tags=["workflow-connectors"])


@router.get("")
async def get_feishu_settings(principal: Principal = Depends(require_role("admin"))):
    return get_connector_config_service().get_feishu_public(principal.tenant_id)


@router.put("")
async def save_feishu_settings(
    request: FeishuConnectorSettingsRequest,
    principal: Principal = Depends(require_role("admin")),
):
    try:
        return get_connector_config_service().save_feishu(
            principal.tenant_id,
            request.model_dump(mode="json"),
            principal.key_id,
        )
    except (ValueError, SecretEncryptionError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc


@router.post("/test")
async def test_feishu_settings(principal: Principal = Depends(require_role("admin"))):
    try:
        result = get_connector_config_service().test_feishu(principal.tenant_id)
        return {"ok": True, **result}
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except (WorkflowConnectorError, SecretEncryptionError) as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


@router.delete("")
async def delete_feishu_settings(principal: Principal = Depends(require_role("admin"))):
    return {"deleted": get_connector_config_service().delete_feishu(principal.tenant_id)}
