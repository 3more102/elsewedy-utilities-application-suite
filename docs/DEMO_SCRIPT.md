# EUAS Management Demonstration Script

## Scenario: New Cairo Substation / TR-001

1. Sign in as `omar / EUAS@2026`.
2. Open **Executive Dashboard** and point out asset health, work backlog, PM compliance, inventory risk and maintenance cost.
3. Open **GIS / Locations** and select **New Cairo Substation**.
4. Open **Assets** → `TR-001 — 33/11 kV Power Transformer`.
5. Show its Warning condition, criticality, location, vendor, hierarchy, maintenance information, documents and work history.
6. Open `WO-10025 — Investigate Transformer Oil Temperature`.
7. Show High priority, assigned technician, supervisor, safety requirements, instructions and checklist.
8. Switch to `tech1 / Tech@2026` and open **Field Service**.
9. Start the assigned work, enter a reading/condition, add labor, consume a spare part, complete checklist items, add a note/photo/document and finish with a technician signature.
10. Submit/complete the transformer inspection. A failed inspection item can generate corrective work automatically.
11. Return as supervisor/manager and close the work order.
12. Re-open the asset to show updated history.
13. Open **Inventory** to show the material transaction and low-stock indicator.
14. Run the reorder scan to show the PR link into **Procurement**.
15. Finish on **Analytics** and **Audit Trail** to show the cost/reliability effect and traceable user actions.

The seeded `WO-10025` starts in Assigned status so the field-execution portion can be demonstrated immediately.

## v4.3 offline field-sync demonstration

For a focused field-service demo, use `tech1 / Tech@2026` and an assigned work order:

1. Open **Field Service** while connected and let the technician snapshot load.
2. Open **Field Synchronization** and show the client ID, zero/low pending count and current conflict count.
3. Put the browser/device offline.
4. Start the assigned work, update a checklist item, add a field note, and update an allowed asset reading/condition.
5. Show that the workspace remains usable from the cached authenticated snapshot and that changes are queued locally.
6. Restore connectivity and use **Synchronize Now** (or allow the online event to flush the queue).
7. Show the pending count returning to zero and the server snapshot refreshing.
8. To demonstrate conflict safety, create another offline mutable update, change the same server entity from a manager session, then reconnect.
9. Open **Field Synchronization** and show the explicit conflict with current server state.
10. Choose either **Keep Server** or **Retry Mine** to demonstrate auditable human resolution instead of silent last-writer-wins behavior.

Do not present closed-app background sync, native encrypted storage, offline inventory consumption, or offline photo/file upload as v4.3 capabilities.
