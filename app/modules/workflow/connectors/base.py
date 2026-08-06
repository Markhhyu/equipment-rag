from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.modules.workflow.domain.models import WorkflowCase


@dataclass(frozen=True)
class StartedWorkflow:
    """Vendor-neutral result returned after an external workflow is started."""

    instance_id: str
    status: str = "started"


class WorkflowConnectorError(RuntimeError):
    """Safe connector failure that can be returned without exposing credentials."""


class WorkflowConnector(Protocol):
    """Boundary implemented by WeCom, DingTalk, Feishu, OA, or custom adapters."""

    connector_type: str

    def start_case(self, case: WorkflowCase) -> StartedWorkflow:
        """Start one external workflow for a vendor-neutral case."""
        ...
