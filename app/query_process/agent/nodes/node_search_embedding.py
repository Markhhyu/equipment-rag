import os
import sys

from dotenv import find_dotenv, load_dotenv

from app.clients.milvus_utils import (
    create_hybrid_search_requests,
    get_milvus_client,
    hybrid_search
)
from app.core.logger import logger
from app.lm.embedding_utils import generate_embeddings
from app.observability.rag_observability import (
    start_rag_observation,
    summarize_milvus_hits
)
from app.utils.task_utils import (
    add_done_task,
    add_running_task
)
from app.security.tenancy import escape_milvus_literal, tenant_filter


# 加载项目.env配置。
load_dotenv(find_dotenv())


def node_search_embedding(state):
    """
    使用BGE-M3和Milvus执行混合检索。

    执行流程：
    1. 获取改写后的用户问题；
    2. 获取已确认的商品或设备名称；
    3. 使用BGE-M3生成稠密向量和稀疏向量；
    4. 构造Milvus过滤表达式；
    5. 执行稠密+稀疏混合检索；
    6. 将结果写入embedding_chunks。

    :param state: LangGraph当前状态。
    :return:
        {
            "embedding_chunks": 检索结果列表
        }
    """

    logger.info("---node_search_embedding 开始处理---")

    # 获取当前节点名称，供任务进度状态使用。
    node_name = sys._getframe().f_code.co_name

    # 标记节点正在执行。
    add_running_task(
        state["session_id"],
        node_name,
        state.get("is_stream")
    )

    # 获取改写后的问题。
    query = (state.get("rewritten_query") or "").strip()

    # 获取已经确认的商品或设备名称。
    item_names = state.get("item_names") or []

    logger.info(
        f"向量检索参数：query={query}，item_names={item_names}"
    )

    # 问题为空时无法生成有效向量。
    if not query:
        logger.warning("改写后的问题为空，跳过Milvus向量检索")

        # 当前节点属于正常结束，而不是系统异常。
        add_done_task(
            state["session_id"],
            node_name,
            state.get("is_stream")
        )

        return {"embedding_chunks": []}

    # 没有确认商品或设备名称时，当前逻辑无法构建精准过滤条件。
    if not item_names:
        logger.warning("item_names为空，跳过Milvus向量检索")

        add_done_task(
            state["session_id"],
            node_name,
            state.get("is_stream")
        )

        return {"embedding_chunks": []}

    # ==================== 第一阶段：BGE-M3向量化 ====================

    # 创建embedding类型Observation，用于记录：
    # 1. 向量化耗时；
    # 2. 稠密向量维度；
    # 3. 稀疏向量非零维度数量。
    with start_rag_observation(
            as_type="embedding",
            name="bge-m3-query-embedding",
            input_data={
                "query": query
            },
            metadata={
                "input_count": 1,
                "usage": "query-embedding"
            },
            model=os.getenv("BGE_M3") or "bge-m3"
    ) as embedding_observation:

        # generate_embeddings接收文本列表。
        embeddings = generate_embeddings([query])

        # 获取稠密向量列表。
        dense_vectors = embeddings.get("dense") or []

        # 获取稀疏向量列表。
        sparse_vectors = embeddings.get("sparse") or []

        # 检查模型是否正常返回向量。
        if not dense_vectors or not sparse_vectors:
            raise ValueError(
                "BGE-M3向量化失败：dense或sparse结果为空"
            )

        # 当前只有一个问题，因此取第一条向量。
        dense_vec = dense_vectors[0]
        sparse_vec = sparse_vectors[0]

        # 将向量摘要写入Langfuse。
        # 不上传完整向量，避免Trace过大。
        if embedding_observation is not None:
            embedding_observation.update(
                output={
                    "dense_dimension": len(dense_vec),
                    "sparse_nonzero_count": len(sparse_vec)
                }
            )

    logger.info(
        f"BGE-M3向量生成完成，"
        f"dense_dimension={len(dense_vec)}，"
        f"sparse_nonzero_count={len(sparse_vec)}"
    )

    # ==================== 第二阶段：构造Milvus请求 ====================

    # 从环境变量读取Milvus切片集合名称。
    collection_name = os.getenv("CHUNKS_COLLECTION")

    if not collection_name:
        raise ValueError(
            "Milvus配置错误：CHUNKS_COLLECTION环境变量为空"
        )

    # 将每一个商品或设备名称加上双引号，
    # 拼接为Milvus支持的in过滤表达式。
    quoted_item_names = ", ".join(
        f'"{escape_milvus_literal(item_name)}"'
        for item_name in item_names
    )

    # 示例：
    # item_name in ["RS-12数字万用表"]
    expr = tenant_filter(
        str(state.get("tenant_id") or "local"),
        f"item_name in [{quoted_item_names}]",
    )

    logger.info(
        f"Milvus检索集合={collection_name}，过滤条件={expr}"
    )

    # 构造稠密向量和稀疏向量的混合检索请求。
    reqs = create_hybrid_search_requests(
        dense_vector=dense_vec,
        sparse_vector=sparse_vec,
        expr=expr,
        # 每一路向量检索先召回10条候选。
        limit=10
    )

    # ==================== 第三阶段：Milvus混合检索 ====================

    with start_rag_observation(
            as_type="retriever",
            name="milvus-hybrid-retrieval",
            input_data={
                "query": query,
                "item_names": item_names
            },
            metadata={
                "collection": collection_name,
                "filter": expr,
                "dense_weight": 0.8,
                "sparse_weight": 0.2,
                "candidate_limit": 10,
                "result_limit": 5,
                "normalization": True
            }
    ) as retrieval_observation:

        # 获取Milvus客户端。
        client = get_milvus_client()

        if client is None:
            raise RuntimeError("Milvus客户端初始化失败")

        # 执行混合检索。
        res = hybrid_search(
            client=client,
            collection_name=collection_name,
            reqs=reqs,

            # 稠密向量权重80%，稀疏向量权重20%。
            ranker_weights=(0.8, 0.2),

            # 先对两路分数归一化，再进行加权融合。
            norm_score=True,

            # 最终返回Top5。
            limit=5,

            # content需要交给后续RRF和Reranker使用。
            output_fields=[
                "chunk_id",
                "content",
                "item_name"
            ]
        )

        # Milvus批量检索结果的第一层对应当前第一条查询。
        hits = res[0] if res and len(res) > 0 else []

        # 将检索结果摘要写入Langfuse。
        if retrieval_observation is not None:
            retrieval_observation.update(
                output={
                    "hit_count": len(hits),
                    "hits": summarize_milvus_hits(hits)
                }
            )

    logger.info(
        f"Milvus混合检索完成，召回数量={len(hits)}"
    )

    # 标记节点执行完成。
    add_done_task(
        state["session_id"],
        node_name,
        state.get("is_stream")
    )

    return {
        "embedding_chunks": hits
    }


if __name__ == "__main__":
    """
    当前文件的本地测试入口。
    直接运行时，会使用模拟状态测试向量检索节点。
    """

    test_state = {
        "session_id": "test_search_embedding_001",
        "rewritten_query": "RS-12数字万用表如何测量直流电压",
        "item_names": ["RS-PRORS-12数字万用表"],
        "is_stream": False
    }

    try:
        result = node_search_embedding(test_state)

        chunks = result.get("embedding_chunks") or []

        logger.info(
            f"本地测试完成，检索结果数量={len(chunks)}"
        )

    except Exception as e:
        logger.exception(
            f"node_search_embedding本地测试失败：{e}"
        )
