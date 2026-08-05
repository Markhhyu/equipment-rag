import os
import sys
from typing import Iterable, Optional

from dotenv import find_dotenv, load_dotenv

from app.clients.milvus_utils import create_hybrid_search_requests, get_milvus_client, hybrid_search
from app.clients.document_registry_utils import filter_queryable_hits
from app.conf.rag_tuning_config import rag_tuning_config
from app.core.load_prompt import load_prompt
from app.core.logger import logger
from app.lm.embedding_utils import generate_embeddings
from app.lm.lm_utils import get_llm_client
from app.query_process.agent.nodes.node_search_embedding import QUERY_OUTPUT_FIELDS, build_query_filter
from app.utils.task_utils import add_done_task, add_running_task


load_dotenv(find_dotenv())


def step_1_create_hyde_doc(rewritten_query: str) -> str:
    """
    根据用户问题生成用于检索的假设性技术文档。

    HyDE文本只用于扩展向量检索语义，不直接作为最终答案，也不会覆盖真实设备手册内容。
    """
    query = str(rewritten_query or "").strip()
    if not query:
        raise ValueError("rewritten_query不能为空")

    logger.info(f"开始生成HyDE假设文档，问题长度={len(query)}")
    prompt = load_prompt("hyde_prompt", rewritten_query=query)
    response = get_llm_client().invoke(prompt)
    hyde_document = str(getattr(response, "content", "") or "").strip()
    if not hyde_document:
        raise ValueError("大模型返回的HyDE假设文档为空")

    logger.info(f"HyDE假设文档生成完成，字符数={len(hyde_document)}")
    logger.debug(f"HyDE假设文档预览：{hyde_document[:100]}")
    return hyde_document


def step_2_search_embedding_hyde(
    rewritten_query: str,
    hyde_doc: str,
    item_names: Optional[Iterable[str]] = None,
    revision_ids: Optional[Iterable[str]] = None,
    tenant_id: str = "local",
    req_limit: int = 10,
    top_k: int = 5,
    ranker_weights=(0.8, 0.2),
    norm_score: bool = True,
    output_fields=None,
):
    """
    使用“用户问题 + HyDE假设文档”执行Milvus混合检索。

    返回字段默认与普通BGE-M3检索完全一致，包含document_id和图片关联动态字段，
    确保RRF无论选中普通检索还是HyDE检索结果，都能继续执行图片推理。
    """
    query = str(rewritten_query or "").strip()
    hypothetical_document = str(hyde_doc or "").strip()
    if not query:
        raise ValueError("rewritten_query不能为空")
    if not hypothetical_document:
        raise ValueError("hyde_doc不能为空")

    combined_text = f"{query}\n{hypothetical_document}"
    logger.info(f"开始生成HyDE混合向量，组合文本字符数={len(combined_text)}")
    embeddings = generate_embeddings([combined_text])
    dense_vectors = embeddings.get("dense") or []
    sparse_vectors = embeddings.get("sparse") or []
    if not dense_vectors or not sparse_vectors:
        raise ValueError("HyDE向量化失败：dense或sparse结果为空")

    collection_name = os.getenv("CHUNKS_COLLECTION")
    if not collection_name:
        raise ValueError("Milvus配置错误：CHUNKS_COLLECTION环境变量为空")

    cleaned_item_names = [str(value).strip() for value in (item_names or []) if str(value).strip()]
    filter_expression = build_query_filter(
        str(tenant_id or "local"),
        cleaned_item_names,
        [str(value) for value in revision_ids or []],
    )
    fields = list(output_fields or QUERY_OUTPUT_FIELDS)
    logger.info(
        f"执行HyDE混合检索，集合={collection_name}，过滤条件={filter_expression}，"
        f"返回字段数={len(fields)}"
    )

    requests = create_hybrid_search_requests(
        dense_vector=dense_vectors[0],
        sparse_vector=sparse_vectors[0],
        expr=filter_expression,
        limit=req_limit,
    )
    client = get_milvus_client()
    if client is None:
        raise RuntimeError("Milvus客户端初始化失败")

    result = hybrid_search(
        client=client,
        collection_name=collection_name,
        reqs=requests,
        ranker_weights=ranker_weights,
        norm_score=norm_score,
        # 查询阶段先取较多候选，生命周期过滤后再截断。
        limit=max(req_limit, top_k),
        output_fields=fields,
    )
    if result and len(result) > 0:
        result[0] = filter_queryable_hits(str(tenant_id or "local"), result[0])[:top_k]
    hit_count = len(result[0]) if result and len(result) > 0 else 0
    logger.info(f"HyDE混合检索完成，召回数量={hit_count}")
    return result


def node_search_embedding_hyde(state):
    """
    执行HyDE假设文档生成和混合检索。

    HyDE生成失败属于可降级异常，只返回空HyDE结果，普通BGE-M3检索分支仍可继续完成问答。
    """
    logger.info("---node_search_embedding_hyde开始处理---")
    node_name = sys._getframe().f_code.co_name
    add_running_task(state["session_id"], node_name, state.get("is_stream"))

    try:
        query = (state.get("rewritten_query") or state.get("original_query") or "").strip()
        if not query:
            logger.warning("用户问题为空，跳过HyDE检索")
            return {"hyde_embedding_chunks": [], "hyde_doc": ""}

        item_names = state.get("item_names") or []
        logger.info(f"HyDE检索参数：query={query}，item_names={item_names}")

        try:
            hyde_document = step_1_create_hyde_doc(query)
        except Exception as exc:
            logger.error(f"HyDE假设文档生成失败，已降级为空结果：{exc}", exc_info=True)
            return {"hyde_embedding_chunks": [], "hyde_doc": ""}

        try:
            result = step_2_search_embedding_hyde(
                rewritten_query=query,
                hyde_doc=hyde_document,
                item_names=item_names,
                revision_ids=state.get("query_revision_ids") or [],
                tenant_id=str(state.get("tenant_id") or "local"),
                req_limit=rag_tuning_config.retrieval_candidate_limit,
                top_k=rag_tuning_config.retrieval_result_limit,
                ranker_weights=(rag_tuning_config.dense_weight, rag_tuning_config.sparse_weight),
                output_fields=QUERY_OUTPUT_FIELDS,
            )
            hits = result[0] if result else []
            return {"hyde_embedding_chunks": hits, "hyde_doc": hyde_document}
        except Exception as exc:
            logger.error(f"HyDE混合检索失败，已降级为空结果：{exc}", exc_info=True)
            return {"hyde_embedding_chunks": [], "hyde_doc": hyde_document}
    finally:
        add_done_task(state["session_id"], node_name, state.get("is_stream"))
        logger.info("---node_search_embedding_hyde处理结束---")


if __name__ == "__main__":
    test_state = {
        "session_id": "test_hyde_session_001",
        "tenant_id": "local",
        "original_query": "HAK 180烫金机怎么操作？",
        "rewritten_query": "HAK 180烫金机的具体操作步骤是什么？",
        "item_names": ["HAK 180烫金机"],
        "is_stream": False,
    }
    try:
        test_result = node_search_embedding_hyde(test_state)
        logger.info(
            f"HyDE本地测试完成，假设文档长度={len(test_result.get('hyde_doc') or '')}，"
            f"召回数量={len(test_result.get('hyde_embedding_chunks') or [])}"
        )
    except Exception as exc:
        logger.exception(f"node_search_embedding_hyde本地测试失败：{exc}")
