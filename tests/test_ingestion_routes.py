from app.modules.ingestion.api.routes import app


def test_knowledge_governance_router_is_registered():
    registered = {(route.path, method) for route in app.routes for method in getattr(route, "methods", set())}

    expected = {
        ("/knowledge/documents", "GET"),
        ("/knowledge/legacy/register", "POST"),
        ("/knowledge/documents/{document_id}", "GET"),
        ("/knowledge/documents/{document_id}/versions/{revision_id}/publish", "POST"),
        ("/knowledge/documents/{document_id}/versions/{revision_id}/rollback", "POST"),
        ("/knowledge/documents/{document_id}/disable", "POST"),
        ("/knowledge/documents/{document_id}/enable", "POST"),
        ("/knowledge/audit", "GET"),
    }

    assert expected <= registered
