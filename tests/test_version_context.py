from app.modules.knowledge.infrastructure.document_registry import InMemoryDocumentRegistry
from app.modules.qa.graph.nodes.node_search_embedding import build_query_filter
from app.modules.knowledge.domain.version_context import latest_pinned_version_context, resolve_version_context


def _active_version(
    registry: InMemoryDocumentRegistry,
    *,
    revision_id: str,
    equipment_version: str,
) -> None:
    registry.register_import(
        tenant_id="tenant-a",
        document_id="manual-a",
        revision_id=revision_id,
        filename=f"{revision_id}.pdf",
        equipment_version=equipment_version,
        publish_requested=False,
    )
    registry.mark_import_succeeded(
        "tenant-a",
        revision_id,
        chunk_count=1,
        image_count=0,
        item_names=["LJ2268"],
    )
    registry.publish_version("tenant-a", "manual-a", revision_id, actor="tester")


def test_registry_lists_all_parallel_active_versions_for_item_name():
    registry = InMemoryDocumentRegistry()
    _active_version(registry, revision_id="rev-a", equipment_version="A版")
    _active_version(registry, revision_id="rev-b", equipment_version="B版")

    versions = registry.list_active_versions("tenant-a", ["LJ2268"])

    assert {version["revision_id"] for version in versions} == {"rev-a", "rev-b"}
    assert registry.list_active_versions("tenant-a", ["OTHER"]) == []


def test_version_context_requires_choice_then_resolves_selected_revision():
    versions = [
        {
            "document_id": "manual-a",
            "revision_id": "rev-a",
            "item_names": ["LJ2268"],
            "device_model": "LJ2268",
            "equipment_version": "A版",
        },
        {
            "document_id": "manual-a",
            "revision_id": "rev-b",
            "item_names": ["LJ2268"],
            "device_model": "LJ2268",
            "equipment_version": "B版",
        },
    ]

    ambiguous = resolve_version_context("LJ2268怎么开机", versions)
    selected_scope = ambiguous["version_scope_options"][0]["choices"][1]["scope_id"]
    resolved = resolve_version_context("使用这个版本", versions, selected_scope_id=selected_scope)

    assert ambiguous["status"] == "ambiguous"
    assert ambiguous["revision_ids"] == []
    assert resolved["status"] == "resolved"
    assert resolved["revision_ids"] == ["rev-b"]


def test_explicit_version_overrides_pinned_session_context():
    versions = [
        {
            "document_id": "manual-a",
            "revision_id": "rev-a",
            "item_names": ["LJ2268"],
            "device_model": "LJ2268",
            "equipment_version": "A版",
        },
        {
            "document_id": "manual-a",
            "revision_id": "rev-b",
            "item_names": ["LJ2268"],
            "device_model": "LJ2268",
            "equipment_version": "B版",
        },
    ]
    first = resolve_version_context("LJ2268 B版怎么开机", versions)

    reused = resolve_version_context("怎么关机", versions, pinned_context=first["selected_scopes"])
    switched = resolve_version_context("LJ2268 A版怎么关机", versions, pinned_context=first["selected_scopes"])

    assert reused["revision_ids"] == ["rev-b"]
    assert switched["revision_ids"] == ["rev-a"]


def test_latest_pinned_context_and_milvus_revision_filter():
    history = [
        {"role": "assistant", "selected_version_context": [{"revision_id": "rev-a"}]},
        {"role": "user", "text": "下一步呢"},
        {"role": "assistant", "selected_version_context": [{"revision_id": "rev-b"}]},
    ]

    assert latest_pinned_version_context(history) == [{"revision_id": "rev-b"}]
    expression = build_query_filter("tenant-a", ["LJ2268"], ["rev-b"])
    assert 'tenant_id == "tenant-a"' in expression
    assert 'item_name in ["LJ2268"]' in expression
    assert 'revision_id in ["rev-b"]' in expression
