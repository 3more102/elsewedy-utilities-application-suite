# EUAS Tests

Run from the project root:

```bash
python -m pytest -q
```

The v4.9.0 regression suite contains **32 tests** and covers:

- RCM consequence/strategy validation, one-strategy-per-FMEA governance, risk-based review cadence and same-asset CBM/PM linkage;
- four-eyes RCM approval with credential/signature evidence, activation, formal Continue/Revise lifecycle, revision reapproval and rejection handling;
- failure-mode hierarchy, cycle prevention, asset FMEA scoring, review history, governed work conversion and duplicate-work protection;
- CBM→FMEA→event→work traceability and same-asset linkage validation;
- condition-based maintenance rule authoring, Good-quality-only evaluation, consecutive-hit filtering, cooldown and governed auto-work generation;
- connected maintenance, inventory/procurement, PM and inspection workflows;
- Approval Center and RBAC/session/security behavior;
- HSE, project tasks, search, document controls and CRUD safeguards;
- Automation run ledger, alert de-duplication, metrics, exports and backup;
- SQLite/PostgreSQL adapter contract behavior;
- SLA assignment, breach detection/escalation and SLA export;
- durable integration outbox retention/retry and signed webhook delivery contract;
- governed alarm shelving and automatic expiry;
- topology-aware alarm incident correlation and explainable root-cause selection;
- offline field synchronization idempotency, safe same-batch rebase, stale-state conflict detection and explicit resolution;
- approval decision re-authentication, explicit signer intent, evidence persistence and chain tamper detection;
- governed retention Preview/Execute runs, legal holds, protected-class enforcement and retention evidence-chain verification;
- fine-grained permission precedence, credential-confirmed access changes, dynamic role grants and administrator lockout guards.

Tests use a dedicated test database and do not modify the normal `euas.db` database.
