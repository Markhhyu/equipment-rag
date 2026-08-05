"""Persistence and aggregation for tenant-scoped Q&A operations analytics."""

from __future__ import annotations

import os
import threading
from collections import Counter
from copy import deepcopy
from datetime import UTC, date, datetime, time, timedelta, timezone
from typing import Any

from pymongo import ASCENDING, DESCENDING, MongoClient, UpdateOne

from app.platform.runtime.config import load_runtime_config


RESOLUTION_STATUSES = frozenset({"pending", "solved", "partial", "unsolved"})
TECHNICAL_STATUSES = frozenset({"running", "succeeded", "failed"})


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return _utcnow()


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def _date_range(days: int, timezone_offset_minutes: int, now: datetime | None = None) -> tuple[datetime, datetime, timezone, list[date]]:
    days = max(1, min(int(days), 365))
    tz = timezone(timedelta(minutes=timezone_offset_minutes))
    current = _as_utc(now).astimezone(tz)
    dates = [current.date() - timedelta(days=offset) for offset in range(days - 1, -1, -1)]
    start_local = datetime.combine(dates[0], time.min, tzinfo=tz)
    end_local = datetime.combine(current.date() + timedelta(days=1), time.min, tzinfo=tz)
    return start_local.astimezone(UTC), end_local.astimezone(UTC), tz, dates


