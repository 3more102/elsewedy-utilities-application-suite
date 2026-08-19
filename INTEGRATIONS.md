# EUAS Integration Events

EUAS includes a durable transactional event outbox for external integration with an ESB, iPaaS, integration gateway or utility operations platform.

## Event lifecycle

1. A business transaction changes state (for example Work Order `SUBMIT`, `APPROVE`, `START`, `COMPLETE`).
2. EUAS writes `workflow_events` for internal history and an `event_outbox` record in the same database transaction.
3. The automation runner processes `Pending` / retryable `Failed` events.
4. If no webhook is configured, the event is retained and marked `Skipped`.
5. If a webhook is configured, EUAS POSTs the event and records `Delivered` or `Failed` with attempt count/error.

This design keeps maintenance and procurement transactions independent from external network availability.

## Configuration

```text
EUAS_EVENT_WEBHOOK_URL=https://integration.example.com/euas/events
EUAS_EVENT_WEBHOOK_SECRET=<strong-shared-secret>
EUAS_OUTBOX_MAX_ATTEMPTS=5
```

Outbound delivery is disabled when `EUAS_EVENT_WEBHOOK_URL` is blank.

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
- `POST /api/events/outbox/{id}/retry`
- `GET /api/automation/status`
- `POST /api/automation/run`
- `GET /api/metrics`

## Current event families

- `workflow.work_management.*`
- `workflow.procurement.*`
- `sla.response_breached`
- `sla.resolution_breached`
- `sla.policy_updated`

The outbox is intentionally generic so additional utility/SCADA/GIS integration events can be added without changing delivery infrastructure.
