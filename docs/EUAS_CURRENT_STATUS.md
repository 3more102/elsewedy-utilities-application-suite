# EUAS Current Status

Canonical repository: `C:\Users\omar\elsewedy-utilities-application-suite`
Active branch: `oxalpha/session-hardening-wave`
HEAD: `6fc1aea` (uncommitted changes from production hardening session)

## Verified Baseline (New Session)

| Metric | Value |
|--------|-------|
| Tests passed | 540 |
| Tests failed | 0 |
| Warnings | 1 (StarletteDeprecationWarning) |
| API routes | 225 |
| Schema version | 12 |
| Test duration | ~155s |

## Production Hardening Changes (Uncommitted)

### Bug Fixes
- Fixed latent `NameError` in `_distribution_indices_report()` — calls to deleted `kpi_engine` functions replaced with delegation to canonical `kpi_store.compute_reliability_kpis()`
- Added `math.isfinite()` guard to legacy `ingest_telemetry` endpoint (NaN/Infinity rejection)

### Input Hardening
- Added `max_length` constraints to unbounded text fields: `WorkOrderIn`, `WorkOrderPatch`, `NoteIn`, `HSEIn`, `AssetIn`, `AssetPatch`

### Database Hardening
- Added `PRAGMA busy_timeout=5000` for SQLite concurrent write resilience

### Pagination
- Added `limit`/`offset` Query parameters to: `/api/inventory`, `/api/reliability/assets`, `/api/reliability/sites`, `/api/telemetry/channels`, `/api/vendors`, `/api/admin/users`
- Dashboard endpoints (`/api/assets/health`) intentionally unpaginated

### Dead Code Removal (Previous Session)
- Deleted `app/kpi_engine.py` (759 lines)
- Deleted 4 associated test files (717 lines)
- Removed 12 KPI CRUD/explain/recalc endpoints from `application.py`

### New Tests (Previous Session)
- `tests/test_domain_authorization.py` (15 tests) — Domain RBAC contracts
- `tests/test_idor_and_input_validation.py` (15 tests) — Security boundary tests
- `tests/test_operations_action_system.py` — Operations Command Center E2E tests

### Operations Command Center Overhaul
- Lifecycle derivation with documented state precedence
- Restoration intelligence with persisted-timing milestones
- Resolved situations surfacing
- Structured recommendation actions with required roles
- Inbox enrichment with severity, reason, SLA escalation
- Command search with ranked results and site scoping

### Security Hardening
- KPI routes switched to role-based access control
- New permission codes: `reliability.cbm.manage`, `reliability.cbm.decide`, `reliability.fmea.manage`
- Recommendation action roles defined
- CSP-compliant inline style replacement
- Progress element for risk bar visualization

## Documentation Created

| Document | Purpose |
|----------|---------|
| `docs/APPLICATION_DECOMPOSITION_PLAN.md` | 23-phase extraction plan for application.py |
| `docs/API_FRONTEND_COVERAGE.md` | Route classification (frontend consumed, API-only, orphaned, etc.) |
| `docs/DATABASE_INDEX_AUDIT.md` | All 56 indexes documented with query patterns |
| `docs/DATABASE_DEPLOYMENT_GUIDANCE.md` | SQLite WAL, busy_timeout, migration safety |
| `docs/POSTGRESQL_PORTABILITY.md` | SQLite construct portability assessment |
| `docs/EUAS_PRODUCTION_AUDIT.md` | Security headers, secrets, production mode |
| `docs/EUAS_ARCHIVE_MANIFEST.md` | Old folder classification and archive readiness |
| `docs/EUAS_TEST_INTEGRITY.md` | Test contract verification |
| `docs/EUAS_FOLDER_CONSOLIDATION_AUDIT.md` | Folder audit |
| `docs/EUAS_OLD_FOLDERS_STATUS.md` | Old folder status |
| `docs/FINAL_CONSOLIDATION_REPORT.md` | Consolidation summary |

## Known Remaining Items

### Audit Chain Gap
- `application.py:audit()` bypasses the chain lock in `audit_store.append_audit()`. Concurrent requests can fork the hash chain. The locked path in `audit_store.py` is correct; migration of `application.py` endpoints to use the locked path is recommended.

### Concurrency Gaps
- Work-order transitions lack CAS guard on status UPDATE
- Legacy `reserve_work_material` lacks row lock (patched path in `reservation_store.py` is safe)

### Cross-Site References
- No multi-tenancy isolation between sites. Assets, work orders, inventory can cross-reference.

### PostgreSQL Portability
- `julianday()` and `date('now','localtime')` in kpi_store/kpi_service are not PostgreSQL-compatible but not used in production code paths.

## Commands
- Start server: `python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload`
- Full test suite: `python -m pytest tests/ -q --tb=short`
- Quick validation: `python -m pytest tests/test_workflows.py tests/test_database_adapter.py tests/test_auth_session_api.py -q`
