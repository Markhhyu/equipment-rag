"""Document governance repository implementations."""

from __future__ import annotations

import os
import re
import threading
import uuid
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, Iterable

from pymongo import ASCENDING, DESCENDING, MongoClient

from app.modules.knowledge.application.ports import DocumentRegistry
from app.modules.knowledge.domain.document import (
    APPLICABILITY_FIELDS,
    DocumentStatus,
    VersionStatus,
    _clean_text,
    _identifier,
    build_applicability_profile,
    legacy_document_identity,
)
from app.modules.knowledge.domain.trust import normalize_trust_level

def _utcnow() -> datetime:
    return datetime.now(UTC)

def _active_revision_ids(document: dict[str, Any]) -> list[str]:
    """兼容旧文档的active_revision_id，同时支持多个适用范围并行生效。"""
    scoped = document.get("active_revisions")
    if isinstance(scoped, dict):
        result = list(dict.fromkeys(str(value) for value in scoped.values() if str(value).strip()))
        if result:
            return result
    values = document.get("active_revision_ids")
    if isinstance(values, list):
        result = [str(value) for value in values if str(value).strip()]
        if result:
            return result
    legacy = str(document.get("active_revision_id") or "").strip()
    return [legacy] if legacy else []


def _active_revision_for_scope(document: dict[str, Any], applicability_key: str) -> str:
    scoped = document.get("active_revisions")
    if isinstance(scoped, dict) and scoped:
        return str(scoped.get(applicability_key) or "").strip()
    if applicability_key == "default":
        return str(document.get("active_revision_id") or "").strip()
    return ""


def _revision_is_active(document: dict[str, Any], version: dict[str, Any]) -> bool:
    revision_id = str(version.get("revision_id") or "").strip()
    if not revision_id or document.get("status") != DocumentStatus.ACTIVE.value:
        return False
    applicability_key = str(version.get("applicability_key") or "default")
    scoped = document.get("active_revisions")
    if isinstance(scoped, dict) and scoped:
        return _active_revision_for_scope(document, applicability_key) == revision_id
    return revision_id in _active_revision_ids(document) and version.get("status") == VersionStatus.ACTIVE.value


def _derive_active_revisions(document: dict[str, Any], versions: Iterable[dict[str, Any]]) -> dict[str, str]:
    scoped = document.get("active_revisions")
    if isinstance(scoped, dict) and scoped:
        return {
            str(scope): str(revision_id)
            for scope, revision_id in scoped.items()
            if str(scope).strip() and str(revision_id).strip()
        }
    active_ids = set(_active_revision_ids(document))
    result: dict[str, str] = {}
    for version in versions:
        revision_id = str(version.get("revision_id") or "").strip()
        if revision_id in active_ids and version.get("status") == VersionStatus.ACTIVE.value:
            result[str(version.get("applicability_key") or "default")] = revision_id
    if not result:
        legacy = str(document.get("active_revision_id") or "").strip()
        if legacy:
            result["default"] = legacy
    return result


def _document_view(document: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(document)
    active_ids = _active_revision_ids(result)
    result["active_revision_ids"] = active_ids
    if active_ids and str(result.get("active_revision_id") or "") not in active_ids:
        result["active_revision_id"] = active_ids[-1]
    return result


def _public(value: Any) -> Any:
    """把MongoDB文档转换成前端可直接消费的JSON结构。"""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _public(item) for key, item in value.items() if key != "_id"}
    if isinstance(value, list):
        return [_public(item) for item in value]
    return value


def _audit_document(
    tenant_id: str,
    document_id: str,
    action: str,
    actor: str,
    revision_id: str = "",
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "audit_id": uuid.uuid4().hex,
        "tenant_id": tenant_id,
        "document_id": document_id,
        "revision_id": revision_id,
        "action": action,
        "actor": actor,
        "detail": deepcopy(detail or {}),
        "created_at": _utcnow(),
    }


