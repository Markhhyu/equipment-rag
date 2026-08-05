"""Deprecated query graph entrypoint; use app.modules.qa.graph.main_graph."""

from app.modules.qa.graph.main_graph import builder, query_app

__all__ = ["builder", "query_app"]
