import hashlib
import hmac

from app.workflow.models import DeliveryStatus
from app.workflow.store import InMemoryWorkflowStore, _event_payload


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
    assert store.list_events("tenant-b") == []
