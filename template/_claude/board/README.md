# Task board tool

The tool behind the task board in `AI/Tasks/`: a CLI that lists, checks and changes task files, a local web
board for the user, and a watcher that wakes the supervisor session when the user writes on the board or when
an update brings another dev's input. It lives in `.claude/` because it is project tooling, not part of the
product; it imports nothing from the product's code.

The board works on a git checkout or an SVN working copy; `vcs` in `board.json` picks the backend
(`taskboard/vcs/git.py`, `taskboard/vcs/svn.py`). Everything version-control-specific (the main-line guard, the
versioned move of a task file, the periodic update, the watcher's revision tracking, board commits, the review
header field `pr` / `rev`) goes through that backend; the rest of the tool is the same for both.

- File format and states: `AI/Tasks/README.md`
- Flow, roles and move rules: `AI/Methodology.md`
- Supervisor procedures that use it: `.claude/skills/`
- The SVN workflow tool (branches, agent working copies, landing): `.claude/svn/README.md`

## Requirements

Python 3.10+ and the version control client on PATH (`git`, or `svn` with the command line tools). The CLI and
the watcher use only the standard library; the web board needs Flask and markdown-it-py
(`pip install -r .claude/board/requirements.txt`, which also installs pytest for the tests).

## Commands

`python .claude/board/tb.py <command>` from anywhere; `--help` on each command.

