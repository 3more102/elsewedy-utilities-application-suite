# EUAS v4.2.0 — Utility Operations Intelligence

EUAS v4.2.0 extends the operational telemetry and alarm layer for electrical, water and infrastructure assets.

## Telemetry channels

Each channel is linked to an asset and stores a channel code, metric type, engineering unit, source system, active flag and optional warning/critical high/low thresholds. Seeded examples include TR-001 transformer oil temperature/load and PMP-301 pump vibration.

## Ingestion

`POST /api/telemetry/ingest` accepts timestamped readings with value, quality and source. Each reading is persisted in `telemetry_readings`; the associated channel is updated with its latest reading state.

This is a **SCADA-style authenticated API ingestion interface**. The release does not claim a live OPC-UA, Modbus, IEC 61850, MQTT broker or vendor-SCADA connector. Those protocols should be implemented as gateway/integration adapters that call the EUAS ingestion boundary.

## Threshold evaluation

Configured critical thresholds are evaluated before warning thresholds. A violation opens an operational alarm or updates the existing Open/Acknowledged alarm for that channel. Returning to normal automatically marks the active alarm Cleared. This is transparent deterministic threshold logic, not machine-learning anomaly detection.

## Alarm lifecycle

`Open → Acknowledged → Cleared → Closed`

Alarm records retain severity, trigger/threshold values, opened/last-seen timestamps, acknowledgement actor/time, clear/close timestamps, occurrence count, asset/site and optional linked work order.

An authorized user can convert an alarm into one corrective work order. The work order enters the standard EUAS approval/SLA/workflow/audit/outbox model. Repeated requests return the existing linked work order rather than duplicating it.

## Operations views

The Telemetry & Alarms application shows channel health, stale channels, current readings and the alarm queue. Executive Dashboard, Operations, Asset Detail, Global Search, CSV exports and Prometheus-style metrics consume the same records.

## Production integration path

For utility deployment, place protocol-specific edge/gateway services between field/SCADA networks and this API boundary, use dedicated machine identity and TLS/mTLS, add time-series retention/partitioning appropriate to volume, and validate cyber-security zoning with the utility OT security architecture.


## v4.0 additions

The v4.0 Command Center layer adds quality-aware/idempotent ingestion, alarm suppression windows, correlated operational incidents, machine API keys and bucketed telemetry trends. See `COMMAND_CENTER.md` for the complete operator workflow.
