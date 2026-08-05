"""Public knowledge registry access used by ingestion and query use cases."""

from __future__ import annotations

from typing import Any, Iterable

from app.modules.knowledge.application.ports import DocumentRegistry


def get_document_registry() -> DocumentRegistry:
    from app.modules.knowledge.infrastructure.document_registry import get_document_registry as resolve

    return resolve()


def reset_document_registry_for_tests(registry: DocumentRegistry | None = None) -> None:
    from app.modules.knowledge.infrastructure.document_registry import reset_document_registry_for_tests as reset

    reset(registry)


def filter_queryable_hits(tenant_id: str, hits: Iterable[Any]) -> list[Any]:
    from app.modules.knowledge.infrastructure.document_registry import filter_queryable_hits as filter_hits

    return filter_hits(tenant_id, hits)
