"""Deprecated compatibility alias for the knowledge Neo4j adapter."""

import sys

from app.modules.knowledge.infrastructure import neo4j as _implementation


sys.modules[__name__] = _implementation
