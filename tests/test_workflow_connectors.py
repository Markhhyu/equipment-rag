import json
from datetime import UTC, datetime

import pytest

from app.modules.workflow.application.service import WorkflowDispatchError, WorkflowService
from app.modules.workflow.connectors.base import StartedWorkflow, WorkflowConnectorError
from app.modules.workflow.connectors.feishu import FeishuApprovalConfig, FeishuApprovalConnector
from app.modules.workflow.domain.models import WorkflowCase
from app.modules.workflow.infrastructure.store import InMemoryWorkflowStore


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def make_case() -> WorkflowCase:
    now = datetime.now(UTC)
    return WorkflowCase(
        case_id="case-001",
        tenant_id="local",
        case_type="equipment_issue",
        status="pending",
        subject={
            "question": "设备无法启动",
            "trace_id": "trace-001",
            "device_models": ["LJ2268", "LJ2269"],
        },
        context={"answer": "现有知识未找到可靠方案", "review_reason": "证据不足"},
        callback_url="",
        idempotency_key="qa-trace-001",
        created_at=now,
        updated_at=now,
    )


def config_env(**overrides):
    values = {
        "FEISHU_WORKFLOW_ENABLED": "true",
        "FEISHU_APP_ID": "cli_test",
        "FEISHU_APP_SECRET": "secret_test",
        "FEISHU_APPROVAL_CODE": "approval-code",
        "FEISHU_APPROVAL_INITIATOR_ID": "ou_test",
        "FEISHU_APPROVAL_USER_ID_TYPE": "open_id",
        "FEISHU_APPROVAL_FORM_FIELDS_JSON": json.dumps(
            {
                "case_id": {"id": "control-case", "type": "input"},
                "subject.question": {"id": "control-question", "type": "textarea"},
                "subject.device_models": "control-models",
                "context.answer": {"id": "control-answer", "type": "textarea", "max_length": 10},
                "context.missing": "control-missing",
            }
        ),
    }
    values.update(overrides)
    return values


def test_feishu_config_requires_credentials_only_when_enabled():
    assert FeishuApprovalConfig.from_env({}).enabled is False
    with pytest.raises(ValueError, match="FEISHU_APP_ID"):
        FeishuApprovalConfig.from_env({"FEISHU_WORKFLOW_ENABLED": "true"})
    with pytest.raises(ValueError, match="仅支持"):
        FeishuApprovalConfig.from_env({"FEISHU_APPROVAL_USER_ID_TYPE": "email"})


def test_feishu_connector_maps_form_and_reuses_tenant_token():
    session = FakeSession(
        [
            FakeResponse({"code": 0, "tenant_access_token": "tenant-token", "expire": 7200}),
            FakeResponse({"code": 0, "data": {"instance_code": "approval-instance-1"}}),
            FakeResponse({"code": 0, "data": {"instance_code": "approval-instance-2"}}),
        ]
    )
    connector = FeishuApprovalConnector(FeishuApprovalConfig.from_env(config_env()), session=session)

    first = connector.start_case(make_case())
    second = connector.start_case(make_case().model_copy(update={"case_id": "case-002"}))

    assert first.instance_id == "approval-instance-1"
    assert second.instance_id == "approval-instance-2"
    assert len(session.calls) == 3
    token_url, token_request = session.calls[0]
    assert token_url.endswith("/open-apis/auth/v3/tenant_access_token/internal")
    assert token_request["json"] == {"app_id": "cli_test", "app_secret": "secret_test"}

    instance_url, instance_request = session.calls[1]
    assert instance_url.endswith("/open-apis/approval/v4/instances")
    assert instance_request["headers"] == {"Authorization": "Bearer tenant-token"}
    assert instance_request["params"] == {"user_id_type": "open_id"}
    assert instance_request["json"]["uuid"] == "case-001"
    assert json.loads(instance_request["json"]["form"]) == [
        {"id": "control-case", "type": "input", "value": "case-001"},
        {"id": "control-question", "type": "textarea", "value": "设备无法启动"},
        {"id": "control-models", "type": "input", "value": "LJ2268、LJ2269"},
        {"id": "control-answer", "type": "textarea", "value": "现有知识未找到可靠方"},
    ]


def test_feishu_connector_allows_approval_without_prefill_mapping():
    session = FakeSession(
        [
            FakeResponse({"code": 0, "tenant_access_token": "tenant-token", "expire": 7200}),
            FakeResponse({"code": 0, "data": {"instance_code": "approval-instance-1"}}),
        ]
    )
    config = FeishuApprovalConfig.from_env(config_env(FEISHU_APPROVAL_FORM_FIELDS_JSON=""))

    result = FeishuApprovalConnector(config, session=session).start_case(make_case())

    assert result.instance_id == "approval-instance-1"
    assert json.loads(session.calls[1][1]["json"]["form"]) == []


def test_feishu_connector_returns_safe_business_error():
    session = FakeSession([FakeResponse({"code": 99991663, "msg": "approval permission denied"})])
    connector = FeishuApprovalConnector(FeishuApprovalConfig.from_env(config_env()), session=session)

    with pytest.raises(WorkflowConnectorError, match="99991663"):
        connector.start_case(make_case())


def test_feishu_connector_can_validate_approval_definition():
    session = FakeSession(
        [
            FakeResponse({"code": 0, "tenant_access_token": "tenant-token", "expire": 7200}),
            FakeResponse({"code": 0, "data": {"approval_name": "设备问题处理"}}),
        ]
    )
    connector = FeishuApprovalConnector(FeishuApprovalConfig.from_env(config_env()), session=session)

    result = connector.check_connection()

    assert result == {"approval_code": "approval-code", "approval_name": "设备问题处理"}
    assert session.calls[1][0].endswith("/open-apis/approval/v4/approvals/approval-code")
    assert session.calls[1][1]["headers"] == {"Authorization": "Bearer tenant-token"}


class RetryConnector:
    connector_type = "test_oa"

    def __init__(self, fail_once=False):
        self.calls = 0
        self.fail_once = fail_once

    def start_case(self, case):
        self.calls += 1
        if self.fail_once and self.calls == 1:
            raise WorkflowConnectorError("temporary failure")
        return StartedWorkflow(instance_id=f"remote-{case.case_id}")


def test_workflow_service_persists_external_reference_and_skips_duplicate_dispatch():
    store = InMemoryWorkflowStore()
    connector = RetryConnector()
    service = WorkflowService(store, [connector])
    payload = {
        "case_type": "equipment_issue",
        "subject": {"question": "设备无法启动"},
        "context": {},
        "idempotency_key": "qa-trace-001",
    }

    created = service.create_case("local", payload, "query-api")
    repeated = service.create_case("local", payload, "query-api")

    assert connector.calls == 1
    assert repeated["case_id"] == created["case_id"]
    assert created["external_workflows"][0]["connector_type"] == "test_oa"
    assert created["external_workflows"][0]["instance_id"].startswith("remote-")


def test_workflow_service_can_retry_external_dispatch_after_failure():
    store = InMemoryWorkflowStore()
    connector = RetryConnector(fail_once=True)
    service = WorkflowService(store, [connector])
    payload = {
        "case_type": "equipment_issue",
        "subject": {},
        "context": {},
        "idempotency_key": "qa-trace-retry",
    }

    with pytest.raises(WorkflowDispatchError) as error:
        service.create_case("local", payload, "query-api")
    retried = service.create_case("local", payload, "query-api")

    assert error.value.case_id == retried["case_id"]
    assert connector.calls == 2
    assert retried["external_workflows"][0]["connector_type"] == "test_oa"
