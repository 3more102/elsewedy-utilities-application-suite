# EUAS 3.9.0 Release Notes

**Elsewedy Utilities Application Suite**  
**One Platform. Every Asset. Every Operation.**  
**Developed by Omar & Seif**

## Release focus

EUAS 3.9.0 is the **Utility Operations Intelligence** release. It connects asset records to persisted telemetry channels/readings, evaluates explicit operating thresholds and manages operational alarms through acknowledgement, clearance, closure and corrective-work creation.

## Delivered

- Asset-linked telemetry channel CRUD and activation control
- Timestamped telemetry ingestion with quality/source metadata
- Warning/critical high and low threshold evaluation
- Alarm de-duplication/update by active channel alarm
- Open → Acknowledged → Cleared → Closed alarm lifecycle
- Alarm → corrective Work Order with SLA, approval, workflow, audit and outbox linkage
- Active/critical alarm KPIs in Dashboard and Operations
- Telemetry and alarm history in Asset Detail
- Global Search coverage for telemetry/alarm records
- Prometheus-style alarm/channel metrics
- Alarm and telemetry CSV exports
- PWA shell cache upgraded to `euas-shell-v3.9.0`

## Release size

- **161 functional API endpoints**
- **61 relational tables**
- **38 explicit indexes**
- **Schema version 9**
- **10 automated regression tests**

## Scope note

Telemetry is delivered as authenticated **SCADA-style API ingestion**. This release does not claim a live OPC-UA, Modbus, IEC 61850 or vendor-SCADA connector, and threshold evaluation is rules-based rather than predictive AI.
