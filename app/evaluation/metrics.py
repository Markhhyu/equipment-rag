from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from app.evaluation.models import EvalCase, Prediction


_CITATION_PATTERN = re.compile(r"(?:https?://\S+|\[(?:\d+|source|chunk)[^\]]*\])", re.IGNORECASE)
_CLARIFICATION_TERMS = ("请提供", "请确认", "型号", "设备名称", "which model", "please provide", "please confirm")


def normalize_text(value: str) -> str:
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
                self.citation_pass,
                self.latency_pass,
            )
            if value is not None
        ]


def score_case(case: EvalCase, prediction: Prediction) -> CaseMetrics:
    answer = normalize_text(prediction.answer)
    required_matches = sum(normalize_text(term) in answer for term in case.required_terms)
    forbidden_matches = sum(normalize_text(term) in answer for term in case.forbidden_terms)

    clarified = prediction.clarified
    if clarified is None:
        clarified = infer_clarification(prediction.answer)

    retrieval_recall = None
    if prediction.retrieved_source_ids is not None:
        expected = set(case.expected_source_ids)
        retrieved = set(prediction.retrieved_source_ids)
        retrieval_recall = _ratio(len(expected & retrieved), len(expected))

    citation_pass = 1.0
    if case.require_citation:
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
        citation_pass=citation_pass,
        latency_pass=latency_pass,
    )
