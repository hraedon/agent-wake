# Proposal: Prometheus event counters

**Status:** awaiting operator approval; no implementation yet.

## Recommended default

Expose Prometheus text format at `GET /metrics` on the daemon's existing HTTP
listener. Enable it by default only on loopback binds; require an explicit
`wake.metrics_enabled=true` for non-loopback binds. Do not add a metrics SDK:
render the small exposition document with the standard library.

Start with monotonic process-local counters:

- ingests by outcome: accepted, duplicate, rejected, rate-limited;
- routing by outcome: delivered, queued-next-session, no-subscriber;
- reply callbacks by outcome: delivered, failed, no-callback;
- permission relay forwards by outcome.

Use bounded outcome labels only. Source, principal, destination, event ID, and
callback URL must not become labels: they expose identifiers and create
unbounded cardinality. Durable queue depths remain gauges sourced from SQLite.
Counters reset on daemon restart; persistence is deliberately out of scope.

Approval should confirm the loopback/default-enable posture and exact metric
prefix (`agent_wake_`) before BC-WAKE-006 moves to implementation.
