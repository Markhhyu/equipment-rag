"""Deprecated compatibility alias for app.platform.config.milvus_config."""

import sys

from app.platform.config import milvus_config as _implementation


sys.modules[__name__] = _implementation
