"""Deprecated compatibility alias for app.platform.observability.quality_metrics."""

import sys

from app.platform.observability import quality_metrics as _implementation


sys.modules[__name__] = _implementation
