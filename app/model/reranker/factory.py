"""Deprecated compatibility alias for app.platform.ai.reranking.factory."""

import sys

from app.platform.ai.reranking import factory as _implementation


sys.modules[__name__] = _implementation
