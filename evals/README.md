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
- `expected_source_ids`: runtime Chunk IDs, retained for synthetic and legacy fixtures
- `expected_source_refs`: stable `document_id::version_label` references for real manuals
- `must_clarify`, `must_review`, `require_citation`
- `max_latency_ms`
- `tags`

Use synthetic or licensed test content only. Do not commit customer documents,
production traces, personal data, or credentials.

Retrieval evaluation reports three complementary metrics:

- `retrieval_recall`: how many expected sources were retrieved;
- `retrieval_precision`: how much of the retrieved list is relevant;
- `retrieval_mrr`: how early the first expected source appears.

`must_review` verifies that refusal and high-risk evidence gaps set the structured
`requires_human_review` response instead of relying only on cautionary wording.

## Real manual baseline

`datasets/manuals.jsonl` contains a small, reviewed baseline derived from the local
manuals listed below. The PDFs remain under the ignored `doc/` directory and are not
committed. Import them with the exact document/version metadata before running the
live evaluation:

| Local PDF | `document_id` | `version_label` | Suggested applicability |
|---|---|---|---|
| `万用表RS-12的使用.pdf` | `manual-rs12` | `v170801` | device model `RS-12` |
| `LJ2268系列用户手册.pdf` | `manual-lj2268` | `v201807` | device model `LJ2268` |
| `Z26通用机型打印机用户手册（至像）ver02.30-20251029.pdf` | `manual-z26-series` | `v02.30-zhixiang` | equipment version `至像通用机型` |
| `Z26 MIC机型打印机用户手册（联想）ver02.30-20251029.pdf` | `manual-z26-series` | `v02.30-lenovo-mic` | equipment version `联想MIC机型` |

Publish both Z26 revisions as parallel applicability scopes. Then run:

```bash
uv run python -m app.evaluation.cli api \
  --dataset evals/datasets/manuals.jsonl \
  --config evals/manuals-config.toml \
  --base-url http://127.0.0.1:8001 \
  --output-dir build/evaluation-manuals \
  --fail-on-threshold
```

The manual configuration requires retrieval precision `1.00`; a Z26 answer that
mixes the two published scopes therefore fails even when the required words appear.

When API authentication is enabled, pass a dedicated evaluation key with
`--api-key`. Never commit that key or place it in a dataset/report.
