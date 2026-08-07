from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _string_list(value: Any, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ValueError(f"{field_name} must be a list of non-empty strings")
    return [item.strip() for item in value]


def build_source_ref(document_id: Any, version_label: Any = "") -> str:
    """Build a stable evaluation key that survives chunk and revision regeneration."""
    document = str(document_id or "").strip()
    version = str(version_label or "").strip()
    if not document:
        return ""
    return f"{document}::{version}" if version else document


@dataclass(frozen=True)
class EvalCase:
    case_id: str
    query: str
    required_terms: list[str] = field(default_factory=list)
    forbidden_terms: list[str] = field(default_factory=list)
    expected_source_ids: list[str] = field(default_factory=list)
    expected_source_refs: list[str] = field(default_factory=list)
    must_clarify: bool = False
    must_review: bool = False
    require_citation: bool = False
    max_latency_ms: float | None = None
    tags: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EvalCase:
        case_id = str(data.get("id") or "").strip()
        query = str(data.get("query") or "").strip()
        if not case_id:
            raise ValueError("evaluation case requires a non-empty id")
        if not query:
            raise ValueError(f"evaluation case {case_id!r} requires a non-empty query")

        max_latency_ms = data.get("max_latency_ms")
        if max_latency_ms is not None:
            max_latency_ms = float(max_latency_ms)
            if max_latency_ms <= 0:
                raise ValueError(f"evaluation case {case_id!r} max_latency_ms must be positive")

        return cls(
            case_id=case_id,
            query=query,
            required_terms=_string_list(data.get("required_terms"), "required_terms"),
            forbidden_terms=_string_list(data.get("forbidden_terms"), "forbidden_terms"),
            expected_source_ids=_string_list(data.get("expected_source_ids"), "expected_source_ids"),
            expected_source_refs=_string_list(data.get("expected_source_refs"), "expected_source_refs"),
            must_clarify=bool(data.get("must_clarify", False)),
            must_review=bool(data.get("must_review", False)),
            require_citation=bool(data.get("require_citation", False)),
            max_latency_ms=max_latency_ms,
            tags=_string_list(data.get("tags"), "tags"),
        )


@dataclass(frozen=True)
class Prediction:
    case_id: str
    answer: str
    latency_ms: float | None = None
    retrieved_source_ids: list[str] | None = None
    retrieved_source_refs: list[str] | None = None
    clarified: bool | None = None
    requires_human_review: bool | None = None
    trace_id: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Prediction:
        case_id = str(data.get("id") or data.get("case_id") or "").strip()
        if not case_id:
            raise ValueError("prediction requires a non-empty id or case_id")

        latency_ms = data.get("latency_ms")
        if latency_ms is not None:
            latency_ms = float(latency_ms)
            if latency_ms < 0:
                raise ValueError(f"prediction {case_id!r} latency_ms cannot be negative")

        source_ids = data.get("retrieved_source_ids")
        if source_ids is not None:
            source_ids = _string_list(source_ids, "retrieved_source_ids")

        source_refs = data.get("retrieved_source_refs")
        if source_refs is not None:
            source_refs = _string_list(source_refs, "retrieved_source_refs")

        clarified = data.get("clarified")
        if clarified is not None:
            clarified = bool(clarified)

        requires_human_review = data.get("requires_human_review")
        if requires_human_review is not None:
            requires_human_review = bool(requires_human_review)

        return cls(
            case_id=case_id,
            answer=str(data.get("answer") or ""),
            latency_ms=latency_ms,
            retrieved_source_ids=source_ids,
            retrieved_source_refs=source_refs,
            clarified=clarified,
            requires_human_review=requires_human_review,
            trace_id=str(data.get("trace_id") or "") or None,
        )
