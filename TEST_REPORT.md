# EUAS v3.9.0 — QA / Verification Report

**Release:** 3.9.0  
**Schema:** 9

## Verified gates

| Gate | Result |
|---|---|
| Python compile | PASS |
| Frontend JavaScript syntax | PASS |
| Service Worker syntax | PASS |
| Integrated pytest regression | PASS — 10 tests |
| Fresh SQLite initialization | PASS — schema 9 |
| Fresh Uvicorn HTTP smoke | PASS — version 3.9.0 |
| Telemetry channels/readings seed | PASS — 3 / 12 |
| Operational alarm seed | PASS — 1 active warning |
| Telemetry normal → Warning → Critical | PASS |
| Alarm acknowledgement | PASS |
| Alarm → corrective Work Order | PASS |
| Return-to-normal auto-clear | PASS |
| Channel deactivation ingestion guard | PASS |
| Backup → restore | PASS — integrity ok / schema 9 |
| Telemetry backup persistence | PASS — 3 channels / 12 readings / 1 alarm |

## Measured release size

- 161 functional `/api/*` endpoints
- 61 relational tables
- 38 explicit application indexes
- Schema version 9
- 10 automated regression tests

## Scope / limitations

SQLite is the fully exercised reference runtime. The PostgreSQL adapter contract remains present, but no live PostgreSQL server was available in the build environment. Telemetry ingestion is an authenticated EUAS API boundary; live OPC-UA/Modbus/IEC 61850/vendor-SCADA protocol integration is not claimed in this release. Docker configuration is present but a Docker daemon was not available for an image build.

## Clean-room release verification

The candidate release ZIP was extracted into an independent directory and verified from the extracted package itself:

- `pytest -q`: **10 passed**
- `python scripts/smoke_test.py`: **PASS — version=3.9.0**
- Backup → restore: **PASS**
- Restored SQLite `integrity_check`: **ok**
- Restored schema: **9**
- Restored telemetry evidence: **3 channels / 12 readings / 1 operational alarm**
