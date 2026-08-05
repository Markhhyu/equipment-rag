"""Deprecated compatibility imports for knowledge trust rules."""

from app.modules.knowledge.domain.trust import (
    AnswerPolicyDecision,
    TRUST_LEVELS,
    assess_answer_policy,
    enforce_trust_precedence,
    normalize_trust_level,
    trust_metadata,
)

__all__ = [
    "AnswerPolicyDecision",
    "TRUST_LEVELS",
    "assess_answer_policy",
    "enforce_trust_precedence",
    "normalize_trust_level",
    "trust_metadata",
]
