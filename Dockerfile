FROM ghcr.io/astral-sh/uv:0.12.0 AS uv

FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY --from=uv /uv /uvx /bin/
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
COPY prompts ./prompts

RUN mkdir -p /app/logs /app/output

EXPOSE 8000 8001

CMD ["uvicorn", "app.query_process.api.query_service:app", "--host", "0.0.0.0", "--port", "8001"]
