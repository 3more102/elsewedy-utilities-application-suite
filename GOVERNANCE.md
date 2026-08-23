# EUAS Governance & Evidence Controls

EUAS adds operational governance controls intended for a production-style reference deployment.

## Audit integrity

Every new audit record stores:

- prior record hash (`prev_hash`)
- deterministic SHA-256 record hash (`audit_hash`)
- actor, timestamp, action, module, record and before/after values

Verify through `GET /api/audit/integrity` or `python scripts/verify_audit.py`. The verifier walks the complete chain from the first record and returns the first invalid record on mismatch.

This is **tamper-evident**, not absolute immutability. Production compliance may require external anchoring, SIEM replication or WORM storage.

## Electronic approval signature evidence

Approval Center decisions in v4.4 require current-password re-authentication and an explicit intent statement tied to the business record code. A successful decision stores a canonical decision-time record snapshot plus signer identity/role, direct or delegated authority, comments and timestamp in `approval_signature_evidence`.

Evidence rows are linked with SHA-256 previous hashes and can be verified through `GET /api/approval-signatures/verify`. Individual evidence is available at `GET /api/approvals/{approval_id}/signature-evidence`, and management roles can export the evidence ledger from `/api/exports/approval-signatures.csv`.

The evidence chain is **tamper-evident**, not a PKI signature or immutable/WORM store. See `APPROVAL_SIGNATURES.md` for the security and compliance boundary.

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


## Access governance

v4.6 makes role grants and user overrides administrable records rather than fixed UI assumptions. High-risk access changes require current-password re-authentication, a reason and exact confirmation, and generate audit/outbox evidence. Core administrator lockout guards are enforced. Use `GET /api/exports/access-control.csv` for a governance snapshot and see `ACCESS_CONTROL.md` for precedence and security boundaries.

## Retention policies and execution

The policy registry covers audit, approval signatures, work, documents, notifications and integration events. `GET /api/governance/retention/preview` calculates eligibility, legal-hold coverage, protected records, blocked records and transaction-safe executable records using current cutoffs.

v4.5 adds governed run execution. `POST /api/governance/retention/runs` records either a non-destructive Preview or an explicit Execute run. Destructive execution is admin-only and requires both current-password re-authentication and the exact confirmation phrase `EXECUTE RETENTION`. Every run stores per-policy counts and a canonical SHA-256 linked manifest. Verify the run chain with `GET /api/governance/retention/verify` and download a portable evidence package from `/api/governance/retention/runs/{id}/evidence`.

Legal holds can be class-wide (`record_key=*`) or record-scoped. Active holds exclude covered records from execution; releasing a hold requires administrator re-authentication and a release reason. Audit Trail, Approval Signatures and Work Management remain protected from application purge. Documents are blocked from application purge until file/object deletion can be coordinated transactionally with an external object-storage lifecycle. The current executor performs transaction-safe deletion only for Notifications and Integration Events.

This is governed application retention with tamper-evident evidence, not local WORM storage. Evidence ZIPs can be transferred to an access-controlled immutable/WORM repository, which remains deployment infrastructure. See `RETENTION_GOVERNANCE.md`.

## Backup evidence

Administrator-generated SQLite backups are hashed after creation. The SHA-256 is returned in `X-EUAS-Backup-SHA256` and recorded in `backup_records` with size, version, creator and timestamp. This gives operational evidence that a specific bundle was generated; it is not a remote/off-site backup service.
