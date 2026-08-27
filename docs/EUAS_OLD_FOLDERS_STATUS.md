# EUAS Old Folder Status — After Consolidation

Audit date: 2026-08-27
Canonical repository verified: `C:\Users\omar\elsewedy-utilities-application-suite`
Canonical branch: `oxalpha/session-hardening-wave`
Canonical HEAD: `6fc1aea`

## Classification

### SAFE TO ARCHIVE (redundant after integration)

These folders contain commits that are either fully merged into the canonical repo (`oxalpha/session-hardening-wave`) or superseded by newer architecture. No unique uncommitted work remains.

| Folder | Status | Evidence |
|---|---|---|
| `euas-kpi-intel` (`oxalpha/utilities-kpi-intelligence`) | **SAFE** | Merged in HEAD (`e48432e`). KPI engine (`kpi_engine.py`), trend adapters (`kpi_trend_explanation.py`), intelligence modules all present. |
| `euas-mega-session-2` (`oxalpha/hse-why-open-incidents`) | **SAFE** | Merged in HEAD (`36d1511`). HSE open incident analysis integrated. |
| `euas-product` (`oxalpha/utilities-product-development`) | **SAFE** | Merged in HEAD (`52ad454`). Bad-actor detection, evidence shares, reliability CSV exports present. |
| `euas-security` (`oxalpha/security-hardening`) | **SAFE** | Cherry-picked in HEAD (`9e2bbb7`). Auth throttle, CSV injection guard, media type derivation, X-Request-ID hygiene integrated. |
| `euas-telemetry` (`oxalpha/telemetry-hardening`) | **SAFE** | Merged in HEAD (`6fc1aea`). Telemetry idempotency (`client_ref`), non-finite rejection, alarm lifecycle guards present. |
| `euas-parallel-maximo` (`oxalpha/euas-parallel-maximo`) | **SAFE** | Merged in HEAD (`52ad454`). APM analytics formulas (`docs/APM_ANALYTICS.md`), reliability CSV exports, alarm correlation present. |
| `euas-validate` (`oxalpha/kpi-reconciliation`) | **SAFE** | Reconciled in HEAD (`ff0b8c0` + newer unification). Executive snapshot platform and duplicate KPI engine consolidation completed. |
| `euas-postrecon` (`main`) | **SAFE** | Superseded by `oxalpha/session-hardening-wave`. Post-reconciliation operations integrated in newer branches. |
| `euas-supervisor` | **SAFE** | Not a Git repo. Contains only empty `prompts/` and `reports/` folders. No source code. |

### KEEP — UNIQUE WORK REMAINS (requires further review or integration)

These folders contain genuinely missing work that was not fully integrated. They should remain until verified and merged.

