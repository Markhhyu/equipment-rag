from app.query_process.agent.nodes import node_rerank
from app.query_process.agent.state import QueryGraphState


def _scored_doc(chunk_id: str, score: float, *, has_images: bool = False):
    """构造只包含TopK测试所需字段的重排结果。"""
    return {
        "chunk_id": chunk_id,
        "text": f"chunk {chunk_id}",
        "score": score,
        "has_images": has_images,
        "image_object_uris": [f"minio://equipment-rag/images/{chunk_id}.png"] if has_images else [],
    }


def test_visual_question_preserves_retrieved_image_documents(monkeypatch):
    """分数断崖只保留纯文本Top1时，视觉问题仍应补回已召回的图片Chunk。"""
    monkeypatch.setattr(node_rerank, "RERANK_MAX_TOPK", 5)
    monkeypatch.setattr(node_rerank, "RERANK_MIN_TOPK", 1)
    monkeypatch.setattr(node_rerank, "RERANK_GAP_ABS", 0.5)
    monkeypatch.setattr(node_rerank, "RERANK_GAP_RATIO", 0.25)

    scored_docs = [
        _scored_doc("text-top1", 0.99),
        _scored_doc("image-1", 0.40, has_images=True),
        _scored_doc("image-2", 0.35, has_images=True),
        _scored_doc("text-low", 0.30),
    ]

    result = node_rerank.step_3_topk(scored_docs, preserve_image_docs=True)

    assert result[0]["chunk_id"] == "text-top1"
    assert [document["chunk_id"] for document in result if document["has_images"]] == [
        "image-1",
        "image-2",
    ]
    assert len(result) <= node_rerank.RERANK_MAX_TOPK


def test_normal_question_keeps_dynamic_text_topk(monkeypatch):
    """普通文本问题不应为了图片扩大上下文，仍沿用原有分数断崖结果。"""
    monkeypatch.setattr(node_rerank, "RERANK_MAX_TOPK", 5)
    monkeypatch.setattr(node_rerank, "RERANK_MIN_TOPK", 1)
    monkeypatch.setattr(node_rerank, "RERANK_GAP_ABS", 0.5)
    monkeypatch.setattr(node_rerank, "RERANK_GAP_RATIO", 0.25)

    scored_docs = [
        _scored_doc("text-top1", 0.99),
        _scored_doc("image-1", 0.40, has_images=True),
    ]

    result = node_rerank.step_3_topk(scored_docs)

    assert [document["chunk_id"] for document in result] == ["text-top1"]


def test_final_image_fields_are_part_of_langgraph_state():
    """答案节点生成的MinIO引用和签名URL必须能通过LangGraph最终状态返回给API。"""
    assert "image_object_refs" in QueryGraphState.__annotations__
    assert "image_urls" in QueryGraphState.__annotations__
