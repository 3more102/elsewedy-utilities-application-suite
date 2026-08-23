# EUAS Reliability & FMEA

EUAS v4.8.0 introduced, and v4.9.0 continues to use, a governed deterministic **Failure Modes and Effects Analysis (FMEA)** layer that links reusable failure-mode taxonomy, asset-specific risk analysis, condition-based maintenance (CBM), and work management.

## Scope

The v4.8 implementation provides:

- hierarchical failure modes with parent/child relationships and cycle prevention;
- one governed FMEA record per asset/failure-mode pair;
- 1–10 Severity, Occurrence and Detectability ratings;
- deterministic Risk Priority Number (RPN): `Severity × Occurrence × Detectability`;
- reference risk bands: Low `<80`, Medium `80–159`, High `160–299`, Critical `>=300`;
- explicit effects, causes, current controls, recommended action, owner and review date;
- immutable review history recording old/new ratings and RPN;
- governed conversion from an FMEA record to a submitted work order and approval request;
- optional FMEA linkage from CBM rules through CBM events into generated work orders;
- CSV exports and Prometheus-style reliability metrics.

## Failure-mode hierarchy

`failure_modes` stores reusable failure modes. Each mode can optionally point to a parent mode. The API rejects self-parenting and transitive cycles so the hierarchy remains a directed acyclic taxonomy.

A mode can be inactive without deleting historical FMEA records. Inactive modes cannot be linked to new FMEA records.

## Asset FMEA

`asset_fmea` binds one failure mode to one asset. The record stores the functional context, effect, cause, controls, recommended action and current S/O/D ratings.

RPN is recalculated server-side on every create, edit and formal review. The client does not supply the stored RPN or risk band.

## Review evidence

Formal reviews use `fmea_reviews`. Each review stores:

- previous S/O/D ratings and RPN;
- new S/O/D ratings and RPN;
- review notes;
- reviewer identity and timestamp.

The current FMEA record is then updated with the reviewed ratings, risk band, status and next review date.

## Work-management linkage

A reliability user with work-write permission can convert an active FMEA record into a work order. The work order:

- carries `asset_fmea_id`;
- uses the failure-mode number as its failure code;
- includes the current RPN/risk band, effect and cause in the description;
- starts in `Submitted` state;
- creates a normal EUAS approval request rather than bypassing work governance.

EUAS prevents a second active FMEA-generated work order for the same FMEA record until the previous work is Closed, Cancelled or Rejected.

## CBM linkage

A CBM rule may optionally reference an FMEA record for the same asset as its telemetry channel. Cross-asset FMEA links are rejected.

When such a CBM rule triggers, the FMEA link is copied to the CBM event and any generated work order. The generated work order uses the FMEA failure-mode code and includes the FMEA number/RPN in its evidence text.

## Permissions

`reliability.fmea.manage` controls failure-mode and FMEA authoring/review. Baseline grants are assigned to Administrator, Asset Manager, Maintenance Manager, Planner and Supervisor roles. Work-order conversion additionally requires `work.write`.

## Important boundary

The RPN thresholds in v4.8 are deterministic reference bands for the demo/reference application. They are **not** claimed to implement a particular OEM, IEC, ISO, AIAG/VDA or organization-specific FMEA standard. A production rollout should configure ratings, action-priority rules, review cadence and governance to the utility's approved reliability methodology.

## v4.9 RCM handoff

An asset FMEA can now own one governed RCM strategy. The RCM layer consumes the FMEA risk/effect context and can link to an existing CBM rule or PM plan without bypassing their normal execution workflows. See [RCM_STRATEGIES.md](RCM_STRATEGIES.md).
