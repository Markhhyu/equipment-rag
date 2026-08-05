from app.modules.knowledge.application.legacy_migration import migrate_legacy_group, scan_legacy_knowledge
from app.modules.knowledge.domain.document import legacy_document_identity


class _Iterator:
    def __init__(self, rows):
        self._batches = [list(rows)] if rows else []

    def next(self):
        return self._batches.pop(0) if self._batches else []

    def close(self):
        return None


class _FakeMilvusClient:
    def __init__(self, rows):
        self.rows = [dict(row) for row in rows]
        self.next_id = 1000
        self.upsert_calls = []
        self.flush_calls = 0

    def query_iterator(self, *, filter, **_kwargs):
        rows = self.rows
        if "revision_id ==" in filter:
            revision_id = filter.split('revision_id == "', 1)[1].split('"', 1)[0]
            rows = [row for row in rows if row.get("revision_id") == revision_id]
        return _Iterator(rows)

    def upsert(self, *, data, partial_update, **_kwargs):
        assert partial_update is True
        generated_ids = []
        for update in data:
            old_id = update["chunk_id"]
            old_row = next(row for row in self.rows if row["chunk_id"] == old_id)
            self.rows.remove(old_row)
            migrated = {**old_row, **update, "chunk_id": self.next_id}
            self.rows.append(migrated)
            generated_ids.append(self.next_id)
            self.next_id += 1
        self.upsert_calls.append(list(data))
        return {"upsert_count": len(data), "ids": generated_ids}

    def flush(self, **_kwargs):
        self.flush_calls += 1


def test_legacy_migration_adds_required_revision_metadata_and_is_idempotent():
    title = "LJ2268系列用户手册"
    document_id, revision_id = legacy_document_identity("local", title)
    client = _FakeMilvusClient(
        [
            {
                "chunk_id": 1,
                "tenant_id": "local",
                "file_title": title,
                "item_name": "LJ2268/LJ2268W激光打印机",
                "content": "卡纸处理步骤",
            },
            {
                "chunk_id": 2,
                "tenant_id": "local",
                "file_title": title,
                "item_name": "LJ2268/LJ2268W激光打印机",
                "document_id": document_id,
                "revision_id": revision_id,
                "version_label": "legacy-v1",
                "governance_managed": True,
            },
            {
                "chunk_id": 3,
                "tenant_id": "local",
                "file_title": title,
                "document_id": "new-document",
                "revision_id": "new-revision",
                "version_label": "v2",
                "governance_managed": True,
            },
        ]
    )

    groups = scan_legacy_knowledge(client, "equipment_chunks", "local")
    group = groups[title]
    assert group["chunk_count"] == 2
    assert group["pending_chunk_ids"] == [1]

    result = migrate_legacy_group(client, "equipment_chunks", "local", title, group)

    assert result == {"chunk_count": 2, "migrated_count": 1, "rekeyed_count": 1}
    assert client.flush_calls == 1
    migrated = next(row for row in client.rows if row.get("revision_id") == revision_id and row["chunk_id"] != 2)
    assert migrated["content"] == "卡纸处理步骤"
    assert migrated["document_id"] == document_id
    assert migrated["version_label"] == "legacy-v1"
    assert migrated["governance_managed"] is True

    rescanned = scan_legacy_knowledge(client, "equipment_chunks", "local")[title]
    assert rescanned["chunk_count"] == 2
    assert rescanned["pending_chunk_ids"] == []
    assert migrate_legacy_group(client, "equipment_chunks", "local", title, rescanned) == {
        "chunk_count": 2,
        "migrated_count": 0,
        "rekeyed_count": 0,
    }

