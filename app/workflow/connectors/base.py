"""Deprecated compatibility import for workflow connector contracts."""

from app.modules.workflow.connectors.base import StartedWorkflow, WorkflowConnector, WorkflowConnectorError

__all__ = ["StartedWorkflow", "WorkflowConnector", "WorkflowConnectorError"]
