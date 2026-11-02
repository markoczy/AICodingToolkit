---
id: T901
title: "Fixture: open and answered questions in the old format"
type: bug
priority: high
created: 2026-10-20
updated: 2026-10-20
waiting_on: user
blocked_by: []
plan: null
branch: null
pr: null
---

## Problem
Synthetic fixture, written in the question format used before answerers were named. Not a copy of a real task.

## Approach

## Acceptance

## Questions
- **Q1** (open, for user): Which option? (a) one, (b) two. Recommendation: (a).
- **Q2** (open, for supervisor): Is the second open question parsed too?
- **Q3** (answered 2026-10-20, user): Answered question with a long text that, in the real files, would be wrapped on screen but is a single line. Options: (a) x, (b) y. → a

## Decisions
- 2026-10-20, user: Q3 → a

## Log
- 2026-10-20 supervisor: created.
- 2026-10-20 supervisor: asked Q1 (for user).
- 2026-10-20 supervisor: asked Q2 (for supervisor).
