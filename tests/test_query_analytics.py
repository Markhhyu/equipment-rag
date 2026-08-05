from datetime import UTC, datetime, timedelta

from app.modules.analytics.infrastructure.store import InMemoryQueryAnalyticsStore


def test_query_analytics_separates_technical_and_business_outcomes():
    now = datetime(2026, 8, 5, 8, 0, tzinfo=UTC)
    store = InMemoryQueryAnalyticsStore()

    store.record_started("tenant-a", "trace-solved", "session-1", "设备为什么报警？")
    store.records[("tenant-a", "trace-solved")]["started_at"] = now - timedelta(hours=2)
    store.record_completed(
        "tenant-a",
        "trace-solved",
        {"device_names": ["真空泵 A"], "answer_policy": "answer"},
    )
    store.record_feedback("tenant-a", "trace-solved", 1)
    store.record_resolution("tenant-a", "trace-solved", "solved")

    store.record_started("tenant-a", "trace-failed", "session-2", "连接超时")
    store.records[("tenant-a", "trace-failed")]["started_at"] = now - timedelta(days=1)
    store.record_failed("tenant-a", "trace-failed", "model timeout")

    store.record_started("tenant-a", "trace-unsolved", "session-3", "仍然不能启动")
    store.records[("tenant-a", "trace-unsolved")]["started_at"] = now - timedelta(hours=1)
    store.record_completed(
        "tenant-a",
        "trace-unsolved",
        {
            "device_names": ["真空泵 A"],
            "requires_human_review": True,
            "review_reason": "缺少厂商手册",
        },
    )
    store.record_resolution("tenant-a", "trace-unsolved", "unsolved")

    summary = store.summary("tenant-a", 7, 480, now=now)

    assert summary["totals"]["questions"] == 3
    assert summary["totals"]["technical_succeeded"] == 2
    assert summary["totals"]["technical_failed"] == 1
    assert summary["totals"]["solved"] == 1
    assert summary["totals"]["unsolved"] == 1
    assert summary["totals"]["requires_human_review"] == 1
    assert summary["rates"]["technical_success_rate"] == 0.6667
    assert summary["rates"]["confirmed_resolution_rate"] == 0.5
    assert summary["rates"]["outcome_confirmation_rate"] == 0.6667
    assert summary["top_devices"] == [{"name": "真空泵 A", "count": 2}]
    assert summary["failure_reasons"] == [{"reason": "model timeout", "count": 1}]
    assert len(summary["recent_attention"]) == 2


def test_query_analytics_is_idempotent_and_tenant_scoped():
    now = datetime.now(UTC)
    store = InMemoryQueryAnalyticsStore()
    store.record_started("tenant-a", "same-trace", "session-a", "第一次")
    started_at = store.records[("tenant-a", "same-trace")]["started_at"]
    store.record_started("tenant-a", "same-trace", "session-a", "重复请求")
    store.record_started("tenant-b", "same-trace", "session-b", "其他租户")

    assert store.records[("tenant-a", "same-trace")]["started_at"] == started_at
    assert store.summary("tenant-a", 1, 0, now=now)["totals"]["questions"] == 1
    assert store.summary("tenant-b", 1, 0, now=now)["totals"]["questions"] == 1


def test_query_analytics_resolution_validation():
    store = InMemoryQueryAnalyticsStore()
    store.record_started("tenant-a", "trace-a", "session-a", "问题")

    try:
        store.record_resolution("tenant-a", "trace-a", "pending")
    except ValueError as exc:
        assert "solved" in str(exc)
    else:
        raise AssertionError("pending must not be accepted as a user-confirmed result")
