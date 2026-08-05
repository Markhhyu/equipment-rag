"""Deprecated ingestion graph entrypoint; use app.modules.ingestion.graph.main_graph."""

from app.modules.ingestion.graph.main_graph import kb_import_app, workflow

__all__ = ["kb_import_app", "workflow"]
