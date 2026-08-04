from __future__ import annotations

from dataclasses import asdict, dataclass
from statistics import fmean
from typing import Iterable

from app.evaluation.metrics import CaseMetrics, score_case
from app.evaluation.models import EvalCase, Prediction


DEFAULT_THRESHOLDS = {
    "case_pass_score": 0.85,
    "pass_rate": 0.8,
    "answer_present": 1.0,
    "keyword_coverage": 0.8,
    "forbidden_term_pass": 1.0,
    "clarification_pass": 1.0,
    "human_review_pass": 1.0,
    "retrieval_recall": 0.8,
    "retrieval_precision": 0.5,
    "retrieval_mrr": 0.8,
    "citation_pass": 0.8,
    "latency_pass": 0.8,
}


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    query: str
    answer: str
    metrics: CaseMetrics
    score: float
    passed: bool
    tags: list[str]
    trace_id: str | None


@dataclass(frozen=True)
class EvaluationReport:
    cases: list[CaseResult]
    summary: dict[str, float | int | None]
    thresholds: dict[str, float]
    threshold_failures: list[str]

    @property
    def passed(self) -> bool:
        return not self.threshold_failures

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "threshold_failures": self.threshold_failures,
            "thresholds": self.thresholds,
            "summary": self.summary,
            "cases": [
                {
                    **asdict(result),
                    "metrics": asdict(result.metrics),
                }
                for result in self.cases
            ],
        }


def _mean_measured(results: list[CaseResult], metric_name: str) -> float | None:
    values = [getattr(result.metrics, metric_name) for result in results]
    measured = [value for value in values if value is not None]
    return fmean(measured) if measured else None


def evaluate(
    cases: Iterable[EvalCase],
    predictions: Iterable[Prediction],
    thresholds: dict[str, float] | None = None,
) -> EvaluationReport:
    """按用例评分并执行总指标门禁，返回可供 CI 判断的评测报告。"""
    configured = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    prediction_map = {prediction.case_id: prediction for prediction in predictions}
    case_results: list[CaseResult] = []

    for case in cases:
        prediction = prediction_map.get(case.case_id)
        if prediction is None:
            # 缺失预测按空回答处理，使离线回放不会静默跳过失败用例。
            prediction = Prediction(case_id=case.case_id, answer="")

        metrics = score_case(case, prediction)
        measured_values = metrics.measured_values()
        score = fmean(measured_values) if measured_values else 0.0
        metric_thresholds = {
            name: configured[name]
            for name in (
                "answer_present",
                "keyword_coverage",
                "forbidden_term_pass",
                "clarification_pass",
                "human_review_pass",
                "retrieval_recall",
                "retrieval_precision",
                "retrieval_mrr",
                "citation_pass",
                "latency_pass",
            )
        }
        # 平均分达标还不够：每个已测量的关键指标也必须达到独立阈值。
        metric_gates_pass = all(
            value is None or value >= metric_thresholds[name] for name, value in asdict(metrics).items()
        )
        case_results.append(
            CaseResult(
                case_id=case.case_id,
                query=case.query,
                answer=prediction.answer,
                metrics=metrics,
                score=score,
                passed=score >= configured["case_pass_score"] and metric_gates_pass,
                tags=case.tags,
                trace_id=prediction.trace_id,
            )
        )

    total = len(case_results)
    passed_count = sum(result.passed for result in case_results)
    summary: dict[str, float | int | None] = {
        "total_cases": total,
        "passed_cases": passed_count,
        "pass_rate": _ratio(passed_count, total),
    }
    for metric_name in (
        "answer_present",
        "keyword_coverage",
        "forbidden_term_pass",
        "clarification_pass",
        "human_review_pass",
        "retrieval_recall",
        "retrieval_precision",
        "retrieval_mrr",
        "citation_pass",
        "latency_pass",
    ):
        summary[metric_name] = _mean_measured(case_results, metric_name)

    threshold_failures = []
    for metric_name, threshold in configured.items():
        if metric_name == "case_pass_score":
            continue
        measured = summary.get(metric_name)
        if measured is None:
            # 配置了阈值但没有产生数据时视为失败，避免评测能力失效后仍显示通过。
            threshold_failures.append(f"{metric_name}: not measured (required >= {threshold:.3f})")
        elif float(measured) < threshold:
            threshold_failures.append(f"{metric_name}: {float(measured):.3f} < {threshold:.3f}")

    return EvaluationReport(
        cases=case_results,
        summary=summary,
        thresholds=configured,
        threshold_failures=threshold_failures,
    )


def _ratio(matches: int, total: int) -> float:
    return 0.0 if total == 0 else matches / total
