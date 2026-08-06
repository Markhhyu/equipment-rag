import json
import warnings

import pytest
from fastapi import FastAPI, HTTPException

from app.platform.security.auth import Principal, authenticate, require_role
from app.platform.security.config import load_security_config, reset_security_config_for_tests
from app.platform.security.http import configure_http_security
from app.platform.security.routes import router as auth_router


@pytest.fixture(autouse=True)
def reset_config():
    reset_security_config_for_tests()
    yield
    reset_security_config_for_tests()


def test_local_mode_stays_zero_configuration(monkeypatch):
    monkeypatch.delenv("AUTH_MODE", raising=False)
    monkeypatch.delenv("AUTH_API_KEYS_JSON", raising=False)
    monkeypatch.delenv("APP_ENVIRONMENT", raising=False)

    config = load_security_config()

    assert config.auth_mode == "disabled"
    assert config.production is False
    assert config.minio_public_read is False


def test_production_requires_authentication(monkeypatch):
    monkeypatch.setenv("APP_ENVIRONMENT", "production")
    monkeypatch.setenv("AUTH_MODE", "disabled")

    with pytest.raises(ValueError, match="Production startup requires"):
        load_security_config()


@pytest.mark.asyncio
async def test_api_key_authentication_and_role_enforcement(monkeypatch):
    secret = "test-secret-with-more-than-24-characters"
    monkeypatch.setenv("AUTH_MODE", "api_key")
    monkeypatch.setenv(
        "AUTH_API_KEYS_JSON",
        json.dumps(
            [
                {
                    "id": "tenant-a-query",
                    "key": secret,
                    "tenant_id": "tenant-a",
                    "roles": ["query"],
                }
            ]
        ),
    )

    principal = await authenticate(secret)

    assert principal.authenticated is True
    assert principal.tenant_id == "tenant-a"
    assert await require_role("query")(principal) == principal
    with pytest.raises(HTTPException) as denied:
        await require_role("import")(principal)
    assert denied.value.status_code == 403


@pytest.mark.asyncio
async def test_invalid_api_key_is_rejected(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "api_key")
    monkeypatch.setenv(
        "AUTH_API_KEYS_JSON",
        json.dumps(
            [
                {
                    "id": "tenant-a-admin",
                    "key": "correct-secret-with-more-than-24-characters",
                    "tenant_id": "tenant-a",
                    "roles": ["admin"],
                }
            ]
        ),
    )

    with pytest.raises(HTTPException) as denied:
        await authenticate("incorrect-secret-with-more-than-24-chars")
    assert denied.value.status_code == 401


def test_admin_principal_has_all_roles():
    principal = Principal("admin", "tenant-a", frozenset({"admin"}), True)

    assert principal.has_role("query")
    assert principal.has_role("import")


def test_http_hardening_adds_request_and_browser_security_headers(monkeypatch):
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Using `httpx` with `starlette.testclient` is deprecated")
        from fastapi.testclient import TestClient

    monkeypatch.setenv("AUTH_MODE", "disabled")
    app = FastAPI()
    configure_http_security(app)

    @app.get("/health")
    async def health():
        return {"ok": True}

    response = TestClient(app).get("/health", headers={"X-Request-ID": "request-123"})

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "request-123"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"


def test_auth_me_returns_tenant_and_roles_without_exposing_key(monkeypatch):
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Using `httpx` with `starlette.testclient` is deprecated")
        from fastapi.testclient import TestClient

    secret = "test-secret-with-more-than-24-characters"
    monkeypatch.setenv("AUTH_MODE", "api_key")
    monkeypatch.setenv(
        "AUTH_API_KEYS_JSON",
        json.dumps(
            [
                {
                    "id": "factory-a-user",
                    "key": secret,
                    "tenant_id": "factory-a",
                    "roles": ["query", "workflow"],
                }
            ]
        ),
    )
    app = FastAPI()
    app.include_router(auth_router)
    client = TestClient(app)

    assert client.get("/auth/me").status_code == 401
    response = client.get("/auth/me", headers={"X-API-Key": secret})

    assert response.status_code == 200
    assert response.json() == {
        "key_id": "factory-a-user",
        "tenant_id": "factory-a",
        "roles": ["query", "workflow"],
        "authenticated": True,
    }
    assert secret not in response.text


def test_all_browser_services_expose_auth_me():
    from app.modules.ingestion.api.routes import app as ingestion_app
    from app.modules.qa.api.routes import app as query_app
    from app.modules.workflow.api.routes import app as workflow_app

    for app in (ingestion_app, query_app, workflow_app):
        assert any(route.path == "/auth/me" for route in app.routes)
