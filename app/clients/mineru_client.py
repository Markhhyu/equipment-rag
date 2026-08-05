"""Deprecated compatibility alias for the ingestion MinerU adapter."""

import sys

from app.modules.ingestion.infrastructure import mineru as _implementation


sys.modules[__name__] = _implementation
