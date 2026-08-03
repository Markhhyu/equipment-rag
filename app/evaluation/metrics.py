from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from app.evaluation.models import EvalCase, Prediction


_CITATION_PATTERN = re.compile(r"(?:https?://\S+|\[(?:\d+|source|chunk)[^\]]*\])", re.IGNORECASE)
_CLARIFICATION_TERMS = ("请提供", "请确认", "型号", "设备名称", "which model", "please provide", "please confirm")


def normalize_text(value: str) -> str:
    """统一大小写、全半角和多余空白，减少格式差异对评测的影响。"""
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _ratio(matches: int, total: int) -> float:
    return 1.0 if total == 0 else matches / total


def infer_clarification(answer: str) -> bool:
    normalized = normalize_text(answer)
    return any(normalize_text(term) in normalized for term in _CLARIFICATION_TERMS)


@dataclass(frozen=True)
class CaseMetrics:
    answer_present: float
    keyword_coverage: float
    forbidden_term_pass: float
    clarification_pass: float
    retrieval_recall: float | None
    retrieval_precision: float | None
    retrieval_mrr: float | None
    citation_pass: float
    latency_pass: float | None

    def measured_values(self) -> list[float]:
        return [
            value
            for value in (
                self.answer_present,
                self.keyword_coverage,
                self.forbidden_term_pass,
                self.clarification_pass,
                self.retrieval_recall,
                self.retrieval_precision,
                self.retrieval_mrr,
                self.citation_pass,
                self.latency_pass,
            )
            if value is not None
        ]


def score_case(case: EvalCase, prediction: Prediction) -> CaseMetrics:
    """用可重复计算的规则评估单条回答，不调用大模型担任裁判。"""
    answer = normalize_text(prediction.answer)
    required_matches = sum(normalize_text(term) in answer for term in case.required_terms)
    forbidden_matches = sum(normalize_text(term) in answer for term in case.forbidden_terms)

    clarified = prediction.clarified
    if clarified is None:
        clarified = infer_clarification(prediction.answer)

    retrieval_recall = None
    retrieval_precision = None
    retrieval_mrr = None
    if prediction.retrieved_source_ids is not None:
        # 只有API真正返回了召回文档ID时才计算检索指标，绝不使用答案文本伪造召回结果。
        expected = set(case.expected_source_ids)
        retrieved_list = prediction.retrieved_source_ids
        retrieved = set(retrieved_list)
        matches = len(expected & retrieved)
        retrieval_recall = _ratio(matches, len(expected))
        retrieval_precision = _ratio(matches, len(retrieved)) if retrieved else float(not expected)

        # MRR关注“第一条正确来源排在第几位”。正确Chunk越靠前，Reranker越容易构建干净上下文。
        retrieval_mrr = 0.0
        if not expected:
            retrieval_mrr = float(not retrieved_list)
        else:
            for rank, source_id in enumerate(retrieved_list, start=1):
                if source_id in expected:
                    retrieval_mrr = 1.0 / rank
                    break

    citation_pass = 1.0
    if case.require_citation:
        # 引用既可以是期望来源 ID，也可以是可识别的 URL 或引用标记。
        expected_id_is_cited = any(normalize_text(source_id) in answer for source_id in case.expected_source_ids)
        citation_pass = float(expected_id_is_cited or bool(_CITATION_PATTERN.search(prediction.answer)))

    latency_pass = None
    if case.max_latency_ms is not None and prediction.latency_ms is not None:
        latency_pass = float(prediction.latency_ms <= case.max_latency_ms)

    return CaseMetrics(
        answer_present=float(bool(answer)),
        keyword_coverage=_ratio(required_matches, len(case.required_terms)),
        forbidden_term_pass=float(forbidden_matches == 0),
        clarification_pass=float(clarified == case.must_clarify),
        retrieval_recall=retrieval_recall,
        retrieval_precision=retrieval_precision,
        retrieval_mrr=retrieval_mrr,
        citation_pass=citation_pass,
        latency_pass=latency_pass,
    )
