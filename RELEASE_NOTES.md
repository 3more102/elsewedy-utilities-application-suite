# EUAS 4.4.0 Release Notes

## Release focus

EUAS 4.4.0 strengthens the Approval Center with **credential-verified electronic approval signatures and tamper-evident decision evidence**. Approval authority is still checked through the existing workflow/delegation model; v4.4 adds explicit signer re-authentication and evidence instead of creating a parallel approval path.

## Delivered

- Current-password re-authentication for generic Approval Center approve/reject decisions.
- Exact signer-intent statement bound to the target business record code.
- Decision-time snapshot of the approval request and post-decision target record.
- New `approval_signature_evidence` table with one evidence record per decided approval.
- Decision-time signer identity/role snapshot and direct-versus-delegated authority evidence.
- SHA-256 previous-hash chain across approval evidence records.
- Verifier that checks chain links, hashes and duplicated searchable columns against the canonical evidence payload.
- Evidence-detail API and management-only integrity API.
- Protected approval-signature CSV export.
- Approval Center `E-signed` badges and signature detail in workflow history.
- Automation & Reports governance panel now verifies both audit and approval-signature chains.
- Prometheus-style signed-approval count and signature-chain validity metrics.
- Protected `Approval Signatures` retention-policy metadata and retention-preview integration.
- README/schema metadata corrected from the stale v4.3 schema badge.
- Service-worker shell cache advanced to `euas-shell-v4.4.0`.
- Schema version 14 and 22 automated regression tests.

## Approval security model

A successful electronic decision requires all of the following in the same database transaction:

1. The caller is authorized for the approval by role, direct assignment, active delegation, or allowed management authority.
2. The target record is still in the workflow state expected by the pending approval.
3. The signer-intent statement exactly matches `I approve <record code>` or `I reject <record code>`.
4. The acting user's current password verifies against the stored PBKDF2-SHA256 credential.
5. The business workflow update, normal audit event, approval resolution and signature-evidence insert all succeed.

A failed re-authentication or intent check leaves the approval and target workflow unchanged.

## Evidence integrity model

The evidence payload includes the signer snapshot, approval metadata, intent, comments, timestamp and post-decision record snapshot. Each evidence hash links to the previous approval evidence hash. The verifier recomputes every link and verifies searchable evidence columns against their canonical JSON payload.

This mechanism detects stored-content tampering in the reference database. It remains **tamper-evident**, not an immutable/WORM or PKI signature system.

## Upgrade

v4.3 databases upgrade additively from schema 13 to schema 14. The initializer creates `approval_signature_evidence`, its signer/time indexes, and the protected approval-signature retention policy while preserving existing approvals and operational data. Existing previously decided approvals are not retroactively fabricated as electronically signed; only v4.4 decisions generate signature evidence.

## Compliance boundary

EUAS v4.4.0 does not claim eIDAS qualified signatures, FDA 21 CFR Part 11 certification, legal non-repudiation, trusted timestamping, hardware-backed signing keys, or WORM storage. Those require deployment-specific identity, infrastructure, validation and governance controls beyond the reference application.
