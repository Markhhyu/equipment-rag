from fastapi import Depends, FastAPI, HTTPException, Query, status

from app.observability.prometheus_metrics import install_prometheus
from app.security.auth import Principal, require_role
from app.security.http import configure_http_security
from app.workflow.models import (
    CaseActionRequest,
    CreateCaseRequest,
    CreateSubscriptionRequest,
    DeliveryAckRequest,
)
from app.workflow.store import get_workflow_store


app = FastAPI(
    title="workflow service",
    description="厂商无关的人工复核工作流接口；外部连接器负责对接企微、钉钉、飞书或 OA",
)
configure_http_security(app)
install_prometheus(app, "workflow-api")


@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/workflow/cases", status_code=status.HTTP_201_CREATED)
async def create_case(request: CreateCaseRequest, principal: Principal = Depends(require_role("workflow"))):
    return get_workflow_store().create_case(principal.tenant_id, request.model_dump(mode="json"), principal.key_id)


@app.get("/workflow/cases/{case_id}")
async def get_case(case_id: str, principal: Principal = Depends(require_role("workflow"))):
    case = get_workflow_store().get_case(principal.tenant_id, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Workflow case not found")
    return case


@app.post("/workflow/cases/{case_id}/actions")
async def apply_action(
    case_id: str,
    request: CaseActionRequest,
    principal: Principal = Depends(require_role("workflow")),
):
    try:
        return get_workflow_store().apply_action(
            principal.tenant_id,
            case_id,
            request.model_dump(mode="json"),
            principal.key_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Workflow case not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/workflow/events")
async def list_events(
    after: str = "",
    limit: int = Query(default=100, ge=1, le=500),
    principal: Principal = Depends(require_role("workflow")),
):
    return {"items": get_workflow_store().list_events(principal.tenant_id, after=after, limit=limit)}


@app.post("/workflow/subscriptions", status_code=status.HTTP_201_CREATED)
async def create_subscription(
    request: CreateSubscriptionRequest,
    principal: Principal = Depends(require_role("workflow")),
):
    return get_workflow_store().create_subscription(principal.tenant_id, request.model_dump(mode="json"))


@app.get("/workflow/deliveries")
async def list_deliveries(
    delivery_status: str = Query(default="pending", alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
    principal: Principal = Depends(require_role("workflow")),
):
    return {
        "items": get_workflow_store().list_deliveries(
            principal.tenant_id,
            status=delivery_status,
            limit=limit,
        )
    }


@app.post("/workflow/deliveries/{delivery_id}/ack")
async def acknowledge_delivery(
    delivery_id: str,
    request: DeliveryAckRequest,
    principal: Principal = Depends(require_role("workflow")),
):
    try:
        return get_workflow_store().ack_delivery(
            principal.tenant_id,
            delivery_id,
            request.model_dump(mode="json"),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Workflow delivery not found") from exc
