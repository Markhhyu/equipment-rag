"""Connector contracts implemented outside the workflow core."""

from app.modules.workflow.connectors.base import StartedWorkflow, WorkflowConnector, WorkflowConnectorError

__all__ = ["StartedWorkflow", "WorkflowConnector", "WorkflowConnectorError"]
