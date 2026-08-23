# Changelog

All notable public milestones for EUAS are summarized here.

## [4.9.0] — Reliability-Centered Maintenance Strategy Engine

- Added one governed RCM strategy per FMEA with explicit functional-failure and consequence evidence.
- Added Condition-Based, Time-Based, Run-to-Failure, Failure-Finding and Redesign strategy types with deterministic readiness/guard rules.
- Added four-eyes RCM approval through the existing Approval Center, including credential re-authentication, signer intent and hash-chained signature evidence.
- Added risk-based review cadence plus formal Continue / Revise / Retire review history; revised strategies require a new approval cycle.
- Added validated same-context CBM and same-asset preventive-maintenance linkage.
- Added `reliability.rcm.manage` / `reliability.rcm.approve`, Reliability UI strategy register, KPIs, metrics and CSV export.
- Fixed date-sensitive seeded workforce leave so capacity tests remain deterministic across calendar dates.
- Advanced schema to v19 and regression coverage to 32 tests.

## [4.8.0] — Reliability & FMEA Linkage

- Added cycle-safe hierarchical failure-mode taxonomy.
- Added asset-specific FMEA register with effects, causes, controls, recommended actions and server-calculated RPN/risk bands.
- Added formal FMEA review history preserving old/new ratings and reviewer evidence.
- Added governed FMEA-to-work conversion with approval routing and duplicate-active-work protection.
- Added optional same-asset FMEA linkage across manual work orders and CBM rules/events/generated work.
- Added `reliability.fmea.manage`, Reliability/FMEA UI, portfolio metrics and CSV exports.
- Advanced schema to v18 and regression coverage to 30 tests.

## [4.7.0] — Condition-Based Maintenance Rule Editor

- Added governed channel-bound CBM rule authoring with scalar/range operators.
- Added Good-quality-only rule evaluation with consecutive-hit confirmation and cooldown.
- Added traceable CBM event lifecycle, automatic clear, acknowledgement and manual resolution.
- Added recommendation action and approval-routed Condition-Based Maintenance work-order generation.
- Added side-effect-free rule test endpoint, fine-grained CBM permission, UI, metrics and CSV exports.
- Extended telemetry ingest-batch evidence with CBM opened/resolved/work-order counts.
- Advanced schema to v17 and regression coverage to 28 tests.

## [4.6.0] — Fine-Grained Permission Administration

- Added categorized/risk-ranked permission catalog metadata and server-side effective-permission evaluation.
- Added dynamic role-permission administration with credential re-authentication, change reason and exact confirmation.
- Added explicit per-user Allow/Deny/Inherit overrides with optional expiry and deterministic precedence over role grants.
- Added effective-permission endpoint, Administration role matrix/user override UI, access-control CSV export and permission metrics.
- Added audit/outbox evidence for role grants, user overrides and user-role changes.
- Added last-active-admin and core administrator permission lockout guards.
- Preserved domain-level approval, delegation, four-eyes and technician-assignment constraints after permission authorization.
- Advanced schema to v16 and regression coverage to 26 tests.

## [4.5.0] — Governed Retention Execution

- Added persisted Preview/Execute retention-run ledger with per-policy outcome evidence.
- Added class-wide and record-scoped legal holds plus credential-verified hold release.
- Added admin current-password re-authentication and exact confirmation for destructive retention.
- Enforced protected classes and explicit blocking for document binaries pending coordinated object-storage lifecycle.
- Added transaction-safe purge for Notifications and Integration Events.
- Added SHA-256 linked retention-run manifests, verifier, evidence ZIP and CSV export.
- Added retention run/hold/integrity metrics and Automation & Reports governance UI.
- Advanced schema to v15 and regression suite to 24 tests.

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
