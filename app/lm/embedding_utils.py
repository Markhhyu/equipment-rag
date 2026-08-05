"""Deprecated compatibility alias for app.platform.ai.embeddings."""

import sys

from app.platform.ai import embeddings as _implementation


sys.modules[__name__] = _implementation
