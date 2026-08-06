from app.modules.workflow.api.routes import app


def test_workflow_case_query_and_page_routes_are_registered():
    registered = {(route.path, method) for route in app.routes for method in getattr(route, "methods", set())}

    expected = {
        ("/", "GET"),
        ("/workflow.html", "GET"),
        ("/workflow/cases", "GET"),
        ("/workflow/cases", "POST"),
        ("/workflow/cases/{case_id}", "GET"),
        ("/workflow/cases/{case_id}/actions", "POST"),
        ("/workflow/connectors/feishu", "GET"),
        ("/workflow/connectors/feishu", "PUT"),
        ("/workflow/connectors/feishu/test", "POST"),
        ("/workflow/connectors/feishu", "DELETE"),
    }

    assert expected <= registered
