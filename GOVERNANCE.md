# EUAS Governance & Evidence Controls

EUAS adds operational governance controls intended for a production-style reference deployment.

## Audit integrity

Every new audit record stores:

- prior record hash (`prev_hash`)
- deterministic SHA-256 record hash (`audit_hash`)
- actor, timestamp, action, module, record and before/after values

Verify through `GET /api/audit/integrity` or `python scripts/verify_audit.py`. The verifier walks the complete chain from the first record and returns the first invalid record on mismatch.

This is **tamper-evident**, not absolute immutability. Production compliance may require external anchoring, SIEM replication or WORM storage.

## Maintenance cost ledger

Labor and spare-part consumption post separate cost-ledger entries linked to work order and asset. Asset detail pages expose lifetime maintenance cost and recent entries; Analytics aggregates spend by cost type. CSV export is available at `/api/exports/cost-ledger.csv`.

## Asset lifecycle timeline

`GET /api/assets/{id}/timeline` combines work creation/completion, inspections, meter readings, documents and cost postings into one timestamp-ordered lifecycle view.

## Asset dossier snapshots

`POST /api/assets/{id}/dossier` captures a point-in-time asset dossier containing asset metadata, child assets, work history, inspections, documents, cost entries and meter history. The serialized snapshot is stored with SHA-256.

- List: `GET /api/reports/snapshots`
- Read: `GET /api/reports/snapshots/{id}`
- Verify: `GET /api/reports/snapshots/{id}/verify`
- Render: `GET /api/reports/snapshots/{id}/html`

EUAS exposes no report-snapshot update/delete API. Database-level immutability remains an infrastructure concern.

## Retention policies

The policy registry covers audit, work, documents, notifications and integration events. `GET /api/governance/retention/preview` calculates eligible record counts using current cutoffs. The reference build deliberately performs **no automatic destructive purge**.

## Backup evidence

Administrator-generated SQLite backups are hashed after creation. The SHA-256 is returned in `X-EUAS-Backup-SHA256` and recorded in `backup_records` with size, version, creator and timestamp. This gives operational evidence that a specific bundle was generated; it is not a remote/off-site backup service.
