---
name: board-watch
description: Start, restart and handle the task board watcher, which wakes the supervisor when the user answers a question or adds a note on the local web board{{#if multiuser}}, or when an update brings another dev's input{{/if}}. Use at session start, whenever the watcher's background command finishes, and after handling board input.
---

# Board watcher

`tb` = `{{python}} .claude/board/tb.py`.

## Start (once per session, and again after every wake-up)

Run with the **Bash tool**, `run_in_background: true`:

```bash
{{python}} .claude/board/tb.py watch
```

It starts the web board if it isn't running (log: `.claude/board/log/server.log`; URL in the first output line),
{{#if git}}pulls `{{main}}` (`git fetch` + `git merge --ff-only`){{else}}runs `svn update` of `AI/` (never the code){{/if}} at start and every
`watch.update_seconds` ({{update_seconds}} s), then sleeps until there is input for you: a new board Log line
(`<name> (board):`) in any task source{{#if multiuser}}, or other devs' input that arrived with an update (INCOMING){{/if}}. It waits
until nothing more arrives for `watch.quiet_seconds` ({{quiet_seconds}} s) and at least `watch.hold_seconds`
({{hold_seconds}} s) passed since the first input of the batch, so answers across several tasks come in one
wake-up; then it prints the batch and exits. The exit is what wakes you. While it sleeps it costs no model usage. The
board's **Wake now** button ends the wait at once: the output then starts with `WAKE NOW: requested by <name>`,
even when nothing else is pending.

- Don't use a polling loop, `sleep` loops or a monitor tool instead: they wake the session periodically and cost
  tokens while idle.
- Only one watcher at a time. If you're unsure whether one is running, check the background tasks before
  starting another.
- Your own `tb` edits never write `(board)` actors (the CLI refuses such a `--by`), so they never trigger it.
- The board it starts never commits (`tb serve --no-commit`): your board input is committed by you, as in "When
  it wakes you".{{#if multiuser}} Never set `board_commits` in this checkout (`tb me --board-commits` is for coordinators); the
  watcher prints a note if it finds it on.{{/if}}
{{#if multiuser}}
- Input that arrives with an update (INCOMING) wakes it only when it is for you: a new open question for the
  supervisor, or a task that now waits on the supervisor (an answer a dev wrote on their own board usually makes
  its task wait on you). New tasks and devs' board lines on other tasks are listed when something else wakes
  you, never a wake-up of their own. Other commits don't wake it.
{{/if}}
{{#if git}}
- Your own uncommitted edits don't wake it: after a pull, INCOMING counts only tasks whose files changed
  between the two commits, so a `tb move` right after your `gh pr merge && git fetch -q origin && git merge
  --ff-only @{u}` in the same poll (about 5 s) is not reported. Update with that, not `git pull`: the watcher's
  fetch doesn't write `.git/FETCH_HEAD` (git 2.29 or newer), but a `git pull` can still fail with "Cannot
  fast-forward to multiple branches" when it races another fetch. Known limitation: a task the pull changed that
  you also edited in the same poll is still reported, and so is a task of your own commit that you pushed and
  pulled in the same poll together with another commit. Recognise it as yours and restart.
- The pull pauses while `AI/` has uncommitted edits or `{{main}}` has unpushed commits (a "paused" line is
  printed, no wake-up): commit and push your task edits promptly.
{{else}}
- Known limitation: if you create a task (or ask yourself a question), commit and `svn update` within about
  5 s (one watcher poll), that own input comes back once as INCOMING. Recognise it as yours and restart.
- The update pauses while `AI/` has uncommitted edits (a line "update paused: waiting for local task edits to be
  committed" is printed, no wake-up): commit your task edits promptly.
{{/if}}
- It only works while this session is open; otherwise input waits in the files (`tb watch --once` at session
  start shows what is waiting on you). When the session ends, the watcher ends within about 5 s (exit 4,
  `SESSION GONE`) and the board it started stops {{board_idle_minutes}} minutes after its last request (`watch.board_idle_minutes`;
  an open board page keeps it alive). A restarted watcher starts a new board when needed.

## When it wakes you

Output looks like:

```
BOARD INPUT:
  T012: - 2026-10-20 {{user}} (board): answered Q2.
        state 01-Refinement, waiting on supervisor, owner {{user}}
        Q2: Export format: (a) CSV (b) JSON (c) both?
        answer ({{user}}): (a)
  T015: - 2026-10-20 {{user}} (board): please do this one first
        state 02-Ready, waiting on supervisor, owner {{user}}
{{#if multiuser}}
INCOMING ({{#if git}}git pull, 1a2b3c4 -> 5d6e7f8{{else}}svn update, r123 -> r127{{/if}}):
  T031 new task: Faster export (00-Conception)
  T012 Q4 open, for supervisor
  T020 now waiting on supervisor (01-Refinement)
  T016 {{dev}} (board): answered Q1.
{{/if}}
WAITING ON SUPERVISOR:
  T012 Export to CSV (01-Refinement)
```

Either section may be missing; `WAKE NOW: requested by <name> at <time>` comes first when the button was used.
For each task listed{{#if multiuser}} (a new task from another dev: read it with `tb show`; it belongs to that dev's
coordinator unless it waits on you){{/if}}:

1. The block under a task gives you its state, `waiting_on`, owner, and the question with its answer (already in
   the file under the answerer's name, with a Decisions line); `tb show <id>` only when you need more than
   that (the task's sections, other open questions).
2. Act on it: an answer may unblock a move (`task-refine` -> 02-Ready), a start approval may let you start an
   executor (`task-delegate`), a note may change priority or scope (`tb set`, `tb section`), ask a follow-up
   (`tb ask`, with options), or need a reply to the user.
3. Record what you did with `tb log <id> "..."` (or the move's own log line), and set `waiting_on` to whoever is
   next if no move did. Update `AI/SupervisorHandover.md`.
4. `tb done "board input T012, T015"`: the check, the commit of the task files and the handover{{#if git}} and the
   push{{/if}} in one call (it prints the check's problems and commits nothing when there are any).

Then **restart the watcher** (above) and tell the user in one or two lines what you did, only if it matters to
them.

## Exit 3: the update stopped or keeps failing

Handle this first, before other input:

{{#if git}}
- **Diverged** (local `{{main}}` and `origin/{{main}}` both have commits the other lacks): nothing is pulled until
  it is resolved. Never merge task files by hand. If the local commits are only your task commits:
  `git pull --rebase`. On a conflict in a task file take origin's version (during a rebase that is "ours":
  `git checkout --ours <file>`, `git add <file>`, `git rebase --continue`), then redo your change with `tb`,
  `tb check`, commit, push. Anything else in the local commits: `git rebase --abort` and tell the user.
- **Failing** (3 pulls in a row failed): the board shows the local files only. Usually offline or credentials:
  run `git fetch` once by hand to see the message. Tell the user if it persists.
{{else}}
- `UPDATE STOPPED: conflict in AI/Tasks/...`: nothing in `AI/` is updated until it is resolved. Never resolve by
  hand or pick a side with `--accept`. `svn revert <file>` (gives back the incoming version; for a tree conflict
  revert the paths `svn status AI` shows), redo your change with `tb`, `tb check`, commit. Updating resumes by
  itself.
- `UPDATE FAILING: 3 svn updates in a row failed (E170013 ...)`: the board shows the local files only. Usually
  offline or credentials (E215004: run `svn update` once by hand so svn caches them). Tell the user if it
  persists.
{{/if}}

Then restart the watcher. It wakes you only once per problem (`log/watch-update.json`): a restarted watcher whose
problem persists keeps watching board input instead of exiting again.

## Exit 4: SESSION GONE

`SESSION GONE: <image> (pid N) ended; the watcher stops`: a process of the session that started the watcher
ended. Normally nobody sees it (the session is gone). If a live session sees it, it is a false positive: restart
the watcher and record it as a task (`task-new`) with the output.
{{#if multiuser}}

## Coordinator mode

A coordinator runs `tb watch --for <dev>` instead (coordinator skill, "When the watcher wakes you"): the same
watcher, exits and failures, but it wakes only for input that concerns the dev (their own board input, new
questions for them, tasks newly theirs, others' board input on their tasks) and ends with `WAITING ON <DEV>:`;
its board commits the dev's answers and notes (`local.json` `board_commits`, `.claude/board/README.md`, "Board
commits"). Your `tb watch` (without `--for`) is unchanged.
{{/if}}

## If it fails

- `BOARD FAILED: port {{port}} is held by another task board ...`: the port answers, but not with this checkout's
  board (another checkout's or project's board, or another program). Never stop it by image name: find its pid
  (Windows: `netstat -ano | findstr :{{port}}`; Linux/macOS: `lsof -i :{{port}}`), check with the user whose it is, and
  stop only that pid when it is a stray board of this checkout; otherwise restart the watcher with `--port N`.
- `BOARD FAILED` otherwise: read `.claude/board/log/server.log` (port taken, Flask missing:
  `pip install -r .claude/board/requirements.txt`). Fix, restart.
- The board refuses writes when the checkout is not {{on_main}}: run it from {{checkout_phrase}}{{#if git}} (the
  repository's own folder, not a worktree under `.claude/worktrees/`){{/if}}.
