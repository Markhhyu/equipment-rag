"""Document identity, lifecycle states, and applicability rules."""

from __future__ import annotations

import hashlib
import re
from enum import StrEnum
from typing import Any


_SAFE_IDENTIFIER = re.compile(r"^[a-zA-Z0-9_.:-]{1,128}$")


class DocumentStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    DISABLED = "disabled"


class VersionStatus(StrEnum):
    IMPORTING = "importing"
    DRAFT = "draft"
    ACTIVE = "active"
    ARCHIVED = "archived"
    FAILED = "failed"


def _identifier(value: str | None, *, fallback: str | None = None) -> str:
    candidate = str(value or fallback or "").strip()
    if not _SAFE_IDENTIFIER.fullmatch(candidate):
        raise ValueError("文档或版本编号只能包含字母、数字、点、下划线、冒号和短横线，长度不超过128")
    return candidate


def _clean_text(value: str | None, limit: int) -> str:
    return str(value or "").strip()[:limit]


APPLICABILITY_FIELDS = (
    "device_model",
    "equipment_version",
    "software_version",
    "firmware_version",
    "hardware_revision",
    "site_id",
)


def build_applicability_profile(values: dict[str, Any]) -> dict[str, Any]:
    """Build the version scope in which only one revision can be active."""
    profile = {field: _clean_text(values.get(field), 128) for field in APPLICABILITY_FIELDS}
    asset_ids = list(
        dict.fromkeys(
            _clean_text(value, 128)
            for value in (values.get("asset_ids") or [])
            if _clean_text(value, 128)
        )
    )[:500]
    scope_fields = APPLICABILITY_FIELDS if profile["equipment_version"] else tuple(
        field for field in APPLICABILITY_FIELDS if field != "equipment_version"
    )
    normalized = "\0".join(
        [profile[field].casefold() for field in scope_fields]
        + [value.casefold() for value in sorted(asset_ids, key=str.casefold)]
    )
    applicability_key = "default" if not normalized.replace("\0", "") else (
        f"scope-{hashlib.sha256(normalized.encode('utf-8')).hexdigest()[:24]}"
    )
    return {**profile, "asset_ids": asset_ids, "applicability_key": applicability_key}


def legacy_document_identity(tenant_id: str, file_title: str) -> tuple[str, str]:
    """Build stable governance identifiers for legacy vector chunks."""
    normalized_title = str(file_title or "").strip().casefold()
    if not normalized_title:
        raise ValueError("旧知识切片缺少文件标题，无法生成稳定文档编号")
    digest = hashlib.sha256(f"{tenant_id}\0{normalized_title}".encode("utf-8")).hexdigest()[:24]
    document_id = f"legacy-{digest}"
    return document_id, f"{document_id}-v1"
