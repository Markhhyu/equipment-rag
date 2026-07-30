from app.import_process.agent.state import create_default_state, get_default_state


def test_default_import_state_is_isolated():
    first = get_default_state()
    second = get_default_state()

    first["chunks"].append({"content": "changed"})

    assert second["chunks"] == []


def test_import_state_overrides_do_not_mutate_defaults():
    state = create_default_state(
        task_id="task-123",
        local_file_path="/tmp/manual.md",
        is_md_read_enabled=True,
    )

    assert state["task_id"] == "task-123"
    assert state["local_file_path"] == "/tmp/manual.md"
    assert state["is_md_read_enabled"] is True
    assert get_default_state()["task_id"] == ""
