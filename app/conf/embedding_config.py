"""Deprecated compatibility alias for app.platform.config.embedding_config."""

import sys

from app.platform.config import embedding_config as _implementation


sys.modules[__name__] = _implementation
