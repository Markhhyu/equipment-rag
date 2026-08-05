"""In-memory and MongoDB persistence adapters for workflow state."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import threading
import uuid
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from pymongo import ASCENDING, MongoClient
from pymongo.errors import DuplicateKeyError

from app.modules.workflow.domain.models import CaseStatus, DeliveryStatus, WorkflowActionType


TRANSITIONS = {
    WorkflowActionType.ASSIGN: ({CaseStatus.PENDING, CaseStatus.ASSIGNED}, CaseStatus.ASSIGNED),
    WorkflowActionType.START_REVIEW: ({CaseStatus.ASSIGNED}, CaseStatus.IN_REVIEW),
    WorkflowActionType.RESOLVE: ({CaseStatus.PENDING, CaseStatus.ASSIGNED, CaseStatus.IN_REVIEW}, CaseStatus.RESOLVED),
    WorkflowActionType.REJECT: ({CaseStatus.PENDING, CaseStatus.ASSIGNED, CaseStatus.IN_REVIEW}, CaseStatus.REJECTED),
    WorkflowActionType.CANCEL: ({CaseStatus.PENDING, CaseStatus.ASSIGNED, CaseStatus.IN_REVIEW}, CaseStatus.CANCELLED),
}


def _now() -> datetime:
    return datetime.now(UTC)


def _public(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _public(item) for key, item in value.items() if key not in {"_id", "signing_secret"}}
    if isinstance(value, list):
        return [_public(item) for item in value]
    return value


def _event_payload(event: dict[str, Any]) -> bytes:
    serializable = {
        key: value.isoformat() if isinstance(value, datetime) else value
        for key, value in event.items()
    }
    return json.dumps(serializable, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


class InMemoryWorkflowStore:
    def __init__(self) -> None:
        self.cases: dict[tuple[str, str], dict[str, Any]] = {}
        self.actions: dict[tuple[str, str], dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self.subscriptions: dict[tuple[str, str], dict[str, Any]] = {}
        self.deliveries: dict[tuple[str, str], dict[str, Any]] = {}
        self._lock = threading.RLock()

    def create_case(self, tenant_id: str, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        with self._lock:
            existing = next(
                (
                    item
                    for (tenant, _), item in self.cases.items()
                    if tenant == tenant_id and item["idempotency_key"] == payload["idempotency_key"]
                ),
                None,
            )
            if existing:
                return _public(deepcopy(existing))
            now = _now()
            case = {
                "case_id": uuid.uuid4().hex,
                "tenant_id": tenant_id,
                "case_type": payload["case_type"],
                "status": CaseStatus.PENDING.value,
                "subject": deepcopy(payload.get("subject") or {}),
                "context": deepcopy(payload.get("context") or {}),
                "callback_url": str(payload.get("callback_url") or ""),
                "idempotency_key": payload["idempotency_key"],
                "assignee": "",
                "result": {},
                "created_by": actor,
                "created_at": now,
                "updated_at": now,
            }
            self.cases[(tenant_id, case["case_id"])] = case
            self._emit(tenant_id, case, "review.requested")
            return _public(deepcopy(case))

    def get_case(self, tenant_id: str, case_id: str) -> dict[str, Any] | None:
        case = self.cases.get((tenant_id, case_id))
        return _public(deepcopy(case)) if case else None

    def list_cases(
        self,
        tenant_id: str,
        status: str = "",
        query: str = "",
        limit: int = 100,
    ) -> dict[str, Any]:
        normalized_query = query.strip().casefold()
        items = [item for (tenant, _), item in self.cases.items() if tenant == tenant_id]
        if status:
            items = [item for item in items if item["status"] == status]
        if normalized_query:
            items = [
                item
                for item in items
                if normalized_query in json.dumps(item, ensure_ascii=False, default=str).casefold()
            ]
        items.sort(key=lambda item: item["updated_at"], reverse=True)
        total = len(items)
        return {
            "items": _public(deepcopy(items[: max(1, min(limit, 500))])),
            "total": total,
        }

    def apply_action(self, tenant_id: str, case_id: str, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        with self._lock:
            action_key = (tenant_id, payload["idempotency_key"])
            if action_key in self.actions:
                return self.get_case(tenant_id, case_id) or {}
            case = self.cases.get((tenant_id, case_id))
            if not case:
                raise KeyError(case_id)
            action = WorkflowActionType(payload["action"])
            allowed, target = TRANSITIONS[action]
            if CaseStatus(case["status"]) not in allowed:
                raise ValueError(f"状态 {case['status']} 不允许执行动作 {action.value}")
            if action == WorkflowActionType.ASSIGN and not str(payload.get("assignee") or "").strip():
                raise ValueError("assign 动作必须提供 assignee")
            case["status"] = target.value
            if payload.get("assignee"):
                case["assignee"] = str(payload["assignee"])
            if payload.get("result"):
                case["result"] = deepcopy(payload["result"])
            case["updated_at"] = _now()
            self.actions[action_key] = {**deepcopy(payload), "case_id": case_id, "actor": actor}
            self._emit(tenant_id, case, f"review.{target.value}")
            return _public(deepcopy(case))

    def list_events(self, tenant_id: str, after: str = "", limit: int = 100) -> list[dict[str, Any]]:
        items = [item for item in self.events if item["tenant_id"] == tenant_id]
        if after:
            index = next((index for index, item in enumerate(items) if item["event_id"] == after), -1)
            items = items[index + 1 :] if index >= 0 else items
        return _public(deepcopy(items[: max(1, min(limit, 500))]))

    def create_subscription(self, tenant_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            subscription_id = uuid.uuid4().hex
            subscription = {
                "subscription_id": subscription_id,
                "tenant_id": tenant_id,
                "connector_type": payload["connector_type"],
                "callback_url": str(payload["callback_url"]),
                "event_types": list(dict.fromkeys(payload.get("event_types") or [])),
                "signing_secret": payload["signing_secret"],
                "active": True,
                "created_at": _now(),
            }
            self.subscriptions[(tenant_id, subscription_id)] = subscription
            return _public(deepcopy(subscription))

    def list_deliveries(self, tenant_id: str, status: str = "pending", limit: int = 100) -> list[dict[str, Any]]:
        items = [
            item
            for (tenant, _), item in self.deliveries.items()
            if tenant == tenant_id and (not status or item["status"] == status)
        ]
        items.sort(key=lambda item: item["next_retry_at"])
        return _public(deepcopy(items[: max(1, min(limit, 500))]))

    def ack_delivery(self, tenant_id: str, delivery_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            delivery = self.deliveries.get((tenant_id, delivery_id))
            if not delivery:
                raise KeyError(delivery_id)
            delivery.update(
                {
                    "status": str(payload["status"]),
                    "remote_message_id": str(payload.get("remote_message_id") or ""),
                    "error": str(payload.get("error") or ""),
                    "acknowledged_at": _now(),
                }
            )
            if delivery["status"] == DeliveryStatus.FAILED.value:
                delivery["retry_count"] += 1
            return _public(deepcopy(delivery))

    def _emit(self, tenant_id: str, case: dict[str, Any], event_type: str) -> None:
        event = {
            "event_id": uuid.uuid4().hex,
            "event_type": event_type,
            "occurred_at": _now(),
            "tenant_id": tenant_id,
            "case_id": case["case_id"],
            "subject": deepcopy(case["subject"]),
            "context": deepcopy(case["context"]),
            "callback_url": case["callback_url"],
        }
        self.events.append(event)
        for (tenant, subscription_id), subscription in self.subscriptions.items():
            if tenant != tenant_id or not subscription["active"] or event_type not in subscription["event_types"]:
                continue
            signature = hmac.new(
                subscription["signing_secret"].encode("utf-8"),
                _event_payload(event),
                hashlib.sha256,
            ).hexdigest()
            delivery_id = uuid.uuid4().hex
            self.deliveries[(tenant_id, delivery_id)] = {
                "delivery_id": delivery_id,
                "subscription_id": subscription_id,
                "event": deepcopy(event),
                "callback_url": subscription["callback_url"],
                "signature": f"sha256={signature}",
                "status": DeliveryStatus.PENDING.value,
                "retry_count": 0,
                "next_retry_at": _now(),
                "remote_message_id": "",
                "error": "",
            }


class MongoWorkflowStore(InMemoryWorkflowStore):
    """Mongo-backed store preserving the same contract; no vendor connector code lives here."""

    def __init__(self, mongo_url: str, database: str) -> None:
        super().__init__()
        client = MongoClient(mongo_url, appname="equipment-rag-workflow", tz_aware=True)
        db = client[database]
        self._case_collection = db["workflow_cases"]
        self._action_collection = db["workflow_actions"]
        self._event_collection = db["workflow_events"]
        self._subscription_collection = db["workflow_subscriptions"]
        self._delivery_collection = db["workflow_deliveries"]
        self._case_collection.create_index([("tenant_id", ASCENDING), ("case_id", ASCENDING)], unique=True)
        self._case_collection.create_index([("tenant_id", ASCENDING), ("idempotency_key", ASCENDING)], unique=True)
        self._case_collection.create_index(
            [("tenant_id", ASCENDING), ("status", ASCENDING), ("updated_at", ASCENDING)]
        )
        self._action_collection.create_index([("tenant_id", ASCENDING), ("idempotency_key", ASCENDING)], unique=True)
        self._event_collection.create_index([("tenant_id", ASCENDING), ("occurred_at", ASCENDING)])
        self._delivery_collection.create_index([("tenant_id", ASCENDING), ("status", ASCENDING), ("next_retry_at", ASCENDING)])

    def create_case(self, tenant_id: str, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        selector = {"tenant_id": tenant_id, "idempotency_key": payload["idempotency_key"]}
        existing = self._case_collection.find_one(selector, {"_id": 0})
        if existing:
            return _public(existing)
        now = _now()
        case = {
            "case_id": uuid.uuid4().hex,
            "tenant_id": tenant_id,
            "case_type": payload["case_type"],
            "status": CaseStatus.PENDING.value,
            "subject": deepcopy(payload.get("subject") or {}),
            "context": deepcopy(payload.get("context") or {}),
            "callback_url": str(payload.get("callback_url") or ""),
            "idempotency_key": payload["idempotency_key"],
            "assignee": "",
            "result": {},
            "created_by": actor,
            "created_at": now,
            "updated_at": now,
        }
        try:
            self._case_collection.insert_one(case)
        except DuplicateKeyError:
            return _public(self._case_collection.find_one(selector, {"_id": 0}) or {})
        self._emit(tenant_id, case, "review.requested")
        return _public(case)

    def get_case(self, tenant_id: str, case_id: str) -> dict[str, Any] | None:
        case = self._case_collection.find_one({"tenant_id": tenant_id, "case_id": case_id}, {"_id": 0})
        return _public(case) if case else None

    def list_cases(
        self,
        tenant_id: str,
        status: str = "",
        query: str = "",
        limit: int = 100,
    ) -> dict[str, Any]:
        selector: dict[str, Any] = {"tenant_id": tenant_id}
        if status:
            selector["status"] = status
        if query.strip():
            pattern = {"$regex": re.escape(query.strip()), "$options": "i"}
            selector["$or"] = [
                {"case_id": pattern},
                {"case_type": pattern},
                {"assignee": pattern},
                {"subject.question": pattern},
                {"subject.title": pattern},
                {"subject.trace_id": pattern},
                {"subject.device_model": pattern},
            ]
        total = self._case_collection.count_documents(selector)
        items = list(
            self._case_collection.find(selector, {"_id": 0})
            .sort("updated_at", -1)
            .limit(max(1, min(limit, 500)))
        )
        return {"items": _public(items), "total": total}

    def apply_action(self, tenant_id: str, case_id: str, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        action_selector = {"tenant_id": tenant_id, "idempotency_key": payload["idempotency_key"]}
        if self._action_collection.find_one(action_selector):
            return self.get_case(tenant_id, case_id) or {}
        case = self._case_collection.find_one({"tenant_id": tenant_id, "case_id": case_id}, {"_id": 0})
        if not case:
            raise KeyError(case_id)
        action = WorkflowActionType(payload["action"])
        allowed, target = TRANSITIONS[action]
        if CaseStatus(case["status"]) not in allowed:
            raise ValueError(f"状态 {case['status']} 不允许执行动作 {action.value}")
        if action == WorkflowActionType.ASSIGN and not str(payload.get("assignee") or "").strip():
            raise ValueError("assign 动作必须提供 assignee")
        updates: dict[str, Any] = {"status": target.value, "updated_at": _now()}
        if payload.get("assignee"):
            updates["assignee"] = str(payload["assignee"])
        if payload.get("result"):
            updates["result"] = deepcopy(payload["result"])
        result = self._case_collection.update_one(
            {"tenant_id": tenant_id, "case_id": case_id, "status": case["status"]},
            {"$set": updates},
        )
        if result.modified_count != 1:
            raise ValueError("工单状态已被其他处理人更新，请刷新后重试")
        action_record = {
            **deepcopy(payload),
            "tenant_id": tenant_id,
            "case_id": case_id,
            "actor": actor,
            "created_at": _now(),
        }
        try:
            self._action_collection.insert_one(action_record)
        except DuplicateKeyError:
            pass
        updated = self._case_collection.find_one({"tenant_id": tenant_id, "case_id": case_id}, {"_id": 0}) or {}
        self._emit(tenant_id, updated, f"review.{target.value}")
        return _public(updated)

    def list_events(self, tenant_id: str, after: str = "", limit: int = 100) -> list[dict[str, Any]]:
        selector: dict[str, Any] = {"tenant_id": tenant_id}
        if after:
            cursor_event = self._event_collection.find_one(
                {"tenant_id": tenant_id, "event_id": after},
                {"occurred_at": 1},
            )
            if cursor_event:
                selector["occurred_at"] = {"$gt": cursor_event["occurred_at"]}
        items = list(
            self._event_collection.find(selector, {"_id": 0})
            .sort("occurred_at", ASCENDING)
            .limit(max(1, min(limit, 500)))
        )
        return _public(items)

    def create_subscription(self, tenant_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        subscription = {
            "subscription_id": uuid.uuid4().hex,
            "tenant_id": tenant_id,
            "connector_type": payload["connector_type"],
            "callback_url": str(payload["callback_url"]),
            "event_types": list(dict.fromkeys(payload.get("event_types") or [])),
            "signing_secret": payload["signing_secret"],
            "active": True,
            "created_at": _now(),
        }
        self._subscription_collection.insert_one(subscription)
        return _public(subscription)

    def list_deliveries(self, tenant_id: str, status: str = "pending", limit: int = 100) -> list[dict[str, Any]]:
        selector: dict[str, Any] = {"tenant_id": tenant_id}
        if status:
            selector["status"] = status
        items = list(
            self._delivery_collection.find(selector, {"_id": 0})
            .sort("next_retry_at", ASCENDING)
            .limit(max(1, min(limit, 500)))
        )
        return _public(items)

    def ack_delivery(self, tenant_id: str, delivery_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        updates = {
            "status": str(payload["status"]),
            "remote_message_id": str(payload.get("remote_message_id") or ""),
            "error": str(payload.get("error") or ""),
            "acknowledged_at": _now(),
        }
        increment = {"retry_count": 1} if updates["status"] == DeliveryStatus.FAILED.value else {}
        result = self._delivery_collection.update_one(
            {"tenant_id": tenant_id, "delivery_id": delivery_id},
            {"$set": updates, **({"$inc": increment} if increment else {})},
        )
        if result.matched_count != 1:
            raise KeyError(delivery_id)
        return _public(
            self._delivery_collection.find_one(
                {"tenant_id": tenant_id, "delivery_id": delivery_id},
                {"_id": 0},
            )
            or {}
        )

    def _emit(self, tenant_id: str, case: dict[str, Any], event_type: str) -> None:
        event = {
            "event_id": uuid.uuid4().hex,
            "event_type": event_type,
            "occurred_at": _now(),
            "tenant_id": tenant_id,
            "case_id": case["case_id"],
            "subject": deepcopy(case["subject"]),
            "context": deepcopy(case["context"]),
            "callback_url": case["callback_url"],
        }
        self._event_collection.insert_one(deepcopy(event))
        subscriptions = self._subscription_collection.find(
            {"tenant_id": tenant_id, "active": True, "event_types": event_type},
            {"_id": 0},
        )
        deliveries = []
        for subscription in subscriptions:
            digest = hmac.new(
                subscription["signing_secret"].encode("utf-8"),
                _event_payload(event),
                hashlib.sha256,
            ).hexdigest()
            deliveries.append(
                {
                    "delivery_id": uuid.uuid4().hex,
                    "tenant_id": tenant_id,
                    "subscription_id": subscription["subscription_id"],
                    "event": deepcopy(event),
                    "callback_url": subscription["callback_url"],
                    "signature": f"sha256={digest}",
                    "status": DeliveryStatus.PENDING.value,
                    "retry_count": 0,
                    "next_retry_at": _now(),
                    "remote_message_id": "",
                    "error": "",
                }
            )
        if deliveries:
            self._delivery_collection.insert_many(deliveries)


_store: InMemoryWorkflowStore | None = None
_store_lock = threading.RLock()


def get_workflow_store() -> InMemoryWorkflowStore:
    global _store
    if _store is not None:
        return _store
    with _store_lock:
        if _store is None:
            mongo_url = os.getenv("MONGO_URL")
            database = os.getenv("MONGO_DB_NAME")
            _store = MongoWorkflowStore(mongo_url, database) if mongo_url and database else InMemoryWorkflowStore()
        return _store


def reset_workflow_store_for_tests(store: InMemoryWorkflowStore | None = None) -> None:
    global _store
    with _store_lock:
        _store = store
