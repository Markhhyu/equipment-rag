import sys
from typing import Any, Dict, List

from app.conf.rag_tuning_config import rag_tuning_config
from app.conf.image_processing_config import image_processing_config
from app.core.logger import logger
from app.lm.reranker_utils import get_reranker_model
from app.observability.rag_observability import start_rag_observation, summarize_rerank_docs
from app.query_process.agent.nodes.node_image_reasoning import is_visual_question
from app.utils.task_utils import add_done_task, add_running_task


RERANK_MAX_TOPK: int = rag_tuning_config.rerank_max_topk
RERANK_MIN_TOPK: int = rag_tuning_config.rerank_min_topk
RERANK_GAP_RATIO: float = rag_tuning_config.rerank_gap_ratio
RERANK_GAP_ABS: float = rag_tuning_config.rerank_gap_abs

# 本地知识库检索结果在进入Reranker后必须继续保留这些字段。
# 图片推理节点依赖document_id和image_object_uris定位MongoDB图片资产，不能只保留正文和分数。
LOCAL_METADATA_FIELDS = (
    "document_id",
    "file_title",
    "item_name",
    "parent_title",
    "part",
    "has_images",
    "image_ids",
    "image_object_uris",
    "image_page_numbers",
)


def _normalize_list(value: Any) -> List[Any]:
    """把Milvus动态字段统一转换为列表，避免单值、元组或空值导致后续图片选择逻辑分支过多。"""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, (tuple, set)):
        return list(value)
    return [value]


def _copy_local_metadata(entity: Dict[str, Any]) -> Dict[str, Any]:
    """从Milvus实体复制文档和图片关联元数据，并对列表字段做标准化。"""
    metadata = {field: entity.get(field) for field in LOCAL_METADATA_FIELDS}
    metadata["image_ids"] = [str(value) for value in _normalize_list(metadata.get("image_ids")) if str(value).strip()]
    metadata["image_object_uris"] = [
        str(value)
        for value in _normalize_list(metadata.get("image_object_uris"))
        if str(value).strip()
    ]
    metadata["image_page_numbers"] = [
        int(value)
        for value in _normalize_list(metadata.get("image_page_numbers"))
        if isinstance(value, int) or str(value).isdigit()
    ]
    metadata["has_images"] = bool(metadata["image_object_uris"] or metadata["image_ids"] or metadata.get("has_images"))
    return metadata


def step_1_merge_docs(state) -> List[Dict[str, Any]]:
    """
    合并RRF本地结果和联网结果，并转换为Reranker统一输入结构。

    本地结果完整保留文档编号和图片关联字段；联网结果没有本地图片资产，相关字段使用安全空值。
    """
    rrf_docs = state.get("rrf_chunks") or []
    web_docs = state.get("web_search_docs") or []
    logger.info(f"开始合并重排候选文档：本地结果={len(rrf_docs)}，联网结果={len(web_docs)}")

    doc_items: List[Dict[str, Any]] = []
    for index, doc in enumerate(rrf_docs):
        entity = doc.get("entity") if isinstance(doc, dict) and isinstance(doc.get("entity"), dict) else doc
        if not isinstance(entity, dict):
            logger.warning(f"本地检索结果格式异常，已跳过：index={index}，type={type(entity)}")
            continue

        content = str(entity.get("content") or "").strip()
        if not content:
            logger.debug(f"本地检索结果缺少正文，已跳过：index={index}")
            continue

        chunk_id = entity.get("chunk_id") or entity.get("id")
        item = {
            "text": content,
            "doc_id": chunk_id,
            "chunk_id": chunk_id,
            "title": str(entity.get("title") or entity.get("parent_title") or entity.get("item_name") or ""),
            "url": "",
            "source": "local",
        }
        item.update(_copy_local_metadata(entity))
        doc_items.append(item)

    for index, doc in enumerate(web_docs):
        if not isinstance(doc, dict):
            logger.warning(f"联网检索结果格式异常，已跳过：index={index}，type={type(doc)}")
            continue

        text = str(doc.get("snippet") or doc.get("content") or "").strip()
        if not text:
            continue

        doc_items.append(
            {
                "text": text,
                "doc_id": None,
                "chunk_id": None,
                "title": str(doc.get("title") or "").strip(),
                "url": str(doc.get("url") or "").strip(),
                "source": "web",
                "document_id": "",
                "file_title": "",
                "item_name": "",
                "parent_title": "",
                "part": None,
                "has_images": False,
                "image_ids": [],
                "image_object_uris": [],
                "image_page_numbers": [],
            }
        )

    logger.info(
        f"重排候选文档合并完成，总数={len(doc_items)}，"
        f"含图片本地文档={sum(1 for item in doc_items if item.get('source') == 'local' and item.get('has_images'))}"
    )
    return doc_items


