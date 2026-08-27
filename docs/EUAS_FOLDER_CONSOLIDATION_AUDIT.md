# EUAS Folder Consolidation Audit

Canonical repository: `C:\Users\omar\elsewedy-utilities-application-suite`
Canonical branch: `oxalpha/session-hardening-wave`
Canonical HEAD: `6fc1aea`
Audit date: 2026-08-27

## Folder Inventory Table

| Folder | Branch | HEAD | Dirty? | Unique Work | Already Integrated? | Action |
|---|---|---|---|---|---|---|
| `euas-audit` | `oxalpha/audit-dr-hardening` | `7e54d48` | No (clean) | Audit chain anchoring, HSE KPI recommendations, PM capacity risk, cost roll-ups, MTTR, material shortage bridge, audit verification tests, concurrency smoke scripts | Partially (KPI engine integrated in HEAD, audit chain partially present) | **INTEGRATE MISSING** — cherry-pick audit/KPI commits; merge audit_store.py additions; keep worktree until verified |
| `euas-css-audit` | `oxalpha/euas-css-audit-20260825-0936` | `99d070a` | No | CSS/dashboard audit, maintenance intelligence cards, emergency snapshot-only policy, UI tests | Mostly skipped in consolidation (complex UI conflicts; newer UI in HEAD) | **REVIEW** — check if styles/app.js changes are missing; compare CSS changes |
| `euas-independent-verifier` | `oxalpha/euas-independent-verifier` | `032c8b5` | No | Independent verification module, audit chain, execution coordination docs | Partially present (audit verification tests exist in main repo) | **VERIFY** — check `audit_verification.py` differences; likely redundant |
| `euas-kpi-intel` | `oxalpha/utilities-kpi-intelligence` | `dcd1b1f` | No | KPI explanation drivers ranking by impact magnitude | Integrated in HEAD (`e48432e` merge) | **SAFE** — fully integrated |
| `euas-mega-session-2` | `oxalpha/hse-why-open-incidents` | `e294d26` | No | HSE KPI explanation access, open incident analysis | Integrated in HEAD (`36d1511` merge) | **SAFE** — fully integrated |
| `euas-opsaction` | `oxalpha/euas-operations-action-system` | `b84b024` | **YES** — `app/operations_store.py` modified; untracked: `_fix_outages.py`, `_recs_new.py`, `tests/test_operations_action_system.py` | Enhanced operations command center (lifecycle derivation, restoration intelligence, resolved situations, action bridges with roles, deterministic search, inbox enrichment, blocker chain, timeline determinism) | **NOT FULLY INTEGRATED** — basic `operations_store.py` exists but enhanced version with action bridges, lifecycle states, and restoration intelligence is missing | **INTEGRATE** — manually apply dirty file changes; add test file; resolve conflicts carefully |
| `euas-outbox` | `oxalpha/outbox-hardening` | `a068d2b` | No | Single-flight automation dispatch, webhook-skip observability, notify_once dedup, peer-locked rows skip | Partially integrated (security hardening includes outbox) | **REVIEW** — compare `outbox_store.py` differences |
| `euas-postrecon` | `main` | `450a50e` | No | Post-reconciliation operations integration | Old `main` branch; HEAD is ahead of `main` in canonical repo (`main` at `99d070a`) | **SAFE TO ARCHIVE** — work superseded by newer branches |
| `euas-product` | `oxalpha/utilities-product-development` | `7f07010` | No | Bad-actor detection, evidence shares, repeat-failure counts | Integrated in HEAD (`52ad454` merge) | **SAFE** — fully integrated |
| `euas-security` | `oxalpha/security-hardening` | `9228805` | No | Auth throttle, CSV injection guard (`_csv_safe_cell`), document media type derivation, X-Request-ID hygiene, pre-auth credential bounds | Cherry-picked in HEAD (`9e2bbb7`) | **SAFE** — fully integrated |
| `euas-telemetry` | `oxalpha/telemetry-hardening` | `a182e04` | No | Telemetry idempotency (`client_ref`), non-finite measurement rejection, alarm lifecycle guards, health readiness checks | Merged in HEAD (`6fc1aea`) | **SAFE** — fully integrated |
| `euas-trend-adapters` | `oxalpha/condition-intelligence-ui` | `8e5e7e3` | No | Alarm and condition trend intelligence with WHY drill-downs, trend explanation adapters | Partially integrated (`kpi_trend_explanation.py` exists) | **REVIEW** — compare UI/intelligence differences |
| `euas-validate` | `oxalpha/kpi-reconciliation` | `ff0b8c0` | No | KPI reconciliation, duplicate KPI engine consolidation, executive snapshot | Mostly integrated (`kpi_engine.py` unified) | **SAFE** — largely integrated |
| `EUAS_parallel_maximo` | `oxalpha/euas-parallel-maximo` | `69fa0b8` | No | APM analytics formulas documentation, reliability CSV exports, alarm correlation | Merged in HEAD (`52ad454`) | **SAFE** — fully integrated |
| `euas-supervisor` | **NOT A GIT REPO** | N/A | N/A | Contains `prompts/` and `reports/` folders only (empty of code) | Not applicable | **SAFE TO REMOVE** — empty/non-repo folder |

## Unique Branches Found (not in canonical HEAD)

