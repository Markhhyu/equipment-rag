import json

from app.utils.sse_utils import (
    SSEEvent,
    _sse_pack,
    create_sse_queue,
    get_sse_queue,
    push_to_session,
    remove_sse_queue,
)


def test_sse_pack_preserves_event_and_unicode_payload():
    packed = _sse_pack(SSEEvent.DELTA, {"text": "设备正常"})
    event_line, data_line, *_ = packed.splitlines()

    assert event_line == "event: delta"
    assert json.loads(data_line.removeprefix("data: ")) == {"text": "设备正常"}


def test_sse_queue_lifecycle():
    session_id = "session-test"
    stream_queue = create_sse_queue(session_id)

    try:
        assert get_sse_queue(session_id) is stream_queue

        push_to_session(session_id, SSEEvent.FINAL, {"answer": "done"})

        assert stream_queue.get_nowait() == {
            "event": SSEEvent.FINAL,
            "data": {"answer": "done"},
        }
    finally:
        remove_sse_queue(session_id)

    assert get_sse_queue(session_id) is None
