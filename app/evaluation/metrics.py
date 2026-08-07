from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from app.evaluation.models import EvalCase, Prediction


_CITATION_PATTERN = re.compile(r"(?:https?://\S+|\[(?:\d+|source|chunk)[^\]]*\])", re.IGNORECASE)
_CLARIFICATION_TERMS = ("请提供", "请确认", "型号", "设备名称", "which model", "please provide", "please confirm")
_HUMAN_REVIEW_TERMS = ("人工复核", "安全负责人", "设备工程师确认", "现场评估", "human review")


def normalize_text(value: str) -> str:
    """统一大小写、全半角和多余空白，减少格式差异对评测的影响。"""
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def contains_term(text: str, term: str) -> bool:
    """Match exact normalized content while tolerating PDF layout whitespace."""
    normalized_text = normalize_text(text)
    normalized_term = normalize_text(term)
    return normalized_term in normalized_text or normalized_term.replace(" ", "") in normalized_text.replace(" ", "")


def _ratio(matches: int, total: int) -> float:
    return 1.0 if total == 0 else matches / total


def infer_clarification(answer: str) -> bool:
    normalized = normalize_text(answer)
    return any(normalize_text(term) in normalized for term in _CLARIFICATION_TERMS)


def infer_human_review(answer: str) -> bool:
    normalized = normalize_text(answer)
    return any(normalize_text(term) in normalized for term in _HUMAN_REVIEW_TERMS)


@dataclass(frozen=True)
class CaseMetrics:
    answer_present: float
    keyword_coverage: float
    forbidden_term_pass: float
    clarification_pass: float
    human_review_pass: float
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
                self.human_review_pass,
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
    required_matches = sum(contains_term(answer, term) for term in case.required_terms)
    forbidden_matches = sum(contains_term(answer, term) for term in case.forbidden_terms)

    clarified = prediction.clarified
    if clarified is None:
        clarified = infer_clarification(prediction.answer)
    requires_human_review = prediction.requires_human_review
    if requires_human_review is None:
        requires_human_review = infer_human_review(prediction.answer)

    retrieval_recall = None
    retrieval_precision = None
    retrieval_mrr = None
    expected_source_keys = case.expected_source_refs or case.expected_source_ids
    retrieved_source_keys = (
        prediction.retrieved_source_refs if case.expected_source_refs else prediction.retrieved_source_ids
    )
    if retrieved_source_keys is not None:
        # 真实手册优先使用document_id::version_label稳定引用；旧数据集继续兼容Chunk ID。
        expected = set(expected_source_keys)
        retrieved_list = retrieved_source_keys
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
        expected_id_is_cited = any(contains_term(answer, source_id) for source_id in expected_source_keys)
        citation_pass = float(expected_id_is_cited or bool(_CITATION_PATTERN.search(prediction.answer)))

    latency_pass = None
    if case.max_latency_ms is not None and prediction.latency_ms is not None:
        latency_pass = float(prediction.latency_ms <= case.max_latency_ms)

    return CaseMetrics(
        answer_present=float(bool(answer)),
        keyword_coverage=_ratio(required_matches, len(case.required_terms)),
        forbidden_term_pass=float(forbidden_matches == 0),
        clarification_pass=float(clarified == case.must_clarify),
        human_review_pass=float(requires_human_review == case.must_review),
        retrieval_recall=retrieval_recall,
        retrieval_precision=retrieval_precision,
        retrieval_mrr=retrieval_mrr,
        citation_pass=citation_pass,
        latency_pass=latency_pass,
    )
