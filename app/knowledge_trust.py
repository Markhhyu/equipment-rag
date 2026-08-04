from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable


TRUST_LEVELS = {
    "enterprise_sop": {"label": "企业批准 SOP", "rank": 400, "authoritative": True},
    "manufacturer_manual": {"label": "厂商手册", "rank": 300, "authoritative": True},
    "internal_reference": {"label": "内部参考", "rank": 200, "authoritative": False},
    "external_web": {"label": "外部网页", "rank": 100, "authoritative": False},
}

_BYPASS_ACTION = re.compile(
    r"(?:绕过|短接|拆除|屏蔽|取消|关闭|禁用|跳过|bypass|disable|remove|override).{0,16}"
    r"(?:安全联锁|联锁|安全保护|保护装置|急停|安全门|interlock|safety)",
    re.IGNORECASE,
)
_BYPASS_REVERSED = re.compile(
    r"(?:安全联锁|联锁|安全保护|保护装置|急停|安全门|interlock|safety).{0,16}"
    r"(?:绕过|短接|拆除|屏蔽|取消|关闭|禁用|跳过|bypass|disable|remove|override)",
    re.IGNORECASE,
)
_HIGH_RISK_ACTION = re.compile(
    r"(?:接线|带电|高压|高温|压力设定|修改参数|调整参数|校准|拆机|拆卸|维修|复位|"
    r"启动|停机|操作步骤|怎么操作|如何操作|wiring|high voltage|pressure setting|calibrat|repair)",
    re.IGNORECASE,
)


def normalize_trust_level(value: Any, *, source: str = "local") -> str:
    """Normalize persisted trust metadata and keep legacy local manuals queryable."""
    normalized = str(value or "").strip().casefold()
    if normalized in TRUST_LEVELS:
        return normalized
    if normalized:
        # Unknown explicit values must never be promoted to an authoritative level.
        return "external_web" if str(source or "").casefold() == "web" else "internal_reference"
    return "external_web" if str(source or "").casefold() == "web" else "manufacturer_manual"


def trust_metadata(value: Any, *, source: str = "local") -> dict[str, Any]:
    level = normalize_trust_level(value, source=source)
    metadata = TRUST_LEVELS[level]
    return {
        "trust_level": level,
        "trust_label": metadata["label"],
        "trust_rank": metadata["rank"],
        "authoritative": metadata["authoritative"],
    }


def enforce_trust_precedence(documents: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Exclude unverified web evidence whenever authoritative local evidence is available."""
    docs = [document for document in documents if isinstance(document, dict)]
    has_authoritative_local = any(
        str(document.get("source") or "local") == "local"
        and bool(
            document.get("authoritative")
            if "authoritative" in document
            else trust_metadata(document.get("trust_level"), source="local")["authoritative"]
        )
        for document in docs
    )
    if not has_authoritative_local:
        return docs
    return [
        document
        for document in docs
        if normalize_trust_level(
            document.get("trust_level"), source=str(document.get("source") or "local")
        )
        != "external_web"
    ]


@dataclass(frozen=True)
class AnswerPolicyDecision:
    action: str = "answer"
    requires_human_review: bool = False
    review_reason: str = ""
    answer: str = ""


def assess_answer_policy(question: str, documents: Iterable[dict[str, Any]]) -> AnswerPolicyDecision:
    """Apply deterministic safety gates before any generative answer is produced."""
    query = str(question or "").strip()
    docs = [document for document in documents if isinstance(document, dict)]
    has_authoritative_evidence = any(
        bool(
            document.get("authoritative")
            if "authoritative" in document
            else trust_metadata(document.get("trust_level"), source=str(document.get("source") or "local"))[
                "authoritative"
            ]
        )
        for document in docs
    )

    if _BYPASS_ACTION.search(query) or _BYPASS_REVERSED.search(query):
        return AnswerPolicyDecision(
            action="refuse",
            requires_human_review=True,
            review_reason="请求涉及绕过或停用安全保护，必须由设备安全负责人审核。",
            answer=(
                "我不能提供绕过、短接、拆除或停用安全联锁的操作方法。"
                "请停止相关操作，保持安全保护有效，并联系设备安全负责人或厂商技术支持进行现场评估。"
            ),
        )

    if _HIGH_RISK_ACTION.search(query) and not has_authoritative_evidence:
        return AnswerPolicyDecision(
            action="review",
            requires_human_review=True,
            review_reason="当前只有内部参考或外部网页，缺少企业批准 SOP 或厂商手册作为操作依据。",
            answer=(
                "当前证据不足以支持这项操作，我不能给出具体步骤或参数。"
                "请补充适用版本的企业批准 SOP 或厂商手册，并由设备工程师确认后再执行。"
            ),
        )

    return AnswerPolicyDecision()
