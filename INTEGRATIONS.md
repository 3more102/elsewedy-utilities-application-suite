# EUAS Integration Events

EUAS includes a durable transactional event outbox for external integration with an ESB, iPaaS, integration gateway or utility operations platform.

## Event lifecycle

1. A business transaction changes state (for example Work Order `SUBMIT`, `APPROVE`, `START`, `COMPLETE`).
2. EUAS writes `workflow_events` for internal history and an `event_outbox` record in the same database transaction.
3. The automation runner processes `Pending` / retryable `Failed` events.
4. Before outbound I/O, the processor commits a short durable lease. `attempts` is the delivery-generation fence and `processed_at` is the lease token while the event is in flight.
5. Active leases are not reclaimed. A lease older than `EUAS_OUTBOX_LEASE_SECONDS` is eligible for recovery as a newer attempt generation.
6. If no webhook is configured, the event is retained and marked `Skipped`.
7. If a webhook is configured, EUAS POSTs the event and records `Delivered` or `Failed` with attempt count/error. Finalization is fenced to the exact generation and lease that performed the send.

This design keeps maintenance and procurement transactions independent from external network availability and avoids holding a database row lock for the duration of the webhook request.

Crash recovery remains intentionally at-least-once. If a sender dies after the receiver accepts an event but before EUAS records `Delivered`, a stale lease can later be reclaimed. Receivers should therefore deduplicate on the stable `X-EUAS-Event-ID` when exactly-once downstream effects are required.

## Configuration

```text
EUAS_EVENT_WEBHOOK_URL=https://integration.example.com/euas/events
EUAS_EVENT_WEBHOOK_SECRET=<strong-shared-secret>
EUAS_OUTBOX_MAX_ATTEMPTS=5
EUAS_OUTBOX_LEASE_SECONDS=30
```

Outbound delivery is disabled when `EUAS_EVENT_WEBHOOK_URL` is blank. The lease duration has a minimum of 10 seconds and should remain comfortably above the fixed five-second webhook timeout.

## HTTP delivery

Method: `POST`

Headers:

```text
Content-Type: application/json
X-EUAS-Event: workflow.work_management.complete
X-EUAS-Event-ID: EVT-...
X-EUAS-Signature: sha256=<hmac>   # present when a secret is configured
```

Example body:

```json
{
  "event_no": "EVT-4C2430EE495D43AF",
  "event_type": "workflow.work_management.complete",
  "aggregate_type": "work_order",
  "aggregate_id": "WO-10025",
  "payload": {
    "record_id": 1,
    "record_code": "WO-10025",
    "from_status": "In Progress",
    "to_status": "Completed",
    "actor_id": 5,
    "notes": "Work completed"
  },
  "created_at": "2026-08-19T14:00:00"
}
```

## Signature verification

`X-EUAS-Signature` is `sha256=` followed by the lowercase hex HMAC-SHA256 digest of the exact raw request body, keyed by `EUAS_EVENT_WEBHOOK_SECRET`.

Receivers should compare signatures in constant time and reject replayed event IDs if exactly-once downstream processing is required.

## Management APIs

- `GET /api/events/outbox`
- `GET /api/events/outbox/status` — payload-free operator snapshot with retryable, active-lease, stale-lease, exhausted and oldest-backlog age signals
- `POST /api/events/outbox/{id}/retry`
- `GET /api/automation/status`
- `POST /api/automation/run`
- `GET /api/metrics`

The status endpoint is read-only and is limited to the existing observability operator ceiling (`admin`, `maintenance_manager`, `executive`) plus the database-backed `observability.metrics.read` capability.

## Outbox metrics

`GET /api/metrics` preserves the existing `euas_outbox_pending` and `euas_outbox_attempt_exhausted` gauges and adds the same lease/backlog model used by the status endpoint:

- `euas_outbox_retryable`
- `euas_outbox_queued`
- `euas_outbox_failed_retryable`
- `euas_outbox_active_leases`
- `euas_outbox_stale_leases`
- `euas_outbox_unresolved`
- `euas_outbox_oldest_retryable_age_seconds`

These gauges contain counts and age only; event payloads and delivery error text are not exported.

## Current event families

- `workflow.work_management.*`
- `workflow.procurement.*`
- `sla.response_breached`
- `sla.resolution_breached`
- `sla.policy_updated`

The outbox is intentionally generic so additional utility/SCADA/GIS integration events can be added without changing delivery infrastructure.