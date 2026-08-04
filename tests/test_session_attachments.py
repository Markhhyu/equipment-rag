from io import BytesIO
from types import SimpleNamespace

import pytest

from app.clients import session_attachment_utils
from app.query_process.agent.nodes import node_answer_output, node_image_reasoning
from app.query_process.agent.state import QueryGraphState


def _png_stream(payload_size: int = 64) -> BytesIO:
    return BytesIO(b"\x89PNG\r\n\x1a\n" + b"0" * payload_size)


def test_image_signature_is_checked_instead_of_trusting_filename():
    size, content_type, extension = session_attachment_utils.inspect_image_stream(_png_stream())

    assert size > 0
    assert content_type == "image/png"
    assert extension == ".png"

    with pytest.raises(ValueError, match="无法识别图片内容"):
        session_attachment_utils.inspect_image_stream(BytesIO(b"not-an-image"))


def test_session_attachment_is_stored_under_tenant_and_session_prefix(monkeypatch):
    uploaded = {}

    class FakeMinio:
        def put_object(self, bucket, object_name, stream, *, length, content_type):
            uploaded.update(
                bucket=bucket,
                object_name=object_name,
                length=length,
                content_type=content_type,
                payload=stream.read(),
            )

    monkeypatch.setattr(session_attachment_utils, "get_minio_client", lambda: FakeMinio())
    monkeypatch.setattr(session_attachment_utils, "resolve_object_url", lambda ref: f"preview:{ref}")

    result = session_attachment_utils.store_session_attachment(
        tenant_id="factory-a",
        session_id="session-001",
        original_filename="panel.png",
        stream=_png_stream(),
    )

    assert uploaded["object_name"].startswith("tenants/factory-a/chat_attachments/session-001/")
    assert uploaded["content_type"] == "image/png"
    assert result["object_ref"].startswith("minio://")
    assert result["preview_url"].startswith("preview:minio://")


def test_attachment_reference_cannot_cross_tenant_or_session(monkeypatch):
    class FakeMinio:
        def stat_object(self, *_):
            return SimpleNamespace(size=100, content_type="image/png")

    monkeypatch.setattr(session_attachment_utils, "get_minio_client", lambda: FakeMinio())
    valid_ref = "minio://equipment-rag/tenants/factory-a/chat_attachments/session-001/image.png"

    assert session_attachment_utils.validate_session_attachment_refs(
        "factory-a", "session-001", [valid_ref, valid_ref]
    ) == [valid_ref]

    with pytest.raises(ValueError, match="不属于当前租户或会话"):
        session_attachment_utils.validate_session_attachment_refs(
            "factory-b", "session-001", [valid_ref]
        )


def test_user_attachment_forces_visual_reasoning_but_is_not_echoed_as_answer_image(monkeypatch):
    object_ref = "minio://equipment-rag/tenants/local/chat_attachments/session-001/image.png"
    monkeypatch.setattr(
        node_image_reasoning,
        "_invoke_query_vision",
        lambda question, assets: ("图片显示设备面板红灯亮起。", assets),
    )

    result = node_image_reasoning.node_image_reasoning(
        {
            "session_id": "",
            "tenant_id": "local",
            "original_query": "这个状态正常吗？",
            "user_image_refs": [object_ref],
            "reranked_docs": [],
            "is_stream": False,
        }
    )

    assert result["need_visual_reasoning"] is True
    assert result["image_reasoning_status"] == "completed"
    assert "红灯" in result["image_analysis_context"]
    assert result["image_reasoning_object_uris"] == []
    assert result["image_assets"][0]["session_attachment"] is True

    state: QueryGraphState = {
        "image_assets": result["image_assets"],
        "image_reasoning_object_uris": [],
    }
    assert node_answer_output._selected_image_object_refs(state) == []


def test_query_state_declares_session_attachment_field():
    assert "user_image_refs" in QueryGraphState.__annotations__


def test_operation_question_displays_document_images_without_forcing_vision_model():
    assert node_image_reasoning.should_display_document_images("LJ2268 怎么使用") is True
    assert node_image_reasoning.is_visual_question("LJ2268 怎么使用") is False
