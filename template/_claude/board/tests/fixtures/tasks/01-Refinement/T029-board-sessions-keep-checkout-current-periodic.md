---
id: T029
title: Board and sessions keep the checkout current (periodic update)
type: feature
priority: high
created: 2026-10-20
updated: 2026-10-20
waiting_on: supervisor
blocked_by: [T021]
plan: AI/Plans/2610-06-Periodic-Update.md
branch: null
pr: null
---

## Problem
Alex (chat, 2026-10-20): the web board and the watcher only see the local checkout, so other devs' commits (new
tasks, answers, moves) stay invisible until someone updates by hand. Both board runners (the supervisor's
`tb watch`, the coordinators' `tb serve`) should update periodically, and the supervisor should update before
acting. Overlaps T021 (the board server, in implementation now), so this lands after T021.

## Approach
Plan AI/Plans/2610-06-Periodic-Update.md (approved).

## Acceptance
- `tb serve` and `tb watch` update every N seconds; a conflict stops them and is reported.

## Questions
- **Q1** (answered 2026-10-20, user): @Alex: How should the checkout stay current? (a) tb watch and tb serve update every N s, (b) by hand. → a
- **Q2** (answered 2026-10-20, user): @Alex: Approve plan 2610-06 with the recommended answers? → yes

## Decisions
- 2026-10-20, Alex: Q1 → (a): tb watch and tb serve update every N s.
- 2026-10-20, Alex: Q2 → plan 2610-06 approved.

## Log
- 2026-10-20 supervisor: created.
- 2026-10-20 supervisor: asked Q1 (for user).
- 2026-10-20 supervisor: recorded user's answer to Q1 (chat, Alex).
- 2026-10-20 supervisor: recorded a decision by Alex.
- 2026-10-20 supervisor: asked Q2 (for user).
- 2026-10-20 supervisor: recorded user's answer to Q2 (chat, Alex).
- 2026-10-20 supervisor: recorded a decision by Alex.
- 2026-10-20 supervisor: set plan = AI/Plans/2610-06-Periodic-Update.md, waiting_on = supervisor.
- 2026-10-20 supervisor: waits for T019 (Alex: T019 runs before any new task starts).
