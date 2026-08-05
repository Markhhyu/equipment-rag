from __future__ import annotations

from threading import Lock
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver

from app.platform.runtime.config import load_runtime_config


_checkpointer: BaseCheckpointSaver | None = None
_checkpointer_lock = Lock()
# MongoDB 客户端必须与检查点保存器保持同样的生命周期，不能在函数返回后被回收。
_mongo_client: Any = None


def get_checkpointer() -> BaseCheckpointSaver:
    """按配置创建并复用 LangGraph 检查点保存器。"""
    global _checkpointer, _mongo_client
    if _checkpointer is not None:
        return _checkpointer

    # 双重检查配合锁，保证多个请求同时首次访问时只创建一个保存器。
    with _checkpointer_lock:
        if _checkpointer is not None:
            return _checkpointer

        config = load_runtime_config()
        if config.checkpoint_backend == "memory":
            # 内存后端适合本地开发和测试，进程重启后数据会丢失。
            _checkpointer = InMemorySaver()
        elif config.checkpoint_backend == "mongodb":
            # MongoDB 后端可让 Agent 在服务重启后从最后一个检查点继续运行。
            from langgraph.checkpoint.mongodb import MongoDBSaver
            from pymongo import MongoClient

            _mongo_client = MongoClient(
                config.mongo_url,
                appname="equipment-rag-checkpoints",
                tz_aware=True,
            )
            _checkpointer = MongoDBSaver(
                _mongo_client,
                db_name=config.checkpoint_database,
                ttl=config.checkpoint_ttl_seconds,
            )
        else:
            raise ValueError(
                f"Unsupported LANGGRAPH_CHECKPOINT_BACKEND={config.checkpoint_backend!r}; "
                "expected 'memory' or 'mongodb'"
            )

        return _checkpointer


def checkpoint_config(run_id: str, **metadata: Any) -> dict[str, Any]:
    """把运行 ID 映射为 LangGraph thread_id，并附加链路元数据。"""
    return {
        "configurable": {"thread_id": run_id},
        "metadata": metadata,
    }


def reset_checkpointer_for_tests() -> None:
    global _checkpointer, _mongo_client
    with _checkpointer_lock:
        if _mongo_client is not None:
            _mongo_client.close()
        _mongo_client = None
        _checkpointer = None
