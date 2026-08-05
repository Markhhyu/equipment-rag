import functools
import time
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from app.platform.observability.langfuse_monitor import LANGFUSE_ENABLED, langfuse
from app.platform.observability.prometheus_metrics import observe_stage
from app.platform.observability.quality_metrics import analyze_import_state, analyze_query_state, stage_metrics


def _get_value(data: Any, key: str, default: Any = None) -> Any:
    """从字典或对象中读取字段，兼容Milvus Hit对象和普通字典。"""
    if isinstance(data, dict):
        return data.get(key, default)
    return getattr(data, key, default)


@contextmanager
def start_rag_observation(
    *,
    as_type: str,
    name: str,
    input_data: Optional[Any] = None,
    metadata: Optional[dict] = None,
    model: Optional[str] = None,
) -> Iterator[Optional[Any]]:
    """
    安全创建RAG业务Observation。

    Langfuse关闭或客户端不可用时退化为空上下文，不影响解析、检索、重排、视觉分析和问答主流程。
    """
    if not LANGFUSE_ENABLED or langfuse is None:
        yield None
        return

    with langfuse.start_as_current_observation(
        as_type=as_type,
        name=name,
        input=input_data,
        metadata=metadata,
        model=model,
    ) as observation:
        yield observation


def observed_graph_node(kind: str, name: str, node_function):
    """同时为Langfuse和Prometheus记录一个LangGraph节点的耗时、结果摘要与成功状态。"""

    @functools.wraps(node_function)
    def wrapper(state, *args, **kwargs):
        started = time.perf_counter()
        try:
            with start_rag_observation(
                as_type="span",
                name=name,
                input_data={
                    "task_id": state.get("task_id"),
                    "trace_id": state.get("trace_id"),
                },
                metadata={"pipeline": kind, "node": name},
            ) as observation:
                result = node_function(state, *args, **kwargs)
                merged_state = dict(state)
                if isinstance(result, dict):
                    merged_state.update(result)
                if observation is not None:
                    observation.update(output=stage_metrics(kind, name, merged_state))
            observe_stage(kind, name, time.perf_counter() - started, "completed")
            return result
        except Exception:
            observe_stage(kind, name, time.perf_counter() - started, "failed")
            raise

    return wrapper


def summarize_milvus_hits(hits: list, limit: int = 10) -> list:
    """将Milvus检索结果转换为适合上传到Langfuse的轻量摘要。"""
    result = []
    for rank, hit in enumerate((hits or [])[:limit], start=1):
        entity = _get_value(hit, "entity", {}) or {}
        raw_score = _get_value(hit, "distance", 0.0) or 0.0
        result.append(
            {
                "rank": rank,
                "chunk_id": _get_value(entity, "chunk_id") or _get_value(hit, "id"),
                "item_name": _get_value(entity, "item_name", ""),
                "document_id": _get_value(entity, "document_id", ""),
                "has_images": bool(_get_value(entity, "has_images", False)),
                "image_count": len(_get_value(entity, "image_object_uris", []) or []),
                "score": float(raw_score),
            }
        )
    return result


def summarize_rerank_docs(docs: list, limit: int = 10) -> list:
    """将BGE Reranker结果转换为不包含完整正文的监控摘要。"""
    result = []
    for rank, doc in enumerate((docs or [])[:limit], start=1):
        result.append(
            {
                "rank": rank,
                "chunk_id": doc.get("chunk_id"),
                "document_id": doc.get("document_id", ""),
                "title": doc.get("title", ""),
                "source": doc.get("source", ""),
                "has_images": bool(doc.get("has_images")),
                "image_count": len(doc.get("image_object_uris") or []),
                "score": float(doc.get("score", 0.0) or 0.0),
            }
        )
    return result


def summarize_image_assets(assets: list, limit: int = 10) -> list:
    """
    将图片资产转换为Langfuse轻量摘要。

    不上传图片地址、图片二进制、Base64、完整视觉描述或手册正文，只记录定位、状态和体积等可运营字段。
    """
    result = []
    for rank, asset in enumerate((assets or [])[:limit], start=1):
        if not isinstance(asset, dict):
            continue
        cached_description = (
            asset.get("visual_description")
            or asset.get("structured_caption")
            or asset.get("base_description")
            or asset.get("alt_text")
            or ""
        )
        result.append(
            {
                "rank": rank,
                "image_id": asset.get("image_id", ""),
                "document_id": asset.get("document_id", ""),
                "document_name": asset.get("document_name", ""),
                "page_number": asset.get("page_number"),
                "content_type": asset.get("content_type", ""),
                "file_size": int(asset.get("file_size") or 0),
                "visual_status": asset.get("visual_status", "unknown"),
                "has_cached_description": bool(str(cached_description).strip()),
                "cached_description_length": len(str(cached_description).strip()),
            }
        )
    return result


