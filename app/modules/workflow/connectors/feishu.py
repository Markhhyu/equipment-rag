"""Feishu Approval adapter for vendor-neutral workflow cases."""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

import requests
from dotenv import load_dotenv

from app.modules.workflow.connectors.base import StartedWorkflow, WorkflowConnectorError
from app.modules.workflow.domain.models import WorkflowCase


load_dotenv()

_USER_ID_TYPES = {"open_id", "user_id", "union_id"}
_DEFAULT_FIELD_TYPES = {
    "subject.question": "textarea",
    "context.answer": "textarea",
    "context.review_reason": "textarea",
}


def _as_bool(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _positive_int(value: str | None, default: int) -> int:
    try:
        parsed = int(str(value or ""))
    except ValueError:
        return default
    return parsed if parsed > 0 else default


@dataclass(frozen=True)
class FeishuFormField:
    source: str
    control_id: str
    control_type: str
    max_length: int = 2000


def _parse_form_fields(raw: str) -> tuple[FeishuFormField, ...]:
    if not raw.strip():
        return ()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("FEISHU_APPROVAL_FORM_FIELDS_JSON 必须是有效 JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("FEISHU_APPROVAL_FORM_FIELDS_JSON 必须是字段路径到控件配置的 JSON 对象")

    fields: list[FeishuFormField] = []
    used_ids: set[str] = set()
    for source, config in parsed.items():
        safe_source = str(source or "").strip()
        if isinstance(config, str):
            control_id = config.strip()
            control_type = _DEFAULT_FIELD_TYPES.get(safe_source, "input")
            max_length = 2000
        elif isinstance(config, dict):
            control_id = str(config.get("id") or "").strip()
            control_type = str(config.get("type") or _DEFAULT_FIELD_TYPES.get(safe_source, "input")).strip()
            max_length = _positive_int(str(config.get("max_length") or ""), 2000)
        else:
            raise ValueError(f"飞书表单字段 {safe_source!r} 的配置必须是控件 ID 或 JSON 对象")
        if not safe_source or not control_id or not control_type:
            raise ValueError("飞书表单字段路径、控件 ID 和控件类型不能为空")
        if control_id in used_ids:
            raise ValueError(f"飞书表单控件 ID 重复：{control_id}")
        used_ids.add(control_id)
        fields.append(FeishuFormField(safe_source, control_id, control_type, max_length))
    return tuple(fields)


@dataclass(frozen=True)
class FeishuApprovalConfig:
    enabled: bool = False
    app_id: str = ""
    app_secret: str = field(default="", repr=False)
    approval_code: str = ""
    initiator_user_id: str = ""
    user_id_type: str = "open_id"
    form_fields: tuple[FeishuFormField, ...] = ()
    base_url: str = "https://open.feishu.cn"
    timeout_seconds: int = 10

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> FeishuApprovalConfig:
        values = environ if environ is not None else os.environ
        config = cls(
            enabled=_as_bool(values.get("FEISHU_WORKFLOW_ENABLED")),
            app_id=str(values.get("FEISHU_APP_ID") or "").strip(),
            app_secret=str(values.get("FEISHU_APP_SECRET") or "").strip(),
            approval_code=str(values.get("FEISHU_APPROVAL_CODE") or "").strip(),
            initiator_user_id=str(values.get("FEISHU_APPROVAL_INITIATOR_ID") or "").strip(),
            user_id_type=str(values.get("FEISHU_APPROVAL_USER_ID_TYPE") or "open_id").strip(),
            form_fields=_parse_form_fields(str(values.get("FEISHU_APPROVAL_FORM_FIELDS_JSON") or "")),
            base_url=str(values.get("FEISHU_OPEN_BASE_URL") or "https://open.feishu.cn").strip().rstrip("/"),
            timeout_seconds=_positive_int(values.get("FEISHU_REQUEST_TIMEOUT_SECONDS"), 10),
        )
        config.validate()
        return config

    @classmethod
    def from_values(cls, values: Mapping[str, Any], app_secret: str) -> FeishuApprovalConfig:
        form_fields = values.get("form_fields") or {}
        config = cls(
            enabled=bool(values.get("enabled")),
            app_id=str(values.get("app_id") or "").strip(),
            app_secret=app_secret.strip(),
            approval_code=str(values.get("approval_code") or "").strip(),
            initiator_user_id=str(values.get("initiator_user_id") or "").strip(),
            user_id_type=str(values.get("user_id_type") or "open_id").strip(),
            form_fields=_parse_form_fields(json.dumps(form_fields, ensure_ascii=False)),
            base_url=str(values.get("base_url") or "https://open.feishu.cn").strip().rstrip("/"),
            timeout_seconds=_positive_int(str(values.get("timeout_seconds") or ""), 10),
        )
        config.validate()
        return config

    def storage_values(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "app_id": self.app_id,
            "approval_code": self.approval_code,
            "initiator_user_id": self.initiator_user_id,
            "user_id_type": self.user_id_type,
            "form_fields": {
                item.source: {
                    "id": item.control_id,
                    "type": item.control_type,
                    "max_length": item.max_length,
                }
                for item in self.form_fields
            },
            "base_url": self.base_url,
            "timeout_seconds": self.timeout_seconds,
        }

    def validate(self) -> None:
        if self.user_id_type not in _USER_ID_TYPES:
            raise ValueError("FEISHU_APPROVAL_USER_ID_TYPE 仅支持 open_id、user_id 或 union_id")
        if not self.enabled:
            return
        missing = [
            name
            for name, value in (
                ("FEISHU_APP_ID", self.app_id),
                ("FEISHU_APP_SECRET", self.app_secret),
                ("FEISHU_APPROVAL_CODE", self.approval_code),
                ("FEISHU_APPROVAL_INITIATOR_ID", self.initiator_user_id),
                ("FEISHU_APPROVAL_FORM_FIELDS_JSON", self.form_fields),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"已启用飞书工作流，但缺少配置：{', '.join(missing)}")


def _source_value(case_data: dict[str, Any], source: str) -> Any:
    value: Any = case_data
    for part in source.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _form_value(value: Any, max_length: int) -> str:
    if isinstance(value, bool):
        text = "是" if value else "否"
    elif isinstance(value, list) and all(not isinstance(item, (dict, list)) for item in value):
        text = "、".join(str(item) for item in value if item is not None)
    elif isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    else:
        text = str(value if value is not None else "")
    return text[:max_length]


class FeishuApprovalConnector:
    connector_type = "feishu_approval"

    def __init__(self, config: FeishuApprovalConfig, session: requests.Session | None = None) -> None:
        config.validate()
        self.config = config
        self.session = session or requests.Session()
        self._token = ""
        self._token_expires_at = 0.0
        self._token_lock = threading.RLock()

    def start_case(self, case: WorkflowCase) -> StartedWorkflow:
        form = self._build_form(case)
        payload = {
            "approval_code": self.config.approval_code,
            "user_id": self.config.initiator_user_id,
            "form": json.dumps(form, ensure_ascii=False, separators=(",", ":")),
            "uuid": case.case_id,
        }
        response = self._post_json(
            "/open-apis/approval/v4/instances",
            payload,
            headers={"Authorization": f"Bearer {self._tenant_access_token()}"},
            params={"user_id_type": self.config.user_id_type},
        )
        instance_id = str((response.get("data") or {}).get("instance_code") or "").strip()
        if not instance_id:
            raise WorkflowConnectorError("飞书未返回审批实例编号")
        return StartedWorkflow(instance_id=instance_id)

    def check_connection(self) -> dict[str, str]:
        response = self._get_json(
            f"/open-apis/approval/v4/approvals/{self.config.approval_code}",
            headers={"Authorization": f"Bearer {self._tenant_access_token()}"},
            params={"user_id_type": self.config.user_id_type},
        )
        data = response.get("data") or {}
        name = str(data.get("approval_name") or data.get("name") or "").strip()
        return {"approval_code": self.config.approval_code, "approval_name": name}

    def _build_form(self, case: WorkflowCase) -> list[dict[str, str]]:
        case_data = case.model_dump(mode="json")
        form: list[dict[str, str]] = []
        for field_config in self.config.form_fields:
            value = _source_value(case_data, field_config.source)
            if value is None or value == "" or value == []:
                continue
            form.append(
                {
                    "id": field_config.control_id,
                    "type": field_config.control_type,
                    "value": _form_value(value, field_config.max_length),
                }
            )
        return form

    def _tenant_access_token(self) -> str:
        with self._token_lock:
            now = time.monotonic()
            if self._token and now < self._token_expires_at:
                return self._token
            response = self._post_json(
                "/open-apis/auth/v3/tenant_access_token/internal",
                {"app_id": self.config.app_id, "app_secret": self.config.app_secret},
            )
            token = str(response.get("tenant_access_token") or "").strip()
            if not token:
                raise WorkflowConnectorError("飞书未返回 tenant_access_token")
            expires_in = _positive_int(str(response.get("expire") or ""), 7200)
            self._token = token
            self._token_expires_at = now + max(expires_in - 60, 1)
            return token

    def _post_json(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        return self._request_json("post", path, payload=payload, headers=headers, params=params)

    def _get_json(
        self,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        return self._request_json("get", path, headers=headers, params=params)

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        try:
            request = getattr(self.session, method)
            response = request(
                f"{self.config.base_url}{path}",
                **({"json": payload} if payload is not None else {}),
                headers=headers,
                params=params,
                timeout=self.config.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise WorkflowConnectorError(f"飞书接口连接失败：{exc.__class__.__name__}") from exc
        if not 200 <= response.status_code < 300:
            raise WorkflowConnectorError(f"飞书接口返回 HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise WorkflowConnectorError("飞书接口返回了无效 JSON") from exc
        if not isinstance(body, dict):
            raise WorkflowConnectorError("飞书接口返回格式不正确")
        code = body.get("code", "unknown")
        if str(code) != "0":
            message = str(body.get("msg") or "请求失败").strip()[:300]
            raise WorkflowConnectorError(f"飞书业务错误 {code}：{message}")
        return body
