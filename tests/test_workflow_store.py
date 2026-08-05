import hashlib
import hmac

from app.modules.workflow.domain.models import DeliveryStatus
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
