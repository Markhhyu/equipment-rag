from __future__ import annotations

import os
import uuid

import pytest

from app.modules.workflow.infrastructure.store import MongoWorkflowStore


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_MONGO_INTEGRATION") != "1", reason="MongoDB integration test is opt-in"),
]


def test_mongo_workflow_persists_signed_delivery_and_case_transition():
    database = f"equipment_rag_workflow_test_{uuid.uuid4().hex}"
    store = MongoWorkflowStore(os.environ["MONGO_URL"], database)
    try:
        store.create_subscription(
            "integration",
            {
                "connector_type": "custom-adapter",
                "callback_url": "https://connector.example.test/events",
                "event_types": ["review.requested"],
                "signing_secret": "integration-signing-secret-123456",
            },
        )
        case = store.create_case(
            "integration",
            {
                "case_type": "answer_review",
                "subject": {"trace_id": "trace-integration"},
                "context": {"reason": "insufficient_evidence"},
                "callback_url": "",
                "idempotency_key": "case-integration",
            },
            "test",
        )
        deliveries = store.list_deliveries("integration")
        assert len(deliveries) == 1
        assert deliveries[0]["signature"].startswith("sha256=")
        assert deliveries[0]["event"]["case_id"] == case["case_id"]

        assigned = store.apply_action(
            "integration",
            case["case_id"],
            {"action": "assign", "assignee": "operator", "idempotency_key": "assign-integration"},
            "test",
        )
        assert assigned["status"] == "assigned"
        assert store.get_case("integration", case["case_id"])["assignee"] == "operator"
    finally:
        store._case_collection.database.client.drop_database(database)
