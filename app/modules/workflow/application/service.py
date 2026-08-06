"""Application orchestration for local cases and optional external workflows."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any, Iterable

from app.modules.workflow.connectors.base import WorkflowConnector, WorkflowConnectorError
from app.modules.workflow.domain.models import WorkflowCase
from app.modules.workflow.infrastructure.store import InMemoryWorkflowStore, get_workflow_store


class WorkflowDispatchError(RuntimeError):
    def __init__(self, case_id: str, failures: list[str]) -> None:
        self.case_id = case_id
        self.failures = failures
        super().__init__("；".join(failures))


class WorkflowService:
    """Create the local audit record first, then trigger configured external systems."""

    def __init__(
        self,
        store: InMemoryWorkflowStore,
        connectors: Iterable[WorkflowConnector] = (),
        connector_provider: Callable[[str], Iterable[WorkflowConnector]] | None = None,
    ) -> None:
        self.store = store
        self.connectors = tuple(connectors)
        self.connector_provider = connector_provider

    def create_case(self, tenant_id: str, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        case_data = self.store.create_case(tenant_id, payload, actor)
        existing_types = {
            str(reference.get("connector_type") or "") for reference in case_data.get("external_workflows") or []
        }
        failures: list[str] = []
        case = WorkflowCase.model_validate(case_data)

        try:
            connectors = self.connector_provider(tenant_id) if self.connector_provider else self.connectors
        except WorkflowConnectorError as exc:
            raise WorkflowDispatchError(case.case_id, [f"connector_config: {exc}"]) from exc
        for connector in connectors:
            if connector.connector_type in existing_types:
                continue
            try:
                started = connector.start_case(case)
                case_data = self.store.attach_external_workflow(
                    tenant_id,
                    case.case_id,
                    connector.connector_type,
                    started.instance_id,
                    started.status,
                )
                existing_types.add(connector.connector_type)
            except WorkflowConnectorError as exc:
                failures.append(f"{connector.connector_type}: {exc}")

        if failures:
            raise WorkflowDispatchError(case.case_id, failures)
        return case_data


_service: WorkflowService | None = None
_service_lock = threading.RLock()


def get_workflow_service() -> WorkflowService:
    global _service
    if _service is not None:
        return _service
    with _service_lock:
        if _service is None:
            from app.modules.workflow.connectors.factory import get_enabled_workflow_connectors

            _service = WorkflowService(get_workflow_store(), connector_provider=get_enabled_workflow_connectors)
        return _service


def reset_workflow_service_for_tests(service: WorkflowService | None = None) -> None:
    global _service
    with _service_lock:
        _service = service
