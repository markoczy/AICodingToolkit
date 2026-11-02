---
name: task-delegate
description: Start an agent on a task - executor or executor-light to plan or implement on a task branch{{#if git}} and open a pull request{{/if}}, reviewer to review a {{review_noun}}{{#if analyst}} and measure its data impact, analyst to answer a data question{{/if}}, researcher to answer a reading-heavy question - with a complete brief, and record its result in the task file when it returns. Use whenever work should leave the supervisor's context.
---

# Delegation

`tb` = `{{python}} .claude/board/tb.py`{{#if svn}}, `sv` = `{{python}} .claude/svn/sv.py`{{/if}}. Agents are defined in `.claude/agents/`.
Start them with the Agent tool (`subagent_type`), in the background, so you stay responsive to the user.

## Pick the agent

| Work | Agent | Isolation |
|---|---|---|
| Plan for a non-trivial task; new design; first implementation of a large or logic-heavy change | `executor` | `worktree` (implementation); none for plan only |
| Review fix list; small contained bug; docs/config; refactor with a parity test | `executor-light` | `worktree` |
| Review one {{review_noun}}{{#if analyst}}, including its measured data impact{{/if}} | `reviewer` | none (it {{#if git}}checks the PR out into its own temporary worktree{{else}}makes its own working copy with `sv wt add`{{/if}}) |
{{#if analyst}}
| Data question (how often, how much, does A agree with B) | `analyst` | none |
{{/if}}
| Reading-heavy question (library, API, data source, paper, unfamiliar code area) | `researcher` | none |

{{#if svn}}
`isolation: "worktree"` gives the agent a fresh trunk checkout in `<checkout>.worktrees/` (WorktreeCreate hook,
`.claude/svn/`); the agent switches it to its branch. {{/if}}Escalate `executor-light` -> `executor` after two review
rounds on the same task, or as soon as it reports a design question. Never start two writers on the same branch
or files at the same time.

## Before starting

- **Implementation:** the task is in 02-Ready **and the start approval is recorded** (a Decisions line
  "start approved"{{#if open}}, by anyone{{else}} by its owner{{/if}}; ground rule 2). If not, ask for it (`tb ask <id> --for user "Start <id>?
  (a) yes (recommended) (b) not yet"`) and stop. Branch name `<id lowercase>-<slug>` (e.g.
  `t012-weekly-export-labels`): {{#if svn}}`sv branch create <branch> -m "<ID>: branch for <title>"`, then {{/if}}`tb move <id>
  implementation --branch <branch>`, {{commit_push}} the task file, update the handover, and start the executor
  with that branch name{{#if git}} (it creates the branch in its worktree){{/if}}.
- **Fix round:** the task is back in 03 with its branch{{#if git}} and PR; the same branch and PR are{{else}}; the same branch is{{/if}} reused.
- **Plan only:** the task stays in 01; `tb set <id> waiting_on=executor`; `tb log <id> "plan commissioned"`.
- **Review:** the task is in 04 (`tb move <id> codereview {{review_flag}}` when the executor reported the
  {{#if git}}PR{{else}}revision{{/if}}).
- **{{#if analyst}}Data question / research{{else}}Research{{/if}}:** `tb log <id> "{{#if analyst}}analysis|{{/if}}research: <question>"` if it belongs to a task. A
  task of `type: research` goes to 03 while the agent runs (`tb move <id> 3`, no branch) and from 03 straight to
  07 with `--tokens` and `--reason "result: ..."` when the answer is logged; it never uses 04 or 06.

Every start goes into `AI/SupervisorHandover.md` ("Agents"), every return out of it.

## Brief templates

The agent starts cold: everything it needs is in the prompt or in files it is told to read. Copy decisions from
the task file; don't paraphrase them away.

**Executor - implement**
{{#if git}}
```
Task <id>: <title>. Task file: <path> (read it; don't edit task files, don't run tb write commands).
Plan: <path or "none - small fix">. Branch: create <branch> from origin/{{main}} in your worktree.
Decisions already made with the user (binding):
- <copied from Decisions>
Done means: <acceptance, short>. Merge origin/{{main}} and run the full suite before handing back; push and open
the PR with gh pr create. Final message short (your agent definition, "Short final message"): PR URL, impact,
tests, proposals, tokens, and the full path of your report file (AI/Temp/reports/ in your worktree) with the
details.
Out of scope: <...>. Context you may use but must never write into tracked files: <... or "none">.
Save tokens: reuse what you computed, no repeated full runs, no idle polling; stop and report on a design question.
```
{{else}}
```
Task <id>: <title>. Task file: <path> (read it; don't edit task files, don't run tb write commands).
Plan: <path or "none - small fix">. Branch: <branch> (exists; run `{{python}} .claude/svn/sv.py switch <branch>`
in your working copy first).
Decisions already made with the user (binding):
- <copied from Decisions>
Done means: <acceptance, short>. Commit with `sv commit`, sync with `sv sync` before handing back. Final message
short (your agent definition, "Short final message"): branch head revision, impact, tests, proposals, tokens, and
the report file in AI/Temp/reports/ with the details.
Out of scope: <...>. Context you may use but must never write into versioned files: <... or "none">.
Save tokens: reuse what you computed, no repeated full rebuilds, no idle polling; stop and report on a design question.
```
{{/if}}

**Executor - plan only** (no isolation needed)
```
Task <id>: <title>. Task file: <path>. Write a plan only (no implementation, no commits): template in
AI/Plans/README.md, file name from `{{python}} .claude/board/tb.py next-plan "<title>"`. Write the plan to the main
checkout's AI/Temp/plans/<file name>, not into your final message; I move it into AI/Plans and commit it. Final
message short: the file, a few lines of summary, the open questions one line each. Decisions so far: <...>.
Questions for the user go into the plan's "Open questions", each with options (a), (b)... and a recommendation.
Then stop and report.
```

**Executor-light - fix round**
{{#if git}}
```
Task <id>, branch <branch>, PR #<n> (check the branch out in your worktree). Fix exactly these review findings
on that branch:
1. <finding: file:line, what is wrong, the decided fix>
...
The reviewer's full report (evidence, repro scripts): <main checkout>/AI/Temp/reports/<id>-review-<sha7>.md.
Merge origin/{{main}}, run the full suite, push to the same branch (no new PR). Short final message: new head
commit, per finding the change and covering test in one line, tokens; before/after evidence in your report file
(AI/Temp/reports/ in your worktree, full path in the message). Stop and report if a fix needs a design choice.
```
{{else}}
```
Task <id>, branch <branch> (switch your working copy to it). Fix exactly these review findings on that branch:
1. <finding: file:line, what is wrong, the decided fix>
...
The reviewer's full report (evidence, repro scripts): AI/Temp/reports/<id>-review-r<rev>.md in the main checkout.
Commit with `sv commit`, sync with `sv sync`; short final message: new branch head revision, per finding the change
and covering test in one line, tokens; evidence in your report file. Stop and report if a fix needs a design choice.
```
{{/if}}

**Reviewer**
{{#if git}}
```
Review PR #<n> (branch <branch>, head <sha>) for task <id>. Task file: <path>. Plan: <path or none>.
Decisions: <...>. The executor claims: <expected impact from its report>. Verify it{{#if analyst}} numerically{{/if}}.
Data/configs you may use read-only: <... or "none">. The executor's report file: <full path it gave, usually in its worktree>.
Short final message (verdict, findings one line each, impact, proposals, tokens); the full report with
deployment notes in AI/Temp/reports/<id>-review-<sha7>.md.
```
{{else}}
```
Review branch <branch> at r<rev> for task <id>. Task file: <path>. Plan: <path or none>. Decisions: <...>.
The executor claims: <expected impact from its report>. Verify it{{#if analyst}} numerically{{/if}}.
Data/configs you may use read-only: <... or "none">. The executor's report file: AI/Temp/reports/<id>-executor-r<rev>.md.
Short final message (verdict, findings one line each, impact, proposals, tokens); the full report with
deployment notes in AI/Temp/reports/<id>-review-r<rev>.md.
```
{{/if}}
{{#if analyst}}

**Analyst**
```
Question: <one data question>. Why we ask: <the decision it informs, task id>. Data: <where, read-only>.
Short final message (answer with key numbers, method, report file); the details and how to reproduce in
AI/Temp/reports/; stop when answered.
```
{{/if}}

**Researcher**
```
Question: <one question>. Why we ask: <the decision it informs, task id>. Sources to prefer: <...>.
Short final message (answer, relevance, report file); the sourced details in AI/Temp/reports/; stop when answered.
```

## On return (do this immediately)

Read the agent's short final message and its token count from the completion notification. Open its report file
(`AI/Temp/reports/`) only for a detail you need now (a finding to pass on, a number to log); never paste it into a
brief: name the file instead, so the next agent reads it. Note the agent's {{worktree}} {{#if git}}path{{else}}name (its path under
`<checkout>.worktrees/`){{/if}} in the log, so it can be removed later.

{{#if git}}
- **Executor, PR opened:** `tb move <id> codereview --pr <n>`; `tb log <id> "executor: <one-line summary>;
  impact: <...>; worktree <path>; tokens <n>k"`; record proposed tasks (`task-new`); start the reviewer.
{{else}}
- **Executor, branch handed back:** `tb move <id> codereview --rev <n>`; `tb log <id> "executor: <one-line
  summary>; impact: <...>; working copy <name>; tokens <n>k"`; record proposed tasks (`task-new`); start the
  reviewer.
{{/if}}
- **Executor, plan written:** move the file from `AI/Temp/plans/` to `AI/Plans/`{{#if svn}} (`mv`, then `svn add`){{/if}},
  `tb set <id> plan=<path> waiting_on=supervisor`; `tb log <id> "plan written; tokens <n>k"`; commit; continue
  in `task-refine` (step 5).
- **Executor stopped with a question:** `tb ask <id> --for user|supervisor "<question, options (a)/(b)/..., its
  recommendation>"`; a question for the user moves the task to 05-UserReview (`tb move <id> userreview`: no new
  input while it is in 03/04); if it needs a redesign, `tb move <id> refinement --reason "<design question>"`.
- **Reviewer:** `tb log <id> "review {{#if git}}PR #<n> @<sha7>{{else}}r<rev>{{/if}}: <verdict>; <top findings>; tokens <n>k"`, then the
  `task-land` skill.
- **{{#if analyst}}Analyst / researcher{{else}}Researcher{{/if}}:** `tb log <id> "{{#if analyst}}analysis|{{/if}}research: <answer in one or two lines>; tokens <n>k"`;
  act on it.
- **Agent failed or hit a usage limit:** resume it with SendMessage (it keeps its context and {{worktree}}) rather
  than starting a new one; log what happened.

Agents' final messages arrive in your context in full, so briefs and messages to agents stay short too: point to
files (task, plan, report) instead of copying them. Then update the handover, `tb check`, {{commit_push}}
(`{{vcs_commit_tasks}}`), and tell the user only what matters to them.
