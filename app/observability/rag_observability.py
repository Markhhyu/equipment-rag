from contextlib import contextmanager
from typing import Any, Iterator, Optional

from app.observability.langfuse_monitor import (
    LANGFUSE_ENABLED,
    langfuse
)


def _get_value(
        data: Any,
        key: str,
        default: Any = None
) -> Any:
    """
    从字典或对象中读取字段。

    Milvus返回结果有时是dict，有时是Hit对象。
    通过这个方法统一读取，避免业务代码到处判断类型。

    :param data: 字典或普通对象。
    :param key: 需要读取的字段名。
    :param default: 字段不存在时返回的默认值。
    :return: 对应字段值。
    """

    # 字典使用get读取。
    if isinstance(data, dict):
        return data.get(key, default)

    # 普通对象使用getattr读取。
    return getattr(data, key, default)


@contextmanager
def start_rag_observation(
        *,
        as_type: str,
        name: str,
        input_data: Optional[Any] = None,
        metadata: Optional[dict] = None,
        model: Optional[str] = None
) -> Iterator[Optional[Any]]:
    """
    安全创建一个RAG业务Observation。

    当Langfuse被关闭时，该方法会退化为空上下文，
    不影响BGE、Milvus、Reranker等正常业务执行。

    :param as_type:
        Observation类型，例如：
        embedding、retriever、span、tool。
    :param name: Observation名称。
    :param input_data: 本阶段输入数据。
    :param metadata: 附加业务参数。
    :param model: 使用的模型名称。
    :return: 当前Observation；监控关闭时返回None。
    """

    # 监控关闭时，不创建Observation。
    if not LANGFUSE_ENABLED or langfuse is None:
        yield None
        return

    # 创建自定义Observation。
    # 如果with代码块内部出现异常，Langfuse会自动记录异常状态。
    with langfuse.start_as_current_observation(
            as_type=as_type,
            name=name,
            input=input_data,
            metadata=metadata,
            model=model
    ) as observation:
        yield observation


def summarize_milvus_hits(
        hits: list,
        limit: int = 10
) -> list:
    """
    将Milvus检索结果转换为适合上传到Langfuse的摘要。

    注意：
    1. 不上传完整向量；
    2. 不上传完整设备手册正文；
    3. 只上传Chunk ID、设备名称、排名和分数。

    :param hits: Milvus返回的检索结果。
    :param limit: 最多记录多少条结果。
    :return: 精简后的检索结果列表。
    """

    result = []

    # 只记录指定数量，防止Trace数据量过大。
    for rank, hit in enumerate((hits or [])[:limit], start=1):

        # Milvus的业务字段通常位于entity中。
        entity = _get_value(hit, "entity", {}) or {}

        # distance在当前Milvus代码中代表融合后的相关性分数。
        raw_score = _get_value(hit, "distance", 0.0) or 0.0

        result.append({
            "rank": rank,
            "chunk_id": (
                _get_value(entity, "chunk_id")
                or _get_value(hit, "id")
            ),
            "item_name": _get_value(
                entity,
                "item_name",
                ""
            ),
            # 转为Python原生float，避免numpy类型无法JSON序列化。
            "score": float(raw_score)
        })

    return result


def summarize_rerank_docs(
        docs: list,
        limit: int = 10
) -> list:
    """
    将BGE Reranker结果转换为监控摘要。

    不记录完整正文，只记录：
    1. 排名；
    2. Chunk ID；
    3. 标题；
    4. 来源；
    5. Reranker分数。

    :param docs: 已完成重排的文档。
    :param limit: 最多记录多少条。
    :return: 精简后的重排结果。
    """

    result = []

    for rank, doc in enumerate((docs or [])[:limit], start=1):
        result.append({
            "rank": rank,
            "chunk_id": doc.get("chunk_id"),
            "title": doc.get("title", ""),
            "source": doc.get("source", ""),
            "score": float(doc.get("score", 0.0) or 0.0)
        })

    return result


def score_query_result(final_state: dict) -> None:
    """
    为当前问答Trace增加第一版确定性评分。

    当前只做不依赖大模型的基础判断：
    1. retrieval_hit：是否检索到参考文档；
    2. answer_generated：是否生成了非空答案；
    3. rerank_top1_raw_score：Top1重排原始分数。

    注意：
    这些指标不能证明答案一定正确，
    只是用于快速发现“没有检索结果”或“没有生成答案”等明显异常。

    :param final_state: LangGraph执行结束后的完整状态。
    """

    # 监控未启用时，不创建Score。
    if not LANGFUSE_ENABLED or langfuse is None:
        return

    # 获取当前正在执行的Trace ID。
    # 此方法必须在trace_query上下文内部调用。
    trace_id = langfuse.get_current_trace_id()

    # 当前上下文不存在Trace时直接退出，避免出现孤立Score。
    if not trace_id:
        return

    # 获取最终回答。
    answer = (final_state.get("answer") or "").strip()

    # 获取最终进入Prompt的重排文档。
    reranked_docs = final_state.get("reranked_docs") or []

    # 评分1：是否成功检索到参考文档。
    langfuse.create_score(
        trace_id=trace_id,
        name="retrieval_hit",
        value=1.0 if reranked_docs else 0.0,
        data_type="BOOLEAN",
        comment="重排完成后是否存在可用于回答的参考文档"
    )

    # 评分2：是否成功生成非空答案。
    langfuse.create_score(
        trace_id=trace_id,
        name="answer_generated",
        value=1.0 if answer else 0.0,
        data_type="BOOLEAN",
        comment="本轮问答是否成功生成非空答案"
    )

    # 只有存在重排文档时，才记录Top1分数。
    if reranked_docs:
        top1_score = float(
            reranked_docs[0].get("score", 0.0) or 0.0
        )

        langfuse.create_score(
            trace_id=trace_id,
            name="rerank_top1_raw_score",
            value=top1_score,
            data_type="NUMERIC",
            comment="BGE Reranker返回的Top1原始相关性分数"
        )