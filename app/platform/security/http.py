from __future__ import annotations

import re
import time
import uuid

from fastapi import FastAPI, Request
from starlette.datastructures import MutableHeaders
from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.platform.observability.logging import bind_log_context, clear_log_context, logger
from app.platform.security.config import load_security_config


_REQUEST_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_.:-]{1,128}$")


class SecurityHeadersMiddleware:
    """添加安全响应头，并让请求关联上下文覆盖完整响应和后台任务。"""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        supplied_request_id = request.headers.get("X-Request-ID", "")
        request_id = supplied_request_id if _REQUEST_ID_PATTERN.fullmatch(supplied_request_id) else uuid.uuid4().hex
        request.state.request_id = request_id
        started = time.perf_counter()
        status_code = 500
        context_token = bind_log_context(request_id=request_id)

        async def send_with_headers(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = request_id
                headers["X-Content-Type-Options"] = "nosniff"
                headers["X-Frame-Options"] = "DENY"
                headers["Referrer-Policy"] = "no-referrer"
                headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
            await send(message)

        try:
            await self.app(scope, receive, send_with_headers)
        except Exception:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            logger.bind(
                event="http_request",
                http_method=request.method,
                http_path=request.url.path,
                http_status=500,
                duration_ms=duration_ms,
            ).exception("HTTP request failed")
            raise
        else:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            logger.bind(
                event="http_request",
                http_method=request.method,
                http_path=request.url.path,
                http_status=status_code,
                duration_ms=duration_ms,
            ).info("HTTP request completed")
        finally:
            clear_log_context(context_token)


def configure_http_security(app: FastAPI) -> None:
    """集中注册安全响应头和受限 CORS，供三个 API 服务复用。"""

    config = load_security_config()
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(config.cors_allowed_origins),
        allow_credentials=config.auth_mode == "password",
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-API-Key", "X-Request-ID"],
        expose_headers=["X-Request-ID"],
    )
