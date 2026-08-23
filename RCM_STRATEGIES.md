# EUAS Reliability-Centered Maintenance (RCM) Strategies

EUAS v4.9.0 adds a governed **Reliability-Centered Maintenance strategy model** that converts an approved asset FMEA into an explicit maintenance policy while reusing the existing CBM, preventive-maintenance, Approval Center and electronic-evidence services.

## Strategy model

Each `rcm_strategies` record is unique to one asset FMEA and records the functional failure, consequence classification, maintenance strategy, task description, technical justification, owner and review due date. Optional linkage connects the strategy to an existing CBM rule or preventive-maintenance plan.

Supported consequence classes are:

- Safety
- Environmental
- Operational
- Non-Operational
- Hidden

Supported strategy types are:

- **Condition-Based** — requires an active CBM rule linked to the same FMEA before submission or activation.
- **Time-Based** — requires an interval and an active PM plan for the same asset before submission or activation.
- **Run-to-Failure** — permitted only where consequence governance allows it; Safety and Environmental consequences are rejected.
- **Failure-Finding** — requires a defined interval for the discovery task.
- **Redesign** — records an engineering-change strategy rather than inventing an automatic execution workflow.

## Governed lifecycle

```text
Draft → Review → Approved → Active
  ↑        │                    │
  └─ Reject / Revise ───────────┘
                              └─ Retire → Retired
```

1. A permitted reliability user authors a Draft strategy.
2. Submission validates the strategy and creates a Maintenance Manager approval request.
3. The requester cannot approve their own strategy.
4. The approver must possess `reliability.rcm.approve` and complete current-password re-authentication plus exact signer intent.
5. The existing approval-signature service stores tamper-evident decision evidence.
6. An Approved strategy is explicitly activated after readiness is revalidated.
7. Formal review outcomes are **Continue**, **Revise** or **Retire**. Revise returns the strategy to Draft and requires a new independent approval cycle.

## Review cadence

The reference implementation derives the default next review from the linked FMEA risk band:

| FMEA risk band | Default review interval |
|---|---:|
| Critical | 90 days |
| High | 180 days |
| Medium | 365 days |
| Low | 730 days |

The due date is governance metadata, not an autonomous maintenance execution trigger. Organizations should configure review rules to their approved engineering methodology.

## Authorization

- `reliability.rcm.manage` — author, edit Drafts, submit, activate and perform formal strategy reviews.
- `reliability.rcm.approve` — additional critical permission required when deciding an RCM Approval Center request.

Permission checks do not bypass the four-eyes rule, signer re-authentication, FMEA linkage rules or lifecycle constraints.

## Reporting and observability

EUAS provides:

- RCM strategy register and strategy-type mix;
- FMEA strategy coverage percentage;
- overdue RCM reviews;
- Critical FMEA records without an RCM strategy;
- Critical-asset CBM coverage indicator;
- `EUAS_rcm_strategies.csv` export;
- metrics including active strategies, overdue reviews, strategy coverage and critical FMEA gaps.

## Engineering boundary

EUAS v4.9 implements transparent, deterministic RCM **workflow and evidence governance** for this reference application. It does **not** claim SAE JA1011/JA1012 certification, IEC/ISO conformance, OEM approval, regulated safety-case validation, or that the built-in consequence/strategy rules are sufficient for a real utility's engineering policy. Production deployment requires the operator's approved RCM methodology, engineering authority and regulatory controls.
