# EUAS v4.4.0 — QA / Verification Report

**Release:** 4.4.0  
**Schema:** 14  
**Focus:** Credential-verified Approval Center signatures and tamper-evident decision evidence

## Verification summary

| Check | Result |
|---|---|
| Python source compilation | **PASS** |
| Frontend JavaScript syntax (`node --check`) | **PASS** |
| Service-worker JavaScript syntax (`node --check`) | **PASS** |
| Integrated pytest regression suite | **PASS — 22 tests** |
| Fresh Uvicorn HTTP smoke | **PASS — version 4.4.0** |
| Fresh SQLite initialization | **PASS — schema 14** |
| v4.3.0 → v4.4.0 SQLite schema upgrade | **PASS — 13 → 14** |
| SQLite integrity check after upgrade | **PASS — ok** |
| Approval current-password re-authentication | **PASS** |
| Exact signer-intent binding | **PASS** |
| Signature evidence persistence + record snapshot | **PASS** |
| Approval-signature chain verification | **PASS** |
| Deliberate evidence tamper detection | **PASS** |
| Delegated-authority evidence capture | **PASS** |
| Signature metrics + protected CSV export | **PASS** |
| Release metadata/source-count consistency | **PASS** |

## Release metrics

- **195 application HTTP routes** total
- **194 routes under `/api/`**
- **71 relational tables**
- **55 explicit application indexes**
- **Schema version 14**
- **22 automated regression tests**

## v4.4 approval-signature verification

Regression coverage verifies:

1. A pending approval remains unchanged when the signer omits electronic-signature intent.
2. An incorrect signer-intent statement is rejected.
3. An incorrect current password is rejected with no workflow mutation.
4. A valid authorized signer can approve after successful current-password re-authentication.
5. A successful decision returns `SIG-*` evidence with a 64-character SHA-256 hash.
6. The evidence payload records approval metadata, signer identity/role, decision, intent, comments, timestamp and the post-decision business-record snapshot.
7. Approval list results expose evidence metadata for electronically signed decisions.
8. Signature evidence can be read only by users with approval/evidence visibility.
9. The global approval-signature evidence chain verifies from its first row through its head hash.
10. A direct database modification to a duplicated evidence column is detected as a payload/column mismatch.
11. Restoring the original value restores successful chain verification.
12. Approval decisions exercised through active delegation store `delegated_authority=1`.
13. Prometheus-style metrics expose signed-approval count and signature-chain validity.
14. The management CSV export contains the evidence identifier, signer, intent and chain hashes.
15. `Approval Signatures` is present as a protected retention-policy data class.
16. The v4.4 service-worker shell uses `euas-shell-v4.4.0`.
17. The release manifest, README badges, source-measured route/table/index counts and test count agree.

## Upgrade verification

A clean v4.3.0 database was initialized at schema 13 and then opened by v4.4.0. Verification confirmed:

- schema migration recorded version **14**;
- `approval_signature_evidence` was created;
- protected `Approval Signatures` retention policy was inserted;
- the existing **6 seeded assets** remained present;
- the existing approval-request count was preserved;
- `PRAGMA integrity_check = ok` after the upgrade.

Previously decided v4.3 approvals are intentionally **not** backfilled with fabricated electronic signatures. Only decisions made under the v4.4 signature contract produce evidence.

## Compliance boundary

v4.4.0 provides application-level electronic approval evidence and tamper detection. It does **not** claim PKI digital signatures, eIDAS qualified signatures, FDA 21 CFR Part 11 certification, trusted timestamping, legal non-repudiation, hardware-backed signing keys or WORM storage. Regulated deployments require deployment-specific identity, infrastructure, validation and governance controls beyond this reference build.
