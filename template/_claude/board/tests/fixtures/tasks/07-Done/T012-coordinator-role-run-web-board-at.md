---
id: T012
title: "Coordinator role: run the web board at session start, never the watcher"
type: docs
priority: medium
created: 2026-10-20
updated: 2026-10-20
waiting_on: null
blocked_by: []
plan: null
branch: t012-coordinator-web-board
pr: 63
---

## Problem
Coordinators should run the local web board (`tb serve`) but not the watcher (`tb watch`), and the role definition
should say so. Today the coordinator role only tolerates the board. Seen in Robin's first coordinator session
(2026-10-20): the board wasn't running, and `tb serve` then failed with "No module named 'flask'" because the board's
requirements weren't installed. Requested by Robin.

## Approach
Docs-only change, no plan needed (small, unambiguous).

- The coordinator skill's session start: check that Flask is importable; if not, give the dev the install
  command; then start `tb serve` in the background and tell the dev the URL.
- `AI/Methodology.md`: the coordinator row in the Roles table gains "runs its own web board (`tb serve`)".

## Acceptance
- The coordinator skill's session start starts `tb serve` in the background and handles a missing Flask.
- The coordinator skill still forbids `tb watch`.
- No change to the board tooling itself; the tooling tests still pass.

## Questions
- **Q1** (answered 2026-10-20, user): @Alex: T012 (Robin's task) is in 02-Ready: a docs-only change so coordinator sessions start the web board at session start but never the watcher. Can the supervisor pick it up now? Options: (a) start it now, (b) later, after T003/T004, (c) change the approach or drop it. Recommendation: (a). → a

## Decisions
- 2026-10-20, Robin: owner: Robin
- 2026-10-20, Robin: approach approved, docs-only, no plan needed; move to 02-Ready
- 2026-10-20, user: Q1 → a
- 2026-10-20, Alex: scope addition: anyone (Alex, Robin, any dev) may answer a task's open questions, even when the question was asked by or for another dev; the answer is recorded under the answerer's name.

## Log
- 2026-10-20 coordinator (Robin): created in 00-Conception.
- 2026-10-20 coordinator (Robin): recorded a decision by Robin.
- 2026-10-20 coordinator (Robin): 00-Conception → 01-Refinement.
- 2026-10-20 coordinator (Robin): rewrote Approach.
- 2026-10-20 coordinator (Robin): rewrote Acceptance.
- 2026-10-20 coordinator (Robin): recorded a decision by Robin.
- 2026-10-20 coordinator (Robin): 01-Refinement → 02-Ready.
- 2026-10-20 coordinator (Robin): asked Q1 (for user).
- 2026-10-20 user (board): answered Q1.
- 2026-10-20 user (board): do it now
- 2026-10-20 supervisor: 02-Ready → 03-Implementation (branch t012-coordinator-web-board).
- 2026-10-20 supervisor: executor-light started on t012-coordinator-web-board (Alex: do it now)
- 2026-10-20 supervisor: recorded a decision by Alex.
- 2026-10-20 supervisor: 03-Implementation → 04-CodeReview (PR #63).
- 2026-10-20 supervisor: review PR #63: approve; acceptance met.
- 2026-10-20 supervisor: 04-CodeReview → 07-Done (merged PR #63 into master, reviewer approved; docs only, nothing to deploy).
- 2026-10-20 supervisor: tokens: executor-light 80k, reviewer 19k (no plan; docs-only).
