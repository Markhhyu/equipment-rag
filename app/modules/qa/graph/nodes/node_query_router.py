"""Deterministic retrieval planning for equipment questions."""

from __future__ import annotations

import re
import sys
from typing import Any

from app.platform.config.rag_tuning_config import rag_tuning_config
from app.platform.observability.logging import logger
from app.platform.runtime.task_progress import add_done_task, add_running_task


_EXTERNAL_INTENT = re.compile(
    r"(?:联网|网上|互联网|网页|外部资料|官网|官方(?:网站|公告)|最新(?:版|版本|驱动|固件|公告|消息)?|"
    r"近期|今天|当前发布|发布日期|online|internet|official website|latest)",
    re.IGNORECASE,
)
_HIGH_RISK_INTENT = re.compile(
    r"(?:带电|高压|接线|拆机|拆卸|绕过|短接|屏蔽|联锁|安全保护|压力设定|修改参数|校准|"
    r"live wiring|high voltage|bypass|interlock)",
    re.IGNORECASE,
)
_EXACT_LOOKUP_INTENT = re.compile(
    r"(?:故障码|错误码|报警码|代码|参数|规格|额定|默认值|电压|电流|功率|尺寸|重量|范围|"
    r"版本|型号|数值|是多少|多少|多久|多大|error code|fault code|parameter|specification)",
    re.IGNORECASE,
)
_EXPLICIT_FAULT_CODE = re.compile(
    r"(?:故障码|错误码|报警码|error code|fault code).{0,12}[A-Z]{0,5}[-_: ]?\d{1,6}|"
    r"\b(?:ERR(?:OR)?|ALM|FAULT|E|F)[-_: ]?\d{2,6}\b",
    re.IGNORECASE,
)
_TROUBLESHOOTING_INTENT = re.compile(
    r"(?:故障|异常|无法|不能|失败|卡纸|报警|报错|不工作|不开机|断线|连接不上|打印不了|"
    r"无响应|怎么办|如何排查|排查思路|可能原因|为什么|原因是什么|怎么解决|怎么处理|"
    r"troubleshoot|not working|failed|failure|why)",
    re.IGNORECASE,
)


def build_retrieval_plan(
    query: str,
    *,
    hyde_mode: str = "adaptive",
    web_search_mode: str = "explicit",
) -> dict[str, Any]:
    """Classify one query and select only retrieval branches that can add value."""
    text = str(query or "").strip()
    external = bool(_EXTERNAL_INTENT.search(text))
    high_risk = bool(_HIGH_RISK_INTENT.search(text))
    exact_fault_code = bool(_EXPLICIT_FAULT_CODE.search(text))
    exact_lookup = bool(_EXACT_LOOKUP_INTENT.search(text))
    troubleshooting = bool(_TROUBLESHOOTING_INTENT.search(text))

    if high_risk:
        query_type = "high_risk"
    elif external:
        query_type = "external_update"
    elif exact_fault_code:
        query_type = "exact_lookup"
    elif troubleshooting:
        query_type = "troubleshooting"
    elif exact_lookup:
        query_type = "exact_lookup"
    else:
        query_type = "general"

    normalized_hyde_mode = str(hyde_mode or "adaptive").strip().casefold()
    if high_risk:
        use_hyde = False
    elif normalized_hyde_mode == "always":
        use_hyde = True
    elif normalized_hyde_mode == "disabled":
        use_hyde = False
    else:
        use_hyde = troubleshooting and not exact_fault_code and not high_risk

    normalized_web_mode = str(web_search_mode or "explicit").strip().casefold()
    if high_risk or normalized_web_mode == "disabled":
        use_web = False
    elif normalized_web_mode == "always":
        use_web = True
    else:
        use_web = external

    reasons = [f"query_type:{query_type}"]
    reasons.append(f"hyde:{'enabled' if use_hyde else 'skipped'}")
    reasons.append(f"web:{'enabled' if use_web else 'skipped'}")
    if high_risk and external:
        reasons.append("web_blocked_for_high_risk")
    return {
        "query_type": query_type,
        "use_local": True,
        "use_hyde": use_hyde,
        "use_web": use_web,
        "reasons": reasons,
    }


def node_query_router(state):
    """Create the retrieval plan once so parallel branches share the same decision."""
    node_name = sys._getframe().f_code.co_name
    session_id = str(state.get("session_id") or "")
    add_running_task(session_id, node_name, state.get("is_stream"))
    try:
        query = str(state.get("original_query") or state.get("rewritten_query") or "").strip()
        plan = build_retrieval_plan(
            query,
            hyde_mode=rag_tuning_config.hyde_mode,
            web_search_mode=rag_tuning_config.web_search_mode,
        )
        logger.info(
            f"检索路由完成：query_type={plan['query_type']}，"
            f"local={plan['use_local']}，hyde={plan['use_hyde']}，web={plan['use_web']}"
        )
        return {"retrieval_plan": plan}
    finally:
        add_done_task(session_id, node_name, state.get("is_stream"))
