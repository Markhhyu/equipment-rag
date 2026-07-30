from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Callable, TypeVar

from app.evaluation.models import EvalCase, Prediction
from app.evaluation.runner import EvaluationReport


T = TypeVar("T")


def load_jsonl(path: Path, factory: Callable[[dict], T]) -> list[T]:
    records = []
    with path.open("r", encoding="utf-8") as source:
        for line_number, raw_line in enumerate(source, start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                data = json.loads(line)
                records.append(factory(data))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError(f"{path}:{line_number}: {exc}") from exc
    return records


def load_cases(path: Path) -> list[EvalCase]:
    return load_jsonl(path, EvalCase.from_dict)


def load_predictions(path: Path) -> list[Prediction]:
    return load_jsonl(path, Prediction.from_dict)


def load_thresholds(path: Path) -> dict[str, float]:
    with path.open("rb") as source:
        data = tomllib.load(source)
    thresholds = data.get("thresholds", {})
    if not isinstance(thresholds, dict):
        raise ValueError(f"{path}: [thresholds] must be a table")
    return {str(name): float(value) for name, value in thresholds.items()}


def write_report(report: EvaluationReport, output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "report.json"
    markdown_path = output_dir / "report.md"
    json_path.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, markdown_path


def render_markdown(report: EvaluationReport) -> str:
    status = "PASS" if report.passed else "FAIL"
    lines = [
        f"# Evaluation report: {status}",
        "",
        "| Metric | Result | Threshold |",
        "|---|---:|---:|",
    ]
    for metric_name, threshold in report.thresholds.items():
        if metric_name == "case_pass_score":
            continue
        value = report.summary.get(metric_name)
        rendered = "not measured" if value is None else f"{float(value):.3f}"
        lines.append(f"| `{metric_name}` | {rendered} | {threshold:.3f} |")

    lines.extend(
        [
            "",
            "## Cases",
            "",
            "| Case | Score | Status | Tags |",
            "|---|---:|---|---|",
        ]
    )
    for result in report.cases:
        case_status = "PASS" if result.passed else "FAIL"
        lines.append(f"| `{result.case_id}` | {result.score:.3f} | {case_status} | {', '.join(result.tags)} |")

    if report.threshold_failures:
        lines.extend(["", "## Threshold failures", ""])
        lines.extend(f"- {failure}" for failure in report.threshold_failures)

    return "\n".join(lines) + "\n"
