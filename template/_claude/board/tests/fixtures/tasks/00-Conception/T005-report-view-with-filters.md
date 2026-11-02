---
id: T005
title: "Report view: filters, grouping, export"
type: feature
priority: high
created: 2026-10-20
updated: 2026-10-20
waiting_on: null
blocked_by: [T019]
plan: null
branch: null
pr: null
---

## Problem
A report view over the imported records: filter by date and category, group by category, export the result.
Depends on the folder layout of T019.

## Approach
Open: one page with the filters on the left and the table on the right, or a wizard.

## Acceptance
- The example data set renders in under a second.
- Export produces the same rows as the table shows.

## Questions
- **Q1** (answered 2026-10-20, user): Grouping (a) by category only, (b) by any column. → a
- **Q2** (open, for supervisor): Which export format first?

## Decisions
- 2026-10-20, user: Q1 → a

## Log
- 2026-10-20 supervisor: created.
- 2026-10-20 supervisor: asked Q1 (for user).
- 2026-10-20 supervisor: recorded user's answer to Q1 (chat).
- 2026-10-20 supervisor: asked Q2 (for supervisor).
- 2026-10-20 supervisor: from the T004 review: validation must report every invalid row, not only the first.
