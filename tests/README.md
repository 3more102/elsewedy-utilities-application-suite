# EUAS Tests

Run from the project root:

```bash
pytest -q
```

Current regression suite covers:

- connected maintenance, inventory/procurement, PM and inspection workflows;
- Approval Center and RBAC/session/security behavior;
- HSE, project tasks, search, document controls and CRUD safeguards;
- Automation run ledger, alert de-duplication, metrics, exports and backup;
- SQLite/PostgreSQL adapter contract behavior;
- SLA assignment, breach detection/escalation and SLA export;
- durable integration outbox retention/retry;
- signed webhook serialization/HMAC delivery contract using an in-process fake receiver.

Tests use a dedicated test database and do not modify the normal `euas.db` database.
