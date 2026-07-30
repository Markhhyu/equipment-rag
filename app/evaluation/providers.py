from __future__ import annotations

import time
from dataclasses import dataclass

import requests

from app.evaluation.models import EvalCase, Prediction


@dataclass
class QueryApiProvider:
    base_url: str
    timeout_seconds: float = 120.0

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
            timeout=self.timeout_seconds,
        )
        latency_ms = (time.perf_counter() - started) * 1000
        response.raise_for_status()
        payload = response.json()

        retrieved_source_ids = payload.get("retrieved_source_ids")
        if not isinstance(retrieved_source_ids, list):
            retrieved_source_ids = None

        return Prediction(
            case_id=case.case_id,
            answer=str(payload.get("answer") or ""),
            latency_ms=latency_ms,
            retrieved_source_ids=retrieved_source_ids,
            clarified=payload.get("clarified"),
            trace_id=str(payload.get("trace_id") or "") or None,
        )
