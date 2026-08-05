"""Deprecated compatibility alias for runtime rate limiting."""

import sys

from app.platform.runtime import rate_limit as _implementation


sys.modules[__name__] = _implementation
