"""Connector composition kept outside workflow domain and application rules."""

from app.modules.workflow.application.connector_config_service import get_connector_config_service
from app.modules.workflow.connectors.base import WorkflowConnector


def get_enabled_workflow_connectors(tenant_id: str) -> tuple[WorkflowConnector, ...]:
    return get_connector_config_service().connectors_for_tenant(tenant_id)


def reset_workflow_connectors_for_tests() -> None:
    return None
