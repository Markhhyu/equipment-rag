"""State contract shared by all question-answering graph nodes."""

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
    # 用户在聊天框上传、仅属于当前会话的图片引用；不会写入Milvus或知识库图片资产集合。
    user_image_refs: List[str]
    selected_version_scope_id: str
    reset_version_context: bool
    query_revision_ids: List[str]
    selected_version_context: List[Dict[str, Any]]
    retrieval_plan: Dict[str, Any]

    # 设备或商品识别结果。
    item_names: List[str]
    # 当向量检索只能得到候选、尚未得到用户确认时暂存在这里。
    # 该字段会写入澄清助手消息，供下一轮“是的/就是这个”恢复单个候选。
    pending_item_names: List[str]

    # 多路检索中间结果。
    embedding_chunks: list
    hyde_embedding_chunks: list
    hyde_doc: str
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
    # 最终答案节点实际使用的稳定MinIO引用，以及返回给浏览器的短期签名URL。
    # 这两个字段必须声明在LangGraph状态中，否则节点内虽然生成成功，合并状态时仍会被丢弃。
    image_object_refs: List[str]
    image_urls: List[str]
    version_scope_options: List[Dict[str, Any]]
    # 最终回答使用的结构化证据，供API和前端展示文档版本、章节与原文片段。
    sources: List[Dict[str, Any]]
    answer_policy: str
    requires_human_review: bool
    review_reason: str
    image_reasoning_error: str

    # 最终生成结果。
    prompt: str
    answer: str
