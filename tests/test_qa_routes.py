import pytest

from app.modules.qa.api import session_routes
from app.modules.qa.api.routes import app
from app.platform.security.auth import Principal


def test_query_support_routers_are_registered():
    registered = {(route.path, method) for route in app.routes for method in getattr(route, "methods", set())}

    expected = {
        ("/", "GET"),
        ("/apps.html", "GET"),
        ("/feedback", "POST"),
        ("/resolution", "POST"),
        ("/analytics/summary", "GET"),
        ("/attachments/config", "GET"),
        ("/attachments/{session_id}", "POST"),
        ("/stream/{session_id}", "GET"),
        ("/history/{session_id}", "GET"),
        ("/history/{session_id}", "DELETE"),
    }

    assert expected <= registered


@pytest.mark.asyncio
async def test_history_exposes_stable_image_refs_and_resolved_urls(monkeypatch):
    object_ref = "minio://equipment-rag/tenants/local/chat_attachments/session-a/panel.png"
    monkeypatch.setattr(
        session_routes,
        "get_recent_messages",
        lambda *_args, **_kwargs: [{"role": "user", "text": "面板报警", "image_urls": [object_ref]}],
    )
    monkeypatch.setattr(session_routes, "resolve_object_urls", lambda refs: [f"preview:{ref}" for ref in refs])
    principal = Principal("test", "local", frozenset({"query"}), True)

    result = await session_routes.history("session-a", principal=principal)

    assert result["items"][0]["image_refs"] == [object_ref]
    assert result["items"][0]["image_urls"] == [f"preview:{object_ref}"]