def _clean_names(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return list(dict.fromkeys(str(value).strip()[:128] for value in values if str(value).strip()))[:20]


def _summary(records: list[dict[str, Any]], days: int, timezone_offset_minutes: int, now: datetime | None = None) -> dict[str, Any]:
    start, end, tz, dates = _date_range(days, timezone_offset_minutes, now)
    filtered = [record for record in records if start <= _as_utc(record.get("started_at")) < end]
    filtered.sort(key=lambda record: _as_utc(record.get("started_at")), reverse=True)

    totals = {
        "questions": len(filtered),
        "technical_succeeded": 0,
        "technical_failed": 0,
        "technical_in_progress": 0,
        "solved": 0,
        "partial": 0,
        "unsolved": 0,
        "pending_confirmation": 0,
        "requires_human_review": 0,
        "positive_feedback": 0,
        "negative_feedback": 0,
    }
    trend_map = {
        value.isoformat(): {
            "date": value.isoformat(),
            "questions": 0,
            "technical_succeeded": 0,
            "technical_failed": 0,
            "solved": 0,
            "partial": 0,
            "unsolved": 0,
        }
        for value in dates
    }
    device_counts: Counter[str] = Counter()
    failure_counts: Counter[str] = Counter()
    attention: list[dict[str, Any]] = []

    for record in filtered:
        day = _as_utc(record.get("started_at")).astimezone(tz).date().isoformat()
        bucket = trend_map[day]
        bucket["questions"] += 1

        technical_status = str(record.get("technical_status") or "running")
        technical_key = {
            "succeeded": "technical_succeeded",
            "failed": "technical_failed",
        }.get(technical_status, "technical_in_progress")
        totals[technical_key] += 1
        if technical_key in bucket:
            bucket[technical_key] += 1

        resolution_status = str(record.get("resolution_status") or "pending")
        if resolution_status not in RESOLUTION_STATUSES:
            resolution_status = "pending"
        resolution_key = "pending_confirmation" if resolution_status == "pending" else resolution_status
        totals[resolution_key] += 1
        if resolution_status in {"solved", "partial", "unsolved"}:
            bucket[resolution_status] += 1

        if bool(record.get("requires_human_review")):
            totals["requires_human_review"] += 1
        feedback_value = record.get("feedback_value")
        if feedback_value == 1:
            totals["positive_feedback"] += 1
        elif feedback_value == 0:
            totals["negative_feedback"] += 1

        device_names = _clean_names(record.get("device_names"))
        device_counts.update(device_names)
        raw_error = str(record.get("error") or "").strip()
        error = raw_error.splitlines()[0][:160] if raw_error else ""
        if technical_status == "failed":
            failure_counts[error or "未知运行异常"] += 1

        if technical_status == "failed" or resolution_status in {"partial", "unsolved"} or bool(record.get("requires_human_review")):
            attention.append(
                {
                    "trace_id": str(record.get("trace_id") or ""),
                    "session_id": str(record.get("session_id") or ""),
                    "question": str(record.get("question_preview") or "")[:200],
                    "technical_status": technical_status,
                    "resolution_status": resolution_status,
                    "requires_human_review": bool(record.get("requires_human_review")),
                    "review_reason": str(record.get("review_reason") or "")[:300],
                    "device_names": device_names,
                    "started_at": _as_utc(record.get("started_at")).isoformat(),
                }
            )

    confirmed = totals["solved"] + totals["partial"] + totals["unsolved"]
    finished = totals["technical_succeeded"] + totals["technical_failed"]
    return {
        "generated_at": _as_utc(now).isoformat(),
        "timezone_offset_minutes": timezone_offset_minutes,
        "range": {
            "days": len(dates),
            "start_date": dates[0].isoformat(),
            "end_date": dates[-1].isoformat(),
        },
        "totals": totals,
        "rates": {
            "technical_success_rate": _ratio(totals["technical_succeeded"], finished),
            "confirmed_resolution_rate": _ratio(totals["solved"], confirmed),
            "outcome_confirmation_rate": _ratio(confirmed, totals["questions"]),
        },
        "trend": list(trend_map.values()),
        "top_devices": [{"name": name, "count": count} for name, count in device_counts.most_common(8)],
        "failure_reasons": [{"reason": reason, "count": count} for reason, count in failure_counts.most_common(6)],
        "recent_attention": attention[:12],
    }


class InMemoryQueryAnalyticsStore:
    def __init__(self) -> None:
        self.records: dict[tuple[str, str], dict[str, Any]] = {}
        self._lock = threading.RLock()

    def record_started(self, tenant_id: str, trace_id: str, session_id: str, question: str) -> None:
        with self._lock:
            key = (tenant_id, trace_id)
            now = _utcnow()
            existing = self.records.get(key)
            if existing:
                existing.update({"technical_status": "running", "updated_at": now})
                return
            self.records[key] = {
                "tenant_id": tenant_id,
                "trace_id": trace_id,
                "session_id": session_id,
                "question_preview": str(question or "").strip()[:200],
                "technical_status": "running",
                "resolution_status": "pending",
                "resolution_source": "",
                "feedback_value": None,
                "requires_human_review": False,
                "review_reason": "",
                "device_names": [],
                "started_at": now,
                "updated_at": now,
            }

    def record_completed(self, tenant_id: str, trace_id: str, result: dict[str, Any]) -> None:
        self._update(
            tenant_id,
            trace_id,
            {
                "technical_status": "succeeded",
                "answer_policy": str(result.get("answer_policy") or "answer"),
                "requires_human_review": bool(result.get("requires_human_review")),
                "review_reason": str(result.get("review_reason") or "")[:500],
                "device_names": _clean_names(result.get("device_names")),
                "completed_at": _utcnow(),
            },
        )

    def record_failed(self, tenant_id: str, trace_id: str, error: str) -> None:
        self._update(
            tenant_id,
            trace_id,
            {"technical_status": "failed", "error": str(error or "")[:2000], "completed_at": _utcnow()},
        )

    def record_feedback(self, tenant_id: str, trace_id: str, value: int) -> None:
        self._update(tenant_id, trace_id, {"feedback_value": int(value), "feedback_at": _utcnow()})

    def record_resolution(self, tenant_id: str, trace_id: str, status: str, comment: str = "") -> None:
        if status not in RESOLUTION_STATUSES - {"pending"}:
            raise ValueError("解决结果只能是 solved、partial 或 unsolved")
        self._update(
            tenant_id,
            trace_id,
            {
                "resolution_status": status,
                "resolution_source": "user",
                "resolution_comment": str(comment or "").strip()[:500],
                "resolution_updated_at": _utcnow(),
            },
        )

    def summary(self, tenant_id: str, days: int, timezone_offset_minutes: int, now: datetime | None = None) -> dict[str, Any]:
        records = [deepcopy(value) for (tenant, _), value in self.records.items() if tenant == tenant_id]
        return _summary(records, days, timezone_offset_minutes, now)

    def _update(self, tenant_id: str, trace_id: str, values: dict[str, Any]) -> None:
        with self._lock:
            key = (tenant_id, trace_id)
            now = _utcnow()
            record = self.records.setdefault(
                key,
                {
                    "tenant_id": tenant_id,
                    "trace_id": trace_id,
                    "session_id": "",
                    "question_preview": "",
                    "technical_status": "running",
                    "resolution_status": "pending",
                    "started_at": now,
                },
            )
            record.update(deepcopy(values))
            record["updated_at"] = now


class MongoQueryAnalyticsStore(InMemoryQueryAnalyticsStore):
    def __init__(self, mongo_url: str, database: str, collection: str, run_collection: str) -> None:
        client = MongoClient(mongo_url, appname="equipment-rag-query-analytics", tz_aware=True)
        db = client[database]
        self.collection = db[collection]
        self.run_collection = db[run_collection]
        self.history_collection = db["chat_message"]
        self.collection.create_index([("tenant_id", ASCENDING), ("trace_id", ASCENDING)], unique=True)
        self.collection.create_index([("tenant_id", ASCENDING), ("started_at", DESCENDING)])
        self.collection.create_index([("tenant_id", ASCENDING), ("resolution_status", ASCENDING), ("started_at", DESCENDING)])

    def record_started(self, tenant_id: str, trace_id: str, session_id: str, question: str) -> None:
        now = _utcnow()
        self.collection.update_one(
            {"tenant_id": tenant_id, "trace_id": trace_id},
            {
                "$set": {"technical_status": "running", "updated_at": now},
                "$setOnInsert": {
                    "tenant_id": tenant_id,
                    "trace_id": trace_id,
                    "session_id": session_id,
                    "question_preview": str(question or "").strip()[:200],
                    "resolution_status": "pending",
                    "resolution_source": "",
                    "feedback_value": None,
                    "requires_human_review": False,
                    "review_reason": "",
                    "device_names": [],
                    "started_at": now,
                },
            },
            upsert=True,
        )

    def record_completed(self, tenant_id: str, trace_id: str, result: dict[str, Any]) -> None:
        self._mongo_update(
            tenant_id,
            trace_id,
            {
                "technical_status": "succeeded",
                "answer_policy": str(result.get("answer_policy") or "answer"),
                "requires_human_review": bool(result.get("requires_human_review")),
                "review_reason": str(result.get("review_reason") or "")[:500],
                "device_names": _clean_names(result.get("device_names")),
                "completed_at": _utcnow(),
            },
        )

    def record_failed(self, tenant_id: str, trace_id: str, error: str) -> None:
        self._mongo_update(
            tenant_id,
            trace_id,
            {"technical_status": "failed", "error": str(error or "")[:2000], "completed_at": _utcnow()},
        )

    def record_feedback(self, tenant_id: str, trace_id: str, value: int) -> None:
        self._mongo_update(tenant_id, trace_id, {"feedback_value": int(value), "feedback_at": _utcnow()})

    def record_resolution(self, tenant_id: str, trace_id: str, status: str, comment: str = "") -> None:
        if status not in RESOLUTION_STATUSES - {"pending"}:
            raise ValueError("解决结果只能是 solved、partial 或 unsolved")
        self._mongo_update(
            tenant_id,
            trace_id,
            {
                "resolution_status": status,
                "resolution_source": "user",
                "resolution_comment": str(comment or "").strip()[:500],
                "resolution_updated_at": _utcnow(),
            },
        )

    def summary(self, tenant_id: str, days: int, timezone_offset_minutes: int, now: datetime | None = None) -> dict[str, Any]:
        start, end, _, _ = _date_range(days, timezone_offset_minutes, now)
        self._backfill_runs(tenant_id, start, end)
        records = list(
            self.collection.find(
                {"tenant_id": tenant_id, "started_at": {"$gte": start, "$lt": end}},
                {"_id": 0},
            ).sort("started_at", DESCENDING)
        )
        return _summary(records, days, timezone_offset_minutes, now)

    def _mongo_update(self, tenant_id: str, trace_id: str, values: dict[str, Any]) -> None:
        now = _utcnow()
        insert_defaults = {
            "tenant_id": tenant_id,
            "trace_id": trace_id,
            "session_id": "",
            "question_preview": "",
            "technical_status": "running",
            "resolution_status": "pending",
            "started_at": now,
        }
        insert_defaults = {key: value for key, value in insert_defaults.items() if key not in values}
        self.collection.update_one(
            {"tenant_id": tenant_id, "trace_id": trace_id},
            {
                "$set": {**deepcopy(values), "updated_at": now},
                "$setOnInsert": insert_defaults,
            },
            upsert=True,
        )

    def _backfill_runs(self, tenant_id: str, start: datetime, end: datetime) -> None:
        runs = list(
            self.run_collection.find(
                {"tenant_id": tenant_id, "kind": "query", "created_at": {"$gte": start, "$lt": end}},
                {"_id": 0},
            )
        )
        if not runs:
            return
        status_map = {"succeeded": "succeeded", "failed": "failed", "cancelled": "failed"}
        operations = []
        trace_ids = []
        for run in runs:
            trace_id = str(run.get("run_id") or "")
            if not trace_id:
                continue
            trace_ids.append(trace_id)
            result = run.get("result") or {}
            input_data = run.get("input") or {}
            operations.append(
                UpdateOne(
                    {"tenant_id": tenant_id, "trace_id": trace_id},
                    {
                        "$setOnInsert": {
                            "tenant_id": tenant_id,
                            "trace_id": trace_id,
                            "session_id": str(input_data.get("session_id") or ""),
                            "question_preview": str(input_data.get("user_query") or "").strip()[:200],
                            "technical_status": status_map.get(str(run.get("status") or ""), "running"),
                            "resolution_status": "pending",
                            "resolution_source": "",
                            "feedback_value": None,
                            "requires_human_review": bool(result.get("requires_human_review")),
                            "review_reason": str(result.get("review_reason") or "")[:500],
                            "answer_policy": str(result.get("answer_policy") or ""),
                            "device_names": [],
                            "error": str(run.get("error") or "")[:2000],
                            "started_at": _as_utc(run.get("created_at")),
                            "completed_at": _as_utc(run.get("updated_at")),
                            "updated_at": _as_utc(run.get("updated_at")),
                        }
                    },
                    upsert=True,
                )
            )
        if operations:
            self.collection.bulk_write(operations, ordered=False)
        history = self.history_collection.find(
            {"trace_id": {"$in": trace_ids}, "role": "assistant"},
            {"_id": 0, "trace_id": 1, "feedback_value": 1, "resolution_status": 1},
        )
        history_updates = []
        for item in history:
            values: dict[str, Any] = {}
            if item.get("feedback_value") in (0, 1):
                values["feedback_value"] = item["feedback_value"]
            if item.get("resolution_status") in RESOLUTION_STATUSES - {"pending"}:
                values["resolution_status"] = item["resolution_status"]
                values["resolution_source"] = "user"
            if values:
                history_updates.append(
                    UpdateOne(
                        {"tenant_id": tenant_id, "trace_id": item["trace_id"]},
                        {"$set": values},
                    )
                )
        if history_updates:
            self.collection.bulk_write(history_updates, ordered=False)


_store: InMemoryQueryAnalyticsStore | None = None
_store_lock = threading.RLock()


def get_query_analytics_store() -> InMemoryQueryAnalyticsStore:
    global _store
    if _store is not None:
        return _store
    with _store_lock:
        if _store is None:
            config = load_runtime_config()
            if config.run_store_backend == "mongodb":
                _store = MongoQueryAnalyticsStore(
                    config.mongo_url,
                    config.mongo_database,
                    os.getenv("QUERY_ANALYTICS_COLLECTION") or "query_outcomes",
                    config.run_collection,
                )
            else:
                _store = InMemoryQueryAnalyticsStore()
        return _store


def reset_query_analytics_store_for_tests(store: InMemoryQueryAnalyticsStore | None = None) -> None:
    global _store
    with _store_lock:
        _store = store
