# Changelog

All notable public milestones for EUAS are summarized here.

## [4.4.0] — Approval Signature Evidence

- Added current-password re-authentication for generic Approval Center decisions.
- Added exact signer-intent statements bound to the target record code.
- Added decision-time record snapshots and credential-verification evidence.
- Added hash-chained approval signature evidence with integrity verification.
- Added direct/delegated authority capture in each signed decision.
- Added Approval Center signature badges/history detail, governance verification, metrics and protected CSV export.
- Added protected approval-signature retention policy metadata.
- Fixed stale README schema metadata from v4.3 and added release-metadata regression coverage.
- Advanced schema to v14 and regression suite to 22 tests.

## [4.3.0] — Offline Field Synchronization

- Added authenticated field bootstrap snapshots with deterministic entity hashes.
- Added durable client registration and idempotent field operation ledger.
- Added conflict-safe offline work transitions, task state, asset reading/condition and dispatch updates.
- Added append-only offline field notes.
- Added safe same-batch rebase for ordered operations from one offline snapshot.
- Added explicit discard/retry conflict resolution with server-hash verification.
- Added browser field snapshot/queue/conflict UI and session-expiry-bounded offline reopen.
- Added field-sync metrics and protected CSV export.
- Advanced schema to v13 and regression suite to 20 tests.

## [4.2.0] — Topology-Aware Root-Cause Correlation

- Added directed asset-topology links with duplicate/cycle protection.
- Extended alarm incidents from same-asset grouping to incremental multi-asset topology correlation.
- Added deterministic root-cause candidate, score, explanation and topology-hop evidence.
- Corrective work generated from an incident now targets the root-cause candidate.
- Added Command Center topology management/view, metrics and CSV export.
- Fixed the v4.1 Command Center browser binding bug for actionable and shelved alarm queues.
- Advanced schema to v12 and regression suite to 18 tests.

## [4.1.0] — Governed Alarm Shelving

- Added operator alarm-shelf requests for active alarms.
- Added four-eyes Maintenance Manager approval through the existing Approval Center.
- Added explicit duration policy: 5–1440 minutes, with Critical alarms capped at 120 minutes.
- Added approved shelf expiry and manual revoke/restore behavior without mutating the alarm lifecycle.
- Added actionable-vs-shelved Command Center queues, audit/outbox evidence, metrics and CSV export.
- Advanced schema to v11 and regression suite to 16 tests.

## [4.0.0] — Utility Command Center & Integration Reliability

- Added Utility Command Center across incidents, alarms, outages, dispatch and telemetry quality.
- Added idempotent telemetry ingestion batches and per-reading external IDs.
- Added quality-aware threshold evaluation.
- Added alarm suppression windows.
- Added deterministic asset/time-window alarm incident correlation.
- Added incident acknowledgement, auto-resolution and corrective-work generation.
- Added machine-to-machine `telemetry:write` API keys.
- Added telemetry quality/series APIs, v4 observability metrics and CSV exports.
- Advanced schema to v10 and expanded regression suite to 14 tests.

## [3.9.0] — Utility Operations Intelligence

- Added asset-linked telemetry channels and timestamped readings.
- Added warning/critical high and low threshold evaluation.
- Added operational alarm lifecycle and corrective work-order generation.
- Integrated alarms with dashboard, operations, search, metrics and CSV exports.
- Advanced schema to v9.
- Expanded regression suite to 10 tests.

## [3.8.0] — Execution Coordination & Outage Reliability

- Added work-order material reservations.
- Added dispatch lifecycle and one-active-dispatch guard.
- Added explicit planned/forced asset outages.
- Moved reliability calculations to outage timestamps when available.

## [3.7.0] — Workforce Planning, Parts Readiness & Reliability

- Added crafts, technician profiles, shifts, absences and efficiency.
- Added planned material/craft requirements.
- Added site/asset MTBF, MTTR and availability analytics.

## [3.6.0] — Planning, Asset Health & Approval Continuity

- Added rule-based asset health scoring and history.
- Added 90-day maintenance forecast.
- Added approval delegation.

## Earlier 3.x Releases

Earlier 3.x releases established governance, SLA/outbox, automation, procurement, PM, inventory, field service, inspections, HSE, projects, documents, GIS, RBAC, audit history and the full EUAS application launchpad.
