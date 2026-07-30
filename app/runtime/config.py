from __future__ import annotations

import os
from dataclasses import dataclass


def _positive_int(name: str, default: int) -> int:
    """读取正整数环境变量；缺失或格式错误时使用安全默认值。"""
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
    except ValueError:
        return default
    return value if value > 0 else default


@dataclass(frozen=True)
class RuntimeConfig:
    """集中保存 Agent 运行状态与检查点相关配置。"""

    run_store_backend: str
    checkpoint_backend: str
    mongo_url: str
    mongo_database: str
    run_collection: str
    checkpoint_database: str
    checkpoint_ttl_seconds: int
    lease_seconds: int
    max_attempts: int


def load_runtime_config() -> RuntimeConfig:
    """从环境变量构建运行时配置，不在业务代码中散落默认值。"""
    mongo_database = os.getenv("MONGO_DB_NAME") or "equipment_rag"
    return RuntimeConfig(
        run_store_backend=(os.getenv("RUN_STORE_BACKEND") or "memory").strip().lower(),
        checkpoint_backend=(os.getenv("LANGGRAPH_CHECKPOINT_BACKEND") or "memory").strip().lower(),
        mongo_url=os.getenv("MONGO_URL") or "mongodb://127.0.0.1:27017",
        mongo_database=mongo_database,
        run_collection=os.getenv("RUN_STORE_COLLECTION") or "agent_runs",
        checkpoint_database=os.getenv("LANGGRAPH_CHECKPOINT_DB") or f"{mongo_database}_checkpoints",
        checkpoint_ttl_seconds=_positive_int("LANGGRAPH_CHECKPOINT_TTL_SECONDS", 604800),
        lease_seconds=_positive_int("RUN_LEASE_SECONDS", 900),
        max_attempts=_positive_int("RUN_MAX_ATTEMPTS", 3),
    )
