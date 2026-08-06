import hashlib
import hmac

import pytest
from pydantic import ValidationError

from app.modules.workflow.domain.models import CaseActionRequest, DeliveryStatus
from app.modules.workflow.infrastructure.store import InMemoryWorkflowStore, _event_payload


def test_workflow_case_is_idempotent_and_enforces_state_transitions():
    store = InMemoryWorkflowStore()
    payload = {
        "case_type": "answer_review",
        "subject": {"trace_id": "trace-a"},
        "context": {"reason": "insufficient_evidence"},
        "callback_url": "https://rag.example.test/review/callback",
        "idempotency_key": "query-trace-a",
    }

    created = store.create_case("tenant-a", payload, "query-service")
    repeated = store.create_case("tenant-a", payload, "query-service")
    assigned = store.apply_action(
        "tenant-a",
        created["case_id"],
        {"action": "assign", "assignee": "operator-1", "idempotency_key": "assign-a"},
        "connector-a",
    )
    resolved = store.apply_action(
        "tenant-a",
        created["case_id"],
        {"action": "resolve", "result": {"answer": "reviewed"}, "idempotency_key": "resolve-a"},
        "operator-1",
    )

    assert repeated["case_id"] == created["case_id"]
    assert assigned["status"] == "assigned"
    assert resolved["status"] == "resolved"
    assert resolved["result"] == {"answer": "reviewed"}


def test_subscription_creates_signed_vendor_neutral_delivery_and_ack():
    store = InMemoryWorkflowStore()
    secret = "test-signing-secret-at-least-24"
    subscription = store.create_subscription(
        "tenant-a",
        {
            "connector_type": "custom-oa-adapter",
            "callback_url": "https://connector.example.test/events",
            "event_types": ["review.requested"],
            "signing_secret": secret,
        },
    )
    assert "signing_secret" not in subscription

    case = store.create_case(
        "tenant-a",
        {
            "case_type": "answer_review",
            "subject": {"trace_id": "trace-a"},
            "context": {},
            "callback_url": "",
            "idempotency_key": "case-a",
        },
        "query-service",
    )
    deliveries = store.list_deliveries("tenant-a")
    assert len(deliveries) == 1
    delivery = deliveries[0]
    expected = hmac.new(secret.encode(), _event_payload(delivery["event"]), hashlib.sha256).hexdigest()
    assert delivery["signature"] == f"sha256={expected}"
    assert delivery["event"]["case_id"] == case["case_id"]

    acknowledged = store.ack_delivery(
        "tenant-a",
        delivery["delivery_id"],
        {"status": DeliveryStatus.ACKNOWLEDGED.value, "remote_message_id": "remote-1"},
    )
    assert acknowledged["status"] == "acknowledged"
    assert acknowledged["remote_message_id"] == "remote-1"


def test_workflow_store_is_tenant_isolated():
    store = InMemoryWorkflowStore()
    case = store.create_case(
        "tenant-a",
        {
            "case_type": "answer_review",
            "subject": {},
            "context": {},
            "callback_url": "",
            "idempotency_key": "case-a",
        },
        "query-service",
    )

    assert store.get_case("tenant-b", case["case_id"]) is None
    assert store.list_cases("tenant-b") == {"items": [], "total": 0}
    assert store.list_events("tenant-b") == []


def test_workflow_cases_can_be_listed_and_filtered():
    store = InMemoryWorkflowStore()
    first = store.create_case(
        "tenant-a",
        {
            "case_type": "equipment_issue",
            "subject": {"question": "LJ2268 无法启动", "device_model": "LJ2268"},
            "context": {},
            "callback_url": "",
            "idempotency_key": "case-list-a",
        },
        "query-service",
    )
    store.create_case(
        "tenant-a",
        {
            "case_type": "answer_review",
            "subject": {"question": "另一台设备报警"},
            "context": {},
            "callback_url": "",
            "idempotency_key": "case-list-b",
        },
        "query-service",
    )
    store.apply_action(
        "tenant-a",
        first["case_id"],
        {"action": "assign", "assignee": "engineer-a", "idempotency_key": "case-list-assign"},
        "operator",
    )

    all_cases = store.list_cases("tenant-a")
    assigned_cases = store.list_cases("tenant-a", status="assigned")
    matched_cases = store.list_cases("tenant-a", query="LJ2268")

    assert all_cases["total"] == 2
    assert assigned_cases["total"] == 1
    assert assigned_cases["items"][0]["case_id"] == first["case_id"]
    assert matched_cases["total"] == 1
    assert matched_cases["items"][0]["subject"]["device_model"] == "LJ2268"