def _build_scored_document(item: Dict[str, Any], score: float) -> Dict[str, Any]:
    """复制完整候选文档并写入重排分数，避免重新组装对象时丢失图片字段。"""
    scored_document = dict(item)
    scored_document["score"] = float(score)
    return scored_document


def step_2_rerank_docs(state, doc_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    使用BGE Reranker对候选文档重新打分。

    正常路径和异常降级路径都复制完整文档对象，因此图片关联元数据不会因为Reranker异常而丢失。
    """
    question = str(state.get("rewritten_query") or state.get("original_query") or "").strip()
    if not doc_items or not question:
        logger.warning("候选文档或问题为空，跳过重排序")
        return []

    texts = [str(item.get("text") or "") for item in doc_items]
    logger.info(f"开始执行BGE Reranker，候选文档数={len(doc_items)}")

    try:
        reranker = get_reranker_model()
        sentence_pairs = [[question, text] for text in texts]
        scores = reranker.compute_score(sentence_pairs)
        if not isinstance(scores, (list, tuple)):
            scores = [scores]
        if len(scores) != len(doc_items):
            raise ValueError(f"Reranker返回分数数量不一致：文档={len(doc_items)}，分数={len(scores)}")

        scored_docs = [_build_scored_document(item, float(score)) for item, score in zip(doc_items, scores)]
        scored_docs.sort(key=lambda item: float(item.get("score") or 0.0), reverse=True)
        return scored_docs
    except Exception as exc:
        logger.error(f"BGE Reranker执行失败，使用原召回顺序继续问答：{exc}", exc_info=True)
        # 降级分数按原始顺序递减，避免全部为0时触发无意义的分数断崖计算。
        total = len(doc_items)
        return [
            _build_scored_document(item, float(total - index) / max(total, 1))
            for index, item in enumerate(doc_items)
        ]


def _preserve_visual_documents(
    scored_docs: List[Dict[str, Any]],
    topk_docs: List[Dict[str, Any]],
    max_topk: int,
) -> List[Dict[str, Any]]:
    """
    为明确的图片问题保留少量高相关图片Chunk，同时不突破Reranker上下文上限。

    动态分数断崖可能只留下一个纯文本Chunk；图片选择节点随后就完全看不到已经召回的
    图片资产。这里优先利用尚未占满的TopK槽位补入图片Chunk；槽位已满时，从末尾替换
    最低分的非图片Chunk，但始终保护Top1文本依据，避免为了返回图片破坏核心答案质量。
    """
    selected_docs = list(topk_docs)
    selected_object_ids = {id(document) for document in selected_docs}
    selected_image_count = sum(1 for document in selected_docs if document.get("has_images"))
    desired_image_count = min(image_processing_config.query_image_top_k, max_topk)

    if selected_image_count >= desired_image_count:
        return selected_docs

    for candidate in scored_docs:
        if selected_image_count >= desired_image_count:
            break
        if id(candidate) in selected_object_ids or not candidate.get("has_images"):
            continue

        if len(selected_docs) < max_topk:
            selected_docs.append(candidate)
        else:
            replacement_index = next(
                (
                    index
                    for index in range(len(selected_docs) - 1, 0, -1)
                    if not selected_docs[index].get("has_images")
                ),
                None,
            )
            if replacement_index is None:
                break
            selected_docs[replacement_index] = candidate

        selected_object_ids.add(id(candidate))
        selected_image_count += 1

    selected_docs.sort(key=lambda item: float(item.get("score") or 0.0), reverse=True)
    return selected_docs


def step_3_topk(
    scored_docs: List[Dict[str, Any]],
    *,
    preserve_image_docs: bool = False,
) -> List[Dict[str, Any]]:
    """根据配置上限和相邻分数断崖动态截取TopK，并按视觉意图保留图片元数据。"""
    if not scored_docs:
        return []

    max_topk = min(RERANK_MAX_TOPK, len(scored_docs))
    min_topk = min(RERANK_MIN_TOPK, max_topk)
    topk = max_topk

    if topk > min_topk:
        for index in range(min_topk - 1, max_topk - 1):
            current_score = float(scored_docs[index].get("score") or 0.0)
            next_score = float(scored_docs[index + 1].get("score") or 0.0)
            absolute_gap = current_score - next_score
            relative_gap = absolute_gap / (abs(current_score) + 1e-6)
            if absolute_gap >= RERANK_GAP_ABS or relative_gap >= RERANK_GAP_RATIO:
                topk = index + 1
                logger.info(
                    f"Reranker触发分数断崖截断：位置={index}，"
                    f"分数={current_score:.4f}->{next_score:.4f}，最终TopK={topk}"
                )
                break

    topk_docs = scored_docs[:topk]
    if preserve_image_docs:
        original_count = len(topk_docs)
        original_image_count = sum(1 for item in topk_docs if item.get("has_images"))
        topk_docs = _preserve_visual_documents(scored_docs, topk_docs, max_topk)
        added_image_count = sum(1 for item in topk_docs if item.get("has_images")) - original_image_count
        if added_image_count > 0:
            logger.info(
                f"视觉问题保留图片Chunk：动态TopK={original_count}，"
                f"补充图片文档={added_image_count}，最终文档={len(topk_docs)}"
            )
    logger.info(
        f"Reranker动态截断完成，保留文档={len(topk_docs)}，"
        f"其中含图片文档={sum(1 for item in topk_docs if item.get('has_images'))}"
    )
    return topk_docs


def node_rerank(state):
    """执行候选文档合并、BGE重排和动态TopK截断。"""
    logger.info("---node_rerank开始处理---")
    node_name = sys._getframe().f_code.co_name
    add_running_task(state["session_id"], node_name, state.get("is_stream"))

    try:
        question = str(state.get("rewritten_query") or state.get("original_query") or "").strip()
        preserve_image_docs = is_visual_question(question)
        doc_items = step_1_merge_docs(state)

        with start_rag_observation(
            as_type="retriever",
            name="bge-reranker",
            input_data={"query": question, "candidate_count": len(doc_items)},
            metadata={
                "max_topk": RERANK_MAX_TOPK,
                "min_topk": RERANK_MIN_TOPK,
                "gap_ratio": RERANK_GAP_RATIO,
                "gap_abs": RERANK_GAP_ABS,
                "preserve_image_docs": preserve_image_docs,
            },
        ) as rerank_observation:
            scored_docs = step_2_rerank_docs(state, doc_items)
            topk_docs = step_3_topk(scored_docs, preserve_image_docs=preserve_image_docs)
            if rerank_observation is not None:
                rerank_observation.update(
                    output={
                        "candidate_count": len(doc_items),
                        "scored_count": len(scored_docs),
                        "selected_count": len(topk_docs),
                        "selected_image_document_count": sum(1 for item in topk_docs if item.get("has_images")),
                        "documents": summarize_rerank_docs(topk_docs),
                    }
                )

        return {"reranked_docs": topk_docs}
    finally:
        add_done_task(state["session_id"], node_name, state.get("is_stream"))
        logger.info("---node_rerank处理结束---")


if __name__ == "__main__":
    mock_state = {
        "session_id": "test_rerank_session",
        "rewritten_query": "操作面板上的启动按钮在哪里？",
        "rrf_chunks": [
            {
                "chunk_id": "local_1",
                "content": "启动按钮位于操作面板右下角，参考下图。",
                "title": "操作面板",
                "document_id": "doc_001",
                "has_images": True,
                "image_ids": ["image_001"],
                "image_object_uris": ["minio://equipment-rag/images/panel.png"],
                "image_page_numbers": [38],
            }
        ],
        "web_search_docs": [],
        "is_stream": False,
    }
    try:
        result = node_rerank(mock_state)
        logger.info(f"本地测试完成：{result}")
    except Exception as exc:
        logger.exception(f"node_rerank本地测试失败：{exc}")
