"""Deprecated workflow API entrypoint; use app.apps.workflow_api."""

from app.modules.workflow.api.routes import app

__all__ = ["app"]
