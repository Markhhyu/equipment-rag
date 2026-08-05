"""Deprecated compatibility alias for ingestion graph formatting."""

import sys

from app.modules.ingestion.graph import formatting as _implementation


sys.modules[__name__] = _implementation
