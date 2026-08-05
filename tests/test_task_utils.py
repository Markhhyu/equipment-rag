from app.platform.runtime.task_progress import (
    TASK_STATUS_COMPLETED,
    add_done_task,
    add_running_task,
    clear_task,
    get_done_task_list,
    get_running_task_list,
    get_task_result,
    get_task_status,
    set_task_result,
    update_task_status,
)


def test_task_progress_lifecycle():
    task_id = "task-progress-test"
    clear_task(task_id)

    try:
        add_running_task(task_id, "node_entry")
        add_running_task(task_id, "node_entry")
        assert get_running_task_list(task_id) == ["检查文件"]

        add_done_task(task_id, "node_entry")
        assert get_running_task_list(task_id) == []
        assert get_done_task_list(task_id) == ["检查文件"]

        update_task_status(task_id, TASK_STATUS_COMPLETED)
        set_task_result(task_id, "answer", "done")
        assert get_task_status(task_id) == TASK_STATUS_COMPLETED
        assert get_task_result(task_id, "answer") == "done"
        assert get_task_result(task_id, "missing", "fallback") == "fallback"
    finally:
        clear_task(task_id)

    assert get_task_status(task_id) == ""
