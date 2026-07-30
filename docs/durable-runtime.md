# Durable Agent runtime

The runtime persists two related layers in MongoDB when started with Docker
Compose:

1. `agent_runs` stores the run state machine, input required for recovery,
   attempt budget, worker lease, error, and final result.
2. LangGraph checkpoint collections store graph state at every super-step.

The run ID is also the LangGraph `thread_id`. Query runs use their `trace_id`;
import runs use their upload `task_id`.

## State machine

```text
pending -> running -> succeeded
                    -> failed -> pending (retry) -> running
```

Claiming a run creates a time-limited lease and increments its attempt count.
Every completed graph step renews the lease. A failed run, or a running run
whose lease expired after a process crash, can be retried until
`RUN_MAX_ATTEMPTS` is exhausted.

Retrying invokes LangGraph with `None` and the original thread ID. LangGraph
loads the last successful checkpoint and continues from the failed super-step
instead of repeating already completed nodes.

## API

Query service:

```text
GET  /runs/{trace_id}
POST /runs/{trace_id}/retry
```

Import service:

```text
GET  /runs/{task_id}
POST /runs/{task_id}/retry
```

The retry endpoint returns `202 Accepted`. It returns `409 Conflict` when the
run is still actively leased, already succeeded, or has exhausted its attempts.

Recovery is operator/API initiated in this version. The durable records and
checkpoints survive process restarts, but a separate distributed queue worker
is intentionally not introduced while SSE delivery still uses an in-process
connection queue.

## Configuration

| Variable | Compose default | Purpose |
|---|---|---|
| `RUN_STORE_BACKEND` | `mongodb` | `mongodb` or `memory` |
| `RUN_STORE_COLLECTION` | `agent_runs` | MongoDB run collection |
| `RUN_MAX_ATTEMPTS` | `3` | Total claim budget |
| `RUN_LEASE_SECONDS` | `900` | Lease duration renewed at graph boundaries |
| `LANGGRAPH_CHECKPOINT_BACKEND` | `mongodb` | `mongodb` or `memory` |
| `LANGGRAPH_CHECKPOINT_DB` | `equipment_rag_checkpoints` | Checkpoint database |
| `LANGGRAPH_CHECKPOINT_TTL_SECONDS` | `604800` | Checkpoint retention (7 days) |

Plain local Python execution defaults both backends to `memory`; Docker Compose
sets both to `mongodb`.

## Operational notes

- Keep MongoDB backups consistent with the application data retention policy.
- Set checkpoint TTL high enough for the longest permitted recovery window.
- Do not reuse a run ID for different input; creation is idempotent and rejects
  conflicting input.
- Errors are truncated to 2,000 characters before persistence.
- Run result access must be protected by the authentication and tenant controls
  introduced in the governance layer.
