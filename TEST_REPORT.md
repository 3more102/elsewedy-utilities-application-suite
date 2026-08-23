# EUAS v4.9.0 — QA / Verification Report

**Release:** 4.9.0  
**Schema:** 21

## Release gates

| Verification | Result |
|---|---|
| Integrated pytest regression suite | **PASS — 69 source definitions / 71 collected tests** |
| Fresh Uvicorn HTTP smoke | **PASS — version 4.9.0 / RCM read surface** |
| Fresh SQLite initialization | **PASS — schema 21, 88 tables, 84 indexes, integrity ok** |
| v4.8.0 → v4.9.0 SQLite schema upgrade | **PASS — 18 → 19** |
| Python compile | **PASS** |
| Frontend JavaScript syntax | **PASS** |
| Service Worker JavaScript syntax | **PASS** |
| Release metadata/source consistency | **PASS** |

## Source measurements

- **242 application HTTP routes** total
- **241 routes under `/api/`**
- **83 relational tables**
- **76 explicit indexes**
- **32 automated tests**

## v4.9 RCM verification

Regression coverage verifies RCM consequence/strategy validation, one-strategy-per-FMEA governance, risk-based review dates, same-context CBM linkage, same-asset PM linkage, Run-to-Failure safety/environment guardrails, Condition-Based/Time-Based readiness requirements, fine-grained management/approval permissions, four-eyes separation, credential re-authentication, signer-intent evidence, activation, Continue reviews, Revise-to-Draft reapproval, rejection handling, metrics and CSV export.

The v4.4 approval-signature chain is reused for RCM decisions rather than creating weaker parallel approval evidence.

## Calendar determinism correction

The release also fixes v4.8 seeded workforce leave so the demo absence always occupies weekdays in the capacity-planning horizon. This removes a calendar-date-sensitive regression without weakening the original workforce assertion.

## Migration verification

A clean v4.8.0 database was initialized at schema 18 and opened by v4.9.0. Verification confirmed schema 19, `rcm_strategies`, `rcm_strategy_reviews`, all 76 indexes, preservation of six assets and the existing approval request, and SQLite integrity `ok`.

## Boundary

This verification proves the supplied deterministic application behavior and migration path. It does not certify SAE JA1011/JA1012, IEC/ISO compliance, an OEM RCM method, a regulated safety case, or a live PostgreSQL deployment.
