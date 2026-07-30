from typing_extensions import TypedDict

import pytest
from langgraph.graph import END, START, StateGraph

from app.runtime.checkpointing import (
    checkpoint_config,
    get_checkpointer,
    reset_checkpointer_for_tests,
)


class RecoveryState(TypedDict):
    value: int


def test_failed_graph_resumes_from_last_successful_checkpoint(monkeypatch):
    monkeypatch.setenv("LANGGRAPH_CHECKPOINT_BACKEND", "memory")
    reset_checkpointer_for_tests()
    calls = {"first": 0, "unstable": 0}

    def first_node(state: RecoveryState):
        calls["first"] += 1
        return {"value": state["value"] + 1}

    def unstable_node(state: RecoveryState):
        calls["unstable"] += 1
        if calls["unstable"] == 1:
            raise RuntimeError("transient failure")
        return {"value": state["value"] + 10}

    builder = StateGraph(RecoveryState)
    builder.add_node("first", first_node)
    builder.add_node("unstable", unstable_node)
    builder.add_edge(START, "first")
    builder.add_edge("first", "unstable")
    builder.add_edge("unstable", END)
    graph = builder.compile(checkpointer=get_checkpointer())
    config = checkpoint_config("recovery-test", kind="test")

    with pytest.raises(RuntimeError, match="transient"):
        graph.invoke({"value": 0}, config=config)

    recovered = graph.invoke(None, config=config)

    assert recovered["value"] == 11
    assert calls == {"first": 1, "unstable": 2}
    reset_checkpointer_for_tests()


def test_unknown_checkpoint_backend_is_rejected(monkeypatch):
    monkeypatch.setenv("LANGGRAPH_CHECKPOINT_BACKEND", "unknown")
    reset_checkpointer_for_tests()

    with pytest.raises(ValueError, match="Unsupported"):
        get_checkpointer()

    reset_checkpointer_for_tests()
