"""Prometheus 指标定义与 FastAPI 接入工具。

这个模块只记录“数值指标”，不记录用户问题、文档正文、Prompt 或答案内容。
这样既能在 Grafana 中观察趋势，又能避免把设备手册和用户数据放进指标标签。

注意：Prometheus 标签必须是低基数数据。不要把 trace_id、session_id、文件名、
设备名称等不断变化的值作为标签，否则会产生大量时间序列并占用过多内存。
"""

from __future__ import annotations

import os
import time

from fastapi import FastAPI, Request
from prometheus_client import Counter, Gauge, Histogram, make_asgi_app


# 是否开放 /metrics。默认开启；如果部署环境由独立网关统一采集，也可以关闭。
PROMETHEUS_ENABLED = os.getenv("PROMETHEUS_METRICS_ENABLED", "true").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

# HTTP 层指标：用于观察接口吞吐量、错误率和响应时间。
HTTP_REQUESTS = Counter(
    "equipment_rag_http_requests_total",
    "HTTP 请求总数",
    ("service", "method", "route", "status"),
)
HTTP_DURATION = Histogram(
    "equipment_rag_http_request_duration_seconds",
    "HTTP 请求耗时（秒）",
    ("service", "method", "route"),
    # RAG 请求通常比普通 Web 请求慢，因此分桶覆盖 10ms 到 10 分钟。
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600),
)

# Agent 节点指标：可以直接在 Grafana 中定位最慢、最容易失败的节点。
AGENT_STAGE_TOTAL = Counter(
    "equipment_rag_agent_stage_total",
    "Agent 节点执行次数",
    ("kind", "stage", "status"),
)
AGENT_STAGE_DURATION = Histogram(
    "equipment_rag_agent_stage_duration_seconds",
    "Agent 节点执行耗时（秒）",
    ("kind", "stage"),
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600, 1800, 3600),
)
AGENT_RUN_TOTAL = Counter(
    "equipment_rag_agent_run_total",
    "文件导入或问答流程执行次数",
    ("kind", "status"),
)
AGENT_RUN_DURATION = Histogram(
    "equipment_rag_agent_run_duration_seconds",
    "完整文件导入或问答流程耗时（秒）",
    ("kind",),
    buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600, 1800, 3600),
)

# 文件导入质量指标。Gauge 表示“最近一次完成任务”的结果，Histogram 用于观察长期分布。
IMPORT_QUALITY_SCORE = Histogram(
    "equipment_rag_import_quality_proxy_score",
    "文件导入质量代理分数（0~1，并不等于人工正确率）",
    buckets=(0.25, 0.5, 0.7, 0.8, 0.9, 0.95, 1.0),
)
IMPORT_MARKDOWN_CHARS = Histogram(
    "equipment_rag_import_markdown_chars",
    "MinerU 或 Markdown 读取后得到的字符数",
    buckets=(100, 500, 1000, 5000, 10000, 50000, 100000, 500000, 1000000),
)
IMPORT_CHUNKS = Histogram(
    "equipment_rag_import_chunks",
    "单个文件最终生成的切片数量",
    buckets=(1, 2, 5, 10, 20, 50, 100, 200, 500, 1000),
)
IMPORT_RATIO = Gauge(
    "equipment_rag_import_last_ratio",
    "最近一次文件导入的质量比例",
    ("metric",),
)

# 问答质量代理指标。它们适合发现异常趋势，不能替代黄金数据集的人工正确答案。
QUERY_QUALITY_SCORE = Histogram(
    "equipment_rag_query_quality_proxy_score",
    "问答质量代理分数（0~1）",
    buckets=(0.25, 0.5, 0.7, 0.8, 0.9, 1.0),
)
QUERY_RETRIEVAL_COUNT = Histogram(
    "equipment_rag_query_retrieval_count",
    "每次问答各阶段返回的文档数量",
    ("source",),
    buckets=(0, 1, 2, 3, 5, 8, 10, 20, 50),
)
QUERY_RERANK_TOP1 = Histogram(
    "equipment_rag_query_rerank_top1_score",
    "Reranker Top1 原始分数；不同模型之间不要直接横向比较",
    buckets=(-10, -5, -2, -1, 0, 0.25, 0.5, 0.75, 1, 2, 5, 10),
)
QUERY_ANSWER_CHARS = Histogram(
    "equipment_rag_query_answer_chars",
    "最终答案字符数",
    buckets=(0, 50, 100, 200, 500, 1000, 2000, 5000, 10000),
)
USER_FEEDBACK = Counter(
    "equipment_rag_user_feedback_total",
    "用户点赞和点踩次数",
    ("value",),
)


