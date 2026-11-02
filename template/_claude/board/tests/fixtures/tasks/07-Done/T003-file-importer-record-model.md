---
id: T003
title: File importer and record model
type: feature
priority: high
created: 2026-10-20
updated: 2026-10-20
waiting_on: null
blocked_by: []
plan: AI/Plans/2610-02-File-Importer-And-Record-Model.md
branch: t003-file-importer
pr: 81
---

## Problem
Parse the example files in a worker into a source-independent record model (series of records with timestamps,
adjustments, file history). Must parse all 7 example files; unknown columns are kept as data, not dropped.

## Approach
Plan AI/Plans/2610-02-File-Importer-And-Record-Model.md (approved). Two branches: branch 1 (this task's first
branch) parses the files, branch 2 adds the history.

## Acceptance
Branch 1: all 7 example files parse with no error diagnostics; golden counts per kind; spot checks from the plan.

## Questions
- **Q1** (answered 2026-10-20, user): Approve plan 2610-02 with the recommended answers to its 7 open questions? (1) yes, (2) change something. → 1

## Decisions
- 2026-10-20, user: Q1 → 1 (plan approved with the recommended answers).

## Log
- 2026-10-20 supervisor: created.
- 2026-10-20 supervisor: Requirement from T005 (user, 2026-10-20): the record model must expose the series hierarchy.
- 2026-10-20 supervisor: set plan = AI/Plans/2610-02-File-Importer-And-Record-Model.md, waiting_on = supervisor.
- 2026-10-20 supervisor: asked Q1 (for user).
- 2026-10-20 user (board): answered Q1.
- 2026-10-20 supervisor: 01-Refinement → 02-Ready.
- 2026-10-20 supervisor: 02-Ready → 03-Implementation (branch t003-file-importer).
- 2026-10-20 supervisor: executor started on t003-file-importer (branch 1 of plan 2610-02)
- 2026-10-20 supervisor: Alex T004 Q5 = a: T003 branch 1 also types the Amount column; request sent to the running executor.
- 2026-10-20 supervisor: executor: branch 1 done on t003-file-importer (streaming reader, structural model, typed columns).
- 2026-10-20 supervisor: 03-Implementation → 04-CodeReview (PR #81).
- 2026-10-20 supervisor: 04-CodeReview → 07-Done (merged PR #81 into master, reviewer approved).
- 2026-10-20 supervisor: tokens: executor 140k, reviewer 30k.
