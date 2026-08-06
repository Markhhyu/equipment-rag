"""Persistence ports used by workflow connector configuration."""

from __future__ import annotations

from typing import Any, Protocol


class ConnectorConfigRepository(Protocol):
    def get(self, tenant_id: str, connector_type: str) -> dict[str, Any] | None: ...

    def save(
        self,
        tenant_id: str,
        connector_type: str,
        config: dict[str, Any],
        encrypted_secret: str,
        actor: str,
    ) -> dict[str, Any]: ...

    def delete(self, tenant_id: str, connector_type: str) -> bool: ...
