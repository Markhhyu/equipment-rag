from __future__ import annotations

import os
import socket
import threading
from abc import ABC, abstractmethod
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from threading import RLock
from typing import Any, Callable

from app.platform.runtime.config import load_runtime_config


class RunStatus(StrEnum):
    """Agent 一次运行从排队到结束可能出现的状态。"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class RunRecord:
    """可持久化的 Agent 运行记录，也是重试和状态查询的唯一事实来源。"""

    run_id: str
    kind: str
    input: dict[str, Any]
    tenant_id: str = "local"
    status: RunStatus = RunStatus.PENDING
    attempt: int = 0
    max_attempts: int = 3
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    error: str | None = None
    result: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> RunRecord:
        return cls(
            run_id=document["run_id"],
            kind=document["kind"],
            input=document.get("input") or {},
            tenant_id=document.get("tenant_id") or "local",
            status=RunStatus(document["status"]),
            attempt=int(document.get("attempt", 0)),
            max_attempts=int(document.get("max_attempts", 3)),
            lease_owner=document.get("lease_owner"),
            lease_expires_at=document.get("lease_expires_at"),
            error=document.get("error"),
            result=document.get("result") or {},
            created_at=document.get("created_at") or datetime.now(UTC),
            updated_at=document.get("updated_at") or datetime.now(UTC),
        )

    def to_document(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "kind": self.kind,
            "input": deepcopy(self.input),
            "tenant_id": self.tenant_id,
            "status": self.status.value,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "lease_owner": self.lease_owner,
            "lease_expires_at": self.lease_expires_at,
            "error": self.error,
            "result": deepcopy(self.result),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def to_public_dict(self) -> dict[str, Any]:
        # 运行中但租约已过期，通常表示旧 Worker 异常退出，此时允许新 Worker 重试。
        lease_expired = self.lease_expires_at is not None and self.lease_expires_at <= datetime.now(UTC)
        retryable_status = self.status == RunStatus.FAILED or (self.status == RunStatus.RUNNING and lease_expired)
        return {
            "run_id": self.run_id,
            "kind": self.kind,
            "tenant_id": self.tenant_id,
            "status": self.status.value,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "lease_expires_at": self.lease_expires_at.isoformat() if self.lease_expires_at else None,
            "error": self.error,
            "result": deepcopy(self.result),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "retryable": retryable_status and self.attempt < self.max_attempts,
        }


class RunStore(ABC):
    """运行状态存储接口；内存和 MongoDB 后端必须遵守相同状态转换规则。"""

    @abstractmethod
    def create(
        self,
        run_id: str,
        kind: str,
        input_data: dict[str, Any],
        max_attempts: int,
        tenant_id: str = "local",
    ) -> RunRecord:
        raise NotImplementedError

    @abstractmethod
    def get(self, run_id: str) -> RunRecord | None:
        raise NotImplementedError

    def get_for_tenant(self, run_id: str, tenant_id: str) -> RunRecord | None:
        # 查询时同时校验租户，避免仅凭 run_id 读取其他租户的数据。
        record = self.get(run_id)
        if record is None or record.tenant_id != tenant_id:
            return None
        return record

    @abstractmethod
    def claim(self, run_id: str, owner: str, lease_seconds: int) -> RunRecord:
        raise NotImplementedError

    @abstractmethod
    def heartbeat(self, run_id: str, owner: str, lease_seconds: int) -> RunRecord:
        raise NotImplementedError

    @abstractmethod
    def complete(self, run_id: str, owner: str, result: dict[str, Any]) -> RunRecord:
        raise NotImplementedError

    @abstractmethod
    def fail(self, run_id: str, owner: str, error: str) -> RunRecord:
        raise NotImplementedError

    @abstractmethod
    def request_retry(self, run_id: str) -> RunRecord:
        raise NotImplementedError


class InMemoryRunStore(RunStore):
    """用于本地开发和单元测试的线程安全内存实现。"""

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._records: dict[str, RunRecord] = {}
        self._lock = RLock()
        self._clock = clock or (lambda: datetime.now(UTC))

    def create(
        self,
        run_id: str,
        kind: str,
        input_data: dict[str, Any],
        max_attempts: int,
        tenant_id: str = "local",
    ) -> RunRecord:
        with self._lock:
            # 相同输入重复创建时直接返回原记录，实现请求幂等。
            existing = self._records.get(run_id)
            if existing is not None:
                if existing.kind != kind or existing.input != input_data or existing.tenant_id != tenant_id:
                    raise ValueError(f"run_id {run_id!r} already exists with different input")
                return deepcopy(existing)
            record = RunRecord(
                run_id=run_id,
                kind=kind,
                input=deepcopy(input_data),
                max_attempts=max_attempts,
                tenant_id=tenant_id,
            )
            self._records[run_id] = record
            return deepcopy(record)

    def get(self, run_id: str) -> RunRecord | None:
        with self._lock:
            record = self._records.get(run_id)
            return deepcopy(record) if record else None

    def claim(self, run_id: str, owner: str, lease_seconds: int) -> RunRecord:
        with self._lock:
            record = self._required(run_id)
            now = self._clock()
            lease_expired = record.lease_expires_at is not None and record.lease_expires_at <= now
            # 只允许领取待处理任务，或接管已超过租约时间的运行中任务。
            if record.status != RunStatus.PENDING and not (record.status == RunStatus.RUNNING and lease_expired):
                raise RuntimeError(f"run {run_id!r} cannot be claimed from status {record.status.value!r}")
            if record.attempt >= record.max_attempts:
                raise RuntimeError(f"run {run_id!r} exhausted {record.max_attempts} attempts")
            record.status = RunStatus.RUNNING
            record.attempt += 1
            record.lease_owner = owner
            record.lease_expires_at = now + timedelta(seconds=lease_seconds)
            record.error = None
            record.updated_at = now
            return deepcopy(record)

    def heartbeat(self, run_id: str, owner: str, lease_seconds: int) -> RunRecord:
        with self._lock:
            record = self._owned_running(run_id, owner)
            now = self._clock()
            record.lease_expires_at = now + timedelta(seconds=lease_seconds)
            record.updated_at = now
            return deepcopy(record)

    def complete(self, run_id: str, owner: str, result: dict[str, Any]) -> RunRecord:
        with self._lock:
            record = self._owned_running(run_id, owner)
            record.status = RunStatus.SUCCEEDED
            record.result = deepcopy(result)
            record.lease_owner = None
            record.lease_expires_at = None
            record.updated_at = self._clock()
            return deepcopy(record)

    def fail(self, run_id: str, owner: str, error: str) -> RunRecord:
        with self._lock:
            record = self._owned_running(run_id, owner)
            record.status = RunStatus.FAILED
            record.error = error[:2000]
            record.lease_owner = None
            record.lease_expires_at = None
            record.updated_at = self._clock()
            return deepcopy(record)

    def request_retry(self, run_id: str) -> RunRecord:
        with self._lock:
            record = self._required(run_id)
            now = self._clock()
            lease_expired = record.lease_expires_at is not None and record.lease_expires_at <= now
            retryable_status = record.status == RunStatus.FAILED or (
                record.status == RunStatus.RUNNING and lease_expired
            )
            if not retryable_status:
                raise RuntimeError(f"run {run_id!r} cannot retry from status {record.status.value!r}")
            if record.attempt >= record.max_attempts:
                raise RuntimeError(f"run {run_id!r} exhausted {record.max_attempts} attempts")
            record.status = RunStatus.PENDING
            record.error = None
            record.lease_owner = None
            record.lease_expires_at = None
            record.updated_at = now
            return deepcopy(record)

    def _required(self, run_id: str) -> RunRecord:
        record = self._records.get(run_id)
        if record is None:
            raise KeyError(run_id)
        return record

    def _owned_running(self, run_id: str, owner: str) -> RunRecord:
        record = self._required(run_id)
        if record.status != RunStatus.RUNNING or record.lease_owner != owner:
            raise RuntimeError(f"run {run_id!r} is not leased by {owner!r}")
        return record


class MongoRunStore(RunStore):
    """生产环境运行状态存储，使用 MongoDB 原子更新避免多个 Worker 重复执行。"""

    def __init__(self, mongo_url: str, database: str, collection: str) -> None:
        from pymongo import ASCENDING, MongoClient

        self._client = MongoClient(mongo_url, appname="equipment-rag-runs", tz_aware=True)
        self._collection = self._client[database][collection]
        self._collection.create_index([("run_id", ASCENDING)], unique=True)
        self._collection.create_index(
            [("tenant_id", ASCENDING), ("status", ASCENDING), ("lease_expires_at", ASCENDING)]
        )

    def create(
        self,
        run_id: str,
        kind: str,
        input_data: dict[str, Any],
        max_attempts: int,
        tenant_id: str = "local",
    ) -> RunRecord:
        from pymongo.errors import DuplicateKeyError

        record = RunRecord(
            run_id=run_id,
            kind=kind,
            input=deepcopy(input_data),
            max_attempts=max_attempts,
            tenant_id=tenant_id,
        )
        try:
            self._collection.insert_one(record.to_document())
            return record
        except DuplicateKeyError:
            existing = self.get(run_id)
            if existing is None:
                raise
            if existing.kind != kind or existing.input != input_data or existing.tenant_id != tenant_id:
                raise ValueError(f"run_id {run_id!r} already exists with different input") from None
            return existing

    def get(self, run_id: str) -> RunRecord | None:
        document = self._collection.find_one({"run_id": run_id}, {"_id": 0})
        return RunRecord.from_document(document) if document else None

    def claim(self, run_id: str, owner: str, lease_seconds: int) -> RunRecord:
        from pymongo import ReturnDocument

        now = datetime.now(UTC)
        # 条件和状态更新在数据库中一次完成，只有一个 Worker 能成功领取同一任务。
        document = self._collection.find_one_and_update(
            {
                "run_id": run_id,
                "$expr": {"$lt": ["$attempt", "$max_attempts"]},
                "$or": [
                    {"status": RunStatus.PENDING.value},
                    {
                        "status": RunStatus.RUNNING.value,
                        "lease_expires_at": {"$lte": now},
                    },
                ],
            },
            {
                "$set": {
                    "status": RunStatus.RUNNING.value,
                    "lease_owner": owner,
                    "lease_expires_at": now + timedelta(seconds=lease_seconds),
                    "updated_at": now,
                    "error": None,
                },
                "$inc": {"attempt": 1},
            },
            projection={"_id": 0},
            return_document=ReturnDocument.AFTER,
        )
        if document is None:
            raise RuntimeError(f"run {run_id!r} is not claimable")
        return RunRecord.from_document(document)

    def heartbeat(self, run_id: str, owner: str, lease_seconds: int) -> RunRecord:
        now = datetime.now(UTC)
        return self._owned_update(
            run_id,
            owner,
            {
                "$set": {
                    "lease_expires_at": now + timedelta(seconds=lease_seconds),
                    "updated_at": now,
                }
            },
        )

    def complete(self, run_id: str, owner: str, result: dict[str, Any]) -> RunRecord:
        return self._owned_update(
            run_id,
            owner,
            {
                "$set": {
                    "status": RunStatus.SUCCEEDED.value,
                    "result": deepcopy(result),
                    "lease_owner": None,
                    "lease_expires_at": None,
                    "updated_at": datetime.now(UTC),
                }
            },
        )

    def fail(self, run_id: str, owner: str, error: str) -> RunRecord:
        return self._owned_update(
            run_id,
            owner,
            {
                "$set": {
                    "status": RunStatus.FAILED.value,
                    "error": error[:2000],
                    "lease_owner": None,
                    "lease_expires_at": None,
                    "updated_at": datetime.now(UTC),
                }
            },
        )

    def request_retry(self, run_id: str) -> RunRecord:
        from pymongo import ReturnDocument

        document = self._collection.find_one_and_update(
            {
                "run_id": run_id,
                "$expr": {"$lt": ["$attempt", "$max_attempts"]},
                "$or": [
                    {"status": RunStatus.FAILED.value},
                    {
                        "status": RunStatus.RUNNING.value,
                        "lease_expires_at": {"$lte": datetime.now(UTC)},
                    },
                ],
            },
            {
                "$set": {
                    "status": RunStatus.PENDING.value,
                    "error": None,
                    "lease_owner": None,
                    "lease_expires_at": None,
                    "updated_at": datetime.now(UTC),
                }
            },
            projection={"_id": 0},
            return_document=ReturnDocument.AFTER,
        )
        if document is None:
            raise RuntimeError(f"run {run_id!r} is not retryable")
        return RunRecord.from_document(document)

    def _owned_update(self, run_id: str, owner: str, update: dict[str, Any]) -> RunRecord:
        from pymongo import ReturnDocument

        # owner 必须与当前租约一致，防止租约过期后的旧 Worker 覆盖新结果。
        document = self._collection.find_one_and_update(
            {
                "run_id": run_id,
                "status": RunStatus.RUNNING.value,
                "lease_owner": owner,
            },
            update,
            projection={"_id": 0},
            return_document=ReturnDocument.AFTER,
        )
        if document is None:
            raise RuntimeError(f"run {run_id!r} is not leased by {owner!r}")
        return RunRecord.from_document(document)


_run_store: RunStore | None = None
_run_store_lock = RLock()


def get_run_store() -> RunStore:
    """根据配置创建全局运行状态存储，并在后续请求中复用。"""
    global _run_store
    if _run_store is not None:
        return _run_store
    with _run_store_lock:
        if _run_store is not None:
            return _run_store
        config = load_runtime_config()
        if config.run_store_backend == "memory":
            _run_store = InMemoryRunStore()
        elif config.run_store_backend == "mongodb":
            _run_store = MongoRunStore(config.mongo_url, config.mongo_database, config.run_collection)
        else:
            raise ValueError(
                f"Unsupported RUN_STORE_BACKEND={config.run_store_backend!r}; expected 'memory' or 'mongodb'"
            )
        return _run_store


def run_owner() -> str:
    """生成当前执行线程的唯一租约所有者标识。"""
    return f"{socket.gethostname()}:{os.getpid()}:{threading.get_ident()}"


def reset_run_store_for_tests(store: RunStore | None = None) -> None:
    global _run_store
    with _run_store_lock:
        _run_store = store
