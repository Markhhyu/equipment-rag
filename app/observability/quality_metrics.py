from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from pathlib import Path
from statistics import fmean
from typing import Any, Iterable

from app.conf.rag_tuning_config import rag_tuning_config


_CITATION_PATTERN = re.compile(r"(?:https?://\S+|\[(?:\d+|source|chunk)[^\]]*\])", re.IGNORECASE)
_IMAGE_PATTERN = re.compile(r"!\[[^\]]*\]\([^\)]+\)")
_HEADING_PATTERN = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_TABLE_ROW_PATTERN = re.compile(r"^\s*\|.*\|\s*$", re.MULTILINE)


def _round(value: float | int, digits: int = 4) -> float:
    return round(float(value), digits)


def _ratio(numerator: int | float, denominator: int | float) -> float:
    return 0.0 if not denominator else _round(numerator / denominator)


def _percentile(values: list[int | float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    index = (len(ordered) - 1) * percentile
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return _round(ordered[lower], 2)
    return _round(ordered[lower] * (upper - index) + ordered[upper] * (index - lower), 2)


def _plain_dict(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    entity = getattr(value, "entity", None)
    if isinstance(entity, dict):
        return entity
    return {}


def _doc_id(value: Any) -> str:
    item = _plain_dict(value)
    entity = item.get("entity") if isinstance(item.get("entity"), dict) else item
    identifier = entity.get("chunk_id") or item.get("id") or item.get("pk") or item.get("url")
    return "" if identifier is None else str(identifier)


def _score(value: Any) -> float | None:
    item = _plain_dict(value)
    # Milvus可能返回普通dict，也可能返回带distance属性的Hit对象。
    raw = item.get("score", item.get("distance"))
    if raw is None and not isinstance(value, dict):
        raw = getattr(value, "distance", None)
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _safe_json(path_value: str | None) -> Any:
    if not path_value:
        return None
    path = Path(path_value)
    if not path.is_file() or path.stat().st_size > 100 * 1024 * 1024:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def _walk_dicts(value: Any) -> Iterable[dict]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def analyze_chunks(chunks: list[dict] | None, *, min_chars: int = 100, max_chars: int = 2400) -> dict:
    chunks = [chunk for chunk in (chunks or []) if isinstance(chunk, dict)]
    lengths = [len(str(chunk.get("content") or "").strip()) for chunk in chunks]
    hashes = [
        hashlib.sha256(" ".join(str(chunk.get("content") or "").casefold().split()).encode("utf-8")).hexdigest()
        for chunk in chunks
        if str(chunk.get("content") or "").strip()
    ]
    duplicate_count = max(0, len(hashes) - len(set(hashes)))
    healthy_count = sum(min_chars <= length <= max_chars for length in lengths)
    return {
        "count": len(chunks),
        "empty_count": sum(length == 0 for length in lengths),
        "too_short_count": sum(0 < length < min_chars for length in lengths),
        "too_long_count": sum(length > max_chars for length in lengths),
        "healthy_length_ratio": _ratio(healthy_count, len(lengths)),
        "duplicate_count": duplicate_count,
        "duplicate_ratio": _ratio(duplicate_count, len(hashes)),
        "chars_total": sum(lengths),
        "chars_avg": _round(fmean(lengths), 2) if lengths else 0.0,
        "chars_p50": _percentile(lengths, 0.5),
        "chars_p95": _percentile(lengths, 0.95),
        "chars_max": max(lengths, default=0),
    }


def _mineru_artifact_metrics(state: dict) -> dict:
    content_list = _safe_json(state.get("mineru_content_list_path"))
    nodes = list(_walk_dicts(content_list)) if content_list is not None else []
    type_counts: Counter[str] = Counter()
    page_ids: set[str] = set()
    for node in nodes:
        node_type = node.get("type") or node.get("category_type") or node.get("block_type")
        if node_type:
            type_counts[str(node_type).lower()] += 1
        page_id = node.get("page_idx", node.get("page_id", node.get("page_no")))
        if page_id is not None:
            page_ids.add(str(page_id))
    return {
        "content_list_available": content_list is not None,
        "structured_block_count": len(nodes),
        "page_count": len(page_ids),
        "block_types": dict(type_counts.most_common(12)),
        "mineru_version": str(state.get("mineru_version") or ""),
        "mineru_protocol_version": str(state.get("mineru_api_protocol_version") or ""),
    }


def analyze_import_state(state: dict) -> dict:
    markdown = str(state.get("md_content") or "")
    chunks = state.get("chunks") or []
    chunk_metrics = analyze_chunks(
        chunks,
        min_chars=rag_tuning_config.chunk_min_chars,
        max_chars=rag_tuning_config.chunk_max_chars,
    )
    vectorized = [
        chunk for chunk in chunks
        if isinstance(chunk, dict) and chunk.get("dense_vector") is not None and chunk.get("sparse_vector") is not None
    ]
    stored = [chunk for chunk in chunks if isinstance(chunk, dict) and chunk.get("chunk_id") not in (None, "")]
    item_names = [str(chunk.get("item_name") or "").strip() for chunk in chunks if isinstance(chunk, dict)]
    sparse_sizes = [len(chunk.get("sparse_vector") or {}) for chunk in vectorized]
    dense_dimensions = [len(chunk.get("dense_vector") or []) for chunk in vectorized]

    parser = {
        "markdown_chars": len(markdown),
        "markdown_lines": len(markdown.splitlines()),
        "heading_count": len(_HEADING_PATTERN.findall(markdown)),
        "table_row_count": len(_TABLE_ROW_PATTERN.findall(markdown)),
        "image_reference_count": len(_IMAGE_PATTERN.findall(markdown)),
        "replacement_character_count": markdown.count("�"),
        **_mineru_artifact_metrics(state),
    }
    embeddings = {
        "expected_count": len(chunks),
        "success_count": len(vectorized),
        "success_ratio": _ratio(len(vectorized), len(chunks)),
        "dense_dimension": max(dense_dimensions, default=0),
        "sparse_nonzero_avg": _round(fmean(sparse_sizes), 2) if sparse_sizes else 0.0,
    }
    storage = {
        "expected_count": len(chunks),
        "stored_count": len(stored),
        "stored_ratio": _ratio(len(stored), len(chunks)),
    }
    entity = {
        "unique_item_names": len({name for name in item_names if name}),
        "missing_item_name_count": sum(not name for name in item_names),
        "coverage_ratio": _ratio(sum(bool(name) for name in item_names), len(item_names)),
    }

    checks = [
        float(parser["markdown_chars"] > 0),
        float(parser["replacement_character_count"] == 0),
        float(chunk_metrics["count"] > 0),
        chunk_metrics["healthy_length_ratio"],
        1.0 - chunk_metrics["duplicate_ratio"],
        embeddings["success_ratio"],
        storage["stored_ratio"],
        entity["coverage_ratio"],
    ]
    recommendations: list[str] = []
    if not parser["markdown_chars"]:
        recommendations.append("解析结果为空：检查 MinerU 返回包、Markdown 路径和扫描件 OCR 配置。")
    if parser["replacement_character_count"]:
        recommendations.append("解析文本包含乱码替换符：检查源文件编码、OCR 语言和字体映射。")
    if chunk_metrics["too_short_count"]:
        recommendations.append("短切片偏多：提高 RAG_CHUNK_MIN_CHARS 或优化同标题段落合并。")
    if chunk_metrics["too_long_count"]:
        recommendations.append("超长切片存在：降低 RAG_CHUNK_MAX_CHARS，避免检索命中但上下文噪声过大。")
    if chunk_metrics["duplicate_count"]:
        recommendations.append("发现重复切片：检查标题继承、图片摘要替换和重复导入逻辑。")
    if embeddings["success_ratio"] < 1:
        recommendations.append("部分切片未生成完整 Dense/Sparse 向量：检查批处理异常与模型输出。")
    if storage["stored_ratio"] < 1:
        recommendations.append("Milvus 入库或主键回填不完整：核对 insert_count、ids 数量与 Collection Schema。")
    if entity["coverage_ratio"] < 1:
        recommendations.append("部分切片缺少设备名称：优化设备识别 Prompt 或增加文件名规则兜底。")

    return {
        "quality_proxy_score": _round(fmean(checks)) if checks else 0.0,
        "parser": parser,
        "chunks": chunk_metrics,
        "embeddings": embeddings,
        "storage": storage,
        "entity": entity,
        "recommendations": recommendations,
    }


def analyze_query_state(state: dict) -> dict:
    embedding = list(state.get("embedding_chunks") or [])
    hyde = list(state.get("hyde_embedding_chunks") or [])
    web = list(state.get("web_search_docs") or [])
    kg = list(state.get("kg_chunks") or [])
    rrf = list(state.get("rrf_chunks") or [])
    reranked = list(state.get("reranked_docs") or [])
    answer = str(state.get("answer") or "").strip()

    embedding_ids = {_doc_id(item) for item in embedding} - {""}
    hyde_ids = {_doc_id(item) for item in hyde} - {""}
    overlap = embedding_ids & hyde_ids
    rerank_scores = [score for item in reranked if (score := _score(item)) is not None]
    top1 = rerank_scores[0] if rerank_scores else None
    top2 = rerank_scores[1] if len(rerank_scores) > 1 else None
    context_chars = sum(len(str(_plain_dict(item).get("text") or _plain_dict(item).get("content") or "")) for item in reranked)
    citation_count = len(_CITATION_PATTERN.findall(answer))
    clarified = bool(answer and not reranked and any(term in answer for term in ("请提供", "请确认", "型号", "设备名称")))

    retrieval = {
        "embedding_count": len(embedding),
        "hyde_count": len(hyde),
        "web_count": len(web),
        "kg_count": len(kg),
        "unique_local_candidates": len(embedding_ids | hyde_ids),
        "embedding_hyde_overlap_count": len(overlap),
        "embedding_hyde_overlap_ratio": _ratio(len(overlap), len(embedding_ids | hyde_ids)),
        "rrf_count": len(rrf),
        "reranked_count": len(reranked),
        "rerank_top1_score": _round(top1) if top1 is not None else None,
        "rerank_top1_gap": _round(top1 - top2) if top1 is not None and top2 is not None else None,
        "context_chars": context_chars,
    }
    response = {
        "answer_chars": len(answer),
        "answer_generated": bool(answer),
        "citation_count": citation_count,
        "has_citation": citation_count > 0,
        "clarified": clarified,
    }
    checks = [
        float(bool(answer)),
        float(bool(reranked) or clarified),
        float(citation_count > 0 or clarified),
        float(context_chars > 0 or clarified),
    ]
    recommendations: list[str] = []
    if not reranked and not clarified:
        recommendations.append("没有最终参考文档：检查设备名称过滤、Milvus 数据量和召回 TopK。")
    if embedding and hyde and not overlap:
        recommendations.append("普通检索与 HyDE 完全不重合：抽样检查 HyDE 是否偏离问题意图。")
    if len(reranked) == 1:
        recommendations.append("只有一条证据进入回答：可提高召回数量或降低过激的 Rerank 断崖阈值。")
    if answer and reranked and not citation_count:
        recommendations.append("回答未包含可识别引用：在答案 Prompt 中强制输出来源或 Chunk 标识。")
    if context_chars > 20000:
        recommendations.append("回答上下文过长：降低 Rerank TopK 或 Chunk 长度以减少噪声和 Token 成本。")

    return {
        "quality_proxy_score": _round(fmean(checks)) if checks else 0.0,
        "retrieval": retrieval,
        "response": response,
        "recommendations": recommendations,
    }


def stage_metrics(kind: str, node_name: str, state: dict) -> dict:
    """从节点输出提取轻量指标，绝不写入正文或向量。"""
    if kind == "import":
        # 节点Span只计算当前阶段需要的摘要，避免对大型文档反复遍历完整向量和MinerU JSON。
        chunks = [chunk for chunk in (state.get("chunks") or []) if isinstance(chunk, dict)]
        if node_name in {"node_pdf_to_md", "node_md_img"}:
            markdown = str(state.get("md_content") or "")
            return {
                "markdown_chars": len(markdown),
                "heading_count": len(_HEADING_PATTERN.findall(markdown)),
                "image_reference_count": len(_IMAGE_PATTERN.findall(markdown)),
                "mineru_version": str(state.get("mineru_version") or ""),
            }
        if node_name == "node_document_split":
            return analyze_chunks(
                chunks,
                min_chars=rag_tuning_config.chunk_min_chars,
                max_chars=rag_tuning_config.chunk_max_chars,
            )
        if node_name == "node_item_name_recognition":
            names = [str(chunk.get("item_name") or "").strip() for chunk in chunks]
            return {
                "unique_item_names": len({name for name in names if name}),
                "coverage_ratio": _ratio(sum(bool(name) for name in names), len(names)),
            }
        if node_name == "node_bge_embedding":
            success = sum(
                chunk.get("dense_vector") is not None and chunk.get("sparse_vector") is not None for chunk in chunks
            )
            return {"expected_count": len(chunks), "success_count": success, "success_ratio": _ratio(success, len(chunks))}
        if node_name == "node_import_milvus":
            stored = sum(chunk.get("chunk_id") not in (None, "") for chunk in chunks)
            return {"expected_count": len(chunks), "stored_count": stored, "stored_ratio": _ratio(stored, len(chunks))}
        return {}
    report = analyze_query_state(state)
    if node_name == "node_item_name_confirm":
        return {
            "item_name_count": len(state.get("item_names") or []),
            "rewritten_query_chars": len(str(state.get("rewritten_query") or "")),
            "early_answer": bool(state.get("answer")),
        }
    mapping = {
        "node_search_embedding": {"result_count": report["retrieval"]["embedding_count"]},
        "node_search_embedding_hyde": {"result_count": report["retrieval"]["hyde_count"]},
        "node_web_search_mcp": {"result_count": report["retrieval"]["web_count"]},
        "node_query_kg": {"result_count": report["retrieval"]["kg_count"]},
        "node_rrf": {"result_count": report["retrieval"]["rrf_count"]},
        "node_rerank": {
            "result_count": report["retrieval"]["reranked_count"],
            "top1_score": report["retrieval"]["rerank_top1_score"],
            "top1_gap": report["retrieval"]["rerank_top1_gap"],
        },
        "node_answer_output": report["response"],
    }
    return mapping.get(node_name, {})
