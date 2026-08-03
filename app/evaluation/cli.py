from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.evaluation.io import load_cases, load_predictions, load_thresholds, write_report
from app.evaluation.providers import QueryApiProvider
from app.evaluation.runner import evaluate


DEFAULT_DATASET = Path("evals/datasets/smoke.jsonl")
DEFAULT_CONFIG = Path("evals/config.toml")
DEFAULT_OUTPUT = Path("build/evaluation")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Equipment RAG quality evaluations.")
    subparsers = parser.add_subparsers(dest="mode", required=True)

    replay = subparsers.add_parser("replay", help="Evaluate deterministic recorded predictions.")
    replay.add_argument("--predictions", type=Path, required=True)

    api = subparsers.add_parser("api", help="Call a running query API and evaluate its answers.")
    api.add_argument("--base-url", default="http://127.0.0.1:8001")
    api.add_argument("--timeout-seconds", type=float, default=120.0)
    api.add_argument("--api-key", help="查询API启用鉴权时使用；不要把真实Key写进脚本或仓库。")

    for command in (replay, api):
        command.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
        command.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
        command.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
        command.add_argument("--fail-on-threshold", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cases = load_cases(args.dataset)
    thresholds = load_thresholds(args.config)

    if args.mode == "replay":
        predictions = load_predictions(args.predictions)
    else:
        provider = QueryApiProvider(args.base_url, timeout_seconds=args.timeout_seconds, api_key=args.api_key)
        predictions = [provider.predict(case) for case in cases]

    report = evaluate(cases, predictions, thresholds)
    json_path, markdown_path = write_report(report, args.output_dir)
    print(f"evaluation={'PASS' if report.passed else 'FAIL'}")
    print(f"json_report={json_path}")
    print(f"markdown_report={markdown_path}")
    for failure in report.threshold_failures:
        print(f"threshold_failure={failure}")

    if args.fail_on_threshold and not report.passed:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
