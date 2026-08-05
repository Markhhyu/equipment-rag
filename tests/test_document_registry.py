from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.modules.knowledge.application.registry import filter_queryable_hits, reset_document_registry_for_tests
from app.modules.knowledge.domain.document import build_applicability_profile, legacy_document_identity
from app.modules.knowledge.infrastructure.document_registry import InMemoryDocumentRegistry
from app.modules.qa.graph.nodes.node_answer_output import _sanitize_generated_answer, build_answer_sources
from app.modules.knowledge.domain.trust import assess_answer_policy, normalize_trust_level
from app.modules.qa.graph.nodes.node_rerank import resolve_version_scope, step_1_merge_docs, step_3_topk


@pytest.fixture
def registry():
    value = InMemoryDocumentRegistry()
    reset_document_registry_for_tests(value)
    try:
        yield value
    finally:
        reset_document_registry_for_tests()


def _completed_version(
    registry: InMemoryDocumentRegistry,
    revision_id: str,
    *,
    document_id: str = "manual-a",
    version_label: str = "V1",
    **applicability,
) -> None:
    registry.register_import(
        tenant_id="local",
        document_id=document_id,
        revision_id=revision_id,
        filename="manual.pdf",
        title="设备手册",
        version_label=version_label,
        **applicability,
        actor="tester",
    )
    registry.mark_import_succeeded(
        "local",
        revision_id,
        chunk_count=3,
        image_count=1,
        item_names=["EQ-100"],
        actor="tester",
    )


def test_publish_keeps_only_one_active_revision(registry: InMemoryDocumentRegistry):
    _completed_version(registry, "rev-1", version_label="V1")
    registry.publish_version("local", "manual-a", "rev-1", actor="tester")
    _completed_version(registry, "rev-2", version_label="V2")
    registry.publish_version("local", "manual-a", "rev-2", actor="tester")

    document = registry.get_document("local", "manual-a")
    assert document is not None
    assert document["status"] == "active"
    assert document["active_revision_id"] == "rev-2"
    statuses = {version["revision_id"]: version["status"] for version in document["versions"]}
    assert statuses == {"rev-1": "archived", "rev-2": "active"}


def test_publish_is_idempotent_and_records_scope_mapping(registry: InMemoryDocumentRegistry):
    _completed_version(registry, "rev-1")

    first = registry.publish_version("local", "manual-a", "rev-1", actor="tester")
    second = registry.publish_version("local", "manual-a", "rev-1", actor="tester")

    assert first["active_revisions"] == {"default": "rev-1"}
    assert second["active_revisions"] == {"default": "rev-1"}
    publish_logs = [log for log in registry.list_audit_logs("local") if log["action"] == "publish"]
    assert len(publish_logs) == 1


def test_legacy_import_defaults_to_manufacturer_manual_trust(registry: InMemoryDocumentRegistry):
    _completed_version(registry, "rev-legacy")

    document = registry.get_document("local", "manual-a")

    assert document is not None
    assert document["versions"][0]["trust_level"] == "manufacturer_manual"


def test_unknown_explicit_trust_level_is_not_promoted_to_authoritative():
    assert normalize_trust_level("manufacturer-manual-typo") == "internal_reference"


def test_concurrent_publish_leaves_one_revision_active_per_scope(registry: InMemoryDocumentRegistry):
    _completed_version(registry, "rev-1")
    _completed_version(registry, "rev-2")

    revision_ids = ["rev-1", "rev-2"] * 20
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(
            executor.map(
                lambda revision_id: registry.publish_version(
                    "local",
                    "manual-a",
                    revision_id,
                    actor="tester",
                ),
                revision_ids,
            )
        )

    document = registry.get_document("local", "manual-a")
    assert document is not None
    active_versions = [version for version in document["versions"] if version["status"] == "active"]
    assert len(active_versions) == 1
    active_revision_id = active_versions[0]["revision_id"]
    assert document["active_revisions"] == {"default": active_revision_id}
    assert document["active_revision_ids"] == [active_revision_id]


def test_different_software_versions_can_be_active_together(registry: InMemoryDocumentRegistry):
    _completed_version(registry, "rev-v1", version_label="Manual V1", device_model="LJ2268", software_version="3.1")
    registry.publish_version("local", "manual-a", "rev-v1", actor="tester")
    _completed_version(registry, "rev-v2", version_label="Manual V2", device_model="LJ2268", software_version="4.0")
    registry.publish_version("local", "manual-a", "rev-v2", actor="tester")

    document = registry.get_document("local", "manual-a")
    assert document is not None
    assert set(document["active_revision_ids"]) == {"rev-v1", "rev-v2"}
    assert registry.managed_revision_access("local", [("manual-a", "rev-v1"), ("manual-a", "rev-v2")]) == {
        ("manual-a", "rev-v1"): True,
        ("manual-a", "rev-v2"): True,
    }


