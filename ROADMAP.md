# EUAS Roadmap

This roadmap separates **implemented reference capabilities** from the next engineering steps required for a production utility deployment.

## v4.0 — Production Platform Hardening

- [ ] Live PostgreSQL integration test in CI
- [ ] Database migrations with a production migration tool
- [ ] OIDC / SSO integration
- [ ] External object storage for documents and photos
- [ ] Centralized structured logging and tracing
- [ ] Container image build + vulnerability scanning in CI
- [ ] Secrets management integration
- [ ] Backup/restore validation against production PostgreSQL

## Utility Integration

- [ ] OPC-UA connector
- [ ] Modbus gateway adapter
- [ ] IEC 61850 event integration
- [ ] Vendor-SCADA connector framework
- [ ] High-volume telemetry buffering / message broker
- [ ] Historian integration
- [x] Alarm suppression windows and maintenance/test-mode rules
- [x] Deterministic asset/time-window alarm incident correlation
- [x] Operator shelving with approval/expiry policies
- [x] Topology-aware multi-asset root-cause correlation

## Mobile & Field Operations

- [x] Offline-first field-service synchronization
- [ ] Closed-app/background OS sync (explicit v4.3 conflict resolution is implemented)
- [ ] QR and barcode scanning
- [ ] Native camera capture optimization
- [ ] Geofenced dispatch arrival
- [ ] Mobile push notifications

## Maintenance & Reliability

- [ ] Craft calendars and overtime rules
- [ ] Portfolio-level material allocation optimization
- [x] Failure-mode hierarchy / FMEA linkage
- [x] Reliability-centered maintenance strategy models
- [x] Condition-based maintenance rule editor
- [ ] Verified predictive-maintenance models with model governance

## Enterprise Controls

- [ ] Multi-organization / multi-tenant isolation
- [ ] Configurable workflow engine
- [x] Electronic signatures / approval evidence
- [ ] WORM-compatible audit evidence store
- [x] Data-retention execution jobs
- [x] Fine-grained permission administration UI

## Delivery Principle

A roadmap item is marked complete only after implementation, automated verification and documentation. Features that depend on external utility systems remain explicitly labeled as integrations rather than simulated as completed production connectors.
