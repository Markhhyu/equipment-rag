import json
import warnings

import pytest
from fastapi import FastAPI, HTTPException

from app.platform.security.auth import Principal, authenticate, require_role
from app.platform.security.config import load_security_config, reset_security_config_for_tests
from app.platform.security.http import configure_http_security
from app.platform.security.passwords import hash_password, normalize_email, verify_password
from app.platform.security.routes import reset_auth_rate_limits_for_tests, router as auth_router
from app.platform.security import user_store
from app.platform.security.user_store import DuplicateEmailError


@pytest.fixture(autouse=True)
def reset_config():
    reset_security_config_for_tests()
    reset_auth_rate_limits_for_tests()
    yield
    reset_security_config_for_tests()
    reset_auth_rate_limits_for_tests()


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


def test_password_hash_is_salted_and_verifiable():
    first = hash_password("correct horse battery staple")
    second = hash_password("correct horse battery staple")

    assert first != second
    assert verify_password("correct horse battery staple", first) is True
    assert verify_password("incorrect password", first) is False
    assert normalize_email(" User@Example.COM ") == "user@example.com"


def test_registration_requires_password_mode(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "api_key")
    monkeypatch.setenv("AUTH_REGISTRATION_ENABLED", "true")
    monkeypatch.setenv(
        "AUTH_API_KEYS_JSON",
        json.dumps(
            [
                {
                    "id": "automation",
                    "key": "test-secret-with-more-than-24-characters",
                    "tenant_id": "public",
                    "roles": ["query"],
                }
            ]
        ),
    )

    with pytest.raises(ValueError, match="requires AUTH_MODE=password"):
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
        "email": "",
        "auth_type": "api_key",
    }
    assert secret not in response.text


def test_all_browser_services_expose_auth_me():
    from app.modules.ingestion.api.routes import app as ingestion_app
    from app.modules.qa.api.routes import app as query_app
    from app.modules.workflow.api.routes import app as workflow_app

    for app in (ingestion_app, query_app, workflow_app):
        assert any(route.path == "/auth/me" for route in app.routes)


class _FakeUserStore:
    def __init__(self):
        self.users = {}
        self.sessions = {}

    def create_user(self, *, email, password_hash, tenant_id, roles):
        if email in self.users:
            raise DuplicateEmailError("该邮箱已注册")
        user = {
            "user_id": f"user-{len(self.users) + 1}",
            "email": email,
            "password_hash": password_hash,
            "tenant_id": tenant_id,
            "roles": sorted(roles),
            "status": "active",
        }
        self.users[email] = user
        return {key: value for key, value in user.items() if key != "password_hash"}

    def find_user_by_email(self, email):
        return self.users.get(email)

    def record_login(self, user_id):
        return None

    def create_session(self, user_id, ttl_seconds):
        token = f"session-{len(self.sessions) + 1}"
        self.sessions[token] = user_id
        return token

    def find_user_by_session(self, token):
        user_id = self.sessions.get(token)
        user = next((item for item in self.users.values() if item["user_id"] == user_id), None)
        if not user or user["status"] != "active":
            return None
        return {key: value for key, value in user.items() if key != "password_hash"}

    def revoke_session(self, token):
        self.sessions.pop(token, None)


def test_email_registration_login_and_logout(monkeypatch):
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Using `httpx` with `starlette.testclient` is deprecated")
        from fastapi.testclient import TestClient

    monkeypatch.setenv("AUTH_MODE", "password")
    monkeypatch.setenv("AUTH_REGISTRATION_ENABLED", "true")
    monkeypatch.setenv("AUTH_REGISTRATION_TENANT_ID", "public-community")
    monkeypatch.setenv("AUTH_COOKIE_SECURE", "false")
    store = _FakeUserStore()
    monkeypatch.setattr(user_store, "get_user_store", lambda: store)
    app = FastAPI()
    app.include_router(auth_router)
    client = TestClient(app)

    config = client.get("/auth/config")
    assert config.json() == {
        "auth_mode": "password",
        "password_login_enabled": True,
        "registration_enabled": True,
    }

    registration = client.post(
        "/auth/register",
        json={"email": " User@Example.COM ", "password": "correct horse battery staple"},
    )
    assert registration.status_code == 201
    assert registration.json()["email"] == "user@example.com"
    assert registration.json()["tenant_id"] == "public-community"
    assert registration.json()["roles"] == ["query"]
    assert registration.json()["auth_type"] == "password"
    assert "password_hash" not in registration.json()
    assert "correct horse battery staple" not in registration.text
    assert "HttpOnly" in registration.headers["set-cookie"]
    assert "SameSite=lax" in registration.headers["set-cookie"]

    current = client.get("/auth/me")
    assert current.status_code == 200
    assert current.json()["email"] == "user@example.com"

    assert client.post("/auth/logout").status_code == 204
    assert client.get("/auth/me").status_code == 401

    bad_login = client.post(
        "/auth/login",
        json={"email": "user@example.com", "password": "incorrect password"},
    )
    assert bad_login.status_code == 401
    login = client.post(
        "/auth/login",
        json={"email": "user@example.com", "password": "correct horse battery staple"},
    )
    assert login.status_code == 200
    assert client.get("/auth/me").status_code == 200


def test_email_registration_rejects_duplicate_and_short_password(monkeypatch):
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Using `httpx` with `starlette.testclient` is deprecated")
        from fastapi.testclient import TestClient

    monkeypatch.setenv("AUTH_MODE", "password")
    monkeypatch.setenv("AUTH_REGISTRATION_ENABLED", "true")
    store = _FakeUserStore()
    monkeypatch.setattr(user_store, "get_user_store", lambda: store)
    app = FastAPI()
    app.include_router(auth_router)
    client = TestClient(app)

    short_password = client.post("/auth/register", json={"email": "user@example.com", "password": "short"})
    assert short_password.status_code == 422

    payload = {"email": "user@example.com", "password": "long-enough-password"}
    assert client.post("/auth/register", json=payload).status_code == 201
    assert client.post("/auth/register", json=payload).status_code == 409
