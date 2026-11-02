---
id: T019
title: Move the scripts into a Tools subfolder
type: refactor
priority: high
created: 2026-10-20
updated: 2026-10-20
waiting_on: supervisor
blocked_by: [T021]
plan: AI/Plans/2610-04-Move-Scripts-Into-A-Tools-Subfolder.md
branch: null
pr: null
---

## Problem
User (2026-10-20): T002 put the scripts `jobs/` and the helper package `lib/` directly in the repo root; the user
prefers them under a `Tools/` subfolder (`Tools/jobs/`, `Tools/lib/`).

## Approach
Plan AI/Plans/2610-04-Move-Scripts-Into-A-Tools-Subfolder.md: move the folders, fix the imports and the run
scripts, keep the history of the files.

## Acceptance
- Every script runs from its new place.
- The tests pass.

## Questions
- **Q1** (answered 2026-10-20, user): Keep `Deploy/` at the repo root or move it too? → keep it
- **Q2** (answered 2026-10-20, user): Name of the folder: (a) Tools, (b) Scripts. → a

## Decisions
- 2026-10-20, Alex: order: T003 lands first, then T019 runs on a base that includes it.
- 2026-10-20, Alex: Q2 → Tools/ at the repo root (capitalised, like Deploy/ and Docs/).
- 2026-10-20, Alex: order (board note): let the running tasks (T004, T020, T021) finish, then pick this up.

## Log
- 2026-10-20 supervisor: created in 00-Conception.
- 2026-10-20 supervisor: 00-Conception → 01-Refinement.
- 2026-10-20 supervisor: asked Q1 (for user).
- 2026-10-20 supervisor: recorded user's answer to Q1 (chat, Alex).
- 2026-10-20 supervisor: recorded a decision by Alex.
- 2026-10-20 supervisor: asked Q2 (for user).
- 2026-10-20 user (board): answered Q2.
- 2026-10-20 supervisor: T004 (demo-critical) starts now; to avoid conflicts T019 runs after it.
- 2026-10-20 supervisor: recorded a decision by Alex.
- 2026-10-20 supervisor: set plan = AI/Plans/2610-04-Move-Scripts-Into-A-Tools-Subfolder.md, waiting_on = supervisor.
