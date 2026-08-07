"""Deterministic quality decisions for imported knowledge revisions."""

from __future__ import annotations

from typing import Any, Iterable, Protocol


QUALITY_PASSED = "passed"
QUALITY_BLOCKED = "blocked"
QUALITY_DISABLED = "disabled"


class QualityGateConfig(Protocol):
    enabled: bool
    min_score: float
    min_healthy_chunk_ratio: float
    max_duplicate_ratio: float
    min_item_name_coverage: float
    reject_replacement_characters: bool


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def evaluate_import_quality(report: dict[str, Any], config: QualityGateConfig) -> dict[str, Any]:
    """Turn observable import metrics into an explicit publish decision."""
    parser = report.get("parser") if isinstance(report.get("parser"), dict) else {}
    chunks = report.get("chunks") if isinstance(report.get("chunks"), dict) else {}
    embeddings = report.get("embeddings") if isinstance(report.get("embeddings"), dict) else {}
    storage = report.get("storage") if isinstance(report.get("storage"), dict) else {}
    entity = report.get("entity") if isinstance(report.get("entity"), dict) else {}
    thresholds = {
        "min_score": config.min_score,
        "min_healthy_chunk_ratio": config.min_healthy_chunk_ratio,
        "max_duplicate_ratio": config.max_duplicate_ratio,
        "min_item_name_coverage": config.min_item_name_coverage,
        "reject_replacement_characters": config.reject_replacement_characters,
    }
    if not config.enabled:
        return {
            "status": QUALITY_DISABLED,
            "publish_allowed": True,
            "score": _number(report.get("quality_proxy_score")),
            "failures": [],
            "warnings": list(report.get("recommendations") or []),
            "thresholds": thresholds,
        }

    failures: list[str] = []
    if _number(parser.get("markdown_chars")) <= 0:
        failures.append("解析正文为空")
    if config.reject_replacement_characters and _number(parser.get("replacement_character_count")) > 0:
        failures.append("解析正文包含乱码替换符")
    if _number(chunks.get("count")) <= 0:
        failures.append("没有生成可检索切片")
    if _number(chunks.get("empty_count")) > 0:
        failures.append("存在空切片")
    if _number(embeddings.get("success_ratio")) < 1.0:
        failures.append("Dense/Sparse 向量生成不完整")
    if _number(storage.get("stored_ratio")) < 1.0:
        failures.append("Milvus 入库或主键回填不完整")
    if _number(report.get("quality_proxy_score")) < config.min_score:
        failures.append(f"综合质量分低于 {config.min_score:.2f}")
    if _number(chunks.get("healthy_length_ratio")) < config.min_healthy_chunk_ratio:
        failures.append(f"合理长度切片比例低于 {config.min_healthy_chunk_ratio:.0%}")
    if _number(chunks.get("duplicate_ratio")) > config.max_duplicate_ratio:
        failures.append(f"重复切片比例高于 {config.max_duplicate_ratio:.0%}")
    if _number(entity.get("coverage_ratio")) < config.min_item_name_coverage:
        failures.append(f"设备名称覆盖率低于 {config.min_item_name_coverage:.0%}")

    return {
        "status": QUALITY_BLOCKED if failures else QUALITY_PASSED,
        "publish_allowed": not failures,
        "score": _number(report.get("quality_proxy_score")),
        "failures": failures,
        "warnings": list(report.get("recommendations") or []),
        "thresholds": thresholds,
    }


def build_chunk_preview(
    chunks: Iterable[dict[str, Any]],
    *,
    limit: int = 6,
    content_chars: int = 700,
) -> list[dict[str, Any]]:
    """Keep a small, evenly distributed and vector-free sample for governance review."""
    values = [chunk for chunk in chunks if isinstance(chunk, dict) and str(chunk.get("content") or "").strip()]
    if not values or limit <= 0:
        return []
    sample_size = min(limit, len(values))
    if sample_size == 1:
        indexes = [0]
    else:
        indexes = sorted({round(index * (len(values) - 1) / (sample_size - 1)) for index in range(sample_size)})

    preview = []
    for index in indexes:
        chunk = values[index]
        content = str(chunk.get("content") or "").strip()
        preview.append(
            {
                "position": index + 1,
                "title": str(chunk.get("title") or "").strip()[:255],
                "parent_title": str(chunk.get("parent_title") or "").strip()[:255],
                "part": int(chunk.get("part") or 0),
                "item_name": str(chunk.get("item_name") or "").strip()[:255],
                "page_numbers": [
                    int(value)
                    for value in (chunk.get("page_numbers") or [])
                    if isinstance(value, int) or str(value).isdigit()
                ][:10],
                "content_chars": len(content),
                "content": content[: max(100, content_chars)],
                "truncated": len(content) > max(100, content_chars),
            }
        )
    return preview


def quality_blocks_publish(version: dict[str, Any]) -> bool:
    gate = version.get("quality_gate")
    return isinstance(gate, dict) and gate.get("status") == QUALITY_BLOCKED
