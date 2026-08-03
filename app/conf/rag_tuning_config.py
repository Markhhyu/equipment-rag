import os
from dataclasses import asdict, dataclass

from dotenv import find_dotenv, load_dotenv


load_dotenv(find_dotenv())


def _get_int(name: str, default: int, *, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _get_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class RagTuningConfig:
    """影响解析、召回、融合和重排效果的集中配置。

    初学者需要特别注意：这些参数不是越大越好。候选数和TopK变大可能提高召回，
    同时也会增加延迟、Token和噪声。任何修改都应使用相同黄金数据集做前后对比。
    """

    chunk_max_chars: int
    chunk_min_chars: int
    retrieval_candidate_limit: int
    retrieval_result_limit: int
    dense_weight: float
    sparse_weight: float
    rrf_k: int
    rrf_max_results: int
    rrf_embedding_weight: float
    rrf_hyde_weight: float
    rerank_max_topk: int
    rerank_min_topk: int
    rerank_gap_ratio: float
    rerank_gap_abs: float

    def to_dict(self) -> dict:
        return asdict(self)


def load_rag_tuning_config() -> RagTuningConfig:
    """读取并校验环境变量。

    配置写错时使用安全默认值；有上下限关系的字段在这里统一校正，
    避免业务节点各自实现不同的兜底规则。
    """

    dense_weight = _get_float("RAG_DENSE_WEIGHT", 0.8)
    sparse_weight = _get_float("RAG_SPARSE_WEIGHT", 0.2)
    if dense_weight + sparse_weight <= 0:
        dense_weight, sparse_weight = 0.8, 0.2

    rerank_max = _get_int("RAG_RERANK_MAX_TOPK", 10)
    rerank_min = min(_get_int("RAG_RERANK_MIN_TOPK", 1), rerank_max)
    chunk_max = _get_int("RAG_CHUNK_MAX_CHARS", 2000, minimum=100)
    chunk_min = min(_get_int("RAG_CHUNK_MIN_CHARS", 500, minimum=1), chunk_max)

    candidate_limit = _get_int("RAG_RETRIEVAL_CANDIDATE_LIMIT", 10)
    # 最终结果数不能超过每一路最初召回的候选数。
    result_limit = min(_get_int("RAG_RETRIEVAL_RESULT_LIMIT", 5), candidate_limit)

    return RagTuningConfig(
        chunk_max_chars=chunk_max,
        chunk_min_chars=chunk_min,
        retrieval_candidate_limit=candidate_limit,
        retrieval_result_limit=result_limit,
        dense_weight=dense_weight,
        sparse_weight=sparse_weight,
        rrf_k=_get_int("RAG_RRF_K", 60),
        rrf_max_results=_get_int("RAG_RRF_MAX_RESULTS", 10),
        rrf_embedding_weight=_get_float("RAG_RRF_EMBEDDING_WEIGHT", 1.0),
        rrf_hyde_weight=_get_float("RAG_RRF_HYDE_WEIGHT", 1.0),
        rerank_max_topk=rerank_max,
        rerank_min_topk=rerank_min,
        rerank_gap_ratio=_get_float("RAG_RERANK_GAP_RATIO", 0.25),
        rerank_gap_abs=_get_float("RAG_RERANK_GAP_ABS", 0.5),
    )


rag_tuning_config = load_rag_tuning_config()