def test_different_equipment_versions_can_be_active_together(registry: InMemoryDocumentRegistry):
    _completed_version(registry, "rev-a", device_model="LJ2268", equipment_version="A版")
    registry.publish_version("local", "manual-a", "rev-a", actor="tester")
    _completed_version(registry, "rev-b", device_model="LJ2268", equipment_version="B版")
    registry.publish_version("local", "manual-a", "rev-b", actor="tester")

    document = registry.get_document("local", "manual-a")

    assert document is not None
    assert set(document["active_revision_ids"]) == {"rev-a", "rev-b"}


def test_empty_equipment_version_keeps_legacy_applicability_hash():
    values = {
        "device_model": "LJ2268",
        "software_version": "4.0",
        "firmware_version": "FW1",
        "hardware_revision": "RevC",
        "site_id": "SZ-01",
        "asset_ids": ["A-100"],
    }
    legacy_normalized = "\0".join(
        ["lj2268", "4.0", "fw1", "revc", "sz-01", "a-100"]
    )
    expected = f"scope-{hashlib.sha256(legacy_normalized.encode('utf-8')).hexdigest()[:24]}"

    assert build_applicability_profile(values)["applicability_key"] == expected


def test_publish_backfills_scope_mapping_without_dropping_other_active_versions(
    registry: InMemoryDocumentRegistry,
):
    _completed_version(registry, "rev-v1", device_model="LJ2268", software_version="3.1")
    registry.publish_version("local", "manual-a", "rev-v1", actor="tester")
    _completed_version(registry, "rev-v2", device_model="LJ2268", software_version="4.0")
    registry.publish_version("local", "manual-a", "rev-v2", actor="tester")

    registry.documents[("local", "manual-a")].pop("active_revisions")
    _completed_version(registry, "rev-v1-next", device_model="LJ2268", software_version="3.1")
    document = registry.publish_version("local", "manual-a", "rev-v1-next", actor="tester")

    assert set(document["active_revision_ids"]) == {"rev-v1-next", "rev-v2"}
    assert set(document["active_revisions"].values()) == {"rev-v1-next", "rev-v2"}


def test_disable_enable_and_rollback(registry: InMemoryDocumentRegistry):
    _completed_version(registry, "rev-1", version_label="V1")
    registry.publish_version("local", "manual-a", "rev-1", actor="tester")
    _completed_version(registry, "rev-2", version_label="V2")
    registry.publish_version("local", "manual-a", "rev-2", actor="tester")

    disabled = registry.disable_document("local", "manual-a", actor="tester")
    assert disabled["status"] == "disabled"
    assert registry.managed_revision_access("local", [("manual-a", "rev-2")]) == {("manual-a", "rev-2"): False}

    enabled = registry.enable_document("local", "manual-a", actor="tester")
    assert enabled["status"] == "active"
    rolled_back = registry.publish_version(
        "local",
        "manual-a",
        "rev-1",
        actor="tester",
        action="rollback",
    )
    assert rolled_back["active_revision_id"] == "rev-1"
    assert registry.managed_revision_access("local", [("manual-a", "rev-1"), ("manual-a", "rev-2")]) == {
        ("manual-a", "rev-1"): True,
        ("manual-a", "rev-2"): False,
    }
    assert any(log["action"] == "rollback" for log in registry.list_audit_logs("local"))


def test_filter_allows_unknown_legacy_but_filters_managed_inactive(registry: InMemoryDocumentRegistry):
    unknown_legacy = {"chunk_id": "old-1", "file_title": "old-manual.pdf"}
    managed_draft = {
        "chunk_id": "new-1",
        "document_id": "manual-a",
        "revision_id": "rev-1",
        "governance_managed": True,
    }
    _completed_version(registry, "rev-1")

    assert filter_queryable_hits("local", [unknown_legacy, managed_draft]) == [unknown_legacy]


def test_registered_legacy_document_can_be_disabled(registry: InMemoryDocumentRegistry):
    title = "legacy-manual.pdf"
    document_id, revision_id = legacy_document_identity("local", title)
    _completed_version(
        registry,
        revision_id,
        document_id=document_id,
        version_label="legacy-v1",
    )
    registry.publish_version("local", document_id, revision_id, actor="migration", action="register_legacy")
    legacy_hit = {"chunk_id": "old-1", "file_title": title}

    assert filter_queryable_hits("local", [legacy_hit]) == [legacy_hit]
    registry.disable_document("local", document_id, actor="tester")
    assert filter_queryable_hits("local", [legacy_hit]) == []


