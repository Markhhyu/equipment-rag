"""Migrate pre-governance Milvus chunks into the versioned knowledge model."""

from __future__ import annotations

from typing import Any

from app.modules.knowledge.domain.document import legacy_document_identity
from app.platform.security.tenancy import escape_milvus_literal, tenant_filter


LEGACY_VERSION_LABEL = "legacy-v1"
MIGRATION_BATCH_SIZE = 200


def scan_legacy_knowledge(client: Any, collection_name: str, tenant_id: str) -> dict[str, dict[str, Any]]:
    """Group unmanaged chunks and already-migrated legacy chunks by source file."""
    groups: dict[str, dict[str, Any]] = {}
    iterator = client.query_iterator(
        collection_name=collection_name,
        filter=tenant_filter(tenant_id),
        output_fields=[
            "chunk_id",
            "file_title",
            "parent_title",
            "item_name",
            "document_id",
            "revision_id",
            "version_label",
            "governance_managed",
        ],
        batch_size=500,
    )
    try:
        while batch := iterator.next():
            for chunk in batch:
                file_title = str(chunk.get("file_title") or chunk.get("parent_title") or "").strip()
                if not file_title:
                    continue

                document_id, revision_id = legacy_document_identity(tenant_id, file_title)
                is_managed = bool(chunk.get("governance_managed"))
                current_revision = str(chunk.get("revision_id") or "").strip()
                current_version_label = str(chunk.get("version_label") or "").strip()
                if is_managed and current_revision != revision_id:
                    # A governed non-legacy revision must never be rewritten by this migration.
                    continue
                if is_managed and current_version_label not in {"", LEGACY_VERSION_LABEL}:
                    continue

                chunk_id = chunk.get("chunk_id")
                if chunk_id in (None, ""):
                    raise RuntimeError(f"旧知识切片缺少chunk_id，无法迁移：{file_title}")

                group = groups.setdefault(
                    file_title,
                    {
                        "document_id": document_id,
                        "revision_id": revision_id,
                        "chunk_ids": [],
                        "pending_chunk_ids": [],
                        "item_names": set(),
                    },
                )
                group["chunk_ids"].append(int(chunk_id))
                if not is_managed:
                    group["pending_chunk_ids"].append(int(chunk_id))
                item_name = str(chunk.get("item_name") or "").strip()
                if item_name:
                    group["item_names"].add(item_name)
    finally:
        iterator.close()

    for group in groups.values():
        group["chunk_ids"] = sorted(set(group["chunk_ids"]))
        group["pending_chunk_ids"] = sorted(set(group["pending_chunk_ids"]))
        group["item_names"] = sorted(group["item_names"])
        group["chunk_count"] = len(group["chunk_ids"])
    return groups


def _revision_filter(tenant_id: str, revision_id: str) -> str:
    safe_revision_id = escape_milvus_literal(revision_id)
    return tenant_filter(tenant_id, f'revision_id == "{safe_revision_id}"')


def _verify_legacy_revision(
    client: Any,
    collection_name: str,
    tenant_id: str,
    file_title: str,
    document_id: str,
    revision_id: str,
    expected_count: int,
) -> None:
    """Require every migrated chunk to carry the complete governance identity."""
    verified_ids: set[int] = set()
    iterator = client.query_iterator(
        collection_name=collection_name,
        filter=_revision_filter(tenant_id, revision_id),
        output_fields=[
            "chunk_id",
            "file_title",
            "document_id",
            "revision_id",
            "version_label",
            "governance_managed",
        ],
        batch_size=500,
        consistency_level="Strong",
    )
    try:
        while batch := iterator.next():
            for chunk in batch:
                if str(chunk.get("file_title") or "").strip() != file_title:
                    raise RuntimeError(f"版本{revision_id}关联了其他源文件，已停止发布")
                if str(chunk.get("document_id") or "").strip() != document_id:
                    raise RuntimeError(f"版本{revision_id}的document_id校验失败")
                if str(chunk.get("version_label") or "").strip() != LEGACY_VERSION_LABEL:
                    raise RuntimeError(f"版本{revision_id}的version_label校验失败")
                if not chunk.get("governance_managed"):
                    raise RuntimeError(f"版本{revision_id}仍包含未治理切片")
                verified_ids.add(int(chunk["chunk_id"]))
    finally:
        iterator.close()

    if len(verified_ids) != expected_count:
        raise RuntimeError(
            f"版本{revision_id}迁移数量校验失败：预期{expected_count}条，实际{len(verified_ids)}条"
        )


def migrate_legacy_group(
    client: Any,
    collection_name: str,
    tenant_id: str,
    file_title: str,
    group: dict[str, Any],
) -> dict[str, int]:
    """Add mandatory version metadata while preserving each chunk's text and vectors."""
    document_id = str(group["document_id"])
    revision_id = str(group["revision_id"])
    pending_ids = [int(value) for value in group.get("pending_chunk_ids") or []]
    rekeyed_count = 0

    for offset in range(0, len(pending_ids), MIGRATION_BATCH_SIZE):
        batch_ids = pending_ids[offset : offset + MIGRATION_BATCH_SIZE]
        payload = [
            {
                "chunk_id": chunk_id,
                "document_id": document_id,
                "revision_id": revision_id,
                "version_label": LEGACY_VERSION_LABEL,
                "trust_level": "manufacturer_manual",
                "governance_managed": True,
            }
            for chunk_id in batch_ids
        ]
        result = client.upsert(
            collection_name=collection_name,
            data=payload,
            partial_update=True,
        )
        generated_ids = list((result or {}).get("ids") or [])
        if len(generated_ids) != len(batch_ids):
            raise RuntimeError(
                f"版本{revision_id}批量迁移失败：提交{len(batch_ids)}条，返回{len(generated_ids)}个新主键"
            )
        rekeyed_count += len(generated_ids)

    if pending_ids:
        client.flush(collection_name=collection_name)

    expected_count = int(group.get("chunk_count") or 0)
    _verify_legacy_revision(
        client,
        collection_name,
        tenant_id,
        file_title,
        document_id,
        revision_id,
        expected_count,
    )
    return {"chunk_count": expected_count, "migrated_count": len(pending_ids), "rekeyed_count": rekeyed_count}
