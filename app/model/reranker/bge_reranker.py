"""Deprecated compatibility alias for app.platform.ai.reranking.bge."""

import sys

from app.platform.ai.reranking import bge as _implementation


sys.modules[__name__] = _implementation