| Folder | Status | Evidence | Required Action |
|---|---|---|---|
| `euas-opsaction` (`oxalpha/euas-operations-action-system`) | **KEEP UNTIL FULLY VERIFIED** | Dirty uncommitted work integrated (`operations_store.py` enhanced with lifecycle derivation, restoration intelligence, resolved situations, action bridges, deterministic search/inbox). Test file `tests/test_operations_action_system.py` added. One SQL query error in `test_blocker_chain_partial_cancelled_and_reservation_variants` (10 values for 9 columns) needs fix. | Fix remaining SQL query mismatch in test; verify all new routes pass; confirm no conflicts with newer HEAD. Once verified, archive. |
| `euas-audit` (`oxalpha/audit-dr-hardening`) | **KEEP** | 15 commits ahead (`7e54d48`). Contains audit chain anchoring (`audit_store.py`), HSE recommendations, PM capacity risk, material shortage bridge, cost roll-ups (`kpi_service.py`), MTTR, verification tests, concurrency smoke scripts. Some audit features present but full audit chain hardening and verification automation may be missing. | Inspect `audit_store.py`, `audit_verification.py`, `database.py` differences. Cherry-pick or manually integrate audit chain anchoring and verification automation if not fully covered by HEAD's `audit_logs` schema. |
| `euas-css-audit` (`oxalpha/euas-css-audit-20260825-0936`) | **KEEP** | 43 commits ahead (`99d070a`). Large CSS/dashboard audit with emergency snapshot-only policy, maintenance intelligence cards, UI refresh. Some UI changes skipped during prior consolidation due to conflicts with newer HEAD UI. | Compare `static/app.js`, `static/styles.css`, `static/dashboard-action-center.css` against HEAD. Integrate genuinely missing design system updates and emergency policies without overwriting newer navigation/intelligence structure. |
| `euas-outbox` (`oxalpha/outbox-hardening`) | **KEEP** | Partial integration (`security-hardening` cherry-pick covers some outbox). Full outbox single-flight automation, webhook-skip observability (`outbox_store.py`), notify_once dedup may have additional differences not fully captured. | Inspect `app/outbox_store.py` differences (dirty folder has `a068d2b` ahead). Merge remaining notification deduplication and webhook-skip logic if missing. |
| `euas-trend-adapters` (`oxalpha/condition-intelligence-ui`) | **KEEP** | Partially integrated (`kpi_trend_explanation.py` exists in HEAD). Additional alarm/condition trend intelligence with WHY drill-downs may have extra UI/components. | Inspect `app/kpi_trend_explanation.py` and frontend components for missing trend adapter routes or UI cards. |
| `euas-independent-verifier` (`oxalpha/euas-independent-verifier`) | **KEEP** | Independent verification merge (`032c8b5`). May contain additional verification automation or audit chain tests not fully covered. | Compare `tests/test_audit_verification.py` and `app/audit_verification.py` against HEAD. Integrate if missing. |

### KEEP — DIRTY/UNCOMMITTED (work preserved in canonical repo)

| Folder | Status | Evidence |
|---|---|---|
| `euas-opsaction` | **DIRTY WORK RECOVERED** | Uncommitted changes to `app/operations_store.py` manually applied to canonical repo. Untracked `_fix_outages.py`, `_recs_new.py`, `tests/test_operations_action_system.py` copied into canonical `tests/`. Dirty state preserved in git status of old folder but work now exists in canonical repo. |

### UNKNOWN / REQUIRES MANUAL INSPECTION

No folders fall into this category at audit time. All sibling EUAS folders have been inspected and classified above.

## Archive Command (after final verification)

Once all `KEEP` folders are verified and their unique work integrated (or explicitly rejected), move them to archive:

```powershell
# After verification, archive safe folders
New-Item -ItemType Directory -Path C:\Users\omar\EUAS_OLD_ARCHIVE
Move-Item -Path C:\Users\omar\euas-kpi-intel -Destination C:\Users\omar\EUAS_OLD_ARCHIVE
Move-Item -Path C:\Users\omar\euas-mega-session-2 -Destination C:\Users\omar\EUAS_OLD_ARCHIVE
Move-Item -Path C:\Users\omar\euas-product -Destination C:\Users\omar\EUAS_OLD_ARCHIVE
Move-Item -Path C:\Users\omar\euas-security -Destination C:\Users\omar\EUAS_OLD_ARCHIVE
Move-Item -Path C:\Users\omar\euas-telemetry -Destination C:\Users\omar\EUAS_OLD_ARCHIVE
Move-Item -Path C:\Users\omar\EUAS_parallel_maximo -Destination C:\Users\omar\EUAS_OLD_ARCHIVE
Move-Item -Path C:\Users\omar\euas-validate -Destination C:\Users\omar\EUAS_OLD_ARCHIVE
Move-Item -Path C:\Users\omar\euas-postrecon -Destination C:\Users\omar\EUAS_OLD_ARCHIVE
Move-Item -Path C:\Users\omar\euas-supervisor -Destination C:\Users\omar\EUAS_OLD_ARCHIVE
```

## Worktrees Cleaned / Verified

- `.worktrees/euas-operations-command-center-v2` (embedded repo inside `.worktrees/`) — verified inside canonical repo; should be removed manually (`rm -rf .worktrees/euas-operations-command-center-v2`).
- Actual Git worktrees (`euas-audit`, `euas-css-audit`, etc.) remain in `C:\Users\omar` but their commits are either integrated or classified above. Do not delete worktrees until unique work is preserved or integrated.
