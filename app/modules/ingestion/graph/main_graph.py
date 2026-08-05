# 加载环境变量：从 .env 文件读取配置（如Milvus地址、KG服务地址、BGE模型路径等）
from dotenv import load_dotenv
from langgraph.graph import StateGraph, END

from app.modules.ingestion.graph.nodes.node_attach_image_metadata import node_attach_image_metadata
from app.modules.ingestion.graph.nodes.node_bge_embedding import node_bge_embedding
from app.modules.ingestion.graph.nodes.node_document_split import node_document_split
from app.modules.ingestion.graph.nodes.node_entry import node_entry
from app.modules.ingestion.graph.nodes.node_import_milvus import node_import_milvus
from app.modules.ingestion.graph.nodes.node_item_name_recognition import node_item_name_recognition
from app.modules.ingestion.graph.nodes.node_md_img import node_md_img
from app.modules.ingestion.graph.nodes.node_pdf_to_md import node_pdf_to_md
from app.modules.ingestion.graph.state import ImportGraphState
from app.platform.runtime.checkpointing import get_checkpointer
from app.platform.observability.rag_observability import observed_graph_node


load_dotenv()

workflow = StateGraph(ImportGraphState)

workflow.add_node("node_entry", observed_graph_node("import", "node_entry", node_entry))
workflow.add_node("node_pdf_to_md", observed_graph_node("import", "node_pdf_to_md", node_pdf_to_md))
workflow.add_node("node_md_img", observed_graph_node("import", "node_md_img", node_md_img))
workflow.add_node("node_document_split", observed_graph_node("import", "node_document_split", node_document_split))
workflow.add_node("node_attach_image_metadata", observed_graph_node("import", "node_attach_image_metadata", node_attach_image_metadata))
workflow.add_node("node_item_name_recognition", observed_graph_node("import", "node_item_name_recognition", node_item_name_recognition))
workflow.add_node("node_bge_embedding", observed_graph_node("import", "node_bge_embedding", node_bge_embedding))
workflow.add_node("node_import_milvus", observed_graph_node("import", "node_import_milvus", node_import_milvus))

workflow.set_entry_point("node_entry")


def route_after_entry(state: ImportGraphState) -> str:
    if state.get("is_md_read_enabled"):
        return "node_md_img"
    if state.get("is_pdf_read_enabled"):
        return "node_pdf_to_md"
    return END


workflow.add_conditional_edges(
    "node_entry",
    route_after_entry,
    {
        "node_md_img": "node_md_img",
        "node_pdf_to_md": "node_pdf_to_md",
        END: END,
    },
)

workflow.add_edge("node_pdf_to_md", "node_md_img")
workflow.add_edge("node_md_img", "node_document_split")
# 图片关联必须发生在切片之后、向量化之前。
# 原因：只有切片后才能知道一张图片属于哪个语义块，查询时才能精准召回对应图片。
workflow.add_edge("node_document_split", "node_attach_image_metadata")
workflow.add_edge("node_attach_image_metadata", "node_item_name_recognition")
workflow.add_edge("node_item_name_recognition", "node_bge_embedding")
workflow.add_edge("node_bge_embedding", "node_import_milvus")
workflow.add_edge("node_import_milvus", END)

kb_import_app = workflow.compile(checkpointer=get_checkpointer())