- `oxalpha/audit-dr-hardening` (15 commits ahead: audit chain, KPI recommendations, PM/workforce capacity, cost roll-ups, MTTR, material bridge, HSE intelligence, verification tests)
- `oxalpha/euas-css-audit-20260825-0936` (43 commits ahead: CSS audit, maintenance intelligence cards, emergency policies)
- `oxalpha/euas-independent-verifier` (1 commit ahead: independent verifier merge PR #135 — likely redundant with audit tests already in HEAD)
- `oxalpha/euas-operations-action-system` (3 commits ahead + dirty uncommitted work: enhanced operations command center)
- `oxalpha/euas-work-mgmt-lifecycle-risk` (already merged: `7c11f1b`)
- `oxalpha/hse-why-open-incidents` (already merged: `36d1511`)
- `oxalpha/utilities-kpi-intelligence` (already merged: `e48432e`)
- `oxalpha/security-hardening` (cherry-picked: `9e2bbb7`)
- `oxalpha/telemetry-hardening` (merged: `6fc1aea`)
- `oxalpha/euas-parallel-maximo` (merged: `52ad454`)
- `oxalpha/utilities-product-development` (merged: `52ad454`)
- `oxalpha/condition-intelligence-ui` (partially integrated; trend adapters may have extra UI features)
- `oxalpha/kpi-reconciliation` (merged/reconciled via `ff0b8c0` and HEAD unification)
- `oxalpha/outbox-hardening` (partially integrated; review remaining differences)

## Dirty Uncommitted Work Recovered

- `euas-opsaction/app/operations_store.py`: Enhanced lifecycle derivation (`_derive_lifecycle`), restoration intelligence (`_restoration_intelligence`), resolved situations (`_resolved_situations`), recommendation identity and action bridges (`_ALARM_ACK_ROLES`, `_ALARM_WO_ROLES`, `_RESERVE_ROLES`, `_PR_ROLES`), deterministic timeline sorting, inbox enrichment (`_dedupe`, `_age_days`), command search scoring and determinism, blocker chain improvements.
- `euas-opsaction/_fix_outages.py`: Fix script for outage insert parameter counts (not core code, but indicates test/data fixes applied in dirty folder).
- `euas-opsaction/_recs_new.py`: Placeholder file (empty value).
- `euas-opsaction/tests/test_operations_action_system.py`: 863 lines of integration tests covering lifecycle states, restoration intelligence, blocker chain variants, command search determinism, inbox deduplication, recommendation identity stability, cross-site isolation, and concurrency.

## Conflicts Resolved During Prior Merges

From `docs/EUAS_CONSOLIDATION_REPORT.md` and current HEAD (`6fc1aea`):

- `ENGINEERING_EVIDENCE.json`: kept HEAD counts (newer schema version 12, 215 methods, 196 routes, 430 test definitions, 67 tables, 45 indexes).
- `app/application.py`: kept HEAD structure; included `close_outage` endpoint and `kpi_engine` imports.
- `app/config.py`: kept `SCHEMA_VERSION = 12`; added telemetry and security settings.
- `app/database.py`: kept HEAD schema (`audit_chain_anchor`, `kpi_snapshot`, `cbm_recommendations`, `fmea_records`, etc.) and added branch schema additions (`site_reliability_config`, `kpi_definitions`, `telemetry_readings.client_ref`).
- `static/app.js`: merged navigation with `intelligence`, `opscenter`, `analytics`, `telemetry`, `work`, `maintenance`, `assets`.
- `static/styles.css`: merged operations and intelligence CSS.

## Migration / Schema State

- `SCHEMA_VERSION` = 12 (`app/config.py`).
- No duplicate migration files detected.
- Schema initialization (`database.py`) includes: `audit_chain_anchor`, `audit_logs` (prev_hash, audit_hash), `asset_outages` (customers_interrupted), `sites` (customer_count), `telemetry_readings` (client_ref, unique index), `kpi_snapshot`, `kpi_definitions`, `kpi_snapshots`, `cbm_recommendations`, `fmea_records`, `site_reliability_config`.
- Startup path (`init_db`) is deterministic.
- No DB reset performed; all schema changes applied additively.

## Action Plan Per Folder

1. **euas-opsaction**: Manually merge dirty `operations_store.py` changes into canonical `app/operations_store.py`. Apply the enhanced lifecycle, restoration intelligence, resolved situations, recommendation action bridges, deterministic sorting, inbox enrichment, and search scoring. Copy `tests/test_operations_action_system.py` into canonical `tests/`. Resolve any conflicts by preferring newer HEAD structures for database calls but integrating the new logic.
2. **euas-audit**: Inspect audit branch commits (`7e54d48` and parents) for audit chain anchoring and KPI recommendation code. Cherry-pick or manually integrate if missing from HEAD. Check `audit_store.py`, `audit_verification.py`, and `database.py` differences.
3. **euas-css-audit**: Inspect CSS/app changes against HEAD. Only integrate genuinely missing design system updates.
4. **euas-outbox**: Compare `outbox_store.py` differences. Integrate notification deduplication and webhook-skip if missing.
5. **euas-trend-adapters**: Compare `kpi_trend_explanation.py` and UI files. Integrate missing trend explanation adapters.
6. **All others**: Confirm integration via existing HEAD commits; classify as safe for archive.

After integration, run all tests, resolve migration conflicts, remove duplicate code, and create final archive classification.