class InMemoryDocumentRegistry(DocumentRegistry):
    """线程安全的内存实现，用于单元测试和本地无Mongo场景。"""

    def __init__(self) -> None:
        self.documents: dict[tuple[str, str], dict[str, Any]] = {}
        self.versions: dict[tuple[str, str], dict[str, Any]] = {}
        self.audit_logs: list[dict[str, Any]] = []
        self._lock = threading.RLock()

    def _audit(self, *args, **kwargs) -> None:
        self.audit_logs.append(_audit_document(*args, **kwargs))

    def register_import(self, **kwargs) -> dict[str, Any]:
        with self._lock:
            tenant_id = _identifier(kwargs["tenant_id"])
            revision_id = _identifier(kwargs["revision_id"])
            document_id = _identifier(kwargs.get("document_id"), fallback=revision_id)
            key = (tenant_id, document_id)
            version_key = (tenant_id, revision_id)
            if version_key in self.versions:
                return _public(deepcopy(self.versions[version_key]))

            now = _utcnow()
            document = self.documents.get(key)
            if document is None:
                document = {
                    "tenant_id": tenant_id,
                    "document_id": document_id,
                    "title": _clean_text(kwargs.get("title") or kwargs.get("filename"), 255),
                    "status": DocumentStatus.DRAFT.value,
                    "active_revision_id": "",
                    "active_revision_ids": [],
                    "active_revisions": {},
                    "version_count": 0,
                    "item_names": [],
                    "created_at": now,
                    "updated_at": now,
                }
                self.documents[key] = document

            applicability = build_applicability_profile(kwargs)
            version = {
                "tenant_id": tenant_id,
                "document_id": document_id,
                "revision_id": revision_id,
                "version_label": _clean_text(kwargs.get("version_label") or "legacy-v1", 64),
                "trust_level": normalize_trust_level(kwargs.get("trust_level")),
                "filename": _clean_text(kwargs.get("filename"), 255),
                "status": VersionStatus.IMPORTING.value,
                "import_status": "processing",
                "publish_requested": bool(kwargs.get("publish_requested")),
                "source_object_uri": _clean_text(kwargs.get("source_object_uri"), 2000),
                "content_hash": _clean_text(kwargs.get("content_hash"), 128),
                "file_size": max(0, int(kwargs.get("file_size") or 0)),
                "chunk_count": 0,
                "image_count": 0,
                "item_names": [],
                **applicability,
                "error": "",
                "created_at": now,
                "updated_at": now,
            }
            self.versions[version_key] = version
            document["version_count"] += 1
            document["updated_at"] = now
            self._audit(tenant_id, document_id, "register_import", kwargs.get("actor", "system"), revision_id)
            return _public(deepcopy(version))

    def mark_import_succeeded(self, tenant_id: str, revision_id: str, **kwargs) -> dict[str, Any]:
        with self._lock:
            version = self._required_version(tenant_id, revision_id)
            names = list(
                dict.fromkeys(str(value).strip() for value in kwargs.get("item_names", []) if str(value).strip())
            )
            version.update(
                {
                    "status": VersionStatus.DRAFT.value,
                    "import_status": "completed",
                    "chunk_count": max(0, int(kwargs.get("chunk_count") or 0)),
                    "image_count": max(0, int(kwargs.get("image_count") or 0)),
                    "item_names": names,
                    "error": "",
                    "updated_at": _utcnow(),
                }
            )
            document = self.documents[(tenant_id, version["document_id"])]
            document["item_names"] = list(dict.fromkeys((document.get("item_names") or []) + names))
            document["updated_at"] = _utcnow()
            self._audit(
                tenant_id, version["document_id"], "import_completed", kwargs.get("actor", "system"), revision_id
            )
            if version.get("publish_requested"):
                return self.publish_version(
                    tenant_id,
                    version["document_id"],
                    revision_id,
                    actor=kwargs.get("actor", "system"),
                )
            return _public(deepcopy(version))

    def mark_import_failed(self, tenant_id: str, revision_id: str, error: str, actor: str = "system") -> None:
        with self._lock:
            version = self._required_version(tenant_id, revision_id)
            version.update(
                {
                    "status": VersionStatus.FAILED.value,
                    "import_status": "failed",
                    "error": _clean_text(error, 2000),
                    "updated_at": _utcnow(),
                }
            )
            self._audit(
                tenant_id, version["document_id"], "import_failed", actor, revision_id, {"error": version["error"]}
            )

    def publish_version(
        self, tenant_id: str, document_id: str, revision_id: str, *, actor: str, action: str = "publish"
    ) -> dict[str, Any]:
        with self._lock:
            document = self._required_document(tenant_id, document_id)
            version = self._required_version(tenant_id, revision_id)
            if version["document_id"] != document_id:
                raise ValueError("版本不属于指定文档")
            if version.get("import_status") != "completed":
                raise ValueError("只有导入完成的版本才能发布")
            now = _utcnow()
            applicability_key = str(version.get("applicability_key") or "default")
            if not document.get("active_revisions"):
                document["active_revisions"] = _derive_active_revisions(document, self.versions.values())
            if (
                document.get("status") == DocumentStatus.ACTIVE.value
                and _active_revision_for_scope(document, applicability_key) == revision_id
                and version.get("status") == VersionStatus.ACTIVE.value
            ):
                return _public(deepcopy(document))
            for candidate in self.versions.values():
                if candidate["tenant_id"] == tenant_id and candidate["document_id"] == document_id:
                    candidate_scope = str(candidate.get("applicability_key") or "default")
                    if (
                        candidate["revision_id"] != revision_id
                        and candidate["status"] == VersionStatus.ACTIVE.value
                        and candidate_scope == applicability_key
                    ):
                        candidate["status"] = VersionStatus.ARCHIVED.value
                        candidate["updated_at"] = now
            version["status"] = VersionStatus.ACTIVE.value
            version["published_at"] = now
            version["updated_at"] = now
            active_revisions = document.setdefault("active_revisions", {})
            active_revisions[applicability_key] = revision_id
            active_ids = list(dict.fromkeys(str(value) for value in active_revisions.values() if str(value).strip()))
            active_item_names = list(
                dict.fromkeys(
                    name
                    for candidate in self.versions.values()
                    if candidate["tenant_id"] == tenant_id
                    and candidate["document_id"] == document_id
                    and candidate["revision_id"] in active_ids
                    for name in candidate.get("item_names") or []
                )
            )
            document.update(
                {
                    "status": DocumentStatus.ACTIVE.value,
                    "active_revision_id": revision_id,
                    "active_revision_ids": active_ids,
                    "item_names": deepcopy(active_item_names),
                    "updated_at": now,
                }
            )
            self._audit(tenant_id, document_id, action, actor, revision_id)
            return _public(deepcopy(document))

    def disable_document(self, tenant_id: str, document_id: str, *, actor: str) -> dict[str, Any]:
        with self._lock:
            document = self._required_document(tenant_id, document_id)
            document["status"] = DocumentStatus.DISABLED.value
            document["updated_at"] = _utcnow()
            self._audit(tenant_id, document_id, "disable", actor, document.get("active_revision_id", ""))
            return _public(deepcopy(document))

    def enable_document(self, tenant_id: str, document_id: str, *, actor: str) -> dict[str, Any]:
        with self._lock:
            document = self._required_document(tenant_id, document_id)
            revision_ids = _active_revision_ids(document)
            if not revision_ids:
                raise ValueError("文档没有已发布版本，不能启用")
            if not any(
                self._required_version(tenant_id, revision_id)["status"] == VersionStatus.ACTIVE.value
                for revision_id in revision_ids
            ):
                raise ValueError("当前版本不是生效版本，请先发布一个版本")
            document["status"] = DocumentStatus.ACTIVE.value
            document["updated_at"] = _utcnow()
            self._audit(tenant_id, document_id, "enable", actor, revision_ids[-1])
            return _public(deepcopy(document))

    def list_documents(
        self, tenant_id: str, *, status: str = "", query: str = "", skip: int = 0, limit: int = 50
    ) -> dict[str, Any]:
        with self._lock:
            needle = query.strip().lower()
            items = [
                deepcopy(item)
                for (tenant, _), item in self.documents.items()
                if tenant == tenant_id
                and (not status or item.get("status") == status)
                and (
                    not needle
                    or needle in str(item.get("title") or "").lower()
                    or needle in " ".join(item.get("item_names") or []).lower()
                )
            ]
            items.sort(key=lambda value: value.get("updated_at") or datetime.min.replace(tzinfo=UTC), reverse=True)
            total = len(items)
            return {"items": _public(items[max(0, skip) : max(0, skip) + max(1, min(limit, 200))]), "total": total}

    def get_document(self, tenant_id: str, document_id: str) -> dict[str, Any] | None:
        with self._lock:
            document = self.documents.get((tenant_id, document_id))
            if document is None:
                return None
            result = deepcopy(document)
            result["versions"] = [
                deepcopy(item)
                for (tenant, _), item in self.versions.items()
                if tenant == tenant_id and item["document_id"] == document_id
            ]
            result["versions"].sort(key=lambda value: value.get("created_at"), reverse=True)
            return _public(result)

    def list_active_versions(self, tenant_id: str, item_names: Iterable[str]) -> list[dict[str, Any]]:
        requested = {str(value).strip().casefold() for value in item_names if str(value).strip()}
        if not requested:
            return []
        with self._lock:
            result = []
            for (tenant, _), version in self.versions.items():
                if tenant != tenant_id:
                    continue
                document = self.documents.get((tenant_id, str(version.get("document_id") or "")))
                version_names = {str(value).strip().casefold() for value in version.get("item_names") or []}
                if document and requested.intersection(version_names) and _revision_is_active(document, version):
                    result.append(deepcopy(version))
            result.sort(key=lambda value: (str(value.get("document_id") or ""), str(value.get("revision_id") or "")))
            return _public(result)

    def list_audit_logs(self, tenant_id: str, document_id: str = "", limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            items = [
                deepcopy(item)
                for item in self.audit_logs
                if item["tenant_id"] == tenant_id and (not document_id or item["document_id"] == document_id)
            ]
            items.sort(key=lambda value: value["created_at"], reverse=True)
            return _public(items[: max(1, min(limit, 500))])

    def managed_revision_access(
        self, tenant_id: str, revisions: Iterable[tuple[str, str]]
    ) -> dict[tuple[str, str], bool]:
        with self._lock:
            result: dict[tuple[str, str], bool] = {}
            for document_id, revision_id in set(revisions):
                document = self.documents.get((tenant_id, document_id))
                version = self.versions.get((tenant_id, revision_id))
                result[(document_id, revision_id)] = bool(
                    document
                    and version
                    and version.get("document_id") == document_id
                    and _revision_is_active(document, version)
                )
            return result

    def _required_document(self, tenant_id: str, document_id: str) -> dict[str, Any]:
        document = self.documents.get((_identifier(tenant_id), _identifier(document_id)))
        if document is None:
            raise KeyError(document_id)
        return document

    def _required_version(self, tenant_id: str, revision_id: str) -> dict[str, Any]:
        version = self.versions.get((_identifier(tenant_id), _identifier(revision_id)))
        if version is None:
            raise KeyError(revision_id)
        return version


class MongoDocumentRegistry(DocumentRegistry):
    """MongoDB生产实现。停用和版本切换只修改治理元数据，不物理删除向量和原文件。"""

    def __init__(self, mongo_url: str, database: str) -> None:
        self.client = MongoClient(mongo_url, appname="equipment-rag-knowledge-governance", tz_aware=True)
        db = self.client[database]
        self.documents = db["knowledge_documents"]
        self.versions = db["knowledge_document_versions"]
        self.audit = db["knowledge_audit_logs"]
        self.documents.create_index([("tenant_id", ASCENDING), ("document_id", ASCENDING)], unique=True)
        self.documents.create_index([("tenant_id", ASCENDING), ("status", ASCENDING), ("updated_at", DESCENDING)])
        self.versions.create_index([("tenant_id", ASCENDING), ("revision_id", ASCENDING)], unique=True)
        self.versions.create_index([("tenant_id", ASCENDING), ("document_id", ASCENDING), ("created_at", DESCENDING)])
        self.audit.create_index([("tenant_id", ASCENDING), ("document_id", ASCENDING), ("created_at", DESCENDING)])

    def _write_audit(self, *args, **kwargs) -> None:
        self.audit.insert_one(_audit_document(*args, **kwargs))

    def register_import(self, **kwargs) -> dict[str, Any]:
        tenant_id = _identifier(kwargs["tenant_id"])
        revision_id = _identifier(kwargs["revision_id"])
        document_id = _identifier(kwargs.get("document_id"), fallback=revision_id)
        now = _utcnow()
        title = _clean_text(kwargs.get("title") or kwargs.get("filename"), 255)
        self.documents.update_one(
            {"tenant_id": tenant_id, "document_id": document_id},
            {
                "$setOnInsert": {
                    "title": title,
                    "status": DocumentStatus.DRAFT.value,
                    "active_revision_id": "",
                    "active_revision_ids": [],
                    "active_revisions": {},
                    "version_count": 0,
                    "item_names": [],
                    "created_at": now,
                },
                "$set": {"updated_at": now},
            },
            upsert=True,
        )
        existing = self.versions.find_one({"tenant_id": tenant_id, "revision_id": revision_id}, {"_id": 0})
        if existing:
            return _public(existing)

        applicability = build_applicability_profile(kwargs)
        version = {
            "tenant_id": tenant_id,
            "document_id": document_id,
            "revision_id": revision_id,
            "version_label": _clean_text(kwargs.get("version_label") or "legacy-v1", 64),
            "trust_level": normalize_trust_level(kwargs.get("trust_level")),
            "filename": _clean_text(kwargs.get("filename"), 255),
            "status": VersionStatus.IMPORTING.value,
            "import_status": "processing",
            "publish_requested": bool(kwargs.get("publish_requested")),
            "source_object_uri": _clean_text(kwargs.get("source_object_uri"), 2000),
            "content_hash": _clean_text(kwargs.get("content_hash"), 128),
            "file_size": max(0, int(kwargs.get("file_size") or 0)),
            "chunk_count": 0,
            "image_count": 0,
            "item_names": [],
            **applicability,
            "error": "",
            "created_at": now,
            "updated_at": now,
        }
        self.versions.insert_one(version)
        self.documents.update_one(
            {"tenant_id": tenant_id, "document_id": document_id},
            {"$inc": {"version_count": 1}, "$set": {"updated_at": now}},
        )
        self._write_audit(tenant_id, document_id, "register_import", kwargs.get("actor", "system"), revision_id)
        return _public(version)

    def mark_import_succeeded(self, tenant_id: str, revision_id: str, **kwargs) -> dict[str, Any]:
        version = self._required_version(tenant_id, revision_id)
        names = list(dict.fromkeys(str(value).strip() for value in kwargs.get("item_names", []) if str(value).strip()))
        now = _utcnow()
        self.versions.update_one(
            {"tenant_id": tenant_id, "revision_id": revision_id},
            {
                "$set": {
                    "status": VersionStatus.DRAFT.value,
                    "import_status": "completed",
                    "chunk_count": max(0, int(kwargs.get("chunk_count") or 0)),
                    "image_count": max(0, int(kwargs.get("image_count") or 0)),
                    "item_names": names,
                    "error": "",
                    "updated_at": now,
                }
            },
        )
        self.documents.update_one(
            {"tenant_id": tenant_id, "document_id": version["document_id"]},
            {"$addToSet": {"item_names": {"$each": names}}, "$set": {"updated_at": now}},
        )
        self._write_audit(
            tenant_id, version["document_id"], "import_completed", kwargs.get("actor", "system"), revision_id
        )
        if version.get("publish_requested"):
            document = self.publish_version(
                tenant_id,
                version["document_id"],
                revision_id,
                actor=kwargs.get("actor", "system"),
            )
            return document
        return _public(self._required_version(tenant_id, revision_id))

    def mark_import_failed(self, tenant_id: str, revision_id: str, error: str, actor: str = "system") -> None:
        version = self._required_version(tenant_id, revision_id)
        safe_error = _clean_text(error, 2000)
        self.versions.update_one(
            {"tenant_id": tenant_id, "revision_id": revision_id},
            {
                "$set": {
                    "status": VersionStatus.FAILED.value,
                    "import_status": "failed",
                    "error": safe_error,
                    "updated_at": _utcnow(),
                }
            },
        )
        self._write_audit(tenant_id, version["document_id"], "import_failed", actor, revision_id, {"error": safe_error})

    def publish_version(
        self, tenant_id: str, document_id: str, revision_id: str, *, actor: str, action: str = "publish"
    ) -> dict[str, Any]:
        tenant_id = _identifier(tenant_id)
        document_id = _identifier(document_id)
        revision_id = _identifier(revision_id)
        document = self._required_document(tenant_id, document_id)
        version = self._required_version(tenant_id, revision_id)
        if version["document_id"] != document_id:
            raise ValueError("版本不属于指定文档")
        if version.get("import_status") != "completed":
            raise ValueError("只有导入完成的版本才能发布")
        now = _utcnow()
        applicability_key = str(version.get("applicability_key") or "default")
        if not document.get("active_revisions"):
            legacy_ids = _active_revision_ids(document)
            legacy_versions = list(
                self.versions.find(
                    {
                        "tenant_id": tenant_id,
                        "document_id": document_id,
                        "revision_id": {"$in": legacy_ids},
                    },
                    {"_id": 0, "revision_id": 1, "status": 1, "applicability_key": 1},
                )
            )
            active_revisions = _derive_active_revisions(document, legacy_versions)
            if active_revisions:
                self.documents.update_one(
                    {
                        "tenant_id": tenant_id,
                        "document_id": document_id,
                        "$or": [
                            {"active_revisions": {"$exists": False}},
                            {"active_revisions": {}},
                        ],
                    },
                    {"$set": {"active_revisions": active_revisions}},
                )
                document = self._required_document(tenant_id, document_id)
        if (
            document.get("status") == DocumentStatus.ACTIVE.value
            and _active_revision_for_scope(document, applicability_key) == revision_id
            and version.get("status") == VersionStatus.ACTIVE.value
        ):
            current = self.get_document(tenant_id, document_id)
            if current is None:
                raise KeyError(document_id)
            return current

        # active_revisions是查询侧的真相源。单字段更新使同一适用范围的并发发布遵循最后写入者生效，
        # 后续version.status仅作为展示投影，不参与查询授权判断。
        self.documents.update_one(
            {"tenant_id": tenant_id, "document_id": document_id},
            {
                "$set": {
                    f"active_revisions.{applicability_key}": revision_id,
                    "active_revision_id": revision_id,
                    "status": DocumentStatus.ACTIVE.value,
                    "updated_at": now,
                }
            },
        )
        document = self._required_document(tenant_id, document_id)
        active_ids = _active_revision_ids(document)
        scope_selector: dict[str, Any]
        if applicability_key == "default":
            scope_selector = {
                "$or": [
                    {"applicability_key": "default"},
                    {"applicability_key": {"$exists": False}},
                    {"applicability_key": ""},
                ]
            }
        else:
            scope_selector = {"applicability_key": applicability_key}
        self.versions.update_many(
            {
                "tenant_id": tenant_id,
                "document_id": document_id,
                "status": VersionStatus.ACTIVE.value,
                "revision_id": {"$ne": revision_id},
                **scope_selector,
            },
            {"$set": {"status": VersionStatus.ARCHIVED.value, "updated_at": now}},
        )
        self.versions.update_one(
            {"tenant_id": tenant_id, "revision_id": revision_id},
            {"$set": {"status": VersionStatus.ACTIVE.value, "published_at": now, "updated_at": now}},
        )
        active_versions = list(
            self.versions.find(
                {
                    "tenant_id": tenant_id,
                    "document_id": document_id,
                    "revision_id": {"$in": active_ids},
                },
                {"_id": 0, "revision_id": 1, "item_names": 1},
            )
        )
        active_item_names = list(
            dict.fromkeys(name for item in active_versions for name in item.get("item_names") or [] if str(name).strip())
        )
        self.documents.update_one(
            {"tenant_id": tenant_id, "document_id": document_id},
            {
                "$set": {
                    "active_revision_ids": active_ids,
                    "item_names": active_item_names,
                    "updated_at": now,
                }
            },
        )
        self._write_audit(tenant_id, document_id, action, actor, revision_id)
        return _public(self._required_document(tenant_id, document_id))

    def disable_document(self, tenant_id: str, document_id: str, *, actor: str) -> dict[str, Any]:
        document = self._required_document(tenant_id, document_id)
        self.documents.update_one(
            {"tenant_id": tenant_id, "document_id": document_id},
            {"$set": {"status": DocumentStatus.DISABLED.value, "updated_at": _utcnow()}},
        )
        self._write_audit(tenant_id, document_id, "disable", actor, document.get("active_revision_id", ""))
        return _public(self._required_document(tenant_id, document_id))

    def enable_document(self, tenant_id: str, document_id: str, *, actor: str) -> dict[str, Any]:
        document = self._required_document(tenant_id, document_id)
        revision_ids = _active_revision_ids(document)
        if not revision_ids:
            raise ValueError("文档没有已发布版本，不能启用")
        active_count = self.versions.count_documents(
            {
                "tenant_id": tenant_id,
                "revision_id": {"$in": revision_ids},
                "status": VersionStatus.ACTIVE.value,
            }
        )
        if active_count <= 0:
            raise ValueError("当前版本不是生效版本，请先发布一个版本")
        self.documents.update_one(
            {"tenant_id": tenant_id, "document_id": document_id},
            {"$set": {"status": DocumentStatus.ACTIVE.value, "updated_at": _utcnow()}},
        )
        self._write_audit(tenant_id, document_id, "enable", actor, revision_ids[-1])
        return _public(self._required_document(tenant_id, document_id))

    def list_documents(
        self, tenant_id: str, *, status: str = "", query: str = "", skip: int = 0, limit: int = 50
    ) -> dict[str, Any]:
        selector: dict[str, Any] = {"tenant_id": tenant_id}
        if status:
            selector["status"] = status
        if query.strip():
            escaped = re.escape(query.strip())
            selector["$or"] = [
                {"title": {"$regex": escaped, "$options": "i"}},
                {"item_names": {"$regex": escaped, "$options": "i"}},
            ]
        safe_limit = max(1, min(int(limit or 50), 200))
        safe_skip = max(0, int(skip or 0))
        total = self.documents.count_documents(selector)
        items = list(
            self.documents.find(selector, {"_id": 0}).sort("updated_at", DESCENDING).skip(safe_skip).limit(safe_limit)
        )
        return {"items": _public([_document_view(item) for item in items]), "total": total}

    def get_document(self, tenant_id: str, document_id: str) -> dict[str, Any] | None:
        document = self.documents.find_one({"tenant_id": tenant_id, "document_id": document_id}, {"_id": 0})
        if document is None:
            return None
        versions = list(
            self.versions.find({"tenant_id": tenant_id, "document_id": document_id}, {"_id": 0}).sort(
                "created_at", DESCENDING
            )
        )
        scoped = document.get("active_revisions")
        if isinstance(scoped, dict):
            for version in versions:
                if version.get("import_status") != "completed":
                    continue
                if _revision_is_active({**document, "status": DocumentStatus.ACTIVE.value}, version):
                    version["status"] = VersionStatus.ACTIVE.value
                elif version.get("status") == VersionStatus.ACTIVE.value:
                    version["status"] = VersionStatus.ARCHIVED.value
        result = _document_view(document)
        result["versions"] = versions
        return _public(result)

    def list_active_versions(self, tenant_id: str, item_names: Iterable[str]) -> list[dict[str, Any]]:
        requested = list(dict.fromkeys(str(value).strip() for value in item_names if str(value).strip()))
        if not requested:
            return []
        documents = list(
            self.documents.find(
                {
                    "tenant_id": tenant_id,
                    "status": DocumentStatus.ACTIVE.value,
                    "item_names": {"$in": requested},
                },
                {"_id": 0},
            )
        )
        if not documents:
            return []
        revision_ids = list(
            {
                revision_id
                for document in documents
                for revision_id in _active_revision_ids(document)
            }
        )
        versions = list(
            self.versions.find(
                {
                    "tenant_id": tenant_id,
                    "revision_id": {"$in": revision_ids},
                    "status": VersionStatus.ACTIVE.value,
                    "import_status": "completed",
                    "item_names": {"$in": requested},
                },
                {"_id": 0},
            )
        )
        documents_by_id = {str(document.get("document_id") or ""): document for document in documents}
        result = [
            version
            for version in versions
            if (document := documents_by_id.get(str(version.get("document_id") or "")))
            and _revision_is_active(document, version)
        ]
        result.sort(key=lambda value: (str(value.get("document_id") or ""), str(value.get("revision_id") or "")))
        return _public(result)

    def list_audit_logs(self, tenant_id: str, document_id: str = "", limit: int = 100) -> list[dict[str, Any]]:
        selector: dict[str, Any] = {"tenant_id": tenant_id}
        if document_id:
            selector["document_id"] = document_id
        items = list(
            self.audit.find(selector, {"_id": 0})
            .sort("created_at", DESCENDING)
            .limit(max(1, min(int(limit or 100), 500)))
        )
        return _public(items)

    def managed_revision_access(
        self, tenant_id: str, revisions: Iterable[tuple[str, str]]
    ) -> dict[tuple[str, str], bool]:
        pairs = set(revisions)
        if not pairs:
            return {}
        document_ids = list({document_id for document_id, _ in pairs})
        revision_ids = list({revision_id for _, revision_id in pairs})
        documents = {
            item["document_id"]: item
            for item in self.documents.find({"tenant_id": tenant_id, "document_id": {"$in": document_ids}}, {"_id": 0})
        }
        versions = {
            item["revision_id"]: item
            for item in self.versions.find({"tenant_id": tenant_id, "revision_id": {"$in": revision_ids}}, {"_id": 0})
        }
        return {
            (document_id, revision_id): bool(
                (document := documents.get(document_id))
                and (version := versions.get(revision_id))
                and version.get("document_id") == document_id
                and version.get("import_status") == "completed"
                and _revision_is_active(document, version)
            )
            for document_id, revision_id in pairs
        }

    def _required_document(self, tenant_id: str, document_id: str) -> dict[str, Any]:
        document = self.documents.find_one(
            {"tenant_id": _identifier(tenant_id), "document_id": _identifier(document_id)},
            {"_id": 0},
        )
        if document is None:
            raise KeyError(document_id)
        return document

    def _required_version(self, tenant_id: str, revision_id: str) -> dict[str, Any]:
        version = self.versions.find_one(
            {"tenant_id": _identifier(tenant_id), "revision_id": _identifier(revision_id)},
            {"_id": 0},
        )
        if version is None:
            raise KeyError(revision_id)
        return version


_document_registry: DocumentRegistry | None = None
_registry_lock = threading.RLock()


def get_document_registry() -> DocumentRegistry:
    global _document_registry
    if _document_registry is not None:
        return _document_registry
    with _registry_lock:
        if _document_registry is None:
            mongo_url = os.getenv("MONGO_URL")
            database = os.getenv("MONGO_DB_NAME")
            if not mongo_url or not database:
                raise RuntimeError("MONGO_URL和MONGO_DB_NAME未配置，无法使用知识库治理功能")
            _document_registry = MongoDocumentRegistry(mongo_url, database)
        return _document_registry


def reset_document_registry_for_tests(registry: DocumentRegistry | None = None) -> None:
    global _document_registry
    with _registry_lock:
        _document_registry = registry


def filter_queryable_hits(tenant_id: str, hits: Iterable[Any]) -> list[Any]:
    """
    删除草稿、归档和停用版本的Milvus命中。

    旧Chunk没有governance_managed标记时继续放行；这就是legacy-v1兼容策略，避免升级后
    现有知识库突然不可用。新Chunk必须同时满足文档启用、revision属于某个当前生效适用范围。
    """
    hit_list = list(hits or [])
    managed_pairs: set[tuple[str, str]] = set()
    entities: list[tuple[Any, dict[str, Any]]] = []
    for hit in hit_list:
        entity = getattr(hit, "entity", None)
        if entity is None and isinstance(hit, dict):
            entity = hit.get("entity", hit)
        entity_dict = entity if isinstance(entity, dict) else dict(entity or {})
        entities.append((hit, entity_dict))
        if entity_dict.get("governance_managed"):
            document_id = str(entity_dict.get("document_id") or "").strip()
            revision_id = str(entity_dict.get("revision_id") or "").strip()
            if document_id and revision_id:
                managed_pairs.add((document_id, revision_id))

    registry = get_document_registry()
    access = registry.managed_revision_access(tenant_id, managed_pairs) if managed_pairs else {}
    legacy_access: dict[str, bool | None] = {}
    for _, entity in entities:
        if entity.get("governance_managed"):
            continue
        file_title = str(entity.get("file_title") or entity.get("parent_title") or "").strip()
        if not file_title:
            continue
        document_id, revision_id = legacy_document_identity(tenant_id, file_title)
        if document_id in legacy_access:
            continue
        document = registry.get_document(tenant_id, document_id)
        legacy_access[document_id] = (
            None
            if document is None
            else bool(
                document.get("status") == DocumentStatus.ACTIVE.value
                and revision_id in _active_revision_ids(document)
            )
        )
    result: list[Any] = []
    for hit, entity in entities:
        if not entity.get("governance_managed"):
            file_title = str(entity.get("file_title") or entity.get("parent_title") or "").strip()
            if not file_title:
                result.append(hit)
                continue
            document_id, _ = legacy_document_identity(tenant_id, file_title)
            # 未登记的旧Chunk继续兼容放行；登记后则由治理文档状态控制。
            if legacy_access.get(document_id) is not False:
                result.append(hit)
            continue
        pair = (str(entity.get("document_id") or "").strip(), str(entity.get("revision_id") or "").strip())
        if access.get(pair, False):
            result.append(hit)
    return result
