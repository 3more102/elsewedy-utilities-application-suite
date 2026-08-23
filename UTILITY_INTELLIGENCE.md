# EUAS v3.9.0 — Utility Operations Intelligence

EUAS v3.9.0 includes the operational telemetry and alarm application for electrical, water and infrastructure assets inside the wider EUAS application suite.

## Telemetry channels

Each channel is linked to an asset and stores a channel code, metric type, engineering unit, source system, active flag and optional warning/critical high/low thresholds. Seeded examples include TR-001 transformer oil temperature/load and PMP-301 pump vibration.

## Ingestion

`POST /api/telemetry/ingest` accepts timestamped readings with value, quality and source. **Every accepted reading is persisted** in `telemetry_readings`, including delayed or replayed historical samples.

Current channel/alarm state follows **capture/event time**, not network arrival time:

- for readings with an explicit `captured_at`, EUAS compares the actual ISO-8601 instant rather than request-arrival order. Only a strictly newer event-time generation may advance `telemetry_channels.last_value`, `last_quality`, `last_reading_at` or mutate current alarm state;
- an older explicit reading is retained as historical evidence but cannot regress the channel's latest fields, clear a newer active alarm, or reopen an alarm after a newer normal sample;
- an explicit reading at the same event instant, including an equivalent instant expressed with another timezone offset, is also retained as historical evidence and cannot duplicate alarm occurrences or side effects;
- naive (offset-less) timestamps are interpreted in the server's local timezone for that event date, so aware and naive stored markers compare by actual instant;
- when `captured_at` is omitted, EUAS preserves arrival-order behavior: after the channel row is locked the server assigns its arrival marker, equal second-level values remain live generations, and a marker ahead of the host clock (for example after a future-dated explicit sample) is advanced by one microsecond instead of suppressing every untimestamped reading as historical.

The ingest response includes a `historical` count and reports non-current explicit samples with `action: historical`.

Multi-channel ingest batches acquire all participating channel locks in ascending stable channel-ID order. This prevents opposite-order batches such as A→B and B→A from creating PostgreSQL lock-order deadlocks while preserving the caller's original result order.

This is a **SCADA-style authenticated API ingestion interface**. The release does not claim a live OPC-UA, Modbus, IEC 61850, MQTT broker or vendor-SCADA connector. Those protocols should be implemented as gateway/integration adapters that call the EUAS ingestion boundary.

## Threshold evaluation

Configured critical thresholds are evaluated before warning thresholds. A temporally current violation opens an operational alarm or updates the existing Open/Acknowledged alarm for that channel. A temporally current return to normal automatically marks the active alarm Cleared. Historical/replayed readings do not mutate current alarm state. This is transparent deterministic threshold logic, not machine-learning anomaly detection.

Telemetry alarm mutation is coordinated with the manual alarm lifecycle. Before telemetry updates or clears an active alarm, it locks and reloads that alarm and revalidates that the state is still `Open` or `Acknowledged`. A manual close that commits first therefore remains terminal: telemetry cannot regress `Closed → Cleared`. A genuinely newer violating sample after a committed close creates a new alarm generation instead of modifying the closed evidence.

## Alarm lifecycle

`Open → Acknowledged → Cleared → Closed`

Alarm records retain severity, trigger/threshold values, opened/last-seen timestamps, acknowledgement actor/time, clear/close timestamps, occurrence count, asset/site and optional linked work order.

An authorized user can convert an alarm into one corrective work order. The work order enters the standard EUAS approval/SLA/workflow/audit/outbox model. Repeated requests return the existing linked work order rather than duplicating it.

## Operations views

The **Telemetry & Alarms application** shows channel health, stale channels, current readings and the alarm queue. Other EUAS applications—Executive Dashboard, Utilities Operations, Asset Management, Analytics and related connected views—consume the same governed records through the suite's shared service/data layer.

## Concurrency guarantee

PostgreSQL current-state mutation is serialized per telemetry channel, with all channels of one batch locked in canonical ID order. Concurrent old/new explicit samples therefore converge on the newest capture instant regardless of which request reaches the service first, and opposite-order multi-channel batches cannot deadlock. PostgreSQL multi-session CI races cover the important schedules:

1. newer critical + older normal → the newer critical channel/alarm state remains current;
2. newer normal + older critical → the delayed critical sample cannot leave a current alarm open;
3. telemetry auto-clear racing a manual close → the committed close stays terminal with exactly one close audit/event;
4. opposite-order A/B batches → both converge without deadlock or duplicate alarm updates.

Both readings remain in history in every schedule. Equal-instant explicit replays are persisted but do not create a second live generation. Untimestamped readings remain compatible with the existing arrival-ordered API behavior.

## Production integration path

For utility deployment, place protocol-specific edge/gateway services between field/SCADA networks and this API boundary, use dedicated machine identity and TLS/mTLS, add time-series retention/partitioning appropriate to volume, and validate cyber-security zoning with the utility OT security architecture.