| Command | Does |
|---|---|
| `board [--source S] [--owner P \| --mine]` | tasks per state (id, priority, title, owner when set, waiting on, blocked by, `PR #n` or `r<n>`) + the check; `--owner Alex` lists only his tasks (the `default_owner` includes the `null` ones), `--mine` = `--owner` of `tb me`; the check still covers every task |
| `check` | consistency check only; exit 1 on problems |
| `show ID [--full]` | state, header, owner (`- (Alex)` for `null`), the review field (`pr: PR #77` / `rev: r12`), open questions, last log lines, allowed next states |
| `next-id [--source S]` | the id the next new task gets |
| `next-plan "TITLE"` | the path for a new plan (`<plans_dir>/<YYMM>-<NN>-<Title>.md`) |
| `new --title --type --priority [--problem/--problem-file] [--state 00/01] [--source] [--owner P]` | create a task; `--owner` a person from `people` (default `null`; the `default_owner` is stored as `null`); the header gets `pr: null` on a git board, `rev: null` on an svn board |
| `move ID STATE [--reason] [--branch] [--pr N \| --rev N] [--tokens] [--force]` | move with flow checks, Log line and the versioned move (`git mv` / `svn mv`); `--pr` on git boards, `--rev` on svn boards (the other flag is refused with a message naming the configured vcs) |
| `log ID TEXT` | append a Log line |
| `ask ID (--for ROLE \| --to PERSON) TEXT` | add an open question with the next number; `--to Robin` = `--for user` with the text prefixed `@Robin: ` |
| `answer ID QN TEXT [--answered-by] [--via] [--no-decision] [--waiting]` | record an answer (+ Decisions line); anyone may answer any open question; `--answered-by` is a person or a role: required with strict ownership (without it the command stops, nothing is written), default `tb me`, else `user`, with open ownership |
| `decide ID TEXT [--decided-by]` | record a decision; `--decided-by` as `--answered-by`; a decision starting `owner:` is refused (use `set owner=`) |
| `me [NAME] [--board-commits on\|off]` | print (exit 1 when not set; a second line `board commits on` when opted in) or set the person working in this checkout and its opt-in for board commits (`local.json`; coordinator checkouts only, see Board commits) |
| `set ID key=value ...` | title, type, priority, waiting_on, blocked_by, plan, branch, and the review field: `pr=77` or `pr=#77` on git boards, `rev=12` or `rev=r12` on svn boards (the other field is refused) |
| `set ID owner=P [--decided-by G] [--force --reason R]` | set the owner (alone, not with other keys): header, Decisions line and Log in one write; G is a person, default `tb me`. Strict ownership: G must be the current owner, else refused; `--force --reason` overrides and records it; the line is `- <date>, G: owner: P (transferred from <old>)` (`; forced: R`). Open ownership: anyone may, also a third person; the line is `- <date>, G: owner: P (set by G, from <old>)`; `--force` is refused. Same owner: no change. `owner=null` or the `default_owner` stores `null` |
| `migrate-owner [--dry-run]` | one-off format conversion: add `owner` after `priority` in every task file without it (the latest `owner:` Decisions line, else `null`; a `default_owner` name becomes `null`); no Log line, no `updated` change; idempotent; reports unknown names and header/decision mismatches |
| `section ID NAME --text/--file` | rewrite Problem, Approach or Acceptance |
| `serve [--no-update] [--port N] [--idle-exit MIN] [--no-commit]` | run the web board; it updates the checkout periodically unless `--no-update`; `--idle-exit MIN` stops it after MIN minutes without any request (the watcher starts its board so; by hand: no idle exit); it commits its own answers and notes when `local.json` has `board_commits` on, unless `--no-commit` (see Board commits) |
| `watch [--for DEV] [--once] [--quiet S] [--no-server] [--no-update] [--update-seconds N] [--port N] [--no-parent-check]` | update the checkout periodically, wait for input for the supervisor (with `--for DEV`: for that dev's coordinator, see Coordinator watcher), print it, exit; it stops when its session ends unless `--no-parent-check` (see Lifecycle) |

States can be given as `02-Ready`, `02`, `2` or `ready`. Every write command takes `--by` (default
`supervisor`), the actor of its Log line; a `--by` containing `(board)` is refused, so only the web board writes
the watcher's marker.

Guarantees of every write command:

- it runs only in a checkout of the main line (`main_branch`). git: the task folder's checkout is on that branch
  (`git rev-parse --abbrev-ref HEAD` in the folder), or the folder is in no git checkout at all, or git tracks
  nothing in it (a private, untracked task folder does not change with the branch); on a feature branch or a
  detached HEAD it refuses with "The task folder is in a git checkout of branch 'x', not of 'master'; task files
  change on master only". svn: the folder is in a working copy of `^/trunk` (`svn info`), or in no working copy
  at all; in a branch working copy it refuses with "... working copy of '^/branches/x', not of '^/trunk'";
- it sets `updated`, appends the Log line, and keeps `waiting_on` consistent with the open questions;
- it refuses an edit that would add a consistency problem, and writes atomically under the checkout's write
  lock (`log/write.lock`, shared with the periodic update; when it stays busy for 5 s the write fails with
  "could not write" and you retry);
- `move` checks the allowed moves and preconditions (`taskboard/workflow.py`, with per-type overrides in
  `type_rules` in `board.json`, below: a `research` task needs no branch, skips 04 and 06 and always needs
  `--tokens` at Done) and moves versioned files with `git mv` (`git ls-files --error-unmatch`; the rename is
  staged) or `svn mv` (`svn info`; history follows the file), a plain rename otherwise. Into 04-CodeReview it
  needs `branch` and the review field (`tb move T012 codereview --pr 77` / `--rev 12`); landing from 04/05 into
  06/07 needs a `--reason` (e.g. 'merged PR #77 into master, reviewer approved' / 'merged into trunk r57,
  reviewer approved'), Done after a review needs `--tokens`;
- it never commits; it prints a suggested commit message (`commit: tasks: ...`). The one exception is the web
  board of a coordinator checkout that opted in (Board commits).

Exit codes: 0 ok, 1 refused or problems found, 2 config error.

## Config: `board.json`

Plain JSON; keys starting with `_` are ignored (use them for comments). Paths are relative to the repo root.

| Key | Default | Meaning |
|---|---|---|
| `title` | `Task Board` | shown in the web board header |
| `repo_root` | `../..` | the repo root, relative to this config file |
| `vcs` | `git` | the version control of the checkout: `git` or `svn`; chooses the backend and the review header field (`pr` / `rev`); anything else is a config error |
| `main_branch` | `master` for git, `trunk` for svn | the main line; task files change only in a checkout of it. svn: `trunk`, `^/trunk` and `/trunk` all mean `^/trunk` |
| `ownership` | `strict` | who may set a task's owner and whether answers must name their author: `strict` = only the current owner transfers a task (`--force --reason` for anyone else) and `--answered-by` / `--decided-by` are mandatory; `open` = anyone sets the owner and the two flags default to `tb me`, else `user` |
| `plans_dir` | `AI/Plans` | where `next-plan` numbers plans |
| `types` | bug, feature, refactor, docs, research, ops | allowed `type` values |
| `roles` | user, supervisor, executor, reviewer, researcher | who may be named in `waiting_on` and questions; must include user and supervisor |
| `people` | `[]` | the people who answer, decide and own tasks, as written in task files (`["Alex", "Robin"]`): single words, no duplicates, no role names. With `[]` the board answers as `user`, and `owner` can only be `null` |
| `default_owner` | none | the person a task's `owner: null` means (`"Alex"`); must be one of `people`. Without it `null` means unowned and the owner filters have no default person |
| `host`, `port` | `127.0.0.1`, `5070` | web board address; loopback only (no authentication); `tb serve --port N` / `tb watch --port N` override the port |
| `refresh_seconds` | `5` | how often the web board checks for file changes |
| `watch.quiet_seconds` | `120` | the watcher reports after this long without further input |
| `watch.poll_seconds` | `5` | how often the watcher looks at the files |
| `watch.update_seconds` | `60` | seconds between the periodic updates of `tb serve` and `tb watch`; `0` = off; 1-9 are refused |
| `watch.update_paths` | `["AI"]` | the task folders (relative, inside the repo, no `..`). git: the folders whose uncommitted edits pause the update (git updates the whole checkout); svn: the folders that are updated, never the code |
| `watch.update_when_dirty` | `false` | `false`: no update while these folders hold uncommitted changes (untracked / unversioned files don't count, staged and added ones do); `true`: update anyway; git refuses a fast-forward that collides with a local edit (reported as a failure), svn merges and a conflict stops updating |
| `watch.board_idle_minutes` | `15` | a board started by `tb watch` stops after this many minutes without any request (`tb serve --idle-exit`); `0` = never |
| `handover` | none | size check for the supervisor's live-state file: `tb check` and `tb board` print a `warning:` line (never a problem, the exit code stays 0) for each measure over its limit and name by how much. Sub-keys: `path` (default `AI/SupervisorHandover.md`, relative to the repo root), `max_lines` (60), `max_chars` (6000), `max_line_chars` (160, a line over it counts as finished-work history). Without the key, or with the file missing, the check is silent. Nothing is trimmed automatically |
| `reload.budget_bytes` | `20000` | read by the SessionStart reload hook (`tb` only validates it: a whole number of at least 1): the most the hook prints of the files it injects, so Claude Code keeps the output inline instead of persisting it to a file. Handover files (names ending in `Handover.md`) are never dropped; another file that does not fit becomes a one-line pointer. Details in the hook's docstring and below |
| `type_rules` | a built-in `research` entry (the one below) | per-type overrides of the move rules, `{type: rules}`; a type without an entry follows the default flow. Details below |
| `sources` | - | task folders: `name`, `root`, `prefix` (default `T`, unique), `editable` (default false) |

### `type_rules`

Each key is one of `types`; its value may hold these keys (all optional; states are written as `03-Implementation`,
`03` or `implementation`):

| Key | Meaning |
|---|---|
| `moves` | `{state: [states]}`: replaces the allowed moves out of that state for this type (other states keep the default moves) |
| `refuse` | `{state: [states]}`: moves out of that state that are refused with a message naming the type; must not also be in the type's allowed moves |
| `no_branch` | `true`: the type moves into 03-Implementation without a `--branch` |
| `tokens_from` | `[states]`: a move into 07-Done from one of these states needs `--tokens` |

Backward moves still need a `--reason`, and the other preconditions (open questions, sections, the review
field for 04-CodeReview, a reason for landing) are unchanged. `tb show` lists the next states for the task's
type. An unknown type or key, an unknown state, or a `refuse` entry that is also an allowed move is a config
error. The built-in fallback (used only when `board.json` has no `type_rules` key; `{}` means no overrides) is
the `research` entry shipped here: research tasks have no branch, review or deploy.

```json
"type_rules": {
  "research": {
    "moves": {"03-Implementation": ["01-Refinement", "02-Ready", "05-UserReview", "07-Done", "08-Dropped"],
              "05-UserReview": ["01-Refinement", "02-Ready", "03-Implementation", "07-Done", "08-Dropped"]},
    "refuse": {"03-Implementation": ["04-CodeReview", "06-Deploy"], "05-UserReview": ["04-CodeReview", "06-Deploy"]},
    "no_branch": true,
    "tokens_from": ["03-Implementation", "05-UserReview"]
  }
}
```

Several sources: e.g. a private, untracked folder next to the project's tasks. A source that must not be named
in a tracked file goes into this checkout's `local.json` instead (below); it is appended to `board.json`'s:

```json
"sources": [
  {"name": "tasks", "root": "AI/Tasks", "prefix": "T", "editable": true},
  {"name": "private", "root": "PRIVATE_Tasks", "prefix": "P", "editable": true}
]
```

The folder needs the same nine state folders. Commands find the source from the id's prefix; `new` uses the
first source unless `--source` names another. `blocked_by` may name tasks of any source.

### `local.json` (per checkout, not versioned)

`{"me": "Alex"}` next to `board.json`, written by `tb me Alex`, ignored by version control
(`.claude/board/.gitignore` for git, which also ignores `log/`; `svn:ignore` for svn). It names the person
working in this checkout: the person every write of this checkout's web board is made as, and with open
ownership the default of `--answered-by` and `--decided-by`. `me` must be one of `people`; otherwise every
command stops with a config error until `tb me <name>` fixes it.

Optional `board_commits`: `true` (written by `tb me <name> --board-commits on`, `off` writes `false`) lets this
checkout's board commit its own answers and notes (Board commits); only coordinator checkouts set it, never the
supervisor's.

Optional `sources`: more task folders for this checkout only, same keys as in `board.json`, e.g.
`{"me": "Alex", "sources": [{"name": "private", "root": "PRIVATE_Tasks", "prefix": "P", "editable": true}]}`.
Names and prefixes must not clash with `board.json`'s sources. `tb me` keeps them when it changes `me`.

Optional `reload`: a list of repo-relative paths, e.g. `{"reload": ["notes/Handover.md"]}`. `tb` only validates it
(a list of non-empty relative strings without `..`); it is read by the SessionStart reload hook
(`.claude/hooks/reload.py`), which prints each existing file after the hook's own files in the same
`===== path =====` framing, and a one-line note for a file it cannot read. The hook also skips the body of a skill
(`.claude/skills/<name>/SKILL.md`) in its list on `compact` when the transcript shows that skill invoked (the harness
re-lists it itself), with the note `skill <name>: already in context`, and keeps its output within
`reload.budget_bytes` of `board.json` (handover files first in priority, a pointer for a file that does not fit, a
summary line at the end). A missing or invalid `local.json` changes nothing in the hook's output. `tb me` keeps
the key.

## Web board

`tb serve` (or `tb watch`, which starts it) -> http://127.0.0.1:5070/. One column per state (each scrolls on a
wide window; stacked on a phone), filters (source, priority, owner, "Waiting on: me / everyone"), a keyword search (id,
title, owner, text), the check's problems, and a page per task that follows the task when it moves. Cards of
tasks with an owner show `owner <name>`; the task page shows the owner after its path (`owner Alex (default)`
for `null`). The owner filter (with `people` only) uses the effective owner, so choosing the `default_owner`
includes the `null` tasks. The "Waiting on" dropdown (`-`, `me`, `everyone`; the choice is kept per browser in localStorage) and the
"waiting on you" badge: `me` is per person: with `people` configured and `tb me` set, a task waits on them when
it has an open question for the user addressed `@<them>:`, or one without an address and they own the task;
without people or without `tb me`, any open question for the user (`waiting_on: user`). `everyone` shows every
task with an open question for the user, for any person (any dev may answer any question). The board never changes
owners (owners are set through `tb set`). On a task outside 03-Implementation and
04-CodeReview anyone can answer any open question (questions for the user first, each tagged with its role) or
add a note. With `people` configured, every write is made as this checkout's `tb me` person (`local.json`): the forms
show it read-only ("Answering as Alex"), the header shows "You: <name>", and there is no person picker and
no cookie (each developer has their own checkout). Without `tb me` the board refuses writes with the message to
run `python .claude/board/tb.py me <name>`. The answer is written under that name
(`(answered <date>, Alex, for user)`, `- <date>, Alex: Q3 → ...`, Log `Alex (board): answered Q3.`).
A question whose text lists options as `(a) ... (b) ... (c) ...` (letters in order, at least two; they may span the
question's indented lines; `(recommended ...)` inside an option marks the recommended one) is answered with one
button per option and an optional comment; the answer is written as `<letter>: <option text>` (the recommended
marker left out), then `; <comment>` when given. A question without options keeps the free-text form.
The board writes only in a checkout of the main line, only when the file is unchanged since it was displayed,
never moves files and commits nothing except, in a coordinator checkout that opted in, the one task file of its
own answer or note (Board commits); requests must address it by a loopback host and its port.

Plans open read-only at `/plan/<file>` (files in `plans_dir` only), rendered as CommonMark with tables
(markdown-it-py; raw HTML is shown as text). A task's `plan:` field and every existing plan path in its text and
questions (`AI/Plans/....md`) link there; inside a plan, links to other plans and to task files lead to the plan
and to the task (by id). The header's "Light mode" / "Dark mode" button switches the theme (dark by default); the
choice is kept per browser in localStorage. The colour tokens live in `static/base.css`, the project's theme
file.

The header also shows the state of the periodic update ("Updated a1b2c3d, 14:02" / "Updated r123, 14:02",
"Update paused: waiting for local task edits to be committed and pushed (2 files)", "Update failed: git fetch
failed: ..., retrying" / "Update failed: E170013 ..., retrying", "Update stopped: local and origin diverged; pull
--rebase or push manually", "Update stopped: conflict in ...", "Update off: the checkout is on branch 't012-x',
not 'master'; ...") and, with board commits on, `Board commits on` (or `Board commits off: <vcs> is not on PATH`;
the board then only writes). `GET /api/update` returns the update state as JSON (`status`, `message`, `revision`
= the short HEAD commit or the working-copy revision, `vcs`, `last_attempt`, `last_ok`, `failures`,
`conflicts`, `text`, `level`). A board started with `--no-update` shows nothing and `/api/update` says `off`.
`GET /api/fingerprint` returns the file fingerprint the pages poll and `board`, this checkout's board identity
(Lifecycle). Open pages refresh through the file fingerprint; a form opened before an update that changed its
task is refused with "The file changed since you opened it" and keeps the typed text.

## Periodic update

`tb serve` and `tb watch` keep the checkout current: every `watch.update_seconds` the backend of `vcs` runs one
update. `taskboard/update.py` holds the schedule, the reporting and the header's state; the update itself and
its rules are in `taskboard/vcs/git.py` and `taskboard/vcs/svn.py`. The statuses are shared: `ok`, `changed`,
`conflict`, `dirty-skip`, `busy`, `error`, `no-vcs` (the client is not on PATH or the repo root is no
checkout: updating is disabled for the run), `off`; `off-branch` and `diverged` only happen with git.

### git

Every interval: `git fetch` and a fast-forward of the main branch from its upstream. git cannot update one
folder, so the whole checkout moves; uncommitted code edits elsewhere are fine unless the incoming commits
touch the same files, and then git refuses the fast-forward (reported as a failure; the tools never stash, reset
or force anything). The rules, in this order:

1. **Not on the main branch** (a feature branch, a detached HEAD): nothing happens, status `off-branch`
   ("Update off: the checkout is on branch 'x', not 'master'; updating resumes on master"). Not a failure.
2. **Unmerged paths** in the update paths (a merge or rebase the user runs left conflicts): status `conflict`,
   nothing is fetched or merged until the user finishes or aborts it with git.
3. **Local task edits pause it** (`update_when_dirty: false`): while the update paths hold uncommitted changes
   (staged or modified; untracked files don't count) nothing is fetched ("waiting for local task edits to be
   committed and pushed (N files)").
4. `git fetch --no-write-fetch-head --quiet` (plain `git fetch --quiet` on git older than 2.29), then the branch
   against `@{u}` (`git rev-list --left-right --count HEAD...@{u}`):
   - **ahead only** (unpushed commits): paused like local edits ("... (N unpushed commits)"); pushing ends it;
   - **ahead and behind** (diverged): status `diverged`, "local and origin diverged; pull --rebase or push
     manually"; nothing is resolved automatically; the watcher wakes the supervisor once (exit 3);
   - **behind only**: `git merge --ff-only @{u}` under the write lock, after the checks of 1-3 are repeated
     under it;
   - neither: `ok`.
   No upstream branch is a failure ("master has no upstream branch (git branch -u origin/master)").

Only the fast-forward holds the write lock (the fetch and the checks before it only read). Nothing can prompt
(`GIT_TERMINAL_PROMPT=0`, `GCM_INTERACTIVE=never`, no stdin); credentials are never read, stored or logged (git
uses whatever credential helper or SSH agent is already set up). A second updater's cycle is `busy` while git's
own `.git/index.lock` exists.

### svn

Every interval: `svn update --non-interactive --accept postpone <update_paths>` (default `AI`, never the code;
the working copy's `AI` is then often newer than the code folders, which is harmless: `sv sync`, `sv land` and a
full `svn update` equalize it, see `.claude/svn/README.md`). The rules:

- **A conflict stops it.** With a text, property or tree conflict in the update paths (`svn status`) nothing is
  updated. The tools never resolve anything. Recovery: `svn revert <file>` (gives back the incoming version),
  redo your change with `tb`, commit; updating resumes by itself.
- **Local edits pause it** (`update_when_dirty: false`): while the update paths hold uncommitted changes
  (unversioned `?` files don't count, added ones do), nothing is updated ("waiting for local task edits to be
  committed (N files)"). Committing the task edits ends the pause. This avoids the common conflict (two Log
  lines appended to the same task). With `update_when_dirty: true` the update merges, and a conflict stops it.
- The whole cycle (status, update) runs under the write lock. `--non-interactive` on every command: no prompt
  can hang the process; credentials are never read, stored or logged (cached credentials only). A second
  updater's cycle is `busy` on svn's working-copy lock (`E155004`).

### Both

- **One updater per checkout.** `tb serve` updates in a background thread; `tb watch` updates in its own loop
  and starts its board with `tb serve --no-update`. A second updater (a board started by hand) is harmless: the
  write lock and the client's own lock make a cycle `busy`, and it is retried at the next interval; `tb watch`
  prints a note when it finds such a board. `tb watch --once` and `tb board` never update. `tb serve` starts its
  update thread only when its port is free. When the port is taken (another board, e.g. a coordinator's and a
  supervisor's on one machine, or another program) `tb serve` exits 1 with a message naming the port, how to find
  the pid holding it (`netstat -ano | findstr :5070` on Windows, `lsof -i :5070` elsewhere) and the options
  (`--port N`, or `port` in board.json); `tb watch` shows it as `BOARD FAILED: ...` (exit 2). `tb watch` also
  refuses a board on its port that is not this checkout's (see Lifecycle). There is deliberately no automatic
  fallback port: two boards on one checkout would race on the same files.
- **The write lock.** Within the board process a web write waits at most 5 s for a running update and then
  fails with "Could not write the file ... being updated", keeping the typed text. Across processes it is an
  OS lock on the file `log/write.lock` (msvcrt.locking on Windows, flock elsewhere), held on an open handle;
  the file stays on disk. The OS releases the lock when the holder's process ends, so a crashed `tb` never
  blocks the others and there is nothing stale to clean up. A writer waits up to 5 s for it, then fails with
  "could not write" and you retry. `TB_WRITE_LOCK` overrides the lock path (the tests use it). Replacing a task
  file is retried for about a second while Windows reports it as open in another process (a reader, a virus
  scanner).
- **Failures** (offline, authentication, a refused fast-forward, timeout after 120 s): the board keeps working
  on the local files. The first failure and every new message are printed once, repeats are silent, recovery
  prints "update ok again"; after 3 failures in a row the interval backs off to 5 minutes.
- **Logs:** `tb serve` prints one line per state change (for the watcher-started board in `log/server.log`).
  Successful polls (`/api/fingerprint`, `/api/update`, `/static/`) are not logged; errors and other requests are.
  A `server.log` over 1 MB is moved to `server.log.1` (one backup) when the watcher starts the board.

## Watcher

`tb watch` (supervisor session, started in the background; see `.claude/skills/board-watch/`; a coordinator's
`tb watch --for <dev>`: see Coordinator watcher) wakes the supervisor by exiting:

- **exit 0** with `BOARD INPUT:` for new Log lines `- <date> <name> (board): ...` of this checkout's board (old
  `user (board):` lines included), and/or `INCOMING (git pull, <old7> -> <new7>):` / `INCOMING (svn update,
  rA -> rB):` for other devs' input that arrived with an update: new tasks, new open questions for the
  supervisor, tasks now waiting on the supervisor, and board lines written on another dev's board. Whoever moved
  the local revision counts (the watcher, the supervisor's own `git pull` or `svn update`, a board that updates
  itself): the watcher looks at it on every poll (git: `git rev-parse HEAD`, only while the checkout is on the
  main branch; svn: the working-copy revision of the first update path, `svn info`) and reports what is new
  since it moved. The supervisor's own `tb` edits (which don't move the revision) never count. With git, HEAD
  moves that are only commits made in this checkout (every `git reflog` entry since the last look is a
  `commit`) are the supervisor's own and never count, and after any other move only tasks whose files changed
  between the two commits count (`git diff --name-only`), so an uncommitted `tb move` made in the same poll as
  a pull is not input; switching to a feature branch and back is not input either (off the main branch the last
  state is kept). svn cannot tell those apart, so every new key of a moved revision counts (known limitation:
  the supervisor's own task, committed and updated within one poll interval, is reported once). While an update
  runs (git: `.git/index.lock`; svn: locked paths in `svn status`), or when the revision moves while the files
  are read, it keeps the last state and looks again at the next poll, so input is never absorbed half-way. A
  failed look at the revision after one was seen also keeps the last state. A lock present for about 100 s (20
  polls in a row) prints one note (`git index is locked; remove .git/index.lock if no git command is running` /
  `the update paths are locked; run svn cleanup if no svn command is running`) without exiting; it re-arms after
  the lock clears (a crashed command's stale lock). With `--no-update` none of this runs. After the first input
  it waits `quiet_seconds` without further input, so a burst arrives as one report; the `WAITING ON SUPERVISOR`
  list follows.
- **exit 3** with `UPDATE STOPPED: unmerged paths ...` / `UPDATE STOPPED: conflict in ...`, `UPDATE STOPPED:
  local and origin diverged; ...` (git) or `UPDATE FAILING: 3 <vcs> updates in a row failed (...)`. Each
  conflict, divergence or failure wakes the supervisor once: `log/watch-update.json` remembers it, so a restarted
  watcher keeps watching (and keeps the board running) instead of exiting again for the same one. A single
  failure only prints a line; a pause (local edits, unpushed commits) or `off-branch` never wakes it.
- **exit 4** with `SESSION GONE: <image> (pid N) ended; the watcher stops` when the session that started it is
  gone (see Lifecycle). Nobody normally reads it; a live session that sees it restarts the watcher.
- **exit 2** when the board can't be started, or with `BOARD FAILED: port 5070 is held by another task board
  (another checkout or project, or an older board version) or another program: stop it (its pid: ...) or use
  --port N` when the port answers but not with this checkout's board; **130** on Ctrl+C.

The first update runs at start, before the base is taken: input that arrived while no watcher ran is already in
the files (session start and `tb watch --once` show it). While nothing arrives the watcher prints nothing and
never exits; it costs no model usage.

## Board commits

In a coordinator's checkout (`local.json` `"board_commits": true`, set by `tb me <dev> --board-commits on`, which
the coordinator skill runs at session start) the board commits its own answers and notes right away
(`taskboard/commit.py`), so they reach the other checkouts with their next update and the board's own update
never pauses for them. Never in the supervisor's checkout: the supervisor's watcher (`tb watch` without `--for`)
always starts its board with `--no-commit`, and `tb serve --no-commit` overrides `local.json` for any board. When
`local.json` has the key on, the supervisor's watcher prints a note: a board already running in that checkout
that was started otherwise (by hand, or by `tb watch --for`) would commit. The header shows `Board commits on`
(or `Board commits off: <vcs> is not on PATH`; the board then only writes).

After an accepted write (the same guards as always) the board, under its write locks (no update or other write
interleaves):

1. checks the written file: when it has problems (`tb check` problems of that file; other tasks' problems don't
   block) nothing is committed and the page says so;
2. commits that one file with the message `tasks: <id> board input (<dev>)`, no attribution lines:
   - svn: `svn commit <file>` with the dev's cached credentials. The page says `Answer saved and committed
     (r2580).`;
   - git: `git add -- <file>` and `git commit -m ... -- <file>`, no push. The page says `Answer saved and
     committed locally (a1b2c3d); push with git push.` Until the coordinator pushes, the periodic update pauses
     for the unpushed commit as for any own commit;
3. svn only: when the commit is out of date (`E155011`, `E160024`, `E160028`, `E170004`) and the file had no
   local edits before the write, it reverts the file, updates it, redoes the same text edit on the incoming
   version, checks and commits once more (two Log lines appended to one file would conflict in a merge). When
   the incoming version refuses the edit (the question was answered meanwhile, the task moved or was deleted)
   the file stays as it came in and the page shows why, with the typed text. When the update of that file fails
   (locked working copy, offline), the edit is written again on the file as it is (saved, not committed), so it
   is never lost. A file that already had local edits (e.g. an uncommitted `tb ask`) is committed as a whole, but
   never reverted for a redo. A git commit is local and never out of date;
4. any other failure (offline, credentials, timeout after 120 s, a working-copy or index lock) keeps the write
   saved and uncommitted: `Answer saved, not committed: <message>. The coordinator commits it (svn commit AI /
   git commit -- AI, then git push).` The board's update then pauses as for any local edit, and the
   coordinator's watcher wakes for the line.

Known limit (svn): the redo's `svn update` of that one file brings other people's changes to it without moving
the revision of `AI`, so the coordinator's watcher does not report them as INCOMING; the board line itself still
wakes it, and its `WAITING ON <DEV>` list is complete. Other people's `(board)` lines in that file appear under
`BOARD INPUT` as if the dev had written them (they are new on disk, but no update brought them).

## Coordinator watcher

`tb watch --for <dev>` is the coordinator's watcher (`.claude/skills/coordinator/`), started in the background
like the supervisor's. Same loop, update, quiet period, exit codes, parent check and board identity check; it
starts the board only when none of this checkout runs, and that board follows `local.json` `board_commits`.
`--for` must name one of `people` (else exit 2). It exits 0 for:

- `BOARD INPUT:` every new line written on this checkout's board (the dev's own answers and notes, on any task);
- `INCOMING (...)`: what concerns the dev and arrived with an update: `T127 Q5 open, for Robin` (an open
  question for the user addressed `@Robin:`, or unaddressed on a task she owns: the board's "waiting on me"
  rule), `T136 now owned by Robin (01-Refinement)` (a task newly hers, also a new task created for her; `null`
  owners count for `default_owner`), `T184 Alex (board): answered Q2.` (another person's board line on a task
  she owns).

Nothing else wakes it: other devs' activity on their tasks, moves, agents' log lines, questions for the
supervisor, the coordinator's own `tb` edits. The report ends with `WAITING ON ROBIN:` (every task with an open
question for her) instead of `WAITING ON SUPERVISOR:`; `tb watch --for <dev> --once` prints only that list. The
supervisor's `tb watch` (without `--for`) is unchanged.

## Lifecycle

Who ends when (no process outlives its session for long, and the port is free again):

- **The watcher** records its parent-process chain at start (`taskboard/session.py`): the shells and launchers
  above it (`bash`, `sh`, `cmd`, `powershell`, `pwsh`, `python`, `py`, `env`, `timeout`, `winpty`, ...) and the
  first process that is not one of them (the Claude Code process, or the terminal when a person runs it), at
  most 6, each with its creation time (Windows: Toolhelp snapshot and `GetProcessTimes` through ctypes; Linux:
  `/proc/<pid>/stat`; elsewhere the immediate parent only). On every poll (5 s) it checks that each still runs;
  a pid reused by a new process has another creation time and counts as ended. When one ended it prints
  `SESSION GONE` and exits 4 without touching the board, so an orphaned watcher never restarts its board.
  `--no-parent-check` turns this off; when the chain can't be read it prints a note and runs as before.
- **The board `tb watch` starts** is detached (it survives the watcher's exit and restart between wake-ups) and
  runs with `--idle-exit` of `watch.board_idle_minutes` (15): any accepted request keeps it alive (the watcher's
  look at `/api/fingerprint` every poll, an open board or task page, which polls the same endpoint), and 15
  minutes after the last of them it logs `<time> idle for 15 min; the board stops` to `log/server.log` and
  exits 0. It stops under the board's write lock, so a write or an update in progress finishes first. A
  restarted watcher starts a new board when the old one ended (about 2 s; an open form then says the board was
  restarted and keeps the text).
- **A board started by hand** (`tb serve`) has no idle exit unless `--idle-exit` is given.
- **Limits:** an ancestor that can't be queried (access denied: another user's or an elevated process) counts
  as running, so the check can miss such a session's end. The idle thread starts just before the server serves;
  a stop due in that moment waits until serving began (only with test-size limits).
- **Identity:** `/api/fingerprint` returns `"board"`, a hash of the normalized config path (no path exposed).
  The watcher uses a board on its port only when that id is its own; another checkout's or project's board, a
  board of an older version (no id) or another program is `BOARD FAILED` (exit 2), never used silently.

## Layout

```
.claude/board/
  tb.py               entry point
  board.json          config
  local.json          this checkout's person, board commits opt-in, extra sources, reload list (tb me; not versioned)
  .gitignore          local.json, log/, .pytest_cache/, __pycache__/ (git boards)
  taskboard/
    model.py          parsing and the consistency check (plain data, reusable by any front end)
    textedit.py       pure text edits (answer, ask, log, set, owner transfer and migration, sections, new file)
    store.py          guarded atomic writes, the main-line guard and the versioned move (through the backend)
    workflow.py       allowed moves, preconditions, waiting_on per state
    cli.py            the commands
    watch.py          the watcher (supervisor; coordinator with --for)
    update.py         the periodic update's schedule, reporting and header state (the update is the backend's)
    commit.py         board commits of answers and notes (coordinator checkouts)
    session.py        the watcher's parent-process check (exit 4)
    idle.py           the idle exit of a watcher-started board
    winproc.py        no console window for git/svn children on Windows
    vcs/              the version control backends: __init__.py (interface, review field), git.py, svn.py
    web/              Flask app, templates, static (base.css holds the colour tokens), options.py (question options)
  tests/              pytest suite: python -m pytest .claude/board/tests -q (throw-away git and svn repositories)
  log/                server log of the watcher-started board, write.lock (OS-locked, stays), watch-update.json
```

Changing the flow means changing `workflow.py`, the state table in `AI/Tasks/README.md` and the move rules in
`AI/Methodology.md` together.
