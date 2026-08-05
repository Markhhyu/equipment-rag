"""Deprecated compatibility alias for app.platform.ai.reranking.qwen."""

import sys

from app.platform.ai.reranking import qwen as _implementation


sys.modules[__name__] = _implementation
