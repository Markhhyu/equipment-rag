from __future__ import annotations

import time
from dataclasses import dataclass

import requests

from app.evaluation.models import EvalCase, Prediction, build_source_ref


@dataclass
class QueryApiProvider:
    base_url: str
    timeout_seconds: float = 120.0
    api_key: str | None = None

    def predict(self, case: EvalCase) -> Prediction:
        """调用真实查询 API，把在线回答转换为统一的评测预测结构。"""
        started = time.perf_counter()
        response = requests.post(
            f"{self.base_url.rstrip('/')}/query",
            json={
                "query": case.query,
                "session_id": f"eval-{case.case_id}",
                "is_stream": False,
            },
            # 评测专用Key只放在HTTP Header，不写入数据集、报告或日志。
            headers={"X-API-Key": self.api_key} if self.api_key else None,
            timeout=self.timeout_seconds,
        )
        latency_ms = (time.perf_counter() - started) * 1000
        response.raise_for_status()
        payload = response.json()

        retrieved_source_ids = payload.get("retrieved_source_ids")
        if not isinstance(retrieved_source_ids, list):
            retrieved_source_ids = None

        retrieved_source_refs = None
        sources = payload.get("sources")
        if isinstance(sources, list):
            retrieved_source_refs = list(
                dict.fromkeys(
                    source_ref
                    for source in sources
                    if isinstance(source, dict)
                    and (source_ref := build_source_ref(source.get("document_id"), source.get("version_label")))
                )
            )

        return Prediction(
            case_id=case.case_id,
            answer=str(payload.get("answer") or ""),
            latency_ms=latency_ms,
            retrieved_source_ids=retrieved_source_ids,
            retrieved_source_refs=retrieved_source_refs,
            clarified=payload.get("clarified"),
            requires_human_review=payload.get("requires_human_review"),
            trace_id=str(payload.get("trace_id") or "") or None,
        )
