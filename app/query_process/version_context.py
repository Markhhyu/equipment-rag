from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from typing import Any, Iterable


VERSION_FIELDS = (
    "device_model",
    "equipment_version",
    "software_version",
    "firmware_version",
    "hardware_revision",
    "site_id",
    "asset_ids",
)
VERSION_LABELS = ("型号", "设备版本", "软件", "固件", "硬件", "厂区", "设备编号")


def version_profile(version: dict[str, Any]) -> tuple[str, ...]:
    values = tuple(str(version.get(field) or "").strip() for field in VERSION_FIELDS[:-1])
    raw_asset_ids = version.get("asset_ids") or []
    if isinstance(raw_asset_ids, str):
        asset_ids = raw_asset_ids.strip()
    else:
        asset_ids = "、".join(sorted(str(value).strip() for value in raw_asset_ids if str(value).strip()))
    return values + (asset_ids,)


def version_scope_id(profile: tuple[str, ...]) -> str:
    normalized = "\0".join(value.casefold() for value in profile)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20]


def version_label(profile: tuple[str, ...], varying_indexes: set[int] | None = None) -> str:
    parts = []
    for index, (label, value) in enumerate(zip(VERSION_LABELS, profile)):
        if value:
            parts.append(f"{label} {value}")
        elif varying_indexes and index in varying_indexes:
            parts.append(f"{label} 未指定")
    return " / ".join(parts) if parts else "通用版本（未限定设备配置）"


def _normalize(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "").casefold())


def _choice(version: dict[str, Any], varying_indexes: set[int]) -> dict[str, Any]:
    profile = version_profile(version)
    return {
        "document_id": str(version.get("document_id") or ""),
        "revision_id": str(version.get("revision_id") or ""),
        "scope_id": version_scope_id(profile),
        "label": version_label(profile, varying_indexes),
        "version_label": str(version.get("version_label") or ""),
        "item_names": [str(value) for value in version.get("item_names") or [] if str(value).strip()],
        **{field: value for field, value in zip(VERSION_FIELDS, profile)},
    }


def latest_pinned_version_context(history: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    for message in reversed(list(history or [])):
        if message.get("version_scope_options"):
            return []
        context = message.get("selected_version_context")
        if isinstance(context, list) and context:
            return [dict(item) for item in context if isinstance(item, dict)]
    return []


def resolve_version_context(
    question: str,
    active_versions: Iterable[dict[str, Any]],
    *,
    selected_scope_id: str = "",
    pinned_context: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Resolve governed revisions before retrieval and return a vendor-neutral context payload."""
    versions_by_document: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for version in active_versions or []:
        document_id = str(version.get("document_id") or "").strip()
        revision_id = str(version.get("revision_id") or "").strip()
        if document_id and revision_id:
            versions_by_document[document_id].append(dict(version))
    if not versions_by_document:
        return {
            "status": "not_found",
            "revision_ids": [],
            "selected_scopes": [],
            "version_scope_options": [],
        }

    normalized_question = _normalize(question)
    requested_scope_id = str(selected_scope_id or "").strip().casefold()
    pins_by_document = {
        str(item.get("document_id") or ""): item
        for item in pinned_context or []
        if isinstance(item, dict) and item.get("document_id")
    }
    selected_scopes: list[dict[str, Any]] = []
    options: list[dict[str, Any]] = []

    for document_id, versions in sorted(versions_by_document.items()):
        profiles = {version_profile(version) for version in versions}
        varying_indexes = {
            index
            for index in range(len(next(iter(profiles))))
            if len({profile[index].casefold() for profile in profiles}) > 1
        }
        choices = [_choice(version, varying_indexes) for version in versions]
        choices_by_scope = {str(choice["scope_id"]): choice for choice in choices}

        selected: dict[str, Any] | None = None
        if requested_scope_id:
            selected = choices_by_scope.get(requested_scope_id)

        if selected is None and len(choices_by_scope) > 1:
            explicit_matches = [
                choice
                for choice in choices_by_scope.values()
                if any(
                    profile_value and _normalize(profile_value) in normalized_question
                    for index, profile_value in enumerate(version_profile(choice))
                    if index in varying_indexes
                )
                or f"version_scope:{choice['scope_id']}" in normalized_question
            ]
            if len(explicit_matches) == 1:
                selected = explicit_matches[0]

        if selected is None:
            pin = pins_by_document.get(document_id)
            if pin:
                pin_revision = str(pin.get("revision_id") or "")
                pin_scope = str(pin.get("scope_id") or "")
                selected = next(
                    (
                        choice
                        for choice in choices_by_scope.values()
                        if str(choice.get("revision_id") or "") == pin_revision
                        or str(choice.get("scope_id") or "") == pin_scope
                    ),
                    None,
                )

        if selected is None and len(choices_by_scope) == 1:
            selected = next(iter(choices_by_scope.values()))

        if selected is not None:
            selected_scopes.append(selected)
            continue

        ordered_choices = sorted(choices_by_scope.values(), key=lambda item: (str(item["label"]), str(item["revision_id"])))
        options.append(
            {
                "document_id": document_id,
                "options": [str(choice["label"]) for choice in ordered_choices],
                "choices": ordered_choices,
            }
        )

    return {
        "status": "ambiguous" if options else "resolved",
        "revision_ids": [str(item["revision_id"]) for item in selected_scopes],
        "selected_scopes": selected_scopes,
        "version_scope_options": options,
    }
