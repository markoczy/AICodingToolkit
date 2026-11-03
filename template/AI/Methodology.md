# Working Methodology: supervisor, agents and the task board

This document defines how this project is worked on with AI agents: who does what, how work flows through the
task board, and the rules that keep it reliable. The project's own facts (what it is, its architecture, coding
rules, testing, documentation, how to deploy) live in [Context.md](Context.md).

Binding for every session and every agent. Where a skill in `.claude/skills/` covers a step, follow the skill.

{{#if git}}
This is the **git** edition of the methodology: task branches are git branches, agents work in git worktrees, a
review is a pull request, and a landed task is a merged pull request on `{{main}}`. The tool is `tb` (task
board, `.claude/board/`).
{{else}}
This is the **Subversion** edition of the methodology: task branches live under `^/branches/`, agents work in
SVN working copies next to the main checkout, a review is a branch at a given revision, and a landed task is one
commit on `^/trunk`. The tools are `tb` (task board, `.claude/board/`) and `sv` (SVN workflow, `.claude/svn/`).
{{/if}}

## The idea in one paragraph

One long-lived **supervisor** session works with the user on the big picture and never loses it in detail work.
Everything that needs doing is a **task file** in `AI/Tasks/`; the folder a file sits in is its state, and the
file holds the whole history (questions, answers, decisions, log). Detail work goes to short-lived **agents**
with a narrow brief: an **executor** implements one task on its own branch and hands it back for review, a
**reviewer** checks that {{review_noun}}{{#if analyst}} and measures its data impact, an **analyst** computes{{/if}} and a
**researcher** reads so the supervisor doesn't have to. The supervisor records every result in the task file
{{on_main}}, lands what the reviewer approved{{#if has_deploy}}, and verifies it on {{deploy_where}}{{/if}}. The user steers
through chat or through the local web board, and is asked only for decisions.

## Ground rules

These apply to every session ({{#if multiuser}}supervisor and coordinator{{else}}the supervisor, and any other session in this repo{{/if}}) on top of everything below.

1. **Task questions are not repeated in chat.** A question lives in its task file (`tb ask`); the chat names
   only where it is ("T012 Q3-Q5 wait on you") unless the user asks to see the text.
2. **Every task needs {{#if open}}a person's{{else}}the user's{{/if}} approval before it starts.** No executor starts implementing (move into
   03-Implementation) before {{#if open}}someone (anyone, not only the owner){{else}}the task's owner{{/if}} said "start" for that task, in chat or on the board. Record it
   with `tb decide <id> "start approved" --decided-by <person>` (or the answer to a start question). An
   approved plan or 02-Ready is not a start approval; one approval covers one task.
3. **Questions are answerable in a few characters.** Each question offers options (a), (b), (c)... with the
   recommendation first and marked, so "a" is a complete answer. Free text only where options make no sense.
4. **The supervisor keeps `AI/SupervisorHandover.md` current** (see "Context management"). It holds the work
   running now and what the next supervisor needs; it is updated at every step of a running task, rewritten,
   not appended to, and finished work is removed from it.
5. **Compaction keeps little.** What survives a compaction is defined in `CLAUDE.md` ("Compact Instructions");
   `AI/SupervisorHandover.md` is authoritative and is never restated in a summary.
6. **The reload hook restores the supervisor.** After a compaction or `/clear`, `.claude/hooks/reload.py`
   re-injects the supervisor skill and the handover, plus any files the checkout's local board config lists
   (see "Context management").

## Roles

| Role | Who | Does | Never |
|---|---|---|---|
| **user** | {{user}} (the AI workflow owner) | sets goals and priorities, answers questions, approves plans, task starts and judgment calls | - |
| **supervisor** | the main Claude session, in {{checkout_phrase}} | keeps the big picture, runs the board, writes or delegates plans, starts and steers agents, records results, lands approved {{#if git}}PRs{{else}}branches{{/if}}{{#if has_deploy}}, deploys and verifies{{/if}} | does large code changes itself; asks the user what a task file already answers; starts a task without the approval |
{{#if multiuser}}
{{#if strict}}
| **coordinator** | the main Claude session of another developer (the **dev**), in {{own_checkout}} | runs the board for the tasks the dev owns: creates, refines, asks and records the dev's decisions, commissions plans (plan-only executor) and research, moves up to 02-Ready, runs its own watcher `tb watch --for <dev>` (which starts its web board) | starts code-writing executors or the reviewer, branches, lands, deploys, moves into 03-07, decides on tasks the dev doesn't own, runs the supervisor's watcher (`tb watch` without `--for`) |
{{else}}
| **coordinator** | the main Claude session of another developer (the **dev**), in {{own_checkout}} | runs the board for the dev (any task): creates, refines, asks and records decisions, commissions plans (plan-only executor) and research, moves up to 02-Ready, runs its own watcher `tb watch --for <dev>` (which starts its web board) | starts code-writing executors or the reviewer, branches, lands, deploys, moves into 03-07, runs the supervisor's watcher (`tb watch` without `--for`) |
{{/if}}
{{/if}}
| **executor** | agent (`.claude/agents/executor.md`) | implements one task or approved plan in its own {{worktree}} on its task branch, writes the plan first when non-trivial, hands back {{#if git}}a pull request{{else}}the branch and revision{{/if}} | lands, deploys, edits task files, {{#if git}}pushes to `{{main}}`{{else}}commits to trunk{{/if}}, works outside its task's scope |
| **executor-light** | agent (`.claude/agents/executor-light.md`), cheaper model | the same, for already-specified work: a review's fix list, a small contained bug, docs/config, a refactor with a parity test | designs; it stops and hands back instead |
| **reviewer** | agent (`.claude/agents/reviewer.md`) | reviews one {{review_noun}}: intent, correctness, tests, scope, docs, impact{{#if analyst}} measured before/after{{/if}}; returns a verdict | edits, commits, lands{{#if git}}, comments on the PR{{/if}} |
{{#if analyst}}
| **analyst** | agent (`.claude/agents/analyst.md`) | answers one data question by computing on a read-only data copy | writes to the repository or to real data |
{{/if}}
| **researcher** | agent (`.claude/agents/researcher.md`) | answers one reading-heavy question from docs, code and the web with a short sourced summary | writes to the repository |

{{#if multiuser}}
Only one supervisor runs at a time: {{user}}'s session. Other developers work with a **coordinator** session
(`coordinator` skill){{#if strict}} on the tasks they own{{/if}}; implementation, review, landing and deployment stay with
the supervisor.

### Task ownership

{{#if strict}}
A task's **owner** decides its questions, approach, scope, plan and start. The owner is the task's `owner`
header field: a person from `people`, or `null` for the user ({{user}}, `default_owner` in
`.claude/board/board.json`). A coordinator creates the dev's tasks with `tb new ... --owner <dev>`. Ownership
is transferred only by the current owner: `tb set <id> owner=<new> --decided-by <old>` changes the header and
records the giver's decision (`owner: <new> (transferred from <old>)`) and the Log in one write; it refuses a
giver who is not the current owner (`--force --reason` overrides, e.g. for a dev who left). Questions for the
owner of a dev-owned task are asked `--for user` and start with `@<dev>:` (`tb ask --to <dev>`). On the web
board, "Waiting on: me" shows the tasks where the checkout's person (`tb me`) has an open question to answer:
addressed `@<them>:`, or unaddressed on a task they own; "Waiting on: everyone" shows every task with an open
question for the user. The choice is remembered per browser.

**Anyone may answer:** any dev may answer any open question of any task. The answer is recorded under the
answerer's name in the question line (`(answered <date>, {{dev}}, for user)`), the Decisions line and the Log:
with `tb answer ... --answered-by <name>` (mandatory), or through the web board, which always answers as the
checkout's `tb me`. A non-owner's answer stands, under their name, until the task's owner overrules it with a
later decision. The names are the `people` in `.claude/board/board.json`; each dev sets theirs once per checkout
with `tb me <name>`.
{{else}}
A task's **owner** is its creator and the person addressed by its questions and checks; it carries no special
rights. **Anyone may** give the start approval for, edit, refine and answer questions on any task, and change its
owner. The owner is the task's `owner` header field: a person from `people`, or `null` for the user ({{user}},
`default_owner` in `.claude/board/board.json`). A coordinator creates the dev's tasks with
`tb new ... --owner <dev>`. `tb set <id> owner=<new> --decided-by <who>` changes the header and records the
decision (`owner: <new> (set by <who>, from <old>)`) and the Log in one write, for anyone. We work on the
principle that all collaborators act in good faith and everything is documented. Questions for the owner of a
dev-owned task are asked `--for user` and start with `@<dev>:` (`tb ask --to <dev>`). On the web board,
"Waiting on: me" shows the tasks where the checkout's person (`tb me`) has an open question for the user to
answer: addressed `@<them>:`, or unaddressed on a task they own; "Waiting on: everyone" shows every task with an
open question for the user, for any person. The choice is remembered per browser.

**Anyone may answer:** any dev may answer any open question of any task, even one asked by or for another dev or
for the supervisor. The answer is recorded under the answerer's name in the question line
(`(answered <date>, {{dev}}, for user)`), the Decisions line and the Log: with
`tb answer ... --answered-by <name>` (default: the checkout's `tb me`), or through the web board, which always
answers as the checkout's `tb me`. An answer stands, under the answerer's name, until a later decision overrules
it. The names are the `people` in `.claude/board/board.json`; each dev sets theirs once per checkout with
`tb me <name>`.
{{/if}}

{{else}}
One person works here: {{user}}, the user of the one supervisor session. `people` in `.claude/board/board.json`
names them, `tb me {{user}}` (once per checkout) makes them the person of every board write, and a task's `owner`
stays `null` (= `default_owner`). The board and the CLI support several people and coordinator sessions; the
`coordinator` skill and the multi-user rules are left out of this project until they are needed (rebuild the
harness from the toolkit with the multi-user answers).

{{/if}}
The roles a task file may name (`waiting_on`, questions) are configured in `.claude/board/board.json`
(`roles`; `user` and `supervisor` are required). Add a role there before you add an agent for it.

### The supervisor in detail

The supervisor is defined by the `supervisor` skill (`.claude/skills/supervisor/SKILL.md`). In short:

- **Context is the scarce resource.** Keep it for the user, the board and decisions. Delegate anything that
  means reading many files, writing code, reviewing a diff{{#if analyst}} or crunching numbers{{/if}}. Summarize agent reports in a
  few lines.
- **The task file is the memory of a task, the handover the memory of the session.** Before asking the user
  anything, read the task file: an answered question is never asked twice. Every result, answer and decision
  goes into the task file right away; what is running goes into `AI/SupervisorHandover.md` (agent reports and
  your own context are lost on compaction).
- **Task files change {{on_main}} only, through the board CLI** (`{{python}} .claude/board/tb.py ...`). The CLI
  sets `updated`, writes the Log line, keeps `waiting_on` consistent, moves files with `{{vcs_mv}}`, and refuses
  moves whose preconditions are not met. Run `tb check` before committing; it must report no problems.
- **The board watcher runs in the background** for the whole session (`tb watch` with the Bash tool's
  `run_in_background`; restart it after handling its output). It wakes the supervisor when the user answers or
  writes a note on the web board{{#if multiuser}}, when another dev's commit brings input for the supervisor{{/if}}, and when the
  periodic {{#if git}}pull stops (diverged){{else}}`svn update` of `AI/` hits a conflict{{/if}} or keeps failing; idle costs nothing.
- **Landing authority:** the supervisor lands a {{#if git}}PR (`gh pr merge`){{else}}branch (`sv land`, one trunk commit per task){{/if}} once the reviewer
  approved it, without asking again, then tells the user in a short summary what landed and its impact. A
  `needs decision` verdict goes to the user instead.
- **Messages to the user are short and decision-first**: what is needed from them at the top, details below.

## Context management

The supervisor's context is lost in two ways: compaction (automatic when the context fills up) and `/clear` or
a new session. Three things make that cheap.

**`AI/SupervisorHandover.md`** is the supervisor's working memory on disk. It contains only what is live:

- the task(s) in progress and the exact step each is at (e.g. "T137: {{#if git}}PR #80{{else}}branch r2450{{/if}} in review, reviewer running");
- running or awaiting agents: type, task, branch{{#if git}}/PR, worktree{{else}} and revision, working copy{{/if}};
- open decisions and who they wait on (ids and question numbers, not the text);
- standing decisions, pitfalls and facts the next supervisor needs that are not in a task file, plan or Context.md.

Rules: update it at every step of a running task (an agent started or returned, a {{review_noun}} handed in,
reviewed or landed, a deploy done, a decision taken), in the same breath as the `tb` write. Rewrite the affected
lines, never append a diary; remove a task's lines as soon as it is done or parked in a state that needs nothing
from the supervisor; drop a pitfall once the tooling fixed it. Keep it short (a screen or two; `tb check` warns
when it exceeds the `handover` limits in `board.json`). It is versioned and committed with the task commits
(`tasks: ...`), so a supervisor on another machine reads the same state. A new supervisor reads it first.

**Compaction** follows the "Compact Instructions" in `CLAUDE.md`: the summary keeps only the role line, the
current step, unresolved errors, and what happened after the last handover edit; everything in the handover
or the skill is dropped from it. The first action after a compaction is to sync those items into the handover.

**The reload hook** (`.claude/hooks/reload.py`, `SessionStart` on `startup|resume|compact|clear` in
`.claude/settings.json`) prints `.claude/skills/supervisor/SKILL.md` and `AI/SupervisorHandover.md` (then the
files listed under `reload` in the {{untracked}} `.claude/board/local.json`, if any) into the context when the
session is the supervisor. It knows that from `"role": "supervisor"` in the {{untracked}} `.claude/board/local.json`
(`tb me <name> --role supervisor`, the supervisor's checkout only), from the environment variable
`CLAUDE_ROLE=supervisor` for sessions launched with it, or from the session's own transcript (the `supervisor`
skill was invoked in it, via the Skill tool or `/supervisor`). A new session or a `/clear` starts with a new
transcript, so there the role comes from `local.json` or `CLAUDE_ROLE`, else from `/supervisor`. After a
compaction it does not print a skill body the transcript shows invoked (the harness re-lists it; a one-line note stands in),
and it keeps its output within `reload.budget_bytes` of `board.json` (default 20000, handover files never
dropped, a one-line pointer for any other file that does not fit), so Claude Code keeps it inline.

**Context size.** Every turn re-reads the session's whole context, and a supervisor woken by the watcher and by
agents makes many turns a day, so the context size is the main cost of the harness. `.claude/settings.json`
caps the automatic compaction window at {{compact_window_k}} tokens for every session of the checkout (agents included) and
lets an idle session compact itself; the supervisor starts fresh (a `/clear`, in the Claude desktop app by
itself) after a landed task when nothing is running and the user is in chat, as the supervisor skill ("Context
size") describes. The handover and the reload hook make both safe at any time.

## The task board

`AI/Tasks/README.md` is the specification of the files. The states:

| Folder | Meaning | Moves on when | `waiting_on` after the move |
|---|---|---|---|
| `00-Conception` | problem or idea written down; nothing decided | the problem is clear enough to work out a solution | null |
| `01-Refinement` | solution, scope and acceptance worked out; questions asked and answered; plan written if non-trivial | the user approved the approach and no question is open | supervisor |
| `02-Ready` | approved and unblocked; waits for the start approval (ground rule 2) | an executor starts it (branch created) | null |
{{#if git}}
| `03-Implementation` | being implemented on the named branch | the executor opens a pull request (a `research` task has no branch or PR: it moves to 07 when the result is logged) | executor |
| `04-CodeReview` | the PR `pr` is reviewed{{#if analyst}}, including its data impact{{/if}} | the reviewer approves (or it goes back to 03) | reviewer |
{{else}}
| `03-Implementation` | being implemented on the named branch | the executor hands back a branch revision (a `research` task has no branch: it moves to 07 when the result is logged) | executor |
| `04-CodeReview` | the branch is reviewed at the named revision{{#if analyst}}, including its data impact{{/if}} | the reviewer approves (or it goes back to 03) | reviewer |
{{/if}}
| `05-UserReview` | waiting for the user: a decision or a check of the result (optional) | the user answered | user |
{{#if deploy_prod}}
| `06-Deploy` | merged into {{main_ref}}; waiting for deployment and the production check | the check passed | supervisor |
{{else}}
{{#if deploy_test}}
| `06-Deploy` | landed on {{main_ref}}; waiting for deployment to the test server and the check there | the check passed | supervisor |
{{else}}
| `06-Deploy` | not used: nothing is deployed in this project; landed tasks go straight to 07 | - | - |
{{/if}}
{{/if}}
| `07-Done` | finished and verified (agent tokens logged) | - | null |
| `08-Dropped` | rejected, duplicate or obsolete; reason in the log | - | null |

An open question for the user always makes `waiting_on: user`. **No new input while an agent works on a task:**
a task in 03-Implementation or 04-CodeReview gets no question for the user and no new information, so the
implementation and its review see the same state. When a decision for the user comes up there (a `needs decision`
verdict, a question an executor raises), let the running agent finish, ask the question and move the task to
05-UserReview, where the user answers it (also on the web board). After the answer it goes back to 03 for a fix
round, to 06 if it already landed, or on as the answer says. A blocked task stays where it is and names the
blocker in `blocked_by`. A task with nothing to deploy (docs, tooling) moves from 04 (or 05) straight to 07. A
research task skips 04 and 06 and moves from 03 straight to 07 (see "How tasks move").
{{#if deploy_test}}

**Deploy means the test server.** Production releases are not part of a task's flow: they are separate `ops`
tasks that bundle landed work.
{{/if}}
{{#if deploy_none}}

**Nothing is deployed in this project.** Every task moves from 04 (or 05) straight to 07; the acceptance is
verified on the landed {{main_ref}}.
{{/if}}

### How tasks move (enforced by `tb move`)

Allowed moves, forward and back:

```
00 -> 01, 08          01 -> 00*, 02, 05, 08        02 -> 01*, 03, 08
03 -> 01*, 02*, 04, 05, 08                         04 -> 03*, 05, 06, 07, 08
05 -> 01*, 02*, 03*, 04, 06, 07, 08                06 -> 03*, 07
07 -> (final)         08 -> 00*, 01*               (* backward: needs --reason; 08 always needs --reason)
```

(Research tasks differ: 03 -> 07 allowed, 03 -> 04 and 05 -> 04/06 refused; see "Research tasks" below. The
per-type rules are the `type_rules` key of `.claude/board/board.json`, described in `.claude/board/README.md`.)

Preconditions the CLI checks:

- **into 02-Ready or 03-Implementation:** no open question (for anyone), Problem/Approach/Acceptance filled in,
  `blocked_by` empty. Non-trivial work also needs its approved plan linked (`plan:`); that one is the
  supervisor's judgment, see "Plans". The start approval (ground rule 2) is the supervisor's duty, not checked
  by the CLI.
- **into 03-Implementation:** a branch (`--branch`; not for a research task).
{{#if git}}
- **into 04-CodeReview:** a branch and the pull request number (`--pr`).
{{else}}
- **into 04-CodeReview:** a branch and the branch revision handed to review (`--rev`).
{{/if}}
- **into 05-UserReview:** an open question for the user (ask it first: "Does the result look right?"). This is
  also where a decision that came up in 03/04 goes (see above).
- **into 06-Deploy / 07-Done from review:** a reason saying how it landed ("{{landed_reason}}").
- **into 07-Done:** no open question; a task with a reviewed {{review_noun}} logs its agent token counts
  (`--tokens`; a research task always does).

`--force --reason "..."` overrides a failed precondition and logs what was overridden. Use it rarely, and only
on the user's word.

**Research tasks** (`type: research`) have their own path, because the {{#if analyst}}analyst or {{/if}}researcher works on
data or documents and there is no branch, review or deploy: `00 -> 01 -> 02 -> 03 -> 07`, with 05 (back to 03,
or on to 07) and 08 as for every task. Into 03-Implementation needs no branch (`waiting_on` becomes `executor`;
the agent reports to the supervisor). 03 -> 07-Done is allowed directly; every move of a research task into
07-Done (from 03 or from 05) needs the agent token counts (`--tokens`), and a `--reason "result: <answer in a
line>"` is recommended. 04-CodeReview and 06-Deploy are refused from 03 and from 05 with a message that names
the type. The backward moves are unchanged, as are the other types. The rules live in the `type_rules` key of
`.claude/board/board.json`, so another type can get its own path the same way.

### Who edits task files

{{#if multiuser}}
Only the supervisor and coordinators{{#if strict}} (on their dev's tasks){{/if}}, only {{on_main}}, only through `tb`
(or a dev, through a web board). Several checkouts edit the same files, so each writer updates
(`{{vcs_update}}`) right before a `tb` write and {{commit_push}}{{#if git}}es{{else}}s{{/if}} right after; a conflict is
resolved by taking the incoming file and redoing the change with `tb`, never by merging task files by hand. On
top of that, every running board (`tb serve`, and `tb watch` for the supervisor or `tb watch --for <dev>` for a
coordinator) {{#if git}}pulls `{{main}}` every minute (`watch.update_seconds`) with `git fetch` and `git merge --ff-only @{u}`{{else}}runs `svn update` of `AI/` every minute (`watch.update_seconds`), never of the code{{/if}}, so a
checkout sees other devs' task changes within a minute.{{#if git}} Git cannot update one folder, so this pulls the whole
branch; that is fine because code reaches `{{main}}` only through merged PRs.{{/if}} The update pauses while the task
folders have uncommitted edits{{#if git}} or the branch has unpushed commits (commit and push promptly), and stops when local
and `origin/{{main}}` have diverged{{else}} (commit them promptly) and stops on a conflict{{/if}}, without resolving anything: the board
header and the watcher report it{{#if svn}} with the paths; revert the file (`svn revert <file>` gives back the incoming
version), redo the change with `tb`, commit, and updating resumes by itself{{/if}}. Coordinators write
`--by "coordinator (<dev>)"` and run `tb watch --for <dev>`, never the supervisor's `tb watch`. In a coordinator's
checkout the board commits its own answers and notes (below).
{{else}}
Only the supervisor, only {{on_main}}, only through `tb` (or the user, through the web board). The
running board (`tb serve`, started by `tb watch`) {{#if git}}pulls `{{main}}` every minute (`watch.update_seconds`) with
`git fetch` and `git merge --ff-only @{u}`{{else}}runs `svn update` of `AI/` every minute (`watch.update_seconds`),
never of the code{{/if}}, so a second checkout (another machine) sees task changes within a minute. The update
pauses while the task folders have uncommitted edits{{#if git}} or the branch has unpushed commits (commit and push
promptly), and stops when local and `origin/{{main}}` have diverged{{else}} (commit them promptly) and stops on a
conflict{{/if}}, without resolving anything: the board header and the watcher report it.
{{/if}}
Task branches never add, edit or move task files: this keeps task state current in one place and avoids merge
conflicts between a {{review_noun}} and state moves. Executors and reviewers read the task file for the intent and
report back in {{#if git}}the PR and {{/if}}their final message; the supervisor records it.

### The web board and the watcher

`tb serve` runs a local board (loopback only, no authentication) with one column per state, filters, search,
the consistency problems, a page per task, and a read-only Markdown reader for plans (light and dark theme). On
a task outside 03/04 anyone at the board can answer any open question (questions for the user first, each
tagged with its role; a question with options (a), (b), ... gets one button per option) or add a note. Every
board write is made as the checkout's person (`tb me`, shown read-only as "Answering as <name>"); without `tb me`
the board refuses writes and says to run `{{python}} .claude/board/tb.py me <name>`. Answer and note are written
into the file under that name (`{{user}} (board): ...` Log line; `waiting_on: supervisor` unless a user question is
still open). The board writes only {{on_main}} and only when the file is unchanged since it was displayed; it
never moves files. In the supervisor's checkout it never commits (its watcher starts it with `--no-commit`): the
supervisor commits these edits with its next task commit.
{{#if multiuser}}

Each coordinator runs `tb watch --for <dev>` in its own checkout at session start. It starts the dev's board and
wakes the coordinator for input that concerns the dev: the dev's own board answers and notes, new open questions
for the dev, tasks newly owned by the dev, and other people's board input on the dev's tasks; nothing else. The
coordinator opts in once per checkout with `tb me <dev> --board-commits on` (`local.json`, never in the
supervisor's checkout): the board then commits each answer or note right away (`tasks: <id> board input
(<dev>)`, only that task file) when the written file has no problems{{#if git}} (a local commit; the coordinator pushes){{else}}, redoes
its edit on the incoming version when the commit is out of date{{/if}}, and otherwise leaves it saved and
uncommitted with the reason on the page; the coordinator then commits it by hand. Details:
`.claude/board/README.md` ("Board commits", "Coordinator watcher").
{{/if}}

Who ends when: a watcher stops within about 5 s of its session (exit 4); the board a watcher started stops 15
minutes after its last request (`watch.board_idle_minutes`); a board started by hand runs until stopped
(`.claude/board/README.md`, "Lifecycle").

`tb watch` updates every minute, starts the board if needed (without its own update) and exits when new board
Log lines appear (`<name> (board):`), after a quiet period, so several answers arrive together.{{#if multiuser}} It also exits
(`INCOMING`) when an update (its own, the supervisor's or any other) brings other devs' input for the
supervisor: their board answers and notes, new tasks, new open questions for the supervisor, tasks that now
wait on the supervisor. Other commits don't wake it.{{/if}} A {{#if git}}diverged branch{{else}}conflict in `AI/`{{/if}}, or 3 failed updates in
a row, wake it once with exit 3. Use the Bash tool's `run_in_background`, not a polling loop or a monitor tool:
those wake the session and cost tokens while idle. Details: `.claude/board/README.md`.

{{#if git}}
## Git: branches, worktrees, pull requests, merging

- **The main checkout** (the repository's own folder, not a worktree under `.claude/worktrees/`) is the
  supervisor's and stays on `{{main}}`. Task files, plans and the handover change there; code never changes there
  except through merged PRs.
- **One branch per task:** `<id lowercase>-<slug>` (e.g. `t115-mixed-resolution-align`), created by the
  executor in its worktree from the current `origin/{{main}}`, pushed, and deleted when the PR is merged.
- **One worktree per agent:** an executor started with `isolation: "worktree"` gets its own git worktree; two
  writers never share a branch or a worktree. The reviewer checks the PR out into its own temporary worktree
  and removes it when done.
- **Handing in for review:** the executor merges `origin/{{main}}` into its branch (resolves conflicts, runs the
  full suite), pushes and opens the PR with `gh pr create`. The PR number goes into the task:
  `tb move <id> codereview --pr <n>`.
- **Merging:** after the reviewer's `approve`, the supervisor checks that the reviewed head is still the PR's
  head, then `gh pr merge <n> --merge --delete-branch`, and `git fetch -q origin && git merge --ff-only @{u}` in
  the main checkout (not `git pull`: no race with the watcher's fetch on FETCH_HEAD).
- **Cleaning up:** after a merge, remove an agent's worktree without asking when its branch is merged into
  `{{main}}` and the tree is clean (`git worktree remove <path>`); ask the user before `--force` or when the
  branch is unmerged. Check `git worktree list` at session start.
{{else}}
## SVN: branches, working copies, review, landing

The full command reference is `.claude/svn/README.md`. The rules:

- **The main checkout** is the supervisor's and is always on `^/trunk`. Task files and plans change there; code
  never changes there except through `sv land`.
- **One branch per task:** `^/branches/<id lowercase>-<slug>` (e.g. `t012-weekly-export-labels`), created by the
  supervisor (`sv branch create`) when the task moves to 03, deleted by `sv land`.
- **One working copy per agent:** an agent started with `isolation: "worktree"` gets a fresh trunk checkout in
  `<checkout>.worktrees\<name>` next to the main checkout (WorktreeCreate hook). The executor's first step is
  `sv switch <branch>`. Two writers never share a branch or a working copy.
- **Agents commit with `sv commit`** only (a raw `svn commit` in a worktree is blocked by a hook; `sv commit`
  refuses trunk). Executors commit often on their branch; nothing reaches trunk before review.
- **Handing in for review:** the executor syncs the branch with trunk (`sv sync`, resolves conflicts, runs the
  tests, commits) and reports the branch and its head revision. That pair (`branch`, `rev`) is what a pull
  request is in git: `tb move <id> codereview --rev <n>`.
- **Review:** the reviewer reads `sv diff <branch>` and runs the code in its own working copy
  (`sv wt add review-<id> --branch <branch>`), which it removes when done.
- **Landing:** `sv land <branch> -m "<ID>: <title>"` in the main checkout merges the branch, commits it to trunk
  as **one commit per task**, and deletes the branch. On conflicts it reverts and stops: the executor syncs and
  resolves, the reviewer checks again. `sv sync` and `sv land` leave `AI/Tasks` out of the merge (task files
  change on trunk only), so task-file tree conflicts do not occur.
- **Cleaning up:** Claude Code does not remove an agent's working copy when the agent ends. The supervisor runs
  `sv wt remove <name>` after landing (refused while uncommitted changes exist) and checks `sv wt list` at
  session start.
{{/if}}

## Plans

A change is **non-trivial** when it adds behavior, changes an output or a format, migrates data, touches more
than one area, or leaves a real design choice open. Non-trivial work gets a plan before implementation:

1. While the task is in 01-Refinement, the supervisor has the executor write the plan
   (`AI/Plans/<YYMM>-<NN>-<Title>.md`; `tb next-plan "<title>"` gives the path; numbers are never shared;
   the template is in `AI/Plans/README.md`).
2. The plan states the problem, the chosen design with the alternatives considered, the **expected impact**
   (what changes for users, {{#if analyst}}computed values, stored rows, {{/if}}outputs or APIs, and why), config changes, tests,
   docs to update, and an **effort estimate in agent terms**: number of {{#if git}}PRs{{else}}branches{{/if}}, expected review rounds,
   rough tokens per {{#if git}}PR{{else}}branch{{/if}} (e.g. ~150k for a small fix with tests, 1-2M for a large change), and fixed
   run times (migrations, rebuilds) that don't shrink with agent speed.
3. The supervisor summarizes it for the user; the user approves (recorded with `tb decide`). Then
   `tb set <id> plan=AI/Plans/...`, and the task can move to 02-Ready.

A plan-only executor writes the plan to the main checkout's {{untracked}} `AI/Temp/plans/` (not into its final
message); the supervisor moves it into `AI/Plans/` {{on_main}} and commits it (plans are documents, not code, so
they don't need a branch).

Plans are point-in-time records of why something was built a certain way; they are never treated as the current
state of the code. Small, unambiguous fixes go straight to implementation without a plan (but not without the
start approval).

## Delegation

- **Pick the tier.** `executor` (strongest model) for plans, new designs and first implementations of large or
  logic-heavy changes. `executor-light` for review fix lists, small contained bugs, docs/config and refactors
  with a parity test. Escalate light -> full after two review rounds or on a design question. The reviewer
  always runs on the strong model.
- **Brief completely.** An agent starts cold. Its prompt names the task id, the branch, the plan, the decisions
  already made (copied from the task file), what done looks like, and anything private it may use but must never
  write into {{tracked}} files. The `task-delegate` skill has the templates.
- **Run executors in their own {{worktree}}** (`isolation: "worktree"`), so they never touch the supervisor's
  checkout. Independent tasks run in parallel, each on its own branch.
- **Tell agents to save tokens:** reuse what is already computed, no repeated full runs or rebuilds, no idle
  polling, stop and report instead of guessing.
- **Agents never kill processes by image name** (e.g. all `python` or `node` processes): stop a process by its
  id or port.
- **Short final messages, details in report files.** An agent's final message lands in the supervisor's
  context in full, so it is short (about 15 lines: verdict or result, findings and proposals one line each,
  tokens, the report file). The details (evidence, numbers, commands, long proposals) go into a report file in
  an {{untracked}} `AI/Temp/reports/<task id>-<role>-<ref>.md`{{#if git}}: in the agent's own worktree if it works in one (its
  sandbox can't write to the main checkout), else in the main checkout; the final message gives the full path, and
  the supervisor reads a worktree's report before removing that worktree{{else}} in the main checkout (the folder next
  to `<checkout>.worktrees`); the final message names the file{{/if}}. The supervisor does **not** read report files by
  default: it opens one only for a detail it needs now, and passes the file name (never its content) to the next
  agent, e.g. the reviewer's report to the executor of a fix round. Briefs stay short the same way: they point to
  the task, plan and report files instead of copying them.
- **Record on return.** When an agent reports, the supervisor records the result in the task file at once
  (branch, {{#if git}}PR{{else}}revision{{/if}}, verdict, findings, impact, proposed tasks, token count), moves the task and updates
  the handover.

## Review and landing

{{#if git}}
1. Executor hands back the PR -> `tb move <id> 04 --pr <n>`; start the reviewer with the task, plan and
   decisions.
2. Reviewer verdict:
   - `approve` -> `gh pr merge <n> --merge --delete-branch`, then `tb move <id> 06 --reason "merged PR #<n>
     into {{main}}, reviewer approved"` (or straight to 07 when there is nothing to deploy).
   - `changes needed` -> `tb move <id> 03 --reason "review: <short findings>"`; send the findings to the
     executor (or executor-light) as a fix list on the same branch and PR; review again.
   - `needs decision` -> ask the user (`tb ask <id> --for user ...`), move to 05; continue on the answer.
{{else}}
1. Executor hands back branch + revision -> `tb move <id> 04 --rev <n>`; start the reviewer with the task, plan
   and decisions.
2. Reviewer verdict:
   - `approve` -> `sv land <branch> -m "<ID>: <title>"`, then `tb move <id> 06 --reason "merged into trunk
     r<n>, reviewer approved"` (or straight to 07 when there is nothing to deploy).
   - `changes needed` -> `tb move <id> 03 --reason "review: <short findings>"`; send the findings to the
     executor (or executor-light) as a fix list on the same branch; review again.
   - `needs decision` -> ask the user (`tb ask <id> --for user ...`), move to 05; continue on the answer.
{{/if}}
{{#if deploy_prod}}
3. Deploy to production as `AI/Context.md` ("Build, run and deploy") and the private deployment notes describe,
   check production as the task's Acceptance names, then `tb move <id> 07 --tokens "..."`.
{{/if}}
{{#if deploy_test}}
3. Deploy to the test server (per `AI/Context.md`, "Build, run and deploy"), verify the acceptance there, then
   `tb move <id> 07 --tokens "..."`.
{{/if}}
{{#if deploy_none}}
3. Verify the acceptance on the landed {{main_ref}} (nothing is deployed in this project), then
   `tb move <id> 07 --tokens "..."`.
{{/if}}
4. Tell the user in two or three lines: what landed, its impact, anything they need to do.

Things an agent found but did not fix become new tasks (`tb new`, usually in 00-Conception), never silent
scope creep.

## Token log

At 07-Done, log the agents' token counts next to the plan's estimate, e.g.
`--tokens "executor 108k, executor-light 30k, reviewer 56k (estimate ~150k)"`. Take each agent's count from its
completion notification; a resumed agent reports a cumulative count, so log the last value. Write them into the
task log as soon as they arrive on long tasks (`tb log <id> "tokens so far: ..."`); they are lost after context
compaction. The supervisor's own tokens are not counted. Over time this calibrates the estimates.

## Session routine (supervisor)

1. Read [Context.md](Context.md), this file and `AI/SupervisorHandover.md`.
2. `{{vcs_update}}`, `tb board`, `tb watch --once`, {{#if git}}`git worktree list`{{else}}`sv wt list`{{/if}}; start `tb watch` in the
   background (it keeps the checkout current from then on). Update again at the start of every queue step.
3. Handle what is waiting on the supervisor, then on agents; tell the user only what needs them.
4. Keep `AI/SupervisorHandover.md` current at every step; before the session ends, make sure every task file
   and the handover are current and {{committed_pushed}}.

## Commits

- Task changes: one commit per logical step {{on_main}}, message `tasks: <what>` (each `tb` write command
  prints a suggestion, e.g. `tasks: T012 01-Refinement → 02-Ready`){{#if git}}, pushed right away{{/if}}. Several task edits of
  one step can share a commit. Commit only task files, plans and the handover ({{#if git}}`git add AI`{{else}}`svn commit AI -m ...`{{/if}}), never
  stray code changes.
- Code: only on task branches{{#if svn}} (`sv commit`){{/if}}, reaching {{main_ref}} only through a reviewed and {{#if git}}merged PR{{else}}landed branch
  (`sv land`): one trunk commit per task, message `<ID>: <title>`{{/if}}.

## Confidentiality

Anything that must not be published ({{confidential}}) stays out of {{tracked}} files, commit messages{{#if git}}, PR texts{{/if}}
and agent reports, and is passed to agents only in their prompt, with the instruction never to write it anywhere
{{tracked}}. Credentials and passwords are never given to AI tools at all. Reviewers check diffs for it.
