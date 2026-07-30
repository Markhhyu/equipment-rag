from typing_extensions import TypedDict
from typing import List


class QueryGraphState(TypedDict):
    """
    QueryGraphState定义整个查询流程中流转的数据结构。
    """

    session_id: str  # 多轮会话唯一标识
    tenant_id: str  # 调用方租户标识，用于数据面过滤
    trace_id: str  # 当前这一轮问答对应的Langfuse Trace ID
    original_query: str  # 用户原始问题

    # 检索过程中的中间数据
    embedding_chunks: list  # 普通向量检索回来的切片
    hyde_embedding_chunks: list  # HyDE检索回来的切片
    kg_chunks: list  # 图谱检索回来的切片
    web_search_docs: list  # 网络搜索回来的文档

    # 排序过程中的数据
    rrf_chunks: list  # RRF融合排序后的切片
    reranked_docs: list  # 重排序后的最终Top-K文档

    # 生成过程中的数据
    prompt: str  # 组装好的Prompt
    answer: str  # 最终生成的答案

    # 辅助信息
    item_names: List[str]  # 提取出的设备或商品名称
    rewritten_query: str  # 改写后的问题
    history: list  # 历史对话记录
    is_stream: bool  # 是否流式输出
