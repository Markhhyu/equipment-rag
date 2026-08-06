"""In-memory and MongoDB repositories for external workflow connector settings."""

from __future__ import annotations

import os
import threading
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from pymongo import ASCENDING, MongoClient


def _now() -> datetime:
    return datetime.now(UTC)


class InMemoryConnectorConfigRepository:
    def __init__(self) -> None:
        self.records: dict[tuple[str, str], dict[str, Any]] = {}
        self._lock = threading.RLock()

    def get(self, tenant_id: str, connector_type: str) -> dict[str, Any] | None:
        record = self.records.get((tenant_id, connector_type))
        return deepcopy(record) if record else None

    def save(
        self,
        tenant_id: str,
        connector_type: str,
        config: dict[str, Any],
        encrypted_secret: str,
        actor: str,
    ) -> dict[str, Any]:
        with self._lock:
            record = {
                "tenant_id": tenant_id,
                "connector_type": connector_type,
                "config": deepcopy(config),
                "encrypted_secret": encrypted_secret,
                "updated_by": actor,
                "updated_at": _now(),
            }
            self.records[(tenant_id, connector_type)] = record
            return deepcopy(record)

    def delete(self, tenant_id: str, connector_type: str) -> bool:
        with self._lock:
            return self.records.pop((tenant_id, connector_type), None) is not None


class MongoConnectorConfigRepository(InMemoryConnectorConfigRepository):
    def __init__(self, mongo_url: str, database: str) -> None:
        super().__init__()
        client = MongoClient(mongo_url, appname="equipment-rag-workflow-connectors", tz_aware=True)
        self.collection = client[database]["workflow_connector_configs"]
        self.collection.create_index([("tenant_id", ASCENDING), ("connector_type", ASCENDING)], unique=True)

    def get(self, tenant_id: str, connector_type: str) -> dict[str, Any] | None:
        return self.collection.find_one(
            {"tenant_id": tenant_id, "connector_type": connector_type},
            {"_id": 0},
        )

    def save(
        self,
        tenant_id: str,
        connector_type: str,
        config: dict[str, Any],
        encrypted_secret: str,
        actor: str,
    ) -> dict[str, Any]:
        record = {
            "tenant_id": tenant_id,
            "connector_type": connector_type,
            "config": deepcopy(config),
            "encrypted_secret": encrypted_secret,
            "updated_by": actor,
            "updated_at": _now(),
        }
        self.collection.update_one(
            {"tenant_id": tenant_id, "connector_type": connector_type},
            {"$set": record},
            upsert=True,
        )
        return record

    def delete(self, tenant_id: str, connector_type: str) -> bool:
        return self.collection.delete_one(
            {"tenant_id": tenant_id, "connector_type": connector_type}
        ).deleted_count == 1


_repository: InMemoryConnectorConfigRepository | None = None
_repository_lock = threading.RLock()


def get_connector_config_repository() -> InMemoryConnectorConfigRepository:
    global _repository
    if _repository is not None:
        return _repository
    with _repository_lock:
        if _repository is None:
            mongo_url = str(os.getenv("MONGO_URL") or "").strip()
            database = str(os.getenv("MONGO_DB_NAME") or "").strip()
            _repository = (
                MongoConnectorConfigRepository(mongo_url, database)
                if mongo_url and database
                else InMemoryConnectorConfigRepository()
            )
        return _repository


def reset_connector_config_repository_for_tests(
    repository: InMemoryConnectorConfigRepository | None = None,
) -> None:
    global _repository
    with _repository_lock:
        _repository = repository
