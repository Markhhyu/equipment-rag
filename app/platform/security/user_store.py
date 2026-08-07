from __future__ import annotations

import hashlib
import os
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any

from pymongo import ASCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError


def _now() -> datetime:
    return datetime.now(UTC)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class DuplicateEmailError(ValueError):
    pass


class OAuthIdentityConflictError(ValueError):
    pass


class MongoUserStore:
    def __init__(self, mongo_url: str, database: str) -> None:
        client = MongoClient(mongo_url, appname="equipment-rag-identity", tz_aware=True)
        db = client[database]
        self._users = db["auth_users"]
        self._sessions = db["auth_sessions"]
        self._email_verifications = db["auth_email_verifications"]
        self._oauth_identities = db["auth_oauth_identities"]
        self._users.create_index([("email_normalized", ASCENDING)], unique=True)
        self._sessions.create_index([("token_hash", ASCENDING)], unique=True)
        self._sessions.create_index([("expires_at", ASCENDING)], expireAfterSeconds=0)
        self._sessions.create_index([("user_id", ASCENDING), ("created_at", ASCENDING)])
        self._email_verifications.create_index([("token_hash", ASCENDING)], unique=True)
        self._email_verifications.create_index([("expires_at", ASCENDING)], expireAfterSeconds=0)
        self._email_verifications.create_index([("user_id", ASCENDING)])
        self._oauth_identities.create_index([("provider", ASCENDING), ("subject", ASCENDING)], unique=True)
        self._oauth_identities.create_index([("provider", ASCENDING), ("user_id", ASCENDING)], unique=True)

    def create_user(
        self,
        *,
        email: str,
        password_hash: str,
        tenant_id: str,
        roles: frozenset[str],
        status: str = "active",
    ) -> dict[str, Any]:
        now = _now()
        user = {
            "user_id": uuid.uuid4().hex,
            "email": email,
            "email_normalized": email,
            "password_hash": password_hash,
            "tenant_id": tenant_id,
            "roles": sorted(roles),
            "status": status,
            "email_verified_at": now if status == "active" else None,
            "created_at": now,
            "updated_at": now,
            "last_login_at": None,
        }
        try:
            self._users.insert_one(user)
        except DuplicateKeyError as exc:
            raise DuplicateEmailError("该邮箱已注册") from exc
        return self._public_user(user)

    def find_user_by_email(self, email: str) -> dict[str, Any] | None:
        user = self._users.find_one({"email_normalized": email})
        return dict(user) if user else None

    def record_login(self, user_id: str) -> None:
        now = _now()
        self._users.update_one(
            {"user_id": user_id, "status": "active"},
            {"$set": {"last_login_at": now, "updated_at": now}},
        )

    def create_email_verification(self, user_id: str, ttl_seconds: int) -> str:
        token = secrets.token_urlsafe(32)
        now = _now()
        self._email_verifications.delete_many({"user_id": user_id})
        self._email_verifications.insert_one(
            {
                "token_hash": _token_hash(token),
                "user_id": user_id,
                "created_at": now,
                "expires_at": now + timedelta(seconds=ttl_seconds),
            }
        )
        return token

    def verify_email(self, token: str) -> dict[str, Any] | None:
        verification = self._email_verifications.find_one_and_delete(
            {"token_hash": _token_hash(token), "expires_at": {"$gt": _now()}},
        )
        if not verification:
            return None
        now = _now()
        user = self._users.find_one_and_update(
            {"user_id": verification["user_id"], "status": "pending_verification"},
            {"$set": {"status": "active", "email_verified_at": now, "updated_at": now}},
            return_document=ReturnDocument.AFTER,
        )
        return self._public_user(user) if user else None

    def find_or_create_oauth_user(
        self,
        *,
        provider: str,
        subject: str,
        email: str,
        tenant_id: str,
        roles: frozenset[str],
    ) -> dict[str, Any] | None:
        identity = self._oauth_identities.find_one({"provider": provider, "subject": subject})
        if identity:
            user = self._users.find_one({"user_id": identity["user_id"], "status": "active"})
            return self._public_user(user) if user else None

        now = _now()
        user = self._users.find_one({"email_normalized": email})
        if user and user.get("status") not in {"active", "pending_verification"}:
            return None
        if user:
            user = self._users.find_one_and_update(
                {"user_id": user["user_id"]},
                {"$set": {"status": "active", "email_verified_at": now, "updated_at": now}},
                return_document=ReturnDocument.AFTER,
            )
        else:
            try:
                self.create_user(
                    email=email,
                    password_hash="",
                    tenant_id=tenant_id,
                    roles=roles,
                    status="active",
                )
            except DuplicateEmailError:
                pass
            user = self._users.find_one({"email_normalized": email})
        if not user:
            return None

        try:
            self._oauth_identities.insert_one(
                {
                    "provider": provider,
                    "subject": subject,
                    "user_id": user["user_id"],
                    "created_at": now,
                }
            )
        except DuplicateKeyError as exc:
            existing_identity = self._oauth_identities.find_one({"provider": provider, "subject": subject})
            if not existing_identity or existing_identity["user_id"] != user["user_id"]:
                raise OAuthIdentityConflictError("该账号已关联其他 OAuth 身份") from exc
        return self._public_user(user)

    def create_session(self, user_id: str, ttl_seconds: int, auth_type: str = "password") -> str:
        token = secrets.token_urlsafe(32)
        now = _now()
        self._sessions.insert_one(
            {
                "session_id": uuid.uuid4().hex,
                "token_hash": _token_hash(token),
                "user_id": user_id,
                "auth_type": auth_type,
                "created_at": now,
                "expires_at": now + timedelta(seconds=ttl_seconds),
            }
        )
        return token

    def find_user_by_session(self, token: str) -> dict[str, Any] | None:
        session = self._sessions.find_one({"token_hash": _token_hash(token), "expires_at": {"$gt": _now()}})
        if not session:
            return None
        user = self._users.find_one({"user_id": session["user_id"], "status": "active"})
        if not user:
            return None
        public_user = self._public_user(user)
        public_user["auth_type"] = str(session.get("auth_type") or "password")
        return public_user

    def revoke_session(self, token: str) -> None:
        self._sessions.delete_one({"token_hash": _token_hash(token)})

    @staticmethod
    def _public_user(user: dict[str, Any]) -> dict[str, Any]:
        return {
            "user_id": str(user["user_id"]),
            "email": str(user["email"]),
            "tenant_id": str(user["tenant_id"]),
            "roles": [str(role) for role in user.get("roles") or []],
            "status": str(user.get("status") or "active"),
            "created_at": user.get("created_at"),
            "last_login_at": user.get("last_login_at"),
        }


@lru_cache(maxsize=1)
def get_user_store() -> MongoUserStore:
    mongo_url = (os.getenv("MONGO_URL") or "mongodb://127.0.0.1:27017").strip()
    database = (os.getenv("MONGO_DB_NAME") or "equipment_rag").strip()
    return MongoUserStore(mongo_url, database)


def reset_user_store_for_tests() -> None:
    get_user_store.cache_clear()
