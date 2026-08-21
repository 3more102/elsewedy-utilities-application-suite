# EUAS v4.2 — Utility Command Center

EUAS v4.2 extends the control-room layer above the v3.9 telemetry/alarm foundation. It reduces alarm noise, keeps integrations repeatable, groups related alarm evidence into operational incidents, and now uses an explicit directed asset topology to correlate connected equipment and provide an explainable root-cause candidate.

## What changed

### 1. Idempotent telemetry ingestion

`POST /api/telemetry/ingest` now accepts:

- `source_system`
- optional batch `idempotency_key`
- per-reading `external_id`
- `Good`, `Uncertain`, or `Bad` quality

A repeated batch idempotency key returns the original batch outcome instead of storing the data twice. A repeated `external_id` for the same telemetry channel is counted as a duplicate and is not inserted again.

Every ingest creates a `TIB-*` batch record with received, accepted, duplicate, bad-quality, suppression and alarm outcome counters.

### 2. Quality-aware alarms

Only `Good` readings participate in deterministic threshold alarm evaluation. `Uncertain` and `Bad` readings are still retained as operational evidence, but they do not open or clear a threshold alarm.

`GET /api/telemetry/quality` provides 24-hour or custom-window data-quality KPIs. `GET /api/telemetry/series` provides bucketed min/average/max series with quality counts for UI trend views.

### 3. Alarm suppression windows

Planned maintenance, commissioning and testing can create a time-bounded `SUP-*` suppression at site, asset or telemetry-channel scope.

When a good-quality reading violates a threshold inside a matching active suppression window, EUAS stores the telemetry reading but does not create/update an operational alarm. The ingest result records the suppression number and reason.

Suppression windows are audited, searchable and exportable. Expired suppressions are deactivated by the automation engine.

### 4. Alarm incident correlation

Threshold alarms from the same asset are grouped as before. In v4.2, a new alarm can also join an open incident when its asset is a **direct active topology neighbor** of an existing incident member, at the same site, inside the 30-minute correlation window. Incidents can therefore grow incrementally across multiple hops as adjacent alarmed assets join.

An incident contains:

- asset/site context
- derived maximum severity
- member-alarm count
- open/acknowledged/resolved state
- last-seen timestamp
- linked corrective work order when one is created
- correlation mode (`Asset` or `Topology`)
- deterministic root-cause candidate and score
- human-readable root-cause reasoning
- topology hop evidence

When every member alarm clears or closes, EUAS automatically resolves the incident. A still-active incident cannot be manually resolved while member alarms remain active.


### 5. Directed asset topology and root-cause candidate

`asset_topology_links` stores upstream → downstream operational relationships independently from the normal asset parent/child hierarchy. Supported relation labels are free text at the API boundary so utilities can represent relationships such as `Feeds`, `Supplies`, `Drives`, `Contains`, or `Depends On`. Active links reject duplicates, self-links and directed cycles.

For a multi-asset incident, EUAS ranks **alarmed assets only** using a transparent heuristic:

1. how many other alarmed members are reachable downstream;
2. whether the candidate's alarm evidence appeared earliest;
3. severity as a tie-breaker.

The selected `root_cause_asset_id`, percentage-style score, explanation and hop count are persisted on the incident. The score is decision-support evidence, not a probability and not proof of causation.

When an incident creates corrective work, the work order targets the current root-cause candidate rather than always targeting the first asset that opened the incident.

### 6. Incident-to-maintenance conversion

A correlated incident can create one corrective work order rather than a separate work order for every member alarm. The generated work order enters the normal EUAS lifecycle:

`Submitted → Approval → Assignment → Execution → Completion → Closure`

It also receives SLA clocks, audit records and outbox events.

### 7. Machine-to-machine authentication

Admins can create `telemetry:write` integration API keys for a SCADA/API gateway.

The plaintext key:

- begins with `euas_`
- is displayed only once
- is never returned by the list API
- is stored as a SHA-256 digest
- supports expiry
- records last use
- can be revoked immediately

