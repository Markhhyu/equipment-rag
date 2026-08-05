"""Deprecated compatibility alias for QA conversation history."""

import sys

from app.modules.qa.infrastructure import history as _implementation


sys.modules[__name__] = _implementation
