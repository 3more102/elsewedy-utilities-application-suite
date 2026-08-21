# EUAS Tests

Run from the project root:

```bash
pytest -q
```

The v4.4.0 regression suite contains **22 tests** and covers:

- connected maintenance, inventory/procurement, PM and inspection workflows;
- Approval Center and RBAC/session/security behavior;
- HSE, project tasks, search, document controls and CRUD safeguards;
- Automation run ledger, alert de-duplication, metrics, exports and backup;
- SQLite/PostgreSQL adapter contract behavior;
- SLA assignment, breach detection/escalation and SLA export;
- durable integration outbox retention/retry;
- signed webhook serialization/HMAC delivery contract using an in-process fake receiver;
- governed alarm shelving and automatic expiry;
- topology-aware alarm incident correlation and explainable root-cause selection;
- offline field synchronization idempotency, safe same-batch rebase, stale-state conflict detection and explicit resolution;
- PWA field-sync/session-expiry contract checks;
- approval decision re-authentication, explicit signer intent and evidence persistence;
- approval-signature chain verification, deliberate tamper detection, delegated-authority evidence and protected export/metrics.

Tests use a dedicated test database and do not modify the normal `euas.db` database.
