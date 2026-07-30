from datetime import UTC, datetime, timedelta

import pytest

from app.runtime.run_store import InMemoryRunStore, RunStatus


def test_run_lifecycle_and_idempotent_creation():
    store = InMemoryRunStore()
    created = store.create("run-1", "query", {"query": "status"}, max_attempts=3)
    duplicate = store.create("run-1", "query", {"query": "status"}, max_attempts=3)

    assert duplicate == created
    claimed = store.claim("run-1", "worker-1", lease_seconds=60)
    assert claimed.status == RunStatus.RUNNING
    assert claimed.attempt == 1

    heartbeat = store.heartbeat("run-1", "worker-1", lease_seconds=120)
    assert heartbeat.lease_expires_at > claimed.lease_expires_at

    completed = store.complete("run-1", "worker-1", {"answer": "ok"})
    assert completed.status == RunStatus.SUCCEEDED
    assert completed.result == {"answer": "ok"}
    assert completed.to_public_dict()["retryable"] is False


def test_run_retry_respects_attempt_budget():
    store = InMemoryRunStore()
    store.create("run-2", "import", {"path": "manual.md"}, max_attempts=2)

    store.claim("run-2", "worker-1", lease_seconds=60)
    failed = store.fail("run-2", "worker-1", "temporary failure")
    assert failed.to_public_dict()["retryable"] is True

    store.request_retry("run-2")
    store.claim("run-2", "worker-2", lease_seconds=60)
    store.fail("run-2", "worker-2", "failed again")

    with pytest.raises(RuntimeError, match="exhausted"):
        store.request_retry("run-2")


def test_expired_lease_can_be_reclaimed():
    now = datetime.now(UTC)
    current_time = [now]
    store = InMemoryRunStore(clock=lambda: current_time[0])
    store.create("run-3", "query", {}, max_attempts=3)
    store.claim("run-3", "dead-worker", lease_seconds=60)
    current_time[0] = now + timedelta(seconds=61)

    reclaimed = store.claim("run-3", "recovery-worker", lease_seconds=60)

    assert reclaimed.attempt == 2
    assert reclaimed.lease_owner == "recovery-worker"


def test_wrong_owner_and_conflicting_idempotency_are_rejected():
    store = InMemoryRunStore()
    store.create("run-4", "query", {"query": "one"}, max_attempts=3)
    store.claim("run-4", "worker-1", lease_seconds=60)

    with pytest.raises(RuntimeError, match="not leased"):
        store.complete("run-4", "worker-2", {})
    with pytest.raises(ValueError, match="different input"):
        store.create("run-4", "query", {"query": "two"}, max_attempts=3)
