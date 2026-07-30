# 第一阶段只提供固定版本的 uv 可执行文件。
FROM ghcr.io/astral-sh/uv:0.12.0 AS uv

# 运行阶段使用精简 Python 镜像，减少最终镜像体积。
FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY --from=uv /uv /uvx /bin/
# 先复制依赖清单，代码未变化时可复用 Docker 依赖缓存。
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
COPY prompts ./prompts

# 日志和处理结果使用固定目录，便于 Compose 挂载持久化卷。
RUN mkdir -p /app/logs /app/output

EXPOSE 8000 8001

CMD ["uvicorn", "app.query_process.api.query_service:app", "--host", "0.0.0.0", "--port", "8001"]
