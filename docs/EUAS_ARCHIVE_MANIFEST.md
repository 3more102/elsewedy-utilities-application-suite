# EUAS Archive Manifest

Audit date: 2026-08-27
Canonical repository: `C:\Users\omar\elsewedy-utilities-application-suite`
Canonical branch: `oxalpha/session-hardening-wave`
Canonical HEAD: `cf175d2` (pre-commit baseline; hardening commit pending)

This document records the classification of sibling EUAS folders, their integration status, unique work, and recommended action.

## SAFE TO ARCHIVE

These folders contain commits fully merged into or superseded by the canonical HEAD. No unique uncommitted work remains.

### euas-kpi-intel

- Branch: `oxalpha/utilities-kpi-intelligence`
- HEAD: `dcd1b1f`
- Status: Clean
- Unique work: KPI explanation drivers ranking by impact magnitude, trend adapters, intelligence modules
- Integration: Merged in HEAD (`e48432e`). All KPI engine, trend adapters, and intelligence modules present.
- Action: Archive.

### euas-mega-session-2

- Branch: `oxalpha/hse-why-open-incidents`
- HEAD: `e294d26`
- Status: Clean
- Unique work: HSE open incident analysis, KPI explanation access
- Integration: Merged in HEAD (`36d1511`).
- Action: Archive.

### euas-product

- Branch: `oxalpha/utilities-product-development`
- HEAD: `7f07010`
- Status: Clean
- Unique work: Bad-actor detection, evidence shares, repeat-failure counts, reliability CSV exports
- Integration: Merged in HEAD (`52ad454`).
- Action: Archive.

### euas-security

- Branch: `oxalpha/security-hardening`
- HEAD: `9228805`
- Status: Clean
- Unique work: Auth throttle, CSV injection guard, document media type derivation, X-Request-ID hygiene, pre-auth credential bounds
- Integration: Cherry-picked in HEAD (`9e2bbb7`).
- Action: Archive.

### euas-telemetry

- Branch: `oxalpha/telemetry-hardening`
- HEAD: `a182e04`
- Status: Clean
- Unique work: Telemetry idempotency (`client_ref`), non-finite measurement rejection, alarm lifecycle guards, health readiness checks
- Integration: Merged in HEAD (`6fc1aea`).
- Action: Archive.

### euas-parallel-maximo

- Branch: `oxalpha/euas-parallel-maximo`
- HEAD: `69fa0b8`
- Status: Clean
- Unique work: APM analytics formulas documentation, reliability CSV exports, alarm correlation
- Integration: Merged in HEAD (`52ad454`).
- Action: Archive.

### euas-validate

- Branch: `oxalpha/kpi-reconciliation`
- HEAD: `ff0b8c0`
- Status: Clean
- Unique work: KPI reconciliation, duplicate KPI engine consolidation, executive snapshot platform
- Integration: Mostly integrated via HEAD unification. KPI engine is unified.
- Action: Archive.

### euas-postrecon

- Branch: `main`
- HEAD: `450a50e`
- Status: Clean
- Unique work: Post-reconciliation operations integration
- Integration: Old `main` branch; HEAD is ahead. Work superseded by newer branches.
- Action: Archive.

### euas-supervisor

- Branch: Not a Git repository
- Status: Contains only empty `prompts/` and `reports/` folders. No source code.
- Action: Remove directory.

## KEEP -- UNIQUE WORK REMAINS

These folders contain genuinely missing work not fully integrated into the canonical HEAD. They should remain until verified and merged.

### euas-opsaction

- Branch: `oxalpha/euas-operations-action-system`
- HEAD: `b84b024`
- Status: Clean
- Unique work: Enhanced operations_store.py with lifecycle derivation, restoration intelligence, resolved situations, recommendation identity and action bridges
- Integration: All operations_store.py functionality present in HEAD.
- Action: Archive.

### euas-audit

- Branch: `oxalpha/audit-dr-hardening`
- HEAD: `7e54d48`
- Status: Clean
- Commits ahead: 15
- Unique work:
  - Audit chain anchoring (`audit_store.py`)
  - HSE KPI recommendations
  - PM capacity risk analysis
  - Cost roll-ups (`kpi_service.py`)
  - MTTR calculations
  - Material shortage bridge
  - Audit verification tests
  - Concurrency smoke scripts
- Integration status: Partially present. KPI engine integrated in HEAD. Full audit chain hardening and verification automation may be missing.
- Required action: Inspect `audit_store.py`, `audit_verification.py`, `database.py` differences. Cherry-pick or manually integrate audit chain anchoring and verification automation.
- Action: Keep until verified and merged; then archive.

### euas-css-audit

- Branch: `oxalpha/euas-css-audit-20260825-0936`
- HEAD: `99d070a`
- Status: Clean
- Commits ahead: 43
- Unique work:
  - CSS/dashboard audit with emergency snapshot-only policy
  - Maintenance intelligence cards
  - UI refresh components
  - Dashboard action center CSS
- Integration status: Mostly skipped during consolidation due to complex UI conflicts with newer HEAD UI.
- Required action: Compare `static/app.js`, `static/styles.css`, `static/dashboard-action-center.css` against HEAD. Integrate genuinely missing design system updates and emergency policies without overwriting newer navigation/intelligence structure.
- Action: Keep until reviewed and merged; then archive.

