"""Deprecated compatibility alias for app.platform.config.rag_tuning_config."""

import sys

from app.platform.config import rag_tuning_config as _implementation


sys.modules[__name__] = _implementation
