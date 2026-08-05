"""Deprecated compatibility alias for Milvus expression helpers."""

import sys

from app.platform.vector_store import expressions as _implementation


sys.modules[__name__] = _implementation
