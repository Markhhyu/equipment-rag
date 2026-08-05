"""Deprecated compatibility alias for sparse vector helpers."""

import sys

from app.platform.vector_store import sparse as _implementation


sys.modules[__name__] = _implementation
