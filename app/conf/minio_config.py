"""Deprecated compatibility alias for app.platform.config.minio_config."""

import sys

from app.platform.config import minio_config as _implementation


sys.modules[__name__] = _implementation
