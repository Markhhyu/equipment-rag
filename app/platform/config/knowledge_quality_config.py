"""Knowledge import quality-gate configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import find_dotenv, load_dotenv


load_dotenv(find_dotenv())


def _bool_env(name: str, default: bool) -> bool:
    value = str(os.getenv(name, str(default))).strip().casefold()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


def _ratio_env(name: str, default: float) -> float:
    try:
        return min(1.0, max(0.0, float(os.getenv(name, str(default)))))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class KnowledgeQualityConfig:
    enabled: bool
    min_score: float
    min_healthy_chunk_ratio: float
    max_duplicate_ratio: float
    min_item_name_coverage: float
    reject_replacement_characters: bool


def load_knowledge_quality_config() -> KnowledgeQualityConfig:
    return KnowledgeQualityConfig(
        enabled=_bool_env("KNOWLEDGE_QUALITY_GATE_ENABLED", True),
        min_score=_ratio_env("KNOWLEDGE_QUALITY_MIN_SCORE", 0.75),
        min_healthy_chunk_ratio=_ratio_env("KNOWLEDGE_QUALITY_MIN_HEALTHY_CHUNK_RATIO", 0.50),
        max_duplicate_ratio=_ratio_env("KNOWLEDGE_QUALITY_MAX_DUPLICATE_RATIO", 0.05),
        min_item_name_coverage=_ratio_env("KNOWLEDGE_QUALITY_MIN_ITEM_NAME_COVERAGE", 0.80),
        reject_replacement_characters=_bool_env("KNOWLEDGE_QUALITY_REJECT_REPLACEMENT_CHARACTERS", True),
    )


knowledge_quality_config = load_knowledge_quality_config()
