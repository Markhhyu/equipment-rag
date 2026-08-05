"""Deprecated compatibility alias for app.platform.storage.minio."""

import sys

from app.platform.storage import minio as _implementation


sys.modules[__name__] = _implementation
