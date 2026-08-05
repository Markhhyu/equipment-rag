import json

import pytest

from app.platform.runtime.sse import (
    SSEEvent,
    _sse_pack,
    create_sse_queue,
    get_sse_queue,
    push_to_session,
    remove_sse_queue,
    sse_generator,
)


class _ConnectedRequest:
    async def is_disconnected(self):
        return False


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


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_event", [SSEEvent.FINAL, SSEEvent.ERROR])
async def test_sse_generator_stops_and_cleans_queue_after_terminal_event(terminal_event):
    session_id = f"terminal-{terminal_event}"
    create_sse_queue(session_id)
    push_to_session(session_id, terminal_event, {"status": terminal_event})
    stream = sse_generator(session_id, _ConnectedRequest())

    assert (await anext(stream)).startswith("event: ready\n")
    terminal_payload = await anext(stream)
    assert terminal_payload.startswith(f"event: {terminal_event}\n")

    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    assert get_sse_queue(session_id) is None
