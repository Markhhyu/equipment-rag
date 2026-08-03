import json
import re

import app.observability.quality_metrics as quality_metrics
from app.observability.langfuse_monitor import create_query_trace_id
from app.observability.rag_observability import observed_graph_node
from app.observability.quality_metrics import analyze_chunks, analyze_import_state, analyze_query_state, stage_metrics


def test_import_quality_report_covers_parser_chunks_vectors_and_storage(monkeypatch):
    # 不依赖真实MinerU文件，直接模拟content_list解析结果，使测试稳定且无需外部服务。
    monkeypatch.setattr(
        quality_metrics,
        "_safe_json",
        lambda _: [
            {"type": "text", "page_idx": 0, "text": "启动步骤"},
            {"type": "table", "page_idx": 1, "rows": []},
        ],
    )
    state = {
        "md_content": "# 真空泵\n\n| 参数 | 值 |\n|---|---|\n![面板](panel.png)\n" + "启动检查。" * 80,
        "mineru_content_list_path": "mock_content_list.json",
        "mineru_version": "3.x",
        "chunks": [
            {
                "content": "启动检查。" * 80,
                "item_name": "真空泵A",
                "dense_vector": [0.1, 0.2],
                "sparse_vector": {1: 0.5},
                "chunk_id": "101",
            }
        ],
    }

    report = analyze_import_state(state)

    assert report["parser"]["page_count"] == 2
    assert report["parser"]["image_reference_count"] == 1
    assert report["chunks"]["count"] == 1
    assert report["embeddings"]["success_ratio"] == 1.0
    assert report["storage"]["stored_ratio"] == 1.0
    assert report["entity"]["coverage_ratio"] == 1.0


def test_chunk_report_detects_empty_short_long_and_duplicate_content():
    report = analyze_chunks(
        [
            {"content": ""},
            {"content": "短文本"},
            {"content": "重复内容" * 30},
            {"content": "重复内容" * 30},
            {"content": "超长" * 1000},
        ],
        min_chars=100,
        max_chars=1000,
    )

    assert report["empty_count"] == 1
    assert report["too_short_count"] == 1
    assert report["too_long_count"] == 1
    assert report["duplicate_count"] == 1


def test_query_quality_report_exposes_retrieval_overlap_rank_and_citation():
    state = {
        "embedding_chunks": [
            {"entity": {"chunk_id": "1", "content": "压力检查"}, "distance": 0.8},
            {"entity": {"chunk_id": "2", "content": "阀门检查"}, "distance": 0.7},
        ],
        "hyde_embedding_chunks": [
            {"entity": {"chunk_id": "2", "content": "阀门检查"}, "distance": 0.75},
            {"entity": {"chunk_id": "3", "content": "复位步骤"}, "distance": 0.6},
        ],
        "rrf_chunks": [{"chunk_id": "2", "content": "阀门检查"}],
        "reranked_docs": [
            {"chunk_id": "2", "text": "阀门检查", "score": 0.9},
            {"chunk_id": "1", "text": "压力检查", "score": 0.6},
        ],
        "answer": "请先检查压力和阀门。[chunk:2]",
    }

    report = analyze_query_state(state)

    assert report["retrieval"]["unique_local_candidates"] == 3
    assert report["retrieval"]["embedding_hyde_overlap_count"] == 1
    assert report["retrieval"]["rerank_top1_gap"] == 0.3
    assert report["response"]["has_citation"] is True


def test_stage_metrics_never_returns_document_content_or_vectors():
    state = {
        "chunks": [
            {
                "content": "设备正文" * 100,
                "dense_vector": [0.1] * 1024,
                "sparse_vector": {1: 0.5},
                "item_name": "设备A",
            }
        ]
    }

    metrics = stage_metrics("import", "node_bge_embedding", state)
    serialized = json.dumps(metrics, ensure_ascii=False)

    assert metrics["success_ratio"] == 1.0
    assert "设备正文" not in serialized
    assert "dense_vector" not in serialized


def test_trace_id_and_observed_node_work_when_langfuse_is_disabled():
    trace_id = create_query_trace_id()
    assert re.fullmatch(r"[0-9a-f]{32}", trace_id)

    def example_node(state):
        return {"answer": "完成"}

    wrapped = observed_graph_node("query", "example_node", example_node)
    result = wrapped({"trace_id": trace_id})

    assert result == {"answer": "完成"}
