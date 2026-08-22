# EUAS v3.9.0 — Utility Operations Intelligence

EUAS v3.9.0 adds the **Telemetry & Alarms application** to the EUAS suite for electrical, water and infrastructure assets.

## Telemetry channels

Each channel is linked to an asset and stores a channel code, metric type, engineering unit, source system, active flag and optional warning/critical high/low thresholds. Seeded examples include TR-001 transformer oil temperature/load and PMP-301 pump vibration.

## Ingestion

`POST /api/telemetry/ingest` accepts readings with value, quality, source and optional device/event `captured_at`. Every valid reading is persisted in `telemetry_readings`; historical evidence is never discarded merely because a newer sample already exists.

For **explicit `captured_at` values**, EUAS compares the actual ISO-8601 instant rather than request-arrival order. Only a strictly newer event-time generation may advance `telemetry_channels.last_value`, `last_quality`, `last_reading_at` or mutate current alarm state. Delayed readings and equal/equivalent instants are retained with an additive `historical` result classification but cannot regress live state.

For legacy/API callers that omit `captured_at`, EUAS preserves arrival-order behavior. After the channel row is locked, the server generates a microsecond-resolution local marker that is strictly later than the channel's current marker. This prevents rapid same-second requests or concurrent legacy callers from being incorrectly suppressed as equal-time history.

Multi-channel ingest batches acquire all participating channel locks in ascending stable channel-ID order. This prevents opposite-order batches such as A→B and B→A from creating PostgreSQL lock-order deadlocks while preserving the caller's original result order.

This is a **SCADA-style authenticated API ingestion interface**. The release does not claim a live OPC-UA, Modbus, IEC 61850, MQTT broker or vendor-SCADA connector. Those protocols should be implemented as gateway/integration adapters that call the EUAS ingestion boundary.

## Threshold evaluation

Configured critical thresholds are evaluated before warning thresholds. A current violation opens an operational alarm or updates the existing Open/Acknowledged alarm for that channel. Returning to normal automatically marks the active alarm Cleared. This is transparent deterministic threshold logic, not machine-learning anomaly detection.

Telemetry alarm mutation is coordinated with the manual alarm lifecycle. Before telemetry updates or clears an active alarm, it locks and reloads that alarm and revalidates that the state is still `Open` or `Acknowledged`. A manual close that commits first therefore remains terminal: telemetry cannot regress `Closed → Cleared`. A genuinely newer violating sample after a committed close creates a new alarm generation instead of modifying the closed evidence.

## Alarm lifecycle

`Open → Acknowledged → Cleared → Closed`

Alarm records retain severity, trigger/threshold values, opened/last-seen timestamps, acknowledgement actor/time, clear/close timestamps, occurrence count, asset/site and optional linked work order.

An authorized user can convert an alarm into one corrective work order. The work order enters the standard EUAS approval/SLA/workflow/audit/outbox model. Repeated requests return the existing linked work order rather than duplicating it.

## Operations views

The Telemetry & Alarms application shows channel health, stale channels, current readings and the alarm queue. Executive Dashboard, Operations, Asset Detail, Global Search, CSV exports and Prometheus-style metrics consume the same records.

## Production integration path

For utility deployment, place protocol-specific edge/gateway services between field/SCADA networks and this API boundary, use dedicated machine identity and TLS/mTLS, add time-series retention/partitioning appropriate to volume, and validate cyber-security zoning with the utility OT security architecture.
