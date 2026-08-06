"""Manage tenant-scoped connector settings without exposing stored secrets."""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

from app.modules.workflow.application.ports import ConnectorConfigRepository
from app.modules.workflow.connectors.base import WorkflowConnector, WorkflowConnectorError
from app.modules.workflow.connectors.feishu import FeishuApprovalConfig, FeishuApprovalConnector
from app.modules.workflow.infrastructure.connector_config import get_connector_config_repository
from app.platform.security.secrets import SecretCipher, SecretEncryptionError, get_workflow_secret_cipher


FEISHU_CONNECTOR_TYPE = "feishu_approval"


class ConnectorConfigService:
    def __init__(self, repository: ConnectorConfigRepository, cipher: SecretCipher | None = None) -> None:
        self.repository = repository
        self.cipher = cipher

    def get_feishu_public(self, tenant_id: str) -> dict[str, Any]:
        record = self.repository.get(tenant_id, FEISHU_CONNECTOR_TYPE)
        if record:
            return self._public_record(record, source="database")
        env_config = FeishuApprovalConfig.from_env()
        return {
            **env_config.storage_values(),
            "secret_configured": bool(env_config.app_secret),
            "source": "environment" if any(
                [env_config.enabled, env_config.app_id, env_config.approval_code, env_config.app_secret]
            ) else "default",
            "updated_at": None,
        }

    def save_feishu(self, tenant_id: str, values: dict[str, Any], actor: str) -> dict[str, Any]:
        values = dict(values)
        existing = self.repository.get(tenant_id, FEISHU_CONNECTOR_TYPE)
        submitted_secret = str(values.pop("app_secret", "") or "").strip()
        encrypted_secret = str((existing or {}).get("encrypted_secret") or "")
        if submitted_secret:
            encrypted_secret = self._cipher().encrypt(submitted_secret)
            app_secret = submitted_secret
        elif encrypted_secret:
            app_secret = self._cipher().decrypt(encrypted_secret)
        else:
            app_secret = ""
        config = FeishuApprovalConfig.from_values(values, app_secret)
        record = self.repository.save(
            tenant_id,
            FEISHU_CONNECTOR_TYPE,
            config.storage_values(),
            encrypted_secret,
            actor,
        )
        return self._public_record(record, source="database")

    def delete_feishu(self, tenant_id: str) -> bool:
        return self.repository.delete(tenant_id, FEISHU_CONNECTOR_TYPE)

    def test_feishu(self, tenant_id: str) -> dict[str, str]:
        config = self._load_stored_feishu(tenant_id)
        return FeishuApprovalConnector(config).check_connection()

    def connectors_for_tenant(self, tenant_id: str) -> tuple[WorkflowConnector, ...]:
        try:
            record = self.repository.get(tenant_id, FEISHU_CONNECTOR_TYPE)
            if record:
                config = self._config_from_record(record)
            else:
                config = FeishuApprovalConfig.from_env()
            return (FeishuApprovalConnector(config),) if config.enabled else ()
        except (ValueError, SecretEncryptionError) as exc:
            raise WorkflowConnectorError(f"连接器配置不可用：{exc}") from exc

    def _load_stored_feishu(self, tenant_id: str) -> FeishuApprovalConfig:
        record = self.repository.get(tenant_id, FEISHU_CONNECTOR_TYPE)
        if not record:
            config = FeishuApprovalConfig.from_env()
            if not config.enabled:
                raise ValueError("尚未保存并启用飞书审批配置")
            return config
        config = self._config_from_record(record)
        if not config.enabled:
            raise ValueError("飞书审批连接器尚未启用")
        return config

    def _config_from_record(self, record: dict[str, Any]) -> FeishuApprovalConfig:
        encrypted_secret = str(record.get("encrypted_secret") or "")
        app_secret = self._cipher().decrypt(encrypted_secret) if encrypted_secret else ""
        return FeishuApprovalConfig.from_values(record.get("config") or {}, app_secret)

    def _cipher(self) -> SecretCipher:
        return self.cipher or get_workflow_secret_cipher()

    @staticmethod
    def _public_record(record: dict[str, Any], source: str) -> dict[str, Any]:
        updated_at = record.get("updated_at")
        return {
            **(record.get("config") or {}),
            "secret_configured": bool(record.get("encrypted_secret")),
            "source": source,
            "updated_at": updated_at.isoformat() if isinstance(updated_at, datetime) else updated_at,
        }


_service: ConnectorConfigService | None = None
_service_lock = threading.RLock()


def get_connector_config_service() -> ConnectorConfigService:
    global _service
    if _service is not None:
        return _service
    with _service_lock:
        if _service is None:
            _service = ConnectorConfigService(get_connector_config_repository())
        return _service


def reset_connector_config_service_for_tests(service: ConnectorConfigService | None = None) -> None:
    global _service
    with _service_lock:
        _service = service
