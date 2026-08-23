# EUAS v4.3 — Offline Field Synchronization

EUAS v4.3 adds an offline-capable synchronization contract for technician field work. The reference PWA can cache an authenticated field snapshot, queue supported technician changes when connectivity is unavailable, and replay those changes when the connection returns without silently overwriting newer server state.

## Scope

The v4.3 synchronization contract supports these field mutations:

| Entity | Operation | Conflict behavior |
|---|---|---|
| Work order | Start / pause / complete | Hash-checked |
| Work-order checklist task | Set Pending / Completed | Hash-checked |
| Asset | Condition / meter reading update | Hash-checked |
| Dispatch | Accept / en-route / arrive / complete | Hash-checked |
| Work order | Append field note | Append-only; no overwrite conflict |

Inventory consumption remains online-only because stock availability and work-order reservations must be checked against the current authoritative inventory balance. File/photo upload also remains online-only in the reference PWA.

## API contract

### `GET /api/field/sync/bootstrap`

Registers or refreshes a field client and returns the technician's assigned work snapshot. Each mutable entity includes a deterministic SHA-256 `sync_hash` calculated from the fields that matter to that entity's field workflow.

The response contains:

- server time and schema version
- work orders assigned to the signed-in technician
- checklist tasks and their hashes
- linked asset condition/meter state and hash
- technician dispatch records and hashes
- categorized `my_work` queues
- unresolved conflicts for that client

### `POST /api/field/sync/push`

Accepts up to 100 ordered offline operations. Every operation has a globally unique `operation_id` and is recorded in `field_sync_operations`.

The server returns one of:

- `Applied` — mutation committed
- `Conflict` — server state changed since the client's base snapshot
- `Rejected` — invalid, unauthorized or unsupported operation

Replaying an already recorded `operation_id` returns the stored outcome and does not execute the mutation again.

### Safe ordered-batch rebase

Multiple offline edits against the same entity may carry the same original snapshot hash. EUAS permits a later operation in the **same ordered push batch** to rebase only when:

1. the first operation proved that original hash was current when the batch began, and
2. an earlier operation from that same batch already mutated the same entity.

This allows valid offline sequences such as Start → Pause or Start → Complete without weakening protection against server changes made by another user before the batch arrived.

### `GET /api/field/sync/operations`

Returns the audit-oriented synchronization ledger for the signed-in user/client, optionally filtered by status.

### `POST /api/field/sync/conflicts/{operation_id}/resolve`

Explicit conflict resolution supports:

- `discard` — retain the current server version
- `retry` — apply the technician's original operation against the current server state, but only if the supplied `expected_server_hash` still matches

If the server changes again between conflict review and retry, the retry is rejected and the operator must refresh the conflict first.

## Browser/PWA behavior

The reference PWA stores a user-scoped field snapshot and operation queue in browser local storage. The service worker caches the application shell. When connectivity returns while the application is active, queued operations are pushed automatically; technicians can also use the **Field Synchronization** panel manually.

A previously authenticated session can reopen the cached field workspace offline only until the server-issued session expiry timestamp saved at login. Explicit logout removes the cached user/reference/snapshot/queue/conflict data for that user from the browser.

This browser reference is not a substitute for native mobile secure storage, MDM policy or device-level encryption in a production field deployment.

## Security and authority rules

- Technicians can synchronize only work assigned to them.
- Technician task changes must belong to assigned work.
- Technician asset changes require an active linked assigned work order.
- Technician dispatch changes must belong to their own dispatch.
- Work transitions use the same lifecycle/role/SLA rules as normal field execution.
- Mutable operations require a base hash; append-only notes are the deliberate exception.
- Conflict resolution is explicit and recorded.
- Sync push, discard and retry actions are included in the tamper-evident audit trail.

## Persistence and observability

Schema v13 adds:

- `field_sync_clients`
- `field_sync_operations`

Prometheus-style metrics include:

- `euas_field_sync_pending`
- `euas_field_sync_conflicts`
- `euas_field_sync_applied_24h`

A protected management export is available at `/api/exports/field-sync.csv`.

## Deliberate boundaries

v4.3 completes the roadmap item **Offline-first field-service synchronization** for the reference PWA/API contract. It does not claim closed-app/background OS synchronization, native barcode/camera/geofence integration, push notifications, or a production mobile secure-storage implementation. Those remain roadmap items.
