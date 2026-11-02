---
id: T004
title: "Metric core: rolling windows, running totals, edge cases"
type: feature
priority: high
created: 2026-10-20
updated: 2026-10-20
waiting_on: null
blocked_by: []
plan: AI/Plans/2610-03-Metric-Core.md
branch: t004-metric-core
pr: 139
---

## Problem
A shared metric module: rolling windows over record series, running and weighted totals, warm-up handling, and
a module interface so later metrics plug in through a registry.

## Approach
Plan AI/Plans/2610-03-Metric-Core.md (approved), part A only: window helpers, running totals, warm-up rules.

## Acceptance
- Every metric of part A has a golden test.
- Performance smoke: 1000 metrics on one series build well under 2 s.

## Questions
- **Q1** (answered 2026-10-20, user): Reference formulas: (a) the existing reports, (b) decide per metric. → a
- **Q2** (answered 2026-10-20, user): How are metric values shown with the records? Options: (a) rows normal, (b) a second table. → a
- **Q3** (answered 2026-10-20, user): Proposal per metric: total = sum of the window; weighted = recursive with the previous value. OK? → yes
- **Q4** (answered 2026-10-20, user): Approve plan 2610-03 with the recommended answers? (1) split: T004 = core, (2) all in one. → 1
- **Q5** (answered 2026-10-20, user): Parameter contract between T003 and T004 (the two plans disagree): (a) T003 types the column, (b) T004 does. → a

## Decisions
- 2026-10-20, user: Q2 → metric rows get their own style settings.
- 2026-10-20, user: Band display (user, chat): ranges are shown as filled areas.
- 2026-10-20, user: Q4 → 1
- 2026-10-20, user: Q5 → a

## Log
- 2026-10-20 supervisor: created.
- 2026-10-20 supervisor: Q1 user (chat): check the existing reports; otherwise decide per metric.
- 2026-10-20 supervisor: set plan = AI/Plans/2610-03-Metric-Core.md, waiting_on = supervisor.
- 2026-10-20 supervisor: 01-Refinement → 02-Ready.
- 2026-10-20 supervisor: 02-Ready → 03-Implementation (branch t004-metric-core).
- 2026-10-20 supervisor: executor started on t004-metric-core (plan 2610-03 part A)
- 2026-10-20 supervisor: executor: part A done; 84 new tests; lint and build clean, 1000 metrics in 61 ms.
- 2026-10-20 supervisor: 03-Implementation → 04-CodeReview (PR #139).
- 2026-10-20 supervisor: 04-CodeReview → 07-Done (merged PR #139 into master, reviewer approved).
- 2026-10-20 supervisor: tokens: executor 160k, reviewer 40k.
