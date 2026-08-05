from langgraph.graph import END, StateGraph

from app.platform.observability.rag_observability import observed_graph_node
from app.modules.qa.graph.nodes.node_answer_output import node_answer_output
from app.modules.qa.graph.nodes.node_image_reasoning import node_image_reasoning
from app.modules.qa.graph.nodes.node_item_name_confirm import node_item_name_confirm
from app.modules.qa.graph.nodes.node_query_kg import node_query_kg
from app.modules.qa.graph.nodes.node_rerank import node_rerank
from app.modules.qa.graph.nodes.node_rrf import node_rrf
from app.modules.qa.graph.nodes.node_search_embedding import node_search_embedding
from app.modules.qa.graph.nodes.node_search_embedding_hyde import node_search_embedding_hyde
from app.modules.qa.graph.nodes.node_version_context import node_version_context
from app.modules.qa.graph.nodes.node_web_search_mcp import node_web_search_mcp
from app.modules.qa.graph.state import QueryGraphState
from app.platform.runtime.checkpointing import get_checkpointer


builder = StateGraph(QueryGraphState)

builder.add_node("node_item_name_confirm", observed_graph_node("query", "node_item_name_confirm", node_item_name_confirm))
builder.add_node("node_version_context", observed_graph_node("query", "node_version_context", node_version_context))
builder.add_node("node_multi_search", lambda state: state)
builder.add_node("node_search_embedding", observed_graph_node("query", "node_search_embedding", node_search_embedding))
builder.add_node("node_search_embedding_hyde", observed_graph_node("query", "node_search_embedding_hyde", node_search_embedding_hyde))
builder.add_node("node_query_kg", observed_graph_node("query", "node_query_kg", node_query_kg))
builder.add_node("node_web_search_mcp", observed_graph_node("query", "node_web_search_mcp", node_web_search_mcp))
builder.add_node("node_join", lambda state: state)
builder.add_node("node_rrf", observed_graph_node("query", "node_rrf", node_rrf))
builder.add_node("node_rerank", observed_graph_node("query", "node_rerank", node_rerank))

# 查询阶段新增图片推理节点。
# 只有明确需要图片信息的问题才会实际调用视觉模型，普通设备知识问题不会增加额外延迟。
builder.add_node("node_image_reasoning", observed_graph_node("query", "node_image_reasoning", node_image_reasoning))
builder.add_node("node_answer_output", observed_graph_node("query", "node_answer_output", node_answer_output))

builder.set_entry_point("node_item_name_confirm")


def route_after_item_confirm(state: QueryGraphState):
    if state.get("answer"):
        return "node_answer_output"
    return "node_version_context"


builder.add_conditional_edges("node_item_name_confirm", route_after_item_confirm)


def route_after_version_context(state: QueryGraphState):
    if state.get("answer"):
        return "node_answer_output"
    return "node_multi_search"


builder.add_conditional_edges("node_version_context", route_after_version_context)

builder.add_edge("node_multi_search", "node_search_embedding")
builder.add_edge("node_multi_search", "node_search_embedding_hyde")
builder.add_edge("node_multi_search", "node_web_search_mcp")
builder.add_edge("node_multi_search", "node_query_kg")

builder.add_edge("node_search_embedding", "node_join")
builder.add_edge("node_search_embedding_hyde", "node_join")
builder.add_edge("node_web_search_mcp", "node_join")
builder.add_edge("node_query_kg", "node_join")

builder.add_edge("node_join", "node_rrf")
builder.add_edge("node_rrf", "node_rerank")

# Reranker完成后先执行图片增强，再进入最终答案生成。
builder.add_edge("node_rerank", "node_image_reasoning")
builder.add_edge("node_image_reasoning", "node_answer_output")
builder.add_edge("node_answer_output", END)


query_app = builder.compile(checkpointer=get_checkpointer())
