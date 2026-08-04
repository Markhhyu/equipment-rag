import os
import sys

from dotenv import find_dotenv, load_dotenv

from app.clients.milvus_utils import create_hybrid_search_requests, get_milvus_client, hybrid_search
from app.clients.document_registry_utils import filter_queryable_hits
from app.conf.rag_tuning_config import rag_tuning_config
from app.core.logger import logger
from app.lm.embedding_utils import generate_embeddings
from app.observability.rag_observability import start_rag_observation, summarize_milvus_hits
from app.security.tenancy import escape_milvus_literal, tenant_filter
from app.utils.task_utils import add_done_task, add_running_task


load_dotenv(find_dotenv())

# 查询阶段除了正文，还必须取回文档和图片动态字段。
# 这些字段由导入链路的node_attach_image_metadata写入Milvus，用于重排后精确选择当前Chunk关联的图片。
QUERY_OUTPUT_FIELDS = [
    "chunk_id",
    "content",
    "title",
    "parent_title",
    "part",
    "file_title",
    "item_name",
    "document_id",
    "revision_id",
    "version_label",
    "trust_level",
    "device_model",
    "equipment_version",
    "software_version",
    "firmware_version",
    "hardware_revision",
    "site_id",
    "asset_ids",
    "page_numbers",
    "page_start",
    "page_end",
    "governance_managed",
    "has_images",
    "image_ids",
    "image_object_uris",
    "image_page_numbers",
]


def node_search_embedding(state):
    """
    使用BGE-M3和Milvus执行稠密向量、稀疏向量混合检索。

    该节点仍按设备名称执行租户内精准过滤，但返回结果除正文外，还保留文档编号和图片关联字段。
    后续RRF和Reranker必须原样传递这些元数据，图片推理节点才能只分析命中Chunk中的图片。
    """
    logger.info("---node_search_embedding开始处理---")
    node_name = sys._getframe().f_code.co_name
    add_running_task(state["session_id"], node_name, state.get("is_stream"))

    try:
        query = (state.get("rewritten_query") or state.get("original_query") or "").strip()
        item_names = [str(value).strip() for value in (state.get("item_names") or []) if str(value).strip()]
        logger.info(f"向量检索参数：query={query}，item_names={item_names}")

        if not query:
            logger.warning("用户问题和改写问题均为空，跳过Milvus向量检索")
            return {"embedding_chunks": []}
        if not item_names:
            logger.warning("item_names为空，当前精准检索策略无法构建设备过滤条件，跳过Milvus向量检索")
            return {"embedding_chunks": []}

        # 第一阶段：生成BGE-M3稠密和稀疏向量。
        with start_rag_observation(
            as_type="embedding",
            name="bge-m3-query-embedding",
            input_data={"query": query},
            metadata={"input_count": 1, "usage": "query-embedding"},
            model=os.getenv("BGE_M3") or "bge-m3",
        ) as embedding_observation:
            embeddings = generate_embeddings([query])
            dense_vectors = embeddings.get("dense") or []
            sparse_vectors = embeddings.get("sparse") or []
            if not dense_vectors or not sparse_vectors:
                raise ValueError("BGE-M3向量化失败：dense或sparse结果为空")

            dense_vector = dense_vectors[0]
            sparse_vector = sparse_vectors[0]
            if embedding_observation is not None:
                embedding_observation.update(
                    output={
                        "dense_dimension": len(dense_vector),
                        "sparse_nonzero_count": len(sparse_vector),
                    }
                )

        logger.info(
            f"BGE-M3向量生成完成，稠密向量维度={len(dense_vector)}，"
            f"稀疏向量非零维度={len(sparse_vector)}"
        )

        # 第二阶段：构建租户和设备名称过滤条件。
        collection_name = os.getenv("CHUNKS_COLLECTION")
        if not collection_name:
            raise ValueError("Milvus配置错误：CHUNKS_COLLECTION环境变量为空")

        quoted_item_names = ", ".join(
            f'"{escape_milvus_literal(item_name)}"'
            for item_name in item_names
        )
        filter_expression = tenant_filter(
            str(state.get("tenant_id") or "local"),
            f"item_name in [{quoted_item_names}]",
        )
        logger.info(f"Milvus检索集合={collection_name}，过滤条件={filter_expression}")

        requests = create_hybrid_search_requests(
            dense_vector=dense_vector,
            sparse_vector=sparse_vector,
            expr=filter_expression,
            limit=rag_tuning_config.retrieval_candidate_limit,
        )

        # 第三阶段：执行混合检索，并取回图片关联动态字段。
        with start_rag_observation(
            as_type="retriever",
            name="milvus-hybrid-retrieval",
            input_data={"query": query, "item_names": item_names},
            metadata={
                "collection": collection_name,
                "filter": filter_expression,
                "dense_weight": rag_tuning_config.dense_weight,
                "sparse_weight": rag_tuning_config.sparse_weight,
                "candidate_limit": rag_tuning_config.retrieval_candidate_limit,
                "result_limit": rag_tuning_config.retrieval_result_limit,
                "output_fields": QUERY_OUTPUT_FIELDS,
                "normalization": True,
            },
        ) as retrieval_observation:
            client = get_milvus_client()
            if client is None:
                raise RuntimeError("Milvus客户端初始化失败")

            result = hybrid_search(
                client=client,
                collection_name=collection_name,
                reqs=requests,
                ranker_weights=(rag_tuning_config.dense_weight, rag_tuning_config.sparse_weight),
                norm_score=True,
                # 先多取候选，再过滤停用/归档版本，避免失效版本挤占最终TopK。
                limit=rag_tuning_config.retrieval_candidate_limit,
                output_fields=QUERY_OUTPUT_FIELDS,
            )
            raw_hits = result[0] if result and len(result) > 0 else []
            hits = filter_queryable_hits(str(state.get("tenant_id") or "local"), raw_hits)
            hits = hits[: rag_tuning_config.retrieval_result_limit]

            if retrieval_observation is not None:
                retrieval_observation.update(
                    output={
                        "hit_count": len(hits),
                        "filtered_inactive_count": max(0, len(raw_hits) - len(hits)),
                        "hits": summarize_milvus_hits(hits),
                        "image_metadata_hit_count": sum(
                            1
                            for hit in hits
                            if bool(
                                getattr(hit, "entity", {}).get("has_images")
                                if hasattr(getattr(hit, "entity", None), "get")
                                else False
                            )
                        ),
                    }
                )

        logger.info(f"Milvus混合检索完成，召回数量={len(hits)}")
        return {"embedding_chunks": hits}
    finally:
        add_done_task(state["session_id"], node_name, state.get("is_stream"))


if __name__ == "__main__":
    test_state = {
        "session_id": "test_search_embedding_001",
        "tenant_id": "local",
        "rewritten_query": "RS-12数字万用表如何测量直流电压",
        "item_names": ["RS-PRORS-12数字万用表"],
        "is_stream": False,
    }
    try:
        test_result = node_search_embedding(test_state)
        logger.info(f"本地测试完成，检索结果数量={len(test_result.get('embedding_chunks') or [])}")
    except Exception as exc:
        logger.exception(f"node_search_embedding本地测试失败：{exc}")
