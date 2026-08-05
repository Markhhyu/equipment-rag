"""Deprecated compatibility alias for app.platform.ai.reranking.base."""

import sys

from app.platform.ai.reranking import base as _implementation


sys.modules[__name__] = _implementation
