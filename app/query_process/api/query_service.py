"""Deprecated query API entrypoint; use app.apps.query_api."""

from app.modules.qa.api.routes import app, run_query_graph
from app.modules.qa.api.schemas import FeedbackRequest, QueryRequest, ResolutionRequest

__all__ = ["FeedbackRequest", "QueryRequest", "ResolutionRequest", "app", "run_query_graph"]
