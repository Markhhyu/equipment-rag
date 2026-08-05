"""Deprecated compatibility alias for app.platform.config.reranker_config."""

import sys

from app.platform.config import reranker_config as _implementation


sys.modules[__name__] = _implementation