def summarize_image_reasoning(final_state: dict) -> dict:
    """汇总当前问答的视觉意图、候选图片、视觉状态和上下文使用情况。"""
    image_assets = final_state.get("image_assets") or []
    selected_uris = final_state.get("image_reasoning_object_uris") or []
    image_context = str(final_state.get("image_analysis_context") or "").strip()
    image_error = str(final_state.get("image_reasoning_error") or "").strip()
    return {
        "need_visual_reasoning": bool(final_state.get("need_visual_reasoning")),
        "status": final_state.get("image_reasoning_status") or "not_required",
        "selected_image_count": len(selected_uris),
        "session_attachment_count": len(final_state.get("user_image_refs") or []),
        "analyzed_image_count": len(image_assets),
        "available_asset_count": len(image_assets),
        "image_context_length": len(image_context),
        "has_image_context": bool(image_context),
        "has_error": bool(image_error),
        "error_type": image_error.split(":", 1)[0][:100] if image_error else "",
        "assets": summarize_image_assets(image_assets),
    }


def _create_boolean_score(trace_id: str, name: str, value: bool, comment: str) -> None:
    """创建布尔类型Langfuse Score，统一值和数据类型。"""
    langfuse.create_score(
        trace_id=trace_id,
        name=name,
        value=1.0 if value else 0.0,
        data_type="BOOLEAN",
        comment=comment,
    )


def score_query_result(final_state: dict) -> None:
    """
    为当前问答Trace增加确定性评分。

    指标用于趋势、异常检测和链路运营，不能证明最终答案一定正确，也不能替代人工标注评测集。
    """
    if not LANGFUSE_ENABLED or langfuse is None:
        return

    trace_id = langfuse.get_current_trace_id()
    if not trace_id:
        return

    answer = str(final_state.get("answer") or "").strip()
    reranked_docs = final_state.get("reranked_docs") or []
    need_visual_reasoning = bool(final_state.get("need_visual_reasoning"))
    image_status = str(final_state.get("image_reasoning_status") or "not_required")
    image_context = str(final_state.get("image_analysis_context") or "").strip()
    selected_images = final_state.get("image_reasoning_object_uris") or []

    _create_boolean_score(trace_id, "retrieval_hit", bool(reranked_docs), "重排后是否存在可用于回答的参考文档")
    _create_boolean_score(trace_id, "answer_generated", bool(answer), "本轮问答是否成功生成非空答案")
    _create_boolean_score(trace_id, "visual_question_hit", need_visual_reasoning, "用户问题是否命中图片、界面或空间位置类视觉意图")
    _create_boolean_score(trace_id, "visual_candidate_hit", bool(selected_images), "视觉问题是否从重排文档中找到相关图片候选")
    _create_boolean_score(trace_id, "image_context_used", bool(image_context), "最终回答Prompt是否获得图片分析或缓存图片说明上下文")

    if need_visual_reasoning:
        _create_boolean_score(
            trace_id,
            "visual_reasoning_success",
            image_status == "completed",
            "需要视觉分析时，查询阶段视觉模型是否成功完成",
        )
        _create_boolean_score(
            trace_id,
            "visual_reasoning_degraded",
            image_status in {"vision_disabled", "fallback_cached_description", "no_candidate_images"},
            "需要视觉分析时，是否发生关闭、无候选或缓存描述降级",
        )

    if reranked_docs:
        langfuse.create_score(
            trace_id=trace_id,
            name="rerank_top1_raw_score",
            value=float(reranked_docs[0].get("score", 0.0) or 0.0),
            data_type="NUMERIC",
            comment="BGE Reranker返回的Top1原始相关性分数",
        )

    langfuse.create_score(
        trace_id=trace_id,
        name="selected_image_count",
        value=float(len(selected_images)),
        data_type="NUMERIC",
        comment="当前问答实际选择并返回的相关图片数量",
    )

    report = analyze_query_state(final_state)
    langfuse.create_score(
        trace_id=trace_id,
        name="query_quality_proxy",
        value=float(report["quality_proxy_score"]),
        data_type="NUMERIC",
        comment="由召回、引用和答案是否生成等确定性信号组成的代理分数",
    )


def score_import_result(final_state: dict) -> dict:
    """计算文件解析、切片、向量和入库质量，并把关键比例写入Langfuse。"""
    report = analyze_import_state(final_state)
    if not LANGFUSE_ENABLED or langfuse is None:
        return report

    trace_id = langfuse.get_current_trace_id()
    if not trace_id:
        return report

    scores = {
        "import_quality_proxy": report["quality_proxy_score"],
        "chunk_healthy_ratio": report["chunks"]["healthy_length_ratio"],
        "embedding_success_ratio": report["embeddings"]["success_ratio"],
        "milvus_storage_ratio": report["storage"]["stored_ratio"],
        "item_name_coverage_ratio": report["entity"]["coverage_ratio"],
    }
    for score_name, value in scores.items():
        langfuse.create_score(
            trace_id=trace_id,
            name=score_name,
            value=float(value),
            data_type="NUMERIC",
            comment="文件导入流程的确定性质量指标；详细含义见docs/observability.md",
        )
    return report
