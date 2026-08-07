"""RAG retrieval and ranking tuning configuration."""

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


def _get_score(name: str, default: float) -> float:
    """读取0到1之间的分数配置；越界或格式错误时自动使用安全范围。"""
    try:
        return min(1.0, max(0.0, float(os.getenv(name, str(default)))))
    except (TypeError, ValueError):
        return default


def _get_choice(name: str, default: str, allowed: set[str]) -> str:
    value = str(os.getenv(name, default)).strip().casefold()
    return value if value in allowed else default


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
    hyde_mode: str
    web_search_mode: str
    rerank_max_topk: int
    rerank_min_topk: int
    rerank_gap_ratio: float
    rerank_gap_abs: float
    # 商品名称向量对齐阈值。明确型号token一致时不依赖这些分数，
    # 这些值只控制中文名称等“没有明确型号代码”的语义匹配。
    item_name_auto_confirm_score: float
    item_name_auto_confirm_margin: float
    item_name_candidate_score: float

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
    # 参数、规格和安全步骤经常分布在相邻切片中，至少保留两条证据，避免单片段缺口诱发补造。
    rerank_min = min(_get_int("RAG_RERANK_MIN_TOPK", 2), rerank_max)
    chunk_max = _get_int("RAG_CHUNK_MAX_CHARS", 2000, minimum=100)
    chunk_min = min(_get_int("RAG_CHUNK_MIN_CHARS", 500, minimum=1), chunk_max)

    candidate_limit = _get_int("RAG_RETRIEVAL_CANDIDATE_LIMIT", 10)
    # 最终结果数不能超过每一路最初召回的候选数。
    result_limit = min(_get_int("RAG_RETRIEVAL_RESULT_LIMIT", 5), candidate_limit)

    # 候选阈值不能高于自动确认阈值，否则会出现“低分自动确认、高分反而要求澄清”的反直觉区间。
    item_name_auto_confirm_score = _get_score("RAG_ITEM_NAME_AUTO_CONFIRM_SCORE", 0.90)
    item_name_candidate_score = min(
        _get_score("RAG_ITEM_NAME_CANDIDATE_SCORE", 0.78),
        item_name_auto_confirm_score,
    )

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
        hyde_mode=_get_choice("RAG_HYDE_MODE", "adaptive", {"adaptive", "always", "disabled"}),
        web_search_mode=_get_choice("RAG_WEB_SEARCH_MODE", "explicit", {"explicit", "always", "disabled"}),
        rerank_max_topk=rerank_max,
        rerank_min_topk=rerank_min,
        rerank_gap_ratio=_get_float("RAG_RERANK_GAP_RATIO", 0.25),
        rerank_gap_abs=_get_float("RAG_RERANK_GAP_ABS", 0.5),
        item_name_auto_confirm_score=item_name_auto_confirm_score,
        item_name_auto_confirm_margin=_get_score("RAG_ITEM_NAME_AUTO_CONFIRM_MARGIN", 0.08),
        item_name_candidate_score=item_name_candidate_score,
    )


rag_tuning_config = load_rag_tuning_config()
