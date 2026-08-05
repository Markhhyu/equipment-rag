"""Deprecated compatibility alias for platform logging."""

import sys

from app.platform.observability import logging as _implementation


sys.modules[__name__] = _implementation