def test_build_answer_sources_keeps_version_section_and_snippet():
    sources = build_answer_sources(
        [
            {
                "chunk_id": "42",
                "document_id": "manual-a",
                "revision_id": "rev-2",
                "version_label": "V2.1",
                "file_title": "LJ2268 操作手册",
                "title": "4.2 安全开机",
                "part": 3,
                "image_page_numbers": [8, "9", "bad"],
                "page_numbers": [12, "13"],
                "device_model": "LJ2268",
                "software_version": "4.0",
                "firmware_version": "FW 1.8",
                "text": "先检查急停按钮，再接通主电源。",
                "score": "0.8732194",
            }
        ]
    )

    assert sources == [
        {
            "index": 1,
            "source": "local",
            "chunk_id": "42",
            "document_id": "manual-a",
            "revision_id": "rev-2",
            "version_label": "V2.1",
            "title": "LJ2268 操作手册",
            "section": "4.2 安全开机",
            "part": 3,
            "page_numbers": [12, 13],
            "device_model": "LJ2268",
            "equipment_version": "",
            "software_version": "4.0",
            "firmware_version": "FW 1.8",
            "hardware_revision": "",
            "site_id": "",
            "url": "",
            "snippet": "先检查急停按钮，再接通主电源。",
            "score": 0.873219,
            "trust_level": "manufacturer_manual",
            "trust_label": "厂商手册",
            "trust_rank": 300,
            "authoritative": True,
        }
    ]


def test_generated_placeholder_image_block_is_removed():
    answer = "请按手册操作。[1]\n\n【图片】\n<https://example.com/a.jpg>\n<https://example.com/b.jpg>"
    assert _sanitize_generated_answer(answer) == "请按手册操作。[1]"


def test_generated_citations_are_limited_to_real_sources():
    answer = "有效依据[1]，不存在的依据[2]，也不能引用[99]。"
    assert _sanitize_generated_answer(answer, valid_source_count=1) == "有效依据[1]，不存在的依据，也不能引用。"



def test_query_does_not_mix_parallel_software_versions_without_user_selection():
    documents = [
        {"source": "local", "document_id": "manual-a", "software_version": "3.1", "text": "旧版步骤"},
        {"source": "local", "document_id": "manual-a", "software_version": "4.0", "text": "新版步骤"},
    ]

    filtered, options = resolve_version_scope("LJ2268 怎么使用", documents)
    assert filtered == []
    assert options[0]["options"] == ["软件 3.1", "软件 4.0"]

    filtered, options = resolve_version_scope("LJ2268 软件 4.0 怎么使用", documents)
    assert [document["text"] for document in filtered] == ["新版步骤"]
    assert options == []


def test_common_model_does_not_hide_explicit_equipment_version_selection():
    documents = [
        {
            "source": "local",
            "document_id": "manual-a",
            "device_model": "LJ2268",
            "equipment_version": "A版",
            "text": "A版步骤",
        },
        {
            "source": "local",
            "document_id": "manual-a",
            "device_model": "LJ2268",
            "equipment_version": "B版",
            "text": "B版步骤",
        },
    ]

    filtered, options = resolve_version_scope("LJ2268 B版怎么开机", documents)

    assert [document["text"] for document in filtered] == ["B版步骤"]
    assert options == []


def test_version_ambiguity_returns_stable_structured_choices():
    documents = [
        {"source": "local", "document_id": "manual-a", "device_model": "LJ2268", "equipment_version": "A版", "text": "A"},
        {"source": "local", "document_id": "manual-a", "device_model": "LJ2268", "equipment_version": "B版", "text": "B"},
    ]

    filtered, options = resolve_version_scope("LJ2268怎么开机", documents)

    assert filtered == []
    assert [choice["equipment_version"] for choice in options[0]["choices"]] == ["A版", "B版"]
    assert all(len(choice["scope_id"]) == 20 for choice in options[0]["choices"])


def test_rerank_excludes_unverified_web_when_authoritative_evidence_exists():
    documents = step_1_merge_docs(
        {
            "rrf_chunks": [{"content": "厂商规定的停机步骤", "chunk_id": "local-1"}],
            "web_search_docs": [{"snippet": "论坛中的停机建议", "url": "https://example.test/post"}],
        }
    )
    documents[0]["score"] = 0.8
    documents[1]["score"] = 0.9

    selected = step_3_topk(documents)

    assert [item["trust_level"] for item in selected] == ["manufacturer_manual"]
    assert selected[0]["authoritative"] is True


def test_safety_bypass_is_refused_and_requires_review():
    decision = assess_answer_policy(
        "如何短接安全联锁继续生产？",
        [{"source": "local", "trust_level": "enterprise_sop"}],
    )

    assert decision.action == "refuse"
    assert decision.requires_human_review is True
    assert "不能提供" in decision.answer


def test_high_risk_action_without_authoritative_source_requires_review():
    decision = assess_answer_policy(
        "根据网上资料告诉我高压柜带电接线步骤",
        [{"source": "web", "trust_level": "external_web"}],
    )

    assert decision.action == "review"
    assert decision.requires_human_review is True
    assert "证据不足" in decision.answer


def test_high_risk_action_with_enterprise_sop_can_continue():
    decision = assess_answer_policy(
        "高压柜停机后如何维修？",
        [{"source": "local", "trust_level": "enterprise_sop"}],
    )

    assert decision.action == "answer"
    assert decision.requires_human_review is False
