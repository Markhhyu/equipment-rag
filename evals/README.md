# Evaluation

The evaluation layer separates deterministic CI replay from live model and
middleware regression runs.

## Deterministic CI replay

```bash
uv run python -m app.evaluation.cli replay \
  --predictions evals/fixtures/smoke_predictions.jsonl \
  --fail-on-threshold
```

This validates the dataset contract, metric calculations, report generation,
and configured quality thresholds without credentials or external services.

## Live query API regression

Start the project, import a matching test knowledge base, then run:

```bash
uv run python -m app.evaluation.cli api \
  --base-url http://127.0.0.1:8001 \
  --output-dir build/evaluation-live
```

Add `--fail-on-threshold` only when the API exposes every metric required by
`evals/config.toml`. The current API returns answers and latency; retrieval
recall is reported as `not measured` unless `retrieved_source_ids` is present.

Both modes write `report.json` for automation and `report.md` for review.

## Dataset contract

Each JSONL case supports:

- `id`, `query`
- `required_terms`, `forbidden_terms`
- `expected_source_ids`
- `must_clarify`, `require_citation`
- `max_latency_ms`
- `tags`

Use synthetic or licensed test content only. Do not commit customer documents,
production traces, personal data, or credentials.

Retrieval evaluation reports three complementary metrics:

- `retrieval_recall`: how many expected sources were retrieved;
- `retrieval_precision`: how much of the retrieved list is relevant;
- `retrieval_mrr`: how early the first expected source appears.

When API authentication is enabled, pass a dedicated evaluation key with
`--api-key`. Never commit that key or place it in a dataset/report.