def test_resolved_case_can_emit_a_valid_knowledge_candidate_decision():
    store = InMemoryWorkflowStore()
    store.create_subscription(
        "tenant-a",
        {
            "connector_type": "knowledge-candidate-adapter",
            "callback_url": "https://knowledge.example.test/events",
            "event_types": ["review.resolved"],
            "signing_secret": "knowledge-event-secret-at-least-24",
        },
    )
    case = store.create_case(
        "tenant-a",
        {
            "case_type": "equipment_issue",
            "subject": {"question": "设备无法启动"},
            "context": {},
            "callback_url": "",
            "idempotency_key": "knowledge-case-a",
        },
        "query-service",
    )

    resolved = store.apply_action(
        "tenant-a",
        case["case_id"],
        {
            "action": "resolve",
            "result": {
                "root_cause": "接线端子松动",
                "solution": "重新紧固端子",
                "verification": "连续运行两小时无报警",
            },
            "knowledge_decision": "include",
            "idempotency_key": "knowledge-resolve-a",
        },
        "oa-connector",
    )

    assert resolved["knowledge_decision"] == "include"
    delivery = store.list_deliveries("tenant-a")[0]
    assert delivery["event"]["event_type"] == "review.resolved"
    assert delivery["event"]["status"] == "resolved"
    assert delivery["event"]["knowledge_decision"] == "include"
    assert delivery["event"]["result"]["solution"] == "重新紧固端子"


def test_knowledge_include_requires_solution_and_verification():
    store = InMemoryWorkflowStore()
    case = store.create_case(
        "tenant-a",
        {
            "case_type": "equipment_issue",
            "subject": {},
            "context": {},
            "callback_url": "",
            "idempotency_key": "knowledge-case-invalid",
        },
        "query-service",
    )

    with pytest.raises(ValueError, match="solution, verification"):
        store.apply_action(
            "tenant-a",
            case["case_id"],
            {
                "action": "resolve",
                "result": {"root_cause": "未知"},
                "knowledge_decision": "include",
                "idempotency_key": "knowledge-resolve-invalid",
            },
            "oa-connector",
        )

    assert store.get_case("tenant-a", case["case_id"])["status"] == "pending"


def test_non_resolve_action_cannot_set_knowledge_decision():
    store = InMemoryWorkflowStore()
    case = store.create_case(
        "tenant-a",
        {
            "case_type": "equipment_issue",
            "subject": {},
            "context": {},
            "callback_url": "",
            "idempotency_key": "knowledge-case-action",
        },
        "query-service",
    )

    with pytest.raises(ValueError, match="resolve"):
        store.apply_action(
            "tenant-a",
            case["case_id"],
            {
                "action": "assign",
                "assignee": "engineer-a",
                "knowledge_decision": "exclude",
                "idempotency_key": "knowledge-assign-invalid",
            },
            "oa-connector",
        )


def test_case_action_request_validates_knowledge_decision_contract():
    valid = CaseActionRequest.model_validate(
        {
            "action": "resolve",
            "result": {"solution": "更换损坏部件", "verification": "试运行通过"},
            "knowledge_decision": "include",
            "idempotency_key": "validated-resolution",
        }
    )
    assert valid.knowledge_decision == "include"

    with pytest.raises(ValidationError, match="只有 resolve 动作"):
        CaseActionRequest.model_validate(
            {
                "action": "assign",
                "assignee": "engineer-a",
                "knowledge_decision": "exclude",
                "idempotency_key": "invalid-decision-action",
            }
        )

    with pytest.raises(ValidationError, match="solution, verification"):
        CaseActionRequest.model_validate(
            {
                "action": "resolve",
                "result": {},
                "knowledge_decision": "include",
                "idempotency_key": "invalid-decision-content",
            }
        )
