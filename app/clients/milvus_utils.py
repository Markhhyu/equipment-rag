"""Deprecated compatibility alias for app.platform.vector_store.milvus."""

import sys

from app.platform.vector_store import milvus as _implementation


sys.modules[__name__] = _implementation