def install_prometheus(app: FastAPI, service_name: str) -> None:
    """给 FastAPI 安装 HTTP 指标中间件，并挂载 Prometheus 的 /metrics 页面。"""

    if not PROMETHEUS_ENABLED:
        return

    @app.middleware("http")
    async def prometheus_http_middleware(request: Request, call_next):
        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            # 请求执行后 route 对象才一定存在。使用路由模板而不是实际 URL，
            # 避免 /runs/{每个UUID} 生成无限多 Prometheus 标签。
            route = request.scope.get("route")
            route_path = getattr(route, "path", None) or request.url.path
            HTTP_REQUESTS.labels(service_name, request.method, route_path, str(status_code)).inc()
            HTTP_DURATION.labels(service_name, request.method, route_path).observe(time.perf_counter() - started)

    # make_asgi_app 是 prometheus-client 官方提供的 ASGI 暴露方式。
    app.mount("/metrics", make_asgi_app())


def observe_stage(kind: str, stage: str, duration_seconds: float, status: str) -> None:
    if not PROMETHEUS_ENABLED:
        return
    AGENT_STAGE_TOTAL.labels(kind, stage, status).inc()
    AGENT_STAGE_DURATION.labels(kind, stage).observe(max(0.0, duration_seconds))


def observe_run(kind: str, duration_seconds: float, status: str, metrics: dict | None = None) -> None:
    """记录完整运行和最终质量摘要；metrics 来自 quality_metrics.py。"""

    if not PROMETHEUS_ENABLED:
        return
    AGENT_RUN_TOTAL.labels(kind, status).inc()
    AGENT_RUN_DURATION.labels(kind).observe(max(0.0, duration_seconds))
    if not metrics or status != "completed":
        return

    if kind == "import":
        parser = metrics.get("parser") or {}
        chunks = metrics.get("chunks") or {}
        embeddings = metrics.get("embeddings") or {}
        storage = metrics.get("storage") or {}
        entity = metrics.get("entity") or {}
        IMPORT_QUALITY_SCORE.observe(float(metrics.get("quality_proxy_score") or 0.0))
        IMPORT_MARKDOWN_CHARS.observe(float(parser.get("markdown_chars") or 0))
        IMPORT_CHUNKS.observe(float(chunks.get("count") or 0))
        for name, value in {
            "chunk_healthy": chunks.get("healthy_length_ratio"),
            "embedding_success": embeddings.get("success_ratio"),
            "storage_success": storage.get("stored_ratio"),
            "item_name_coverage": entity.get("coverage_ratio"),
        }.items():
            if value is not None:
                IMPORT_RATIO.labels(name).set(float(value))
        return

    retrieval = metrics.get("retrieval") or {}
    response = metrics.get("response") or {}
    QUERY_QUALITY_SCORE.observe(float(metrics.get("quality_proxy_score") or 0.0))
    for source, key in {
        "embedding": "embedding_count",
        "hyde": "hyde_count",
        "web": "web_count",
        "kg": "kg_count",
        "rrf": "rrf_count",
        "rerank": "reranked_count",
    }.items():
        QUERY_RETRIEVAL_COUNT.labels(source).observe(float(retrieval.get(key) or 0))
    if retrieval.get("rerank_top1_score") is not None:
        QUERY_RERANK_TOP1.observe(float(retrieval["rerank_top1_score"]))
    QUERY_ANSWER_CHARS.observe(float(response.get("answer_chars") or 0))


def observe_feedback(value: int) -> None:
    if PROMETHEUS_ENABLED:
        USER_FEEDBACK.labels("positive" if value == 1 else "negative").inc()
