# EUAS 4.9.0 Release Notes

## Reliability-Centered Maintenance Strategy Engine

EUAS 4.9.0 adds a governed RCM strategy layer above the v4.8 asset FMEA register. Reliability teams can now state the functional failure/consequence, select an explicit maintenance strategy, link condition-based or time-based tasks to the existing execution engines, and route the strategy through independent electronically evidenced approval before activation.

### New capabilities

- One governed RCM strategy per FMEA record.
- Consequence classes: Safety, Environmental, Operational, Non-Operational and Hidden.
- Strategy types: Condition-Based, Time-Based, Run-to-Failure, Failure-Finding and Redesign.
- Deterministic guards reject Run-to-Failure for Safety/Environmental consequences.
- Condition-Based submission/activation requires an active CBM rule linked to the same FMEA.
- Time-Based submission/activation requires an interval and active PM plan for the same asset.
- Workflow: Draft → Review → signed Approval → Active.
- Four-eyes separation plus `reliability.rcm.approve` for RCM Approval Center decisions.
- Existing v4.4 password re-authentication, signer intent and hash-chained approval evidence are reused.
- Formal Continue / Revise / Retire reviews with review history; Revise forces a new approval cycle.
- Risk-band-derived default review cadence.
- RCM register UI, coverage/overdue/critical-gap KPIs, CSV export and metrics.

### Reliability linkage

`asset FMEA → RCM strategy → existing CBM or PM task → governed work execution`

RCM does not create a competing work, CBM or PM engine. It records and governs the maintenance policy and validates references to the existing execution services.

### Additional correction

A pre-existing date-sensitive workforce demo-data defect was fixed. Seeded technician leave is now aligned to weekdays inside the planning window, so capacity regression tests remain deterministic regardless of the calendar date on which the suite runs.

## Migration

A clean v4.8.0 SQLite database at schema 18 was upgraded in place to schema 19. Verification preserved six seeded assets and the existing approval request, created both RCM tables and all v4.9 indexes, and returned `PRAGMA integrity_check = ok`.

## Verification target

The final release is source-measured at **242 HTTP routes (241 under `/api/`), 83 relational tables, 76 explicit indexes and 32 automated tests**. Release packaging is accepted only after the full pytest suite, fresh-process HTTP smoke, compile/JavaScript checks, fresh-schema measurement and clean-room ZIP verification pass.

## Boundary

EUAS v4.9 provides deterministic RCM workflow/evidence governance. It does **not** claim SAE JA1011/JA1012 certification, IEC/ISO conformance, OEM approval or a validated regulated safety-case methodology.
