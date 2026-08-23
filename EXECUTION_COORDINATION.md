# EUAS v3.9.0 — Execution Coordination

## Purpose

EUAS v3.9.0 connects planning to real field execution through three first-class records: material reservations, technician dispatches and asset outages.

## Material reservation lifecycle

`Planned Requirement → Reserved → Partially Issued → Issued`

A reservation is tied to one work order and one inventory item. Releasing an active reservation returns the remaining secured quantity to general availability. Direct work-order material issue consumes that work order's active reservation first. Generic inventory ISSUE and TRANSFER operations may use only unreserved stock.

## Dispatch lifecycle

`Dispatched → Accepted → En Route → On Site → Completed`

A dispatcher selects an active technician and optional ETA. A technician may have only one active dispatch at a time. Arrival at site starts an Assigned work order and records the SLA response milestone. Completing dispatch releases the technician but intentionally leaves maintenance completion to the work-order lifecycle.

## Outage lifecycle

`Open → Closed`

An outage stores asset, site, optional work order, type, cause, impact, lost capacity, unit and explicit start/end timestamps. Opening an outage constrains the asset operational status; closing the last open outage restores Operating status.

## Reliability evidence

For assets with forced outage records in the analysis window, EUAS calculates downtime from actual timestamp overlap and reports `downtime_source=outage_events`. For legacy assets without outage evidence, the calculation falls back to completed corrective work-order actual hours and reports `downtime_source=work_order_hours_fallback`.

## Audit and integration

Reservation, dispatch and outage actions are audited. Outage events are also emitted to the durable integration outbox. CSV exports are available for all three execution datasets.
