import json
from pathlib import Path

import pytest

from app.evaluation.io import load_cases, render_markdown
from app.evaluation.metrics import contains_term, infer_clarification, score_case
from app.evaluation.models import EvalCase, Prediction, build_source_ref
from app.evaluation.providers import QueryApiProvider
from app.evaluation.runner import evaluate


def make_case(**overrides):
    data = {
        "id": "case-1",
        "query": "设备如何检查？",
        "required_terms": ["压力", "阀门"],
        "forbidden_terms": ["绕过"],
        "expected_source_ids": ["manual-1"],
        "require_citation": True,
        "max_latency_ms": 1000,
    }
    data.update(overrides)
    return EvalCase.from_dict(data)


def test_score_case_covers_grounding_safety_and_latency():
    case = make_case()
    prediction = Prediction.from_dict(
        {
            "id": "case-1",
            "answer": "检查压力和阀门。[chunk:manual-1]",
            "retrieved_source_ids": ["manual-1", "manual-2"],
            "latency_ms": 500,
            "clarified": False,
        }
    )

    metrics = score_case(case, prediction)

    assert metrics.answer_present == 1.0
    assert metrics.keyword_coverage == 1.0
    assert metrics.forbidden_term_pass == 1.0
    assert metrics.retrieval_recall == 1.0
    assert metrics.retrieval_precision == 0.5
    assert metrics.retrieval_mrr == 1.0
    assert metrics.citation_pass == 1.0
    assert metrics.latency_pass == 1.0


def test_missing_optional_runtime_metadata_is_not_faked():
    metrics = score_case(make_case(), Prediction(case_id="case-1", answer="检查压力和阀门。[1]"))

    assert metrics.retrieval_recall is None
    assert metrics.retrieval_precision is None
    assert metrics.retrieval_mrr is None
    assert metrics.latency_pass is None


def test_human_review_policy_is_scored():
    case = make_case(must_review=True)
    prediction = Prediction(
        case_id="case-1",
        answer="当前证据不足，请由设备工程师确认。",
        requires_human_review=True,
    )

    metrics = score_case(case, prediction)

    assert metrics.human_review_pass == 1.0


def test_critical_metric_failure_fails_case_even_when_average_is_high():
    case = make_case()
    prediction = Prediction(
        case_id="case-1",
        answer="建议绕过限制，同时检查压力和阀门。[manual-1]",
        retrieved_source_ids=["manual-1"],
        latency_ms=100,
    )

    report = evaluate([case], [prediction])

    assert report.cases[0].score > 0.85
    assert report.cases[0].passed is False


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("请提供设备型号。", True),
        ("Please confirm which model is installed.", True),
        ("按下绿色复位按钮。", False),
    ],
)
def test_infer_clarification(answer, expected):
    assert infer_clarification(answer) is expected


def test_evaluate_reports_missing_predictions_and_threshold_failures():
    cases = [make_case(), make_case(id="case-2")]
    predictions = [
        Prediction(
            case_id="case-1",
            answer="检查压力和阀门。[manual-1]",
            retrieved_source_ids=["manual-1"],
            latency_ms=100,
        )
    ]

    report = evaluate(cases, predictions, {"pass_rate": 1.0})

    assert report.passed is False
    assert report.summary["total_cases"] == 2
    assert report.summary["passed_cases"] == 1
    assert any(failure.startswith("pass_rate:") for failure in report.threshold_failures)
    assert "Evaluation report: FAIL" in render_markdown(report)
    json.dumps(report.to_dict())


def test_invalid_case_contract_is_rejected():
    with pytest.raises(ValueError, match="non-empty id"):
        EvalCase.from_dict({"query": "missing id"})


def test_stable_source_refs_take_precedence_over_runtime_chunk_ids():
    case = make_case(
        expected_source_ids=["old-chunk-id"],
        expected_source_refs=["manual-rs12::v170801"],
    )
    prediction = Prediction(
        case_id="case-1",
        answer="检查压力和阀门。[1]",
        retrieved_source_ids=["new-random-chunk-id"],
        retrieved_source_refs=["manual-rs12::v170801"],
        latency_ms=100,
    )

    metrics = score_case(case, prediction)

    assert metrics.retrieval_recall == 1.0
    assert metrics.retrieval_precision == 1.0
    assert metrics.retrieval_mrr == 1.0


def test_query_api_provider_builds_deduplicated_source_refs(monkeypatch):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "answer": "依据手册回答。[1]",
                "retrieved_source_ids": ["chunk-1", "chunk-2"],
                "sources": [
                    {"document_id": "manual-z26", "version_label": "v02.30-zhixiang"},
                    {"document_id": "manual-z26", "version_label": "v02.30-zhixiang"},
                ],
            }

    monkeypatch.setattr("app.evaluation.providers.requests.post", lambda *args, **kwargs: Response())

    prediction = QueryApiProvider("http://query-api").predict(make_case())

    assert prediction.retrieved_source_refs == ["manual-z26::v02.30-zhixiang"]


def test_real_manual_dataset_uses_stable_version_refs():
    cases = load_cases(Path("evals/datasets/manuals.jsonl"))

    assert len(cases) >= 6
    assert all(case.expected_source_refs for case in cases)
    assert all("::" in source_ref for case in cases for source_ref in case.expected_source_refs)
    assert build_source_ref("manual-rs12", "v170801") == "manual-rs12::v170801"


def test_keyword_matching_tolerates_pdf_layout_whitespace_only():
    assert contains_term("使用 0.2 A / 250 V 快速熔断保险丝", "0.2A/250V") is True
    assert contains_term("打印速度为每分钟26页", "每分钟 26 页") is True
    assert contains_term("打印速度为每分钟20页", "每分钟 26 页") is False
