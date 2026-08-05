"""Deprecated compatibility alias for QA server-sent events."""

import sys

from app.platform.runtime import sse as _implementation


sys.modules[__name__] = _implementation
