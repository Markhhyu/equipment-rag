from __future__ import annotations

from app.modules.knowledge.domain.quality import build_chunk_preview, evaluate_import_quality
from app.platform.config.knowledge_quality_config import KnowledgeQualityConfig


def _config(**overrides) -> KnowledgeQualityConfig:
    values = {
        "enabled": True,
        "min_score": 0.75,
        "min_healthy_chunk_ratio": 0.5,
        "max_duplicate_ratio": 0.05,
        "min_item_name_coverage": 0.8,
        "reject_replacement_characters": True,
    }
    values.update(overrides)
    return KnowledgeQualityConfig(**values)


def _healthy_report() -> dict:
    return {
        "quality_proxy_score": 1.0,
        "parser": {"markdown_chars": 5000, "replacement_character_count": 0},
        "chunks": {"count": 10, "empty_count": 0, "healthy_length_ratio": 0.9, "duplicate_ratio": 0.0},
        "embeddings": {"success_ratio": 1.0},
        "storage": {"stored_ratio": 1.0},
        "entity": {"coverage_ratio": 1.0},
        "recommendations": [],
    }


def test_healthy_import_passes_quality_gate():
    decision = evaluate_import_quality(_healthy_report(), _config())

    assert decision["status"] == "passed"
    assert decision["publish_allowed"] is True
    assert decision["failures"] == []


def test_critical_import_failures_cannot_be_hidden_by_average_score():
    report = _healthy_report()
    report["quality_proxy_score"] = 0.95
    report["embeddings"]["success_ratio"] = 0.9
    report["entity"]["coverage_ratio"] = 0.5

    decision = evaluate_import_quality(report, _config())

    assert decision["status"] == "blocked"
    assert decision["publish_allowed"] is False
    assert "Dense/Sparse 向量生成不完整" in decision["failures"]
    assert "设备名称覆盖率低于 80%" in decision["failures"]


def test_disabled_gate_keeps_diagnostics_without_blocking_publish():
    report = _healthy_report()
    report["quality_proxy_score"] = 0.1
    report["recommendations"] = ["检查解析结果"]

    decision = evaluate_import_quality(report, _config(enabled=False))

    assert decision["status"] == "disabled"
    assert decision["publish_allowed"] is True
    assert decision["warnings"] == ["检查解析结果"]


def test_chunk_preview_is_evenly_distributed_truncated_and_vector_free():
    chunks = [
        {
            "title": f"章节 {index}",
            "content": str(index) * 800,
            "item_name": "LJ2268",
            "page_numbers": [index],
            "dense_vector": [0.1, 0.2],
            "sparse_vector": {1: 0.5},
        }
        for index in range(1, 11)
    ]

    preview = build_chunk_preview(chunks, limit=3, content_chars=120)

    assert [item["position"] for item in preview] == [1, 5, 10]
    assert all(len(item["content"]) == 120 for item in preview)
    assert all(item["truncated"] is True for item in preview)
    assert all("dense_vector" not in item and "sparse_vector" not in item for item in preview)
