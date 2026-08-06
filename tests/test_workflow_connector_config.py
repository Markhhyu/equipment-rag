from cryptography.fernet import Fernet
import pytest

from app.modules.workflow.application.connector_config_service import ConnectorConfigService
from app.modules.workflow.application.service import WorkflowService
from app.modules.workflow.infrastructure.connector_config import InMemoryConnectorConfigRepository
from app.modules.workflow.infrastructure.store import InMemoryWorkflowStore
from app.platform.security.secrets import (
    SecretCipher,
    SecretEncryptionError,
    get_workflow_secret_cipher,
    reset_workflow_secret_cipher_for_tests,
)


def settings(secret="app-secret", enabled=True):
    return {
        "enabled": enabled,
        "app_id": "cli_test",
        "app_secret": secret,
        "approval_code": "approval-code",
        "initiator_user_id": "ou_test",
        "user_id_type": "open_id",
        "form_fields": {
            "case_id": {"id": "control-case", "type": "input", "max_length": 100},
            "subject.question": {"id": "control-question", "type": "textarea", "max_length": 2000},
        },
        "base_url": "https://open.feishu.cn",
        "timeout_seconds": 10,
    }


def make_service():
    repository = InMemoryConnectorConfigRepository()
    cipher = SecretCipher(Fernet.generate_key())
    return ConnectorConfigService(repository, cipher), repository, cipher


def test_frontend_connector_config_encrypts_secret_and_never_returns_it():
    service, repository, cipher = make_service()

    saved = service.save_feishu("tenant-a", settings(), "admin-a")
    record = repository.get("tenant-a", "feishu_approval")

    assert saved["enabled"] is True
    assert saved["secret_configured"] is True
    assert "app_secret" not in saved
    assert record["encrypted_secret"] != "app-secret"
    assert cipher.decrypt(record["encrypted_secret"]) == "app-secret"
    assert service.get_feishu_public("tenant-a")["app_id"] == "cli_test"


def test_blank_secret_preserves_existing_encrypted_value_and_delete_is_tenant_scoped():
    service, repository, _ = make_service()
    service.save_feishu("tenant-a", settings(), "admin-a")
    original = repository.get("tenant-a", "feishu_approval")["encrypted_secret"]
    changed = settings(secret="")
    changed["approval_code"] = "approval-code-v2"

    saved = service.save_feishu("tenant-a", changed, "admin-b")

    assert saved["approval_code"] == "approval-code-v2"
    assert repository.get("tenant-a", "feishu_approval")["encrypted_secret"] == original
    assert service.delete_feishu("tenant-b") is False
    assert service.delete_feishu("tenant-a") is True
    assert repository.get("tenant-a", "feishu_approval") is None


def test_disabled_connector_can_be_saved_incomplete_but_is_not_loaded():
    service, _, _ = make_service()
    incomplete = settings(secret="", enabled=False)
    incomplete.update({"app_id": "", "approval_code": "", "initiator_user_id": "", "form_fields": {}})

    saved = service.save_feishu("tenant-a", incomplete, "admin")

    assert saved["enabled"] is False
    assert service.connectors_for_tenant("tenant-a") == ()


def test_workflow_service_uses_connector_provider_for_each_request():
    store = InMemoryWorkflowStore()
    connector_calls = []

    class Connector:
        connector_type = "runtime-oa"

        def start_case(self, case):
            from app.modules.workflow.connectors.base import StartedWorkflow

            connector_calls.append(case.case_id)
            return StartedWorkflow(instance_id=f"remote-{case.case_id}")

    enabled = {"value": False}
    service = WorkflowService(store, connector_provider=lambda _: (Connector(),) if enabled["value"] else ())
    first_payload = {"case_type": "equipment_issue", "subject": {}, "context": {}, "idempotency_key": "first"}
    second_payload = {"case_type": "equipment_issue", "subject": {}, "context": {}, "idempotency_key": "second"}

    first = service.create_case("tenant-a", first_payload, "query")
    enabled["value"] = True
    second = service.create_case("tenant-a", second_payload, "query")

    assert first["external_workflows"] == []
    assert connector_calls == [second["case_id"]]
    assert second["external_workflows"][0]["connector_type"] == "runtime-oa"


def test_development_cipher_generates_and_reuses_ignored_local_key(monkeypatch, tmp_path):
    key_path = tmp_path / "workflow-config.key"
    monkeypatch.setenv("APP_ENVIRONMENT", "development")
    monkeypatch.delenv("WORKFLOW_CONFIG_ENCRYPTION_KEY", raising=False)
    monkeypatch.setenv("WORKFLOW_CONFIG_KEY_FILE", str(key_path))
    reset_workflow_secret_cipher_for_tests()

    encrypted = get_workflow_secret_cipher().encrypt("secret-value")
    reset_workflow_secret_cipher_for_tests()

    assert key_path.exists()
    assert get_workflow_secret_cipher().decrypt(encrypted) == "secret-value"
    reset_workflow_secret_cipher_for_tests()


def test_production_cipher_requires_explicit_master_key(monkeypatch):
    monkeypatch.setenv("APP_ENVIRONMENT", "production")
    monkeypatch.delenv("WORKFLOW_CONFIG_ENCRYPTION_KEY", raising=False)
    reset_workflow_secret_cipher_for_tests()

    try:
        with pytest.raises(SecretEncryptionError, match="生产环境必须配置"):
            get_workflow_secret_cipher()
    finally:
        reset_workflow_secret_cipher_for_tests()
