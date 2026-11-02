# Tasks

Every task is one Markdown file. The folder a file sits in is its state. Nothing else (issue tracker, project
board) holds task state, so any front end can be built on these files alone. The flow, the roles and who may
change what are in [../Methodology.md](../Methodology.md); this file defines the format.

Change task files with the board CLI, not by hand: `{{python}} .claude/board/tb.py <command>` (see
`.claude/board/README.md`). It keeps the rules below, moves files with `{{vcs_mv}}`, and refuses an edit that would
break them. Task files change {{#if git}}in a checkout of `{{main}}`{{else}}in the trunk working copy{{/if}} only.

## States

| Folder | Meaning | Leaves when |
|---|---|---|
| `00-Conception` | A problem or idea is written down. Nothing is decided yet. | the problem is clear enough to work out a solution |
| `01-Refinement` | Solution, scope and acceptance are worked out; questions are asked and answered; a plan is written if the change is non-trivial. | the user approved the approach and no question is open |
| `02-Ready` | Approved and unblocked; anyone can start it. | someone starts it |
{{#if git}}
| `03-Implementation` | Being worked on (branch named in the header). | a pull request is open |
| `04-CodeReview` | Pull request `pr` is reviewed for intent, correctness, scope and {{#if analyst}}data {{/if}}impact. | the reviewer approves, or it goes back to 03 |
{{else}}
| `03-Implementation` | Being worked on (SVN branch `^/branches/<branch>` named in the header). | the executor hands back a branch revision for review |
| `04-CodeReview` | The branch at revision `rev` is reviewed for intent, correctness, scope and {{#if analyst}}data {{/if}}impact. | the reviewer approves, or it goes back to 03 |
{{/if}}
| `05-UserReview` | Waiting for the user: a decision, an answer, or a check of the result. Optional. | the user answered |
{{#if deploy_prod}}
| `06-Deploy` | Merged; waiting for deployment and for the check that production {{#if analyst}}data looks{{else}}behaves{{/if}} as expected. | the check passed |
{{else}}
{{#if deploy_test}}
| `06-Deploy` | Merged; waiting for deployment to the test server and the check there. | the check passed |
{{else}}
| `06-Deploy` | Merged; not used in this project (nothing is deployed): tasks go from 04 or 05 straight to 07. | - |
{{/if}}
{{/if}}
| `07-Done` | Finished and verified. | - |
| `08-Dropped` | Rejected, duplicate or obsolete; the reason is in the log. | - |

A task can go back (e.g. 04 to 03 on requested changes{{#if has_deploy}}, 06 to 03 if {{deploy_where}} shows a problem{{/if}}). A `research`
task has its own path: it moves into 03 without a branch and from 03 straight to 07 (with its agent token
counts), and never uses 04 or 06 (`AI/Methodology.md`, "How tasks move"). A task that is
blocked stays in its folder and names the blocker in `blocked_by`. {{#if git}}Every state folder holds a `.gitkeep`, because git does not track empty folders.{{else}}Empty state folders are versioned in SVN as they are (the `.gitkeep` files are harmless).{{/if}}

## File

Name: `<ID>-<short-slug>.md`; the id is the source's prefix plus a number padded to three digits
(`T052-weekly-export-labels.md`). Numbers are never reused: the next number is one higher than the highest in any
state folder (`tb next-id`).

```markdown
---
id: T052
title: Check how weekly exports are labeled
type: research       # one of the types in .claude/board/board.json ({{types_list}})
priority: high       # high | medium | low
owner: null          # null (= default_owner in .claude/board/board.json) | a person from people
created: 2026-10-07
updated: 2026-10-07
waiting_on: null     # null | a role ({{roles_list}}) | <task id>
blocked_by: []       # task ids
plan: null           # path of a plan in AI/Plans/ for larger changes
{{#if git}}
branch: null         # task branch name, e.g. t052-weekly-export-labels
pr: null             # pull request number
{{else}}
branch: null         # task branch name, e.g. t052-weekly-export-labels (^/branches/<branch>)
rev: null            # SVN revision of the branch handed to review (what a pull request is in git)
{{/if}}
---

## Problem
What is wrong or missing, with evidence.

## Approach
How it will be solved and what is out of scope (or a link to the plan).

## Acceptance
How we know it is done, including what must be checked and what result is expected.

## Questions
- **Q1** (open, for user): ...
- **Q2** (open, for supervisor): ...
- **Q3** (answered 2026-10-07, {{dev}}, for user): @{{dev}}: ... → answer
- **Q4** (answered 2026-10-07, {{user}}, for supervisor): ... → answer

## Decisions
- 2026-10-07, {{dev}}: Q3 → answer

## Log
- 2026-10-07 supervisor: created in 00-Conception.
- 2026-10-08 supervisor: 01-Refinement → 02-Ready.
```

Rules (checked by `tb check`):

- `updated` and a `Log` line change with every state move or material edit. The log is append-only.
- Questions get a number and stay in the file once answered, so they are never asked twice. Each question is
  asked for a role: `(open, for user)`, `(open, for supervisor)` (or another role).{{#if multiuser}} A question for one dev is
  asked for the user and starts with `@<dev>:` (`tb ask --to <dev>`).{{/if}}
- Anyone may answer any open question. Once answered the line names who answered and the role it was for:
  `(answered <date>, <who>, for <role>): ... → answer`, where `<who>` is a person from `people` in
  `.claude/board/board.json` ({{user}}{{#if multiuser}}, {{dev}}, ...{{/if}}) or a role.
- `waiting_on: user` if and only if at least one open question is for the user.
- A task in `07-Done` or `08-Dropped` has no open question and `waiting_on: null` (`tb check` flags it otherwise);
  `tb move <id> dropped` answers every open question "obsolete: task dropped (<reason>)" (no Decisions line).
{{#if strict}}
- `owner` is `null` (the board's `default_owner`, {{user}}) or a person from `people`. It changes only through
  `tb set <id> owner=<new> --decided-by <current owner>`, which also writes the Decisions line
  `- <date>, <giver>: owner: <new> (transferred from <old>)`; the latest such line must name the owner.
  `tb decide "owner: ..."` is refused.
{{else}}
- `owner` is `null` (the board's `default_owner`, {{user}}) or a person from `people`. It changes only through
  `tb set <id> owner=<new> --decided-by <who>`{{#if multiuser}} (anyone may){{/if}}, which also writes the Decisions line
  `- <date>, <who>: owner: <new> (set by <who>, from <old>)`; the latest such line must name the owner.
  `tb decide "owner: ..."` is refused.
{{/if}}
- Decisions say who decided (a person or a role) and when.
- The header is a small YAML subset: one `key: value` per line, `null`, `[]`/`[T012, T013]` lists and `#`
  comments. The title is in double quotes when it contains `: `, ` #` or a quote, or starts with a backtick (the
  CLI quotes it for you; the check flags an unquoted title with `: `).
- The six sections are always present, in this order; sections that don't apply yet stay empty.
- Log lines written through the web board carry the actor `<name> (board)` (`{{dev}} (board): answered Q3.`).
  The board watcher reacts to exactly these lines, and to other people's input that its periodic
  {{#if git}}`git pull --ff-only`{{else}}`svn update` of `AI/`{{/if}} brings (new tasks, open questions for
  the supervisor, tasks now waiting on it); the CLI refuses a `--by` containing `(board)`.

## Commands

From anywhere in the repo (`tb` = `{{python}} .claude/board/tb.py`):

```bash
tb board                                   # list per state + check
tb board --mine                            # only the tasks `tb me` owns (--owner <name> for someone else)
tb check                                   # check only (exit 1 on problems)
tb show T012                               # one task: header, owner, open questions, last log lines, allowed moves
tb new --title "..." --type bug --priority high --problem "..."
{{#if multiuser}}
tb new --title "..." --type bug --priority low --owner {{dev}}   # a task owned by a dev
{{/if}}
tb me {{user}}                             # once per checkout: who works here (untracked local.json)
{{#if multiuser}}
tb set T012 owner={{dev}} --decided-by {{user}}   # transfer: header + Decisions + Log{{#if strict}}, by the current owner{{else}}, by anyone{{/if}}
{{/if}}
tb ask T012 --for user "Which format should the export use? (a) CSV (recommended) (b) JSON"
{{#if multiuser}}
tb ask T012 --to {{dev}} "Which format?"       # = --for user, text "@{{dev}}: Which format?"
{{/if}}
{{#if strict}}
tb answer T012 Q1 "a: CSV" --answered-by {{user}} --via chat  # --answered-by is mandatory (a person or a role); + a Decisions line
{{else}}
tb answer T012 Q1 "a: CSV" --via chat         # records the answer of `tb me` + a Decisions line
tb answer T012 Q2 "yes" --answered-by {{dev}}  # someone else answered (a person or a role)
{{/if}}
tb decide T012 "Approach approved as in the plan" --decided-by {{user}}
tb section T012 approach --file approach.md
tb set T012 priority=high blocked_by=T010 plan=AI/Plans/2610-01-Export.md
tb move T012 ready
tb move T012 3 --branch t012-export
{{#if git}}
tb move T012 codereview --pr 56
tb move T012 deploy --reason "merged PR #56 into {{main}}, reviewer approved"
{{else}}
tb move T012 codereview --rev 56
tb move T012 deploy --reason "merged into trunk r57, reviewer approved"
{{/if}}
tb move T012 done --tokens "executor 140k, reviewer 50k (estimate ~150k)"
tb log T012 "{{deploy_check}} passed: export matches the sample"
```

Tasks that must not be published can live in a separate, {{untracked}} folder with the same layout, registered in
this checkout's `.claude/board/local.json` (`sources`); those use another prefix (e.g. `P`) and their own numbering.
