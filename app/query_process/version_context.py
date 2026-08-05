"""Deprecated compatibility imports for governed version resolution."""

from app.modules.knowledge.domain.version_context import (
    VERSION_FIELDS,
    VERSION_LABELS,
    latest_pinned_version_context,
    resolve_version_context,
    version_label,
    version_profile,
    version_scope_id,
)

__all__ = [
    "VERSION_FIELDS",
    "VERSION_LABELS",
    "latest_pinned_version_context",
    "resolve_version_context",
    "version_label",
    "version_profile",
    "version_scope_id",
]
