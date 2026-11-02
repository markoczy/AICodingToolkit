# sv: SVN workflow tool

The SVN side of the AI workflow. It does what git branches, worktrees and pull requests do in the original
methodology kit: one branch per task, an isolated working copy per agent, a review diff, and landing in trunk.
Standard library Python 3.10+ and the `svn` command line client (TortoiseSVN's command line tools are enough).

`sv` = `python .claude/svn/sv.py` (from the repository root, or with the full path).

## Layout

| Where | What |
|---|---|
| `^/trunk` | The main line. Task files, plans and landed work. The main checkout (the supervisor's) is always on trunk. |
| `^/branches/<id>-<slug>` | One branch per task, e.g. `t012-fix-login`. Created by the supervisor, deleted when it lands. |
| `<main>.worktrees/<name>` | Working copies for agents, next to the main checkout `<main>`, e.g. `<main>.worktrees\agent-a1b2`. Never inside the main checkout. |

## Commands

| Command | Who | Does |
|---|---|---|
| `sv branch create <name> [-m]` | supervisor | copies `^/trunk` to `^/branches/<name>`; refuses when the branch already exists, also when it appears between the check and the copy (`svnmucc cp` fails on an existing target; without svnmucc, `svn copy` is used and a nested `trunk` copy is removed again) |
| `sv branch delete <name> [-m]` / `sv branch list` | supervisor | delete / list task branches |
| `sv wt add <name> [--branch b]` | anyone | check out trunk (or a branch) into the worktrees folder; prints the path |
| `sv wt list` | supervisor | worktrees with their line, revision and number of uncommitted changes |
| `sv wt remove <name\|path> [--force]` | supervisor | delete a worktree; refused while it has uncommitted changes (incl. unversioned files) |
| `sv switch <branch>` | executor | switch its worktree from trunk to the task branch (refused in the main checkout) |
| `sv sync` | executor | `svn update` + merge new trunk revisions into the branch working copy (`AI/Tasks` excluded, see below); reports conflicts |
| `sv commit -m "<msg>"` | executor | commit the branch working copy; refused when a worktree is on trunk; prints the revision |
| `sv diff <branch> [--summarize]` | reviewer | what the branch changes against trunk (`AI/Tasks` not listed); warns when the branch is not synced with trunk |
| `sv land <branch> -m "<msg>" [--keep-branch]` | supervisor | in the main trunk checkout: update, merge the branch, commit (one trunk commit per task), delete the branch; `AI/Tasks` excluded; on real conflicts the merge is reverted |

The board runners (`tb serve`, `tb watch`) also run `svn update` periodically in the trunk checkout they serve,
but of `AI/` only (`.claude/board/README.md`, "Periodic svn update"): the code is never updated under a running
build. The resulting mixed-revision working copy is harmless; `sv sync`, `sv land` and a full `svn update`
equalize it.

## Task files are never merged

Task files (`AI/Tasks/**`) change on trunk only, through `tb`. `sv sync` and `sv land` therefore run the normal
`svn merge` and then undo everything it did under `AI/Tasks` (`svn revert -R`, which also clears tree conflicts
there, and deletion of the files the merge had added): a sync never pulls board moves into a branch, and a land
never pushes a stale or moved task file into trunk, so task-file tree conflicts cannot occur. The merge is still
recorded in `svn:mergeinfo` on the working copy root, so later merges and the "not merged" warning of `sv diff`
stay correct. `sv diff` (also `--summarize`) does not list `AI/Tasks`. Everything else, including the rest of
`AI/`, merges as before; a real conflict outside `AI/Tasks` still stops (sync: reported for resolving; land: merge
reverted). A branch that only touched task files lands as a mergeinfo-only commit.

Every `svn merge` runs with `--non-interactive --accept postpone`: a tree conflict on a moved task file is then
postponed (local missing, local obstruction, incoming delete or move) instead of sending svn 1.14 into its
interactive resolver, which crashed (access violation, or `E235000 ... token.c`) without a terminal. The `AI/Tasks`
drop clears those postponed conflicts and the leftover files; unversioned files that were in `AI/Tasks` before the
merge are left alone. `E235000` is handled like `E155015` should svn still stop with it.

## Claude Code hooks (`.claude/settings.json`)

- **WorktreeCreate** → `sv hook worktree-create`. Replaces git worktrees. An agent started with
  `isolation: "worktree"` gets a fresh **trunk** checkout in the worktrees folder (the path is printed as the last
  stdout line). The executor then runs `sv switch <branch>`.
- **WorktreeRemove** → `sv hook worktree-remove`. Deletes a worktree only when `svn status` is clean; otherwise it
  is kept (exit 1). Claude Code can't see SVN changes, so this check is the only protection.
- **PreToolUse (Bash, PowerShell)** → `sv hook guard`. Inside a worktree, a raw `svn commit`/`svn ci` is blocked;
  agents commit with `sv commit`, which refuses trunk.

Claude Code does **not** remove an agent's worktree when the agent finishes. The supervisor removes it after
landing (`sv wt remove <name>`), and checks `sv wt list` at session start.

## Tests

```bash
python -m pytest .claude/svn/tests -q
```

They create a throw-away repository with `svnadmin` in a temp folder, and refuse to run unless every default
(server URL, worktrees folder) points there.
