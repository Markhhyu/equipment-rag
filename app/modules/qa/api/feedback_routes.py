"""Query feedback, resolution, and operational analytics routes."""

from fastapi import APIRouter, Depends, HTTPException, Query

from app.modules.analytics.infrastructure.store import get_query_analytics_store
from app.modules.qa.api.schemas import FeedbackRequest, ResolutionRequest
from app.modules.qa.infrastructure.history import update_message_feedback, update_message_resolution
from app.platform.observability.langfuse_monitor import submit_trace_feedback
from app.platform.observability.logging import logger
from app.platform.observability.prometheus_metrics import observe_feedback
from app.platform.runtime.run_store import RunStatus, get_run_store
from app.platform.security.auth import Principal, require_role


router = APIRouter()


@router.post("/feedback")
async def submit_feedback(
    request: FeedbackRequest,
    principal: Principal = Depends(require_role("query")),
):
    """接收聊天页面点赞或点踩，并同步写入Langfuse和MongoDB。"""
    try:
        run = get_run_store().get_for_tenant(request.trace_id, principal.tenant_id)
        if run is None or run.kind != "query":
            raise HTTPException(status_code=404, detail="Query run not found")

        submit_trace_feedback(request.trace_id, request.value, request.comment or "")
        matched_count = update_message_feedback(
            request.trace_id,
            request.value,
            request.comment or "",
        )
        get_query_analytics_store().record_feedback(principal.tenant_id, request.trace_id, request.value)
        observe_feedback(request.value)

        if matched_count == 0:
            logger.warning(f"反馈已处理，但MongoDB未找到对应回答，trace_id={request.trace_id}")

        logger.info(f"用户反馈提交成功，trace_id={request.trace_id}，value={request.value}")
        return {
            "message": "反馈已记录",
            "trace_id": request.trace_id,
            "value": request.value,
            "history_updated": matched_count > 0,
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException:
        raise
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception(f"用户反馈提交失败，trace_id={request.trace_id}，错误={exc}")
        raise HTTPException(status_code=500, detail="用户反馈提交失败") from exc


@router.post("/resolution")
async def submit_resolution(
    request: ResolutionRequest,
    principal: Principal = Depends(require_role("query")),
):
    """记录用户确认的解决结果；该口径与点赞/点踩相互独立。"""
    try:
        run = get_run_store().get_for_tenant(request.trace_id, principal.tenant_id)
        if run is None or run.kind != "query":
            raise HTTPException(status_code=404, detail="Query run not found")
        if run.status != RunStatus.SUCCEEDED:
            raise HTTPException(status_code=409, detail="只有已经完成的问答可以确认解决结果")

        get_query_analytics_store().record_resolution(
            principal.tenant_id,
            request.trace_id,
            request.status,
            request.comment or "",
        )
        matched_count = update_message_resolution(request.trace_id, request.status, request.comment or "")
        return {
            "message": "解决结果已记录",
            "trace_id": request.trace_id,
            "status": request.status,
            "history_updated": matched_count > 0,
        }
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception(f"解决结果提交失败，trace_id={request.trace_id}，错误={exc}")
        raise HTTPException(status_code=500, detail="解决结果提交失败") from exc


@router.get("/analytics/summary")
async def analytics_summary(
    days: int = Query(default=7, ge=1, le=365),
    timezone_offset_minutes: int = Query(default=480, ge=-720, le=840),
    principal: Principal = Depends(require_role("query")),
):
    """返回当前租户的问答运营指标、每日趋势和待关注问题。"""
    try:
        return get_query_analytics_store().summary(
            principal.tenant_id,
            days,
            timezone_offset_minutes,
        )
    except Exception as exc:
        logger.exception(f"问答运营统计查询失败，tenant_id={principal.tenant_id}，错误={exc}")
        raise HTTPException(status_code=503, detail="问答运营统计暂时不可用") from exc
