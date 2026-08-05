"""Deprecated compatibility imports for the analytics module."""

from app.modules.analytics.infrastructure.store import (
    InMemoryQueryAnalyticsStore,
    MongoQueryAnalyticsStore,
    get_query_analytics_store,
    reset_query_analytics_store_for_tests,
)

__all__ = [
    "InMemoryQueryAnalyticsStore",
    "MongoQueryAnalyticsStore",
    "get_query_analytics_store",
    "reset_query_analytics_store_for_tests",
]
