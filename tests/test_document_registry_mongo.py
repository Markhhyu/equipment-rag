from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.clients.document_registry_utils import MongoDocumentRegistry


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_MONGO_INTEGRATION") != "1", reason="MongoDB integration test is opt-in"),
]


def _completed_version(
    registry: MongoDocumentRegistry,
    revision_id: str,
    *,
    software_version: str,
) -> None:
    registry.register_import(
        tenant_id="integration",
        document_id="manual-a",
        revision_id=revision_id,
        filename="manual.pdf",
        title="设备手册",
        version_label=revision_id,
        device_model="LJ2268",
        software_version=software_version,
        actor="test",
    )
    registry.mark_import_succeeded(
        "integration",
        revision_id,
        chunk_count=1,
        image_count=0,
        item_names=["LJ2268"],
        actor="test",
    )


def test_concurrent_mongo_publish_preserves_other_active_scopes():
    database = f"equipment_rag_test_{uuid.uuid4().hex}"
    registry = MongoDocumentRegistry(os.environ["MONGO_URL"], database)
    try:
        _completed_version(registry, "rev-31-a", software_version="3.1")
        _completed_version(registry, "rev-31-b", software_version="3.1")
        _completed_version(registry, "rev-40", software_version="4.0")
        registry.publish_version("integration", "manual-a", "rev-40", actor="test")

        revision_ids = ["rev-31-a", "rev-31-b"] * 20
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(
                executor.map(
                    lambda revision_id: registry.publish_version(
                        "integration",
                        "manual-a",
                        revision_id,
                        actor="test",
                    ),
                    revision_ids,
                )
            )

        _completed_version(registry, "rev-draft", software_version="5.0")
        document = registry.get_document("integration", "manual-a")
        assert document is not None
        active_ids = set(document["active_revision_ids"])
        assert "rev-40" in active_ids
        assert len(active_ids & {"rev-31-a", "rev-31-b"}) == 1
        statuses = {version["revision_id"]: version["status"] for version in document["versions"]}
        assert statuses["rev-draft"] == "draft"

        access = registry.managed_revision_access(
            "integration",
            [("manual-a", revision_id) for revision_id in ("rev-31-a", "rev-31-b", "rev-40")],
        )
        assert {revision_id for (_, revision_id), allowed in access.items() if allowed} == active_ids
    finally:
        registry.client.drop_database(database)
