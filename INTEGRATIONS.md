# EUAS Integration Events

EUAS includes a durable transactional event outbox for external integration with an ESB, iPaaS, integration gateway or utility operations platform.

## Event lifecycle

1. A business transaction changes state (for example Work Order `SUBMIT`, `APPROVE`, `START`, `COMPLETE`).
2. EUAS writes `workflow_events` for internal history and an `event_outbox` record in the same database transaction.
3. The automation runner processes `Pending` / retryable `Failed` events.
4. If no webhook is configured, the event is retained and marked `Skipped`.
5. If a webhook is configured, EUAS POSTs the event and records `Delivered` on success.
6. Delivery failures remain `Failed` while retries are available; the final configured failure transitions to terminal `DeadLetter`.
7. Administrators can explicitly retry a dead-lettered event; manual retry resets its attempt budget and returns it to `Pending`.

This design keeps maintenance and procurement transactions independent from external network availability.

## Configuration

```text
EUAS_EVENT_WEBHOOK_URL=https://integration.example.com/euas/events
EUAS_EVENT_WEBHOOK_SECRET=<strong-shared-secret>
EUAS_OUTBOX_MAX_ATTEMPTS=5
```

Outbound delivery is disabled when `EUAS_EVENT_WEBHOOK_URL` is blank. Exhausted events remain queryable in `DeadLetter` with their final attempt count and error for operator review.

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

## v4.0 machine-to-machine telemetry authentication

SCADA/API gateways can authenticate to `POST /api/telemetry/ingest` with a `telemetry:write` integration key:

```http
X-EUAS-Integration-Key: euas_<secret>
```

Admins create keys through `POST /api/integrations/api-keys` or the Utility Command Center. The plaintext secret is returned once. EUAS persists only its SHA-256 digest, plus an administrative key number, display name, expiry, last-used time and active/revoked state.

The endpoint also supports batch `idempotency_key` and per-reading `external_id` values so an edge gateway can safely retry delivery without duplicating persisted telemetry.

A runnable stdlib client is included at `scripts/scada_gateway_demo.py`.

This reference build does not provide native OPC-UA/Modbus/DNP3/IEC 61850 drivers. Those are expected to normalize source data at the edge and call the EUAS API.
