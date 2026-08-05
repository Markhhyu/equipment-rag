from app.modules.qa.api.routes import app


def test_query_support_routers_are_registered():
    registered = {(route.path, method) for route in app.routes for method in getattr(route, "methods", set())}

    expected = {
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
