from typing import Any, Dict, List

from typing_extensions import TypedDict


class QueryGraphState(TypedDict, total=False):
    """
    查询工作流共享状态。

    LangGraph各节点只返回自己负责更新的字段，因此使用total=False允许节点返回部分状态，
    同时保留字段类型提示，避免新增图片推理结果时依赖未声明的动态键。
    """

    # 会话与调用方信息。
    session_id: str
    tenant_id: str
    trace_id: str
    original_query: str
    rewritten_query: str
    history: list
    is_stream: bool

    # 设备或商品识别结果。
    item_names: List[str]

    # 多路检索中间结果。
    embedding_chunks: list
    hyde_embedding_chunks: list
    hyde_doc: str
    kg_chunks: list
    web_search_docs: list

    # 排序结果。
    rrf_chunks: list
    reranked_docs: List[Dict[str, Any]]

    # 查询阶段图片推理结果。
    need_visual_reasoning: bool
    image_reasoning_status: str
    image_assets: List[Dict[str, Any]]
    image_analysis_context: str
    image_reasoning_object_uris: List[str]
    image_reasoning_error: str

    # 最终生成结果。
    prompt: str
    answer: str
