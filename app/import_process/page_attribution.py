"""Deprecated compatibility alias for ingestion page attribution."""

import sys

from app.modules.ingestion import page_attribution as _implementation


sys.modules[__name__] = _implementation
