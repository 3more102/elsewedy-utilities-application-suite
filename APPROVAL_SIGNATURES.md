# EUAS Approval Signature Evidence

EUAS v4.4.0 adds application-level electronic evidence to Approval Center decisions. The goal is to prove who acted, what they intended, what record state they acted on, and whether the stored evidence chain still verifies.

## Decision contract

Every generic Approval Center approve/reject decision now requires:

1. The caller to already hold valid approval authority (direct user, assigned role, active delegation, or management override where allowed).
2. The caller's **current password** to be re-entered and verified against the active user account.
3. An exact signer-intent statement bound to the record code:
   - `I approve <RECORD_CODE>`
   - `I reject <RECORD_CODE>`
4. The target workflow record to still be in the expected state.
5. The current target fingerprint to match the immutable snapshot captured when approval was requested. A materially changed target must be re-requested rather than silently approved under stale evidence.

Authorization checks remain independent of re-authentication. Re-entering a valid password does not grant approval authority.

## Evidence record

A successful decision creates one `approval_signature_evidence` row for the approval request. The evidence contains:

- evidence and approval identifiers;
- module, record type, record ID and business record code;
- final decision;
- signer user ID, username, display name and role as a decision-time snapshot;
- whether authority was exercised through an active delegation, including the delegation identifier in the canonical payload;
- credential-verification flag;
- explicit signer-intent statement;
- decision comments;
- signature timestamp;
- a canonical JSON payload containing the approval request snapshot and post-decision business-record snapshot;
- previous evidence hash and current SHA-256 evidence hash.

Passwords are **never** stored in signature evidence.

## Hash chain

Evidence rows form a global append-only-through-API chain. Each evidence hash is calculated from the previous evidence hash plus the canonical payload. Verification also compares searchable evidence columns back to the canonical payload.

- Verify: `GET /api/approval-signatures/verify`
- Evidence detail: `GET /api/approvals/{approval_id}/signature-evidence`
- Export: `GET /api/exports/approval-signatures.csv`

The verifier reports the first invalid evidence row and failure reason when persisted data no longer matches the chain.

## Request snapshots and delegation scope

Schema 22 binds every new approval request to a canonical target snapshot, SHA-256 fingerprint, resource-version hint and correlation ID. Legacy pending approvals are backfilled once at their first governed decision so upgrades do not force destructive recreation.

Delegations are one-hop only and can be limited by module, record type and resource ID. A delegation is usable only while its time window is active, it has not been revoked, and both the delegator and delegate still hold the required direct approval/domain permissions. Delegation therefore narrows authority; it never manufactures a capability that either party does not possess.

## Decision lifecycle evidence

In addition to the compatibility `approval_signature_evidence` record, `approval_evidence_events` stores a hash-linked lifecycle history for approval requests and delegation mutations. It records actor/effective actor, delegation ID where applicable, target fingerprint, correlation ID and the prior evidence hash.

- Verify lifecycle chain: `GET /api/approval-evidence/verify`
- Reconstruct approval history: `GET /api/approvals/{approval_id}/decision-history`

The lifecycle chain currently records approval requests, approve/reject decisions, delegation creation and explicit delegation revocation. It is integrity evidence, not asymmetric-cryptographic signature proof.

## UI and observability

The Approval Center marks decisions that have signature evidence and exposes signer, intent, authority mode, timestamp and hash in workflow history. Automation & Reports shows the approval-signature chain status alongside the general audit-chain status.

Metrics include:

- `euas_signed_approvals_total`
- `euas_approval_signature_chain_valid`
- `euas_approval_evidence_chain_valid`
- `euas_approval_requested_total` / `euas_approval_granted_total` / `euas_approval_rejected_total` / `euas_approval_expired_total`
- `euas_approval_delegation_active`
- `euas_approval_verification_failure_total`

## Retention

`Approval Signatures` is registered as a protected retention-policy data class. The current reference build provides retention preview only and does not automatically purge protected evidence.

## Compliance boundary

This capability is **electronic approval evidence**, not a claim of:

- a PKI-backed digital signature;
- eIDAS qualified/advanced electronic signature status;
- FDA 21 CFR Part 11 certification;
- legally guaranteed non-repudiation;
- hardware-backed signing keys;
- WORM/immutable database storage.

The SHA-256 chain is tamper-evident, not tamper-proof. A privileged database operator could theoretically rewrite both content and hashes. Regulated deployments should add external identity/MFA, trusted timestamps where required, external evidence anchoring or WORM storage, formal validation, and organization-specific signature policy.
