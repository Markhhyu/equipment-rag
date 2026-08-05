"""Repository contracts required by knowledge governance use cases."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Iterable


class DocumentRegistry(ABC):
    """Knowledge governance repository independent of the persistence provider."""

    @abstractmethod
    def register_import(
        self,
        *,
        tenant_id: str,
        revision_id: str,
        filename: str,
        title: str = "",
        document_id: str | None = None,
        version_label: str = "",
        trust_level: str = "manufacturer_manual",
        source_object_uri: str = "",
        content_hash: str = "",
        file_size: int = 0,
        publish_requested: bool = False,
        device_model: str = "",
        equipment_version: str = "",
        software_version: str = "",
        firmware_version: str = "",
        hardware_revision: str = "",
        site_id: str = "",
        asset_ids: Iterable[str] = (),
        actor: str = "system",
    ) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def mark_import_succeeded(
        self,
        tenant_id: str,
        revision_id: str,
        *,
        chunk_count: int,
        image_count: int,
        item_names: Iterable[str],
        actor: str = "system",
    ) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def mark_import_failed(self, tenant_id: str, revision_id: str, error: str, actor: str = "system") -> None:
        raise NotImplementedError

    @abstractmethod
    def publish_version(
        self,
        tenant_id: str,
        document_id: str,
        revision_id: str,
        *,
        actor: str,
        action: str = "publish",
    ) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def disable_document(self, tenant_id: str, document_id: str, *, actor: str) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def enable_document(self, tenant_id: str, document_id: str, *, actor: str) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def list_documents(
        self,
        tenant_id: str,
        *,
        status: str = "",
        query: str = "",
        skip: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def get_document(self, tenant_id: str, document_id: str) -> dict[str, Any] | None:
        raise NotImplementedError

    @abstractmethod
    def list_active_versions(self, tenant_id: str, item_names: Iterable[str]) -> list[dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    def list_audit_logs(self, tenant_id: str, document_id: str = "", limit: int = 100) -> list[dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    def managed_revision_access(
        self,
        tenant_id: str,
        revisions: Iterable[tuple[str, str]],
    ) -> dict[tuple[str, str], bool]:
        raise NotImplementedError