A gateway sends the key in:

```http
X-EUAS-Integration-Key: euas_...
```

This allows telemetry ingestion without a human bearer session. The integration principal is represented in audit evidence through the internal service identity.

A runnable example is included at `scripts/scada_gateway_demo.py`.

### 8. Command Center UI

The new Command Center combines:

- correlated incidents
- active/critical alarms
- telemetry quality
- active suppressions
- stale telemetry
- open outages/lost capacity
- active technician dispatches
- directed asset topology links
- topology-correlated incident count and root-cause evidence

Operations users can acknowledge incidents, create corrective work, resolve incidents after alarms clear, and manage suppression windows. Admins can create/revoke integration keys.

## Key APIs

| Capability | API |
|---|---|
| Command Center | `GET /api/operations/command-center` |
| Telemetry ingest | `POST /api/telemetry/ingest` |
| Ingest batches | `GET /api/telemetry/batches` |
| Data quality | `GET /api/telemetry/quality` |
| Bucketed series | `GET /api/telemetry/series` |
| Suppression list/create | `GET/POST /api/alarm-suppressions` |
| Suppression deactivate | `POST /api/alarm-suppressions/{id}/deactivate` |
| Asset topology | `GET/POST /api/asset-topology` |
| Deactivate topology link | `POST /api/asset-topology/{id}/deactivate` |
| Incidents | `GET /api/alarm-incidents` |
| Incident detail | `GET /api/alarm-incidents/{id}` |
| Incident acknowledgement | `POST /api/alarm-incidents/{id}/acknowledge` |
| Incident resolve | `POST /api/alarm-incidents/{id}/resolve` |
| Incident corrective work | `POST /api/alarm-incidents/{id}/work-order` |
| Integration keys | `GET/POST /api/integrations/api-keys` |
| Revoke integration key | `POST /api/integrations/api-keys/{id}/revoke` |

## Operational boundaries

EUAS v4.2 implements the application-side ingestion, quality, suppression, shelving, topology and incident logic. It does **not** claim to implement a native OPC-UA, Modbus TCP, DNP3 or IEC 61850 driver. Those protocol adapters should run at the integration/edge layer and call the authenticated EUAS telemetry API.

Alarm correlation and root-cause selection are deterministic. EUAS does **not** claim an ML root-cause model or a verified physical diagnosis. The topology result is an operator decision-support candidate and remains explainable from stored links, timestamps and alarm severity.


## v4.1 governed alarm shelving

An operator may request a temporary shelf for an alarm that is already Open or Acknowledged. A shelf does not change alarm state and does not disable threshold evaluation. Pending requests remain actionable until approved.

Policy:

- duration: 5 to 1440 minutes; Critical alarms: maximum 120 minutes;
- approval: routed to Maintenance Manager through Approval Center;
- four-eyes: the requester cannot approve their own shelf;
- expiry: the automation engine marks an approved shelf Expired and the alarm immediately returns to the actionable queue if it remains active;
- revoke: authorized operations roles can restore an alarm before expiry;
- evidence: request, decision and revoke actions are audited; request/approval/reject/revoke events are placed in the integration outbox.

APIs:

- `GET /api/alarm-shelves`
- `POST /api/alarms/{alarm_id}/shelf`
- `POST /api/alarm-shelves/{shelf_id}/revoke`
- approval remains `POST /api/approvals/{approval_id}/decision`
- `GET /api/exports/alarm-shelves.csv`


## v4.2 topology correlation operational rules

- Correlation remains site-scoped and time-bounded.
- A new alarm joins only when it is on the same asset or directly connected to an existing incident member by an active topology link.
- Multi-hop incidents form incrementally through adjacent alarmed assets; a distant asset is not collapsed into an incident merely because a path exists through non-alarmed intermediate equipment.
- Deactivating a topology link affects future correlation only; existing incident evidence is retained.
- Root-cause fields are recalculated when incident membership changes and are backfilled for legacy incidents at startup.
- The Command Center exposes the topology graph, root-cause candidate, score, reason and hop count.
