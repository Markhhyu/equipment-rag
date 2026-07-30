from __future__ import annotations

from threading import Lock
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver

from app.runtime.config import load_runtime_config


_checkpointer: BaseCheckpointSaver | None = None
_checkpointer_lock = Lock()
_mongo_client: Any = None


def get_checkpointer() -> BaseCheckpointSaver:
    global _checkpointer, _mongo_client
    if _checkpointer is not None:
        return _checkpointer

    with _checkpointer_lock:
        if _checkpointer is not None:
            return _checkpointer

        config = load_runtime_config()
        if config.checkpoint_backend == "memory":
            _checkpointer = InMemorySaver()
        elif config.checkpoint_backend == "mongodb":
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
