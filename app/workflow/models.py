from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import AnyHttpUrl, BaseModel, Field


class CaseStatus(StrEnum):
    PENDING = "pending"
    ASSIGNED = "assigned"
    IN_REVIEW = "in_review"
    RESOLVED = "resolved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class WorkflowActionType(StrEnum):
    ASSIGN = "assign"
    START_REVIEW = "start_review"
    RESOLVE = "resolve"
    REJECT = "reject"
    CANCEL = "cancel"


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    ACKNOWLEDGED = "acknowledged"
    FAILED = "failed"


class CreateCaseRequest(BaseModel):
    case_type: str = Field(default="answer_review", min_length=1, max_length=64)
    subject: dict[str, Any] = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)
    callback_url: AnyHttpUrl | None = None
    idempotency_key: str = Field(min_length=1, max_length=128)


class CaseActionRequest(BaseModel):
    action: WorkflowActionType
    assignee: str = Field(default="", max_length=128)
    comment: str = Field(default="", max_length=2000)
    result: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=1, max_length=128)


class CreateSubscriptionRequest(BaseModel):
    connector_type: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")
    callback_url: AnyHttpUrl
    event_types: list[str] = Field(default_factory=lambda: ["review.requested"])
    signing_secret: str = Field(min_length=24, max_length=512)


class DeliveryAckRequest(BaseModel):
    status: DeliveryStatus
    remote_message_id: str = Field(default="", max_length=256)
    error: str = Field(default="", max_length=2000)


class WorkflowCase(BaseModel):
    case_id: str
    tenant_id: str
    case_type: str
    status: CaseStatus
    subject: dict[str, Any]
    context: dict[str, Any]
    callback_url: str
    idempotency_key: str
    assignee: str = ""
    result: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class WorkflowEvent(BaseModel):
    event_id: str
    event_type: str
    occurred_at: datetime
    tenant_id: str
    case_id: str
    subject: dict[str, Any]
    context: dict[str, Any]
    callback_url: str


class WorkflowDelivery(BaseModel):
    delivery_id: str
    subscription_id: str
    event: WorkflowEvent
    callback_url: str
    signature: str
    status: DeliveryStatus
    retry_count: int = 0
    next_retry_at: datetime
    remote_message_id: str = ""
    error: str = ""
