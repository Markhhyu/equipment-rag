"""Deprecated compatibility alias for the unused legacy QA history adapter."""

import sys

from app.modules.qa.infrastructure import history_legacy as _implementation


sys.modules[__name__] = _implementation