### euas-outbox

- Branch: `oxalpha/outbox-hardening`
- HEAD: `a068d2b`
- Status: Clean
- Commits ahead: 1
- Unique work:
  - Single-flight automation dispatch
  - Webhook-skip observability (`outbox_store.py`)
  - `notify_once` deduplication
  - Peer-locked rows skip logic
- Integration status: Partially integrated (security hardening includes some outbox changes). Full outbox single-flight automation and dedup may have additional differences.
- Required action: Compare `app/outbox_store.py` differences. Merge remaining notification deduplication and webhook-skip logic if missing.
- Action: Keep until reviewed and merged; then archive.

### euas-trend-adapters

- Branch: `oxalpha/condition-intelligence-ui`
- HEAD: `8e5e7e3`
- Status: Clean
- Unique work:
  - Alarm and condition trend intelligence with WHY drill-downs
  - Trend explanation adapters
  - Additional UI components for condition intelligence
- Integration status: Partially integrated. `kpi_trend_explanation.py` exists in HEAD but additional alarm/condition trend UI may be missing.
- Required action: Inspect `app/kpi_trend_explanation.py` and frontend components for missing trend adapter routes or UI cards.
- Action: Keep until reviewed and merged; then archive.

### euas-independent-verifier

- Branch: `oxalpha/euas-independent-verifier`
- HEAD: `032c8b5`
- Status: Clean
- Commits ahead: 1
- Unique work:
  - Independent verification module
  - Audit chain verification automation
  - Execution coordination docs
- Integration status: Partially present. Audit verification tests exist in main repo but additional verification automation may differ.
- Required action: Compare `tests/test_audit_verification.py` and `app/audit_verification.py` against HEAD. Integrate if missing.
- Action: Keep until verified and merged; then archive.

### euas-operations-command-center-v2

- Branch: `oxalpha/session-hardening-wave`
- HEAD: `cf175d2` (identical to canonical HEAD)
- Status: Clean
- Unique work: None — files are byte-identical to canonical HEAD
- Integration: Fully integrated. No unique production behavior exists.
- Action: SAFE_TO_ARCHIVE.

## Cleanup Items

### .worktrees/ Directory

The `.worktrees/` directory exists inside the canonical repository and contains an embedded worktree (`euas-operations-command-center-v2`). This is not a standard Git worktree and should be removed:

```powershell
Remove-Item -Recurse -Force .worktrees
```

### Database Backups

These backup files are in the repository root and should be moved to secure offline storage:

| File | Description |
|---|---|
| `euas.db.backup_canonical_consolidation_20260827_153355.bak` | Consolidation backup from 2026-08-27 |
| `euas.db.pre_audit_anchor_repair.bak` | Pre-audit anchor repair backup |

### Temporary Files

These temporary/test output files should be removed from the repository root:

| File | Description |
|---|---|
| `failure_summary.txt` | Test failure summary output |
| `full_suite_failures.txt` | Full test suite failure log |
| `pytest_baseline.txt` | Pytest baseline output |
| `pytest_current.txt` | Pytest current output |
| `pytest_final.txt` | Pytest final output |
| `pytest_final_final.txt` | Pytest final-final output |
| `pytest_final_regression.txt` | Pytest regression output |
| `pytest_true_baseline.txt` | Pytest true baseline output |

## Archive Command

After all KEEP folders are verified and their unique work integrated (or explicitly rejected):

```powershell
# Create archive directory
New-Item -ItemType Directory -Path C:\Users\omar\EUAS_OLD_ARCHIVE

# Move safe folders
Move-Item -Path C:\Users\omar\euas-kpi-intel -Destination C:\Users\omar\EUAS_OLD_ARCHIVE
Move-Item -Path C:\Users\omar\euas-mega-session-2 -Destination C:\Users\omar\EUAS_OLD_ARCHIVE
Move-Item -Path C:\Users\omar\euas-product -Destination C:\Users\omar\EUAS_OLD_ARCHIVE
Move-Item -Path C:\Users\omar\euas-security -Destination C:\Users\omar\EUAS_OLD_ARCHIVE
Move-Item -Path C:\Users\omar\euas-telemetry -Destination C:\Users\omar\EUAS_OLD_ARCHIVE
Move-Item -Path C:\Users\omar\EUAS_parallel_maximo -Destination C:\Users\omar\EUAS_OLD_ARCHIVE
Move-Item -Path C:\Users\omar\euas-validate -Destination C:\Users\omar\EUAS_OLD_ARCHIVE
Move-Item -Path C:\Users\omar\euas-postrecon -Destination C:\Users\omar\EUAS_OLD_ARCHIVE
Move-Item -Path C:\Users\omar\euas-supervisor -Destination C:\Users\omar\EUAS_OLD_ARCHIVE

# Remove worktrees directory
Remove-Item -Recurse -Force C:\Users\omar\elsewedy-utilities-application-suite\.worktrees

# Remove temporary files
Remove-Item C:\Users\omar\elsewedy-utilities-application-suite\failure_summary.txt
Remove-Item C:\Users\omar\elsewedy-utilities-application-suite\full_suite_failures.txt
Remove-Item C:\Users\omar\elsewedy-utilities-application-suite\pytest_*.txt
```
