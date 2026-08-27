# EUAS Failure Triage

Starting baseline: 489 passed / 17 failed / 1 warning (StarletteDeprecationWarning).
Note: previous stated 505/32 differs because deleted retired-engine test files removed 16 tests; current branch is ahead of origin by 26 commits.

## Root-cause clusters
1. KPI routing / family path conflict - FIXED. Family routes registered correctly; base index /api/kpis added; dynamic {kpi_id} overlay cleaned.
2. Duplicate route installation - VERIFIED. No duplicate path+method pairs remain.
3. Retired KPI engine - DISCONNECTED. Old engine files deleted (tests/test_kpi_engine.py, etc.); one canonical path remains.
4. Authorization contract - FIXED. POST /api/reliability/cbm-evaluation, FMEA mutations, and family routes mapped correctly; reliability.cbm.manage, reliability.fmea.manage, reliability.cbm.decide added.
5. KPI response contract - PARTIAL. Endpoint contracts restored (bad_actors dict contract); remaining test_kpi_automation and dashboard payload tests need telemetry/alarm state verification.
6. KPI site scoping - MOSTLY FIXED. bad_actors_route now uses scoped _bad_actor_rows; reliability family routes propagate filters.
7. CSV formula injection - NOT FIXED. Central safe CSV encoder not implemented.
8. Telemetry validation - NOT FIXED. Pydantic finite_number rejects NaN/Infinity; returns 422 instead of expected 400; batch rollback not atomic; future skew not enforced.
9. CSP / inline style - NOT FIXED. Inline style= and handler patterns still present; CSP generation uses 'unsafe-inline'.
10. Outbox atomicity - NOT FIXED. Concurrent processor duplicate delivery mechanism not transactionally safe.
11. Private document download - NOT FIXED. Route remains but MIME/header behavior may break contract.
12. Reliability/APM site scoping / bad actors - FIXED. test_reliability_bad_actors.py passes; bad_actors_route uses _bad_actor_rows.
13. Frontend action registry - NOT FULLY VERIFIED. Action names still missing or stale; CSP-compliant external event binding needed.