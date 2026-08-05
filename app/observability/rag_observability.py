"""Deprecated compatibility alias for app.platform.observability.rag_observability."""

import sys

from app.platform.observability import rag_observability as _implementation


sys.modules[__name__] = _implementation
