"""Deprecated compatibility alias for app.platform.observability.langfuse_monitor."""

import sys

from app.platform.observability import langfuse_monitor as _implementation


sys.modules[__name__] = _implementation
