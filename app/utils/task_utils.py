"""Deprecated compatibility alias for runtime task progress."""

import sys

from app.platform.runtime import task_progress as _implementation


sys.modules[__name__] = _implementation
