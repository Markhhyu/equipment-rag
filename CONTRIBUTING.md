# Contributing

Thank you for improving Equipment RAG Agent.

## Development setup

Install Python 3.14 and [uv](https://docs.astral.sh/uv/), then run:

```bash
uv sync --frozen --group dev
uv run python scripts/check.py
```

The second command runs the same lockfile, dependency, lint, format, test,
compile, and Compose checks used by CI.

Maintainers can also run the workflow on a branch with
`gh workflow run .github/workflows/ci.yml --ref <branch>`.

## Pull requests

- Branch from the latest target branch and keep each change focused.
- Add or update tests for behavior changes.
- Never commit `.env`, credentials, customer documents, model weights, or
  generated data.
- Describe the operational impact and any manual validation limitations.
- Keep new external services optional unless they are part of the documented
  core stack.

## Code style

Ruff is the source of truth for linting and formatting:

```bash
uv run ruff check app tests scripts
uv run ruff format app/evaluation app/runtime app/security tests scripts
```

The legacy `test/` directory contains manual integration scripts. New automated
tests belong in `tests/` and must be deterministic without external credentials
or running middleware.

Security-sensitive changes must include tenant-boundary tests. Do not accept
`tenant_id` from request bodies; derive it from the authenticated principal.
