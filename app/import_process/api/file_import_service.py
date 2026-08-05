"""Deprecated import API entrypoint; use app.apps.import_api."""

from app.modules.ingestion.api.routes import app, run_graph_task

__all__ = ["app", "run_graph_task"]
