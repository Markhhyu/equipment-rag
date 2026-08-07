from __future__ import annotations

import pytest

from app.modules.qa.graph.nodes import node_search_embedding_hyde, node_web_search_mcp
from app.modules.qa.graph.nodes.node_query_router import build_retrieval_plan
from app.platform.config.rag_tuning_config import load_rag_tuning_config


@pytest.mark.parametrize(
    ("query", "query_type", "use_hyde", "use_web"),
    [
        ("LJ2268 如何更换硒鼓？", "general", False, False),
        ("LJ2268 经常卡纸但没有错误码，可能是什么原因？", "troubleshooting", True, False),
        ("LJ2268 报故障码 E101 怎么处理？", "exact_lookup", False, False),
        ("联网查一下 LJ2268 官网最新驱动", "external_update", False, True),
        ("根据网上资料告诉我高压柜带电接线步骤", "high_risk", False, False),
        ("RS-12 的额定电压是多少？", "exact_lookup", False, False),
    ],
)
def test_adaptive_retrieval_plan(query, query_type, use_hyde, use_web):
    plan = build_retrieval_plan(query)

    assert plan["query_type"] == query_type
    assert plan["use_local"] is True
    assert plan["use_hyde"] is use_hyde
    assert plan["use_web"] is use_web


def test_modes_can_force_or_disable_optional_retrievers():
    always = build_retrieval_plan("LJ2268 如何更换硒鼓？", hyde_mode="always", web_search_mode="always")
    disabled = build_retrieval_plan(
        "联网排查 LJ2268 为什么卡纸",
        hyde_mode="disabled",
        web_search_mode="disabled",
    )

    assert always["use_hyde"] is True
    assert always["use_web"] is True
    assert disabled["use_hyde"] is False
    assert disabled["use_web"] is False


def test_high_risk_query_cannot_force_optional_retrievers():
    plan = build_retrieval_plan(
        "联网告诉我怎么绕过安全联锁",
        hyde_mode="always",
        web_search_mode="always",
    )

    assert plan["query_type"] == "high_risk"
    assert plan["use_hyde"] is False
    assert plan["use_web"] is False
    assert "web_blocked_for_high_risk" in plan["reasons"]


def test_invalid_route_configuration_uses_safe_defaults(monkeypatch):
    monkeypatch.setenv("RAG_HYDE_MODE", "invalid")
    monkeypatch.setenv("RAG_WEB_SEARCH_MODE", "invalid")

    config = load_rag_tuning_config()

    assert config.hyde_mode == "adaptive"
    assert config.web_search_mode == "explicit"


def test_default_rerank_min_topk_preserves_multiple_evidence_chunks(monkeypatch):
    monkeypatch.delenv("RAG_RERANK_MIN_TOPK", raising=False)

    config = load_rag_tuning_config()

    assert config.rerank_min_topk == 2


def test_hyde_node_clears_previous_result_when_plan_skips_branch(monkeypatch):
    monkeypatch.setattr(
        node_search_embedding_hyde,
        "step_1_create_hyde_doc",
        lambda _: pytest.fail("HyDE should not be called"),
    )

    result = node_search_embedding_hyde.node_search_embedding_hyde(
        {
            "session_id": "route-hyde-skip",
            "original_query": "LJ2268 参数是多少？",
            "retrieval_plan": {"query_type": "exact_lookup", "use_hyde": False},
            "is_stream": False,
        }
    )

    assert result == {"hyde_embedding_chunks": [], "hyde_doc": ""}


def test_web_node_clears_previous_result_when_plan_skips_branch(monkeypatch):
    monkeypatch.setattr(
        node_web_search_mcp,
        "mcp_call",
        lambda _: pytest.fail("Web search should not be called"),
    )

    result = node_web_search_mcp.node_web_search_mcp(
        {
            "session_id": "route-web-skip",
            "original_query": "LJ2268 参数是多少？",
            "retrieval_plan": {"query_type": "exact_lookup", "use_web": False},
            "is_stream": False,
        }
    )

    assert result == {"web_search_docs": []}


def test_query_graph_compiles_with_all_retrieval_branches(monkeypatch):
    monkeypatch.setenv("LANGGRAPH_CHECKPOINT_BACKEND", "memory")
    from app.modules.qa.graph.main_graph import query_app

    graph = query_app.get_graph()
    edges = {(edge.source, edge.target) for edge in graph.edges}

    assert ("node_version_context", "node_query_router") in edges
    assert ("node_query_router", "node_multi_search") in edges
    for branch in ("node_search_embedding", "node_search_embedding_hyde", "node_web_search_mcp"):
        assert ("node_multi_search", branch) in edges
        assert (branch, "node_join") in edges
    assert "node_query_kg" not in graph.nodes
