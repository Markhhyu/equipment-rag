"""Deprecated compatibility alias for app.platform.observability.prometheus_metrics."""

import sys

from app.platform.observability import prometheus_metrics as _implementation


sys.modules[__name__] = _implementation
