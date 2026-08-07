from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol
from urllib.parse import urlencode

import requests

from app.platform.security.config import SecurityConfig, load_security_config


class OAuthProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class OAuthIdentity:
    provider: str
    subject: str
    email: str


class OAuthProvider(Protocol):
    name: str

    def authorization_url(self, *, state: str, redirect_uri: str) -> str: ...

    def exchange_code(self, *, code: str, redirect_uri: str) -> OAuthIdentity: ...


class GitHubOAuthProvider:
    name = "github"
    _authorize_url = "https://github.com/login/oauth/authorize"
    _token_url = "https://github.com/login/oauth/access_token"
    _api_url = "https://api.github.com"

    def __init__(self, config: SecurityConfig) -> None:
        self._client_id = config.github_oauth_client_id
        self._client_secret = config.github_oauth_client_secret
        self._timeout = config.github_oauth_timeout_seconds

    def authorization_url(self, *, state: str, redirect_uri: str) -> str:
        return f"{self._authorize_url}?{urlencode({
            'client_id': self._client_id,
            'redirect_uri': redirect_uri,
            'scope': 'read:user user:email',
            'state': state,
        })}"

    def exchange_code(self, *, code: str, redirect_uri: str) -> OAuthIdentity:
        try:
            token_response = requests.post(
                self._token_url,
                headers={"Accept": "application/json"},
                data={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "code": code,
                    "redirect_uri": redirect_uri,
                },
                timeout=self._timeout,
            )
            token_response.raise_for_status()
            token_payload = token_response.json()
            if not isinstance(token_payload, dict):
                raise OAuthProviderError("GitHub returned an invalid token response")
            access_token = str(token_payload.get("access_token") or "")
            if not access_token:
                raise OAuthProviderError("GitHub did not return an access token")

            headers = {
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {access_token}",
                "User-Agent": "equipment-rag-agent",
                "X-GitHub-Api-Version": "2022-11-28",
            }
            user_response = requests.get(f"{self._api_url}/user", headers=headers, timeout=self._timeout)
            user_response.raise_for_status()
            user_payload = user_response.json()
            if not isinstance(user_payload, dict):
                raise OAuthProviderError("GitHub returned an invalid user response")
            subject = str(user_payload.get("id") or "")
            if not subject:
                raise OAuthProviderError("GitHub user response did not contain an id")

            emails_response = requests.get(f"{self._api_url}/user/emails", headers=headers, timeout=self._timeout)
            emails_response.raise_for_status()
            emails_payload = emails_response.json()
            if not isinstance(emails_payload, list):
                raise OAuthProviderError("GitHub returned an invalid email response")
            verified_emails = [
                item
                for item in emails_payload
                if isinstance(item, dict) and item.get("verified") is True and item.get("email")
            ]
            verified_emails.sort(key=lambda item: item.get("primary") is True, reverse=True)
            if not verified_emails:
                raise OAuthProviderError("GitHub account has no verified email")
            return OAuthIdentity(provider=self.name, subject=subject, email=str(verified_emails[0]["email"]))
        except OAuthProviderError:
            raise
        except (requests.RequestException, TypeError, ValueError) as exc:
            raise OAuthProviderError("GitHub OAuth request failed") from exc


@lru_cache(maxsize=4)
def get_oauth_provider(name: str) -> OAuthProvider | None:
    config = load_security_config()
    if name == "github" and config.github_oauth_enabled:
        return GitHubOAuthProvider(config)
    return None


def reset_oauth_providers_for_tests() -> None:
    get_oauth_provider.cache_clear()
