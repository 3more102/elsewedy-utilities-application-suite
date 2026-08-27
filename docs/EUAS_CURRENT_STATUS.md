# EUAS Current Status

Canonical repository: `C:\Users\omar\elsewedy-utilities-application-suite`
Active branch: `oxalpha/session-hardening-wave`
HEAD: cf175d2 (pending session hardening commit)

## Verified Baseline (Post-Hardening)

| Metric | Value |
|--------|-------|
| Tests passed | 553 |
| Tests failed | 0 |
| Warnings | 1 (StarletteDeprecationWarning) |
| API routes | 225 |
| Schema version | 12 |

## Session Hardening Changes

### Audit Chain
- Removed duplicate hash-chain logic from `application.py:audit()`. All audit writes now delegate to the canonical `audit_store.append_audit()` which provides:
  - Transactional row-level locking via `audit_chain_lock`
  - Anchor updates on every append (`audit_chain_anchor`)
  - Concurrent writer safety (no chain fork possible)
- `main.py` already replaces `application.audit` with `append_audit` at import time; the code-level fix eliminates the maintenance hazard of duplicate logic

### Work-Order CAS
- All work-order status transitions already use atomic CAS via `workflow_store.transition_work_atomic()`:
  - `UPDATE work_orders SET status=? WHERE id=? AND status=?`
  - Affected-row count check with `WorkflowTransitionConflict` on 0 rows
  - Stale transitions rejected with HTTP 409
- Dispatch, task toggle, and note append also use CAS patterns

### Legacy Paths
- **Inventory**: `reservation_store.py` provides canonical locked path; `application.py` route is replaced by `install_reservation_routes()`
- **Telemetry**: `telemetry_store.py` provides canonical temporal-integrity path; `application.py` route is replaced by `install_telemetry_temporal_integrity()`
- **Workflow**: `workflow_store.py` provides canonical atomic transition path; `application.py` route is replaced by `install_workflow_transition_routes()`

### New Tests
- `tests/test_session_hardening_audit_and_workflow.py` (13 tests):
  - Audit: normal append, concurrent writers, no fork, anchor match, tamper detection, idempotent init
  - Work-order CAS: approve race, dispatch vs cancel, complete vs hold, stale rejection, one winner, audit for successful only

### Historical Worktree
- `.worktrees/euas-operations-command-center-v2/` is identical to HEAD. Classified as SAFE_TO_ARCHIVE.

## Previous Hardening (Still Present)

### Bug Fixes
- Fixed latent `NameError` in `_distribution_indices_report()` — calls to deleted `kpi_engine` functions replaced with delegation to canonical `kpi_store.compute_reliability_kpis()`
- Added `math.isfinite()` guard to legacy `ingest_telemetry` endpoint (NaN/Infinity rejection)

### Input Hardening
- Added `max_length` constraints to unbounded text fields: `WorkOrderIn`, `WorkOrderPatch`, `NoteIn`, `HSEIn`, `AssetIn`, `AssetPatch`

### Database Hardening
- Added `PRAGMA busy_timeout=5000` for SQLite concurrent write resilience

### Pagination
- Added `limit`/`offset` Query parameters to: `/api/inventory`, `/api/reliability/assets`, `/api/reliability/sites`, `/api/telemetry/channels`, `/api/vendors`, `/api/admin/users`

## Commands
- Start server: `python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload`
- Full test suite: `python -m pytest tests/ -q --tb=short`
