from __future__ import annotations

from typing import Protocol

from app.modules.workflow.domain.models import WorkflowDelivery


class WorkflowConnector(Protocol):
    """Boundary implemented by WeCom, DingTalk, Feishu, OA, or custom adapters."""

    connector_type: str

    def deliver(self, delivery: WorkflowDelivery) -> str:
        """Deliver one signed event and return the remote message identifier."""
        ...
