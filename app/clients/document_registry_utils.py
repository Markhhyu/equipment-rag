"""Deprecated compatibility imports for the knowledge document registry."""

from app.modules.knowledge.application.ports import DocumentRegistry
from app.modules.knowledge.application.registry import (
    filter_queryable_hits,
    get_document_registry,
    reset_document_registry_for_tests,
)
from app.modules.knowledge.domain.document import (
    APPLICABILITY_FIELDS,
    DocumentStatus,
    VersionStatus,
    build_applicability_profile,
    legacy_document_identity,
)
from app.modules.knowledge.infrastructure.document_registry import (
    InMemoryDocumentRegistry,
    MongoDocumentRegistry,
)

__all__ = [
    "APPLICABILITY_FIELDS",
    "DocumentRegistry",
    "DocumentStatus",
    "InMemoryDocumentRegistry",
    "MongoDocumentRegistry",
    "VersionStatus",
    "build_applicability_profile",
    "filter_queryable_hits",
    "get_document_registry",
    "legacy_document_identity",
    "reset_document_registry_for_tests",
]
