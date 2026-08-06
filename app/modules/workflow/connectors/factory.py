"""Connector composition kept outside workflow domain and application rules."""

from functools import lru_cache

from app.modules.workflow.connectors.base import WorkflowConnector
from app.modules.workflow.connectors.feishu import FeishuApprovalConfig, FeishuApprovalConnector


@lru_cache(maxsize=1)
def get_enabled_workflow_connectors() -> tuple[WorkflowConnector, ...]:
    connectors: list[WorkflowConnector] = []
    feishu_config = FeishuApprovalConfig.from_env()
    if feishu_config.enabled:
        connectors.append(FeishuApprovalConnector(feishu_config))
    return tuple(connectors)


def reset_workflow_connectors_for_tests() -> None:
    get_enabled_workflow_connectors.cache_clear()
