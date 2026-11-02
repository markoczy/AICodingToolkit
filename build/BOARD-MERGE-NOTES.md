# Board merge notes

What `template/_claude/board/`, `template/_claude/svn/` and `template/_claude/hooks/reload.py` contain, how the
two diverged copies of the `taskboard` package (a git project and an SVN project) were merged into one, and what
is left open.

## What was merged

Base: the git copy (every feature of it is kept: `type_rules`, `handover` limits, `reload.budget_bytes`,
local.json `sources` and `reload`, strict ownership with `--force --reason`, drop closes open questions, keyed
problems in the consistency check, option buttons on the web board). Ported from the SVN copy:

- board commits (`commit.py`, `tb me --board-commits on|off`, `tb serve --no-commit`, the header note);
- the session / idle lifecycle (`session.py`, `idle.py`, `cli.run_board`, `tb serve --idle-exit`,
  `tb watch --no-parent-check`, exit 4 `SESSION GONE`, `watch.board_idle_minutes` default 15);
- the coordinator watcher `tb watch --for <dev>` (`person_state`, `waiting_on`, `WAITING ON <DEV>`);
- board identity (`config.board_id`, `/api/fingerprint` returns `board`, `watch.board_state` / `ensure_board` /
  `foreign_message`), `rotate_log`, `pid_hint`, `quiet_handler_class`;
- open ownership (anyone sets the owner, Decisions line `owner: <new> (set by <who>, from <old>)`,
  `--answered-by` / `--decided-by` defaulting to `tb me` else `user`) as the `"ownership": "open"` mode;
- the SVN side of everything version-control-specific, as the `svn` backend.

Everything that touches git or svn now lives in `taskboard/vcs/` behind one interface; `store.py`, `update.py`,
`watch.py` and `commit.py` keep their public names and delegate.

## The backend interface (`taskboard/vcs/__init__.py`)

`backend(config)` returns the `Backend` of `config["vcs"]` (`backend_named("git" | "svn")`); `review_field(config)`
the `ReviewField` of the board. A backend provides:

| Member | git | svn |
|---|---|---|
| `name`, `default_main_branch` | `git`, `master` | `svn`, `trunk` |
| `review` (`ReviewField`: name, flag, parse, label, detail, missing text, landing example) | `pr`, `--pr`, `PR #77` | `rev`, `--rev`, `r12`, Log detail `review r12` |
| `available()`, `env()` | `git` on PATH; `GIT_TERMINAL_PROMPT=0`, `GCM_INTERACTIVE=never`, `LC_MESSAGES=C` | `svn` on PATH; `LC_MESSAGES=C`, every command `--non-interactive` |
| `main_branch_block(root, main_branch)` | branch of the folder's checkout, untracked folders allowed | `svn info` relative URL under `^/<main>` |
| `is_versioned(path)`, `move_file(repo_root, src, dst)` | `git ls-files`, `git mv` | `svn info --show-item kind`, `svn mv --force` |
| `update(config, lock_wait) -> UpdateResult` | fetch + ff-only (statuses incl. `off-branch`, `diverged`) | `svn update --accept postpone` of the update paths |
| `revision(config)`, `format_revision(rev)` | short HEAD, `a1b2c3d` | working-copy revision of the first update path, `r12` |
| `first_error(stderr)`, `conflicted(config)`, `conflict_message(paths)`, `conflict_banner(conflicts)` | `fatal:` / `error:` lines; unmerged paths | `E123456` codes; `C` columns |
| `update_running(config)`, `lock_note` | `.git/index.lock` | `L` column of `svn status` |
| `local_revision(config)` | full HEAD while on the main branch, else None | working-copy revision |
| `own_commits_only(config, old, new)`, `changed_tasks(config, old, new)` | reflog, `git diff --name-only` | always False / None ("unknown": every new key counts) |
| `incoming_header(revisions)`, `update_description(config, interval)` | `git pull, a -> b`; "git fetch + fast-forward of master every N s, paused while AI has uncommitted or unpushed edits" | `svn update, rA -> rB`; "svn update of AI every N s" |
| `commit_file(config, path, message) -> (revision, error, stale)` | `git add -- <file>`, `git commit -m <msg> -- <file>`, never stale | `svn commit <file>`, stale on the out-of-date codes |
| `file_is_clean`, `revert_file`, `update_file`, `supports_redo` | status / `git checkout HEAD --`; no redo | status column / `svn revert` / `svn update --accept postpone`; redo on |
| `commit_note(revision)`, `commit_revision_pattern()`, `commit_by_hand` | `committed locally (<sha7>); push with git push` | `committed (r<n>)` |

`UpdateResult` (shared) lives in `vcs/__init__.py` and is re-exported by `update.py`. The status vocabulary is
shared: `ok | changed | conflict | diverged | dirty-skip | off-branch | busy | error | no-vcs | off` (`diverged`
and `off-branch` only with git; `no-git` / `no-svn` of the old copies became `no-vcs`). `Updater.state` carries
`vcs` so `banner()` formats revisions and the conflict text per backend.

Callers: `store.apply_edit(..., vcs=)`, `store.main_branch_block(root, main_branch, vcs=)`, `store.move_file(...,
vcs=)`, `store.is_versioned(path, vcs=)` (default `git`); `update.run_update(config)`, `update.conflicted(config)`;
`watch.local_revision / update_running / own_commits_only / changed_tasks` delegate; `commit.write_and_commit` is
generic; `workflow.move_problems(..., review=)` and `textedit.new_task_text(..., review_field=)` take the review
field; `model.check_tasks` reads `schema["review_field"]` (`config.schema_of` sets it) and `model.format_board`
takes `review_field`; `textedit.set_owner(..., transfer=)` writes the strict (`transferred from`) or the open
(`set by`) Decisions line.

## Config keys

New in `board.json`: `vcs` (`git` | `svn`, default `git`; chooses the backend and the review field),
`ownership` (`strict` | `open`, default `strict`), `watch.board_idle_minutes` (default 15). `main_branch`
defaults to `master` for git and `trunk` for svn. Kept: `type_rules`, `handover`, `reload.budget_bytes`.
Both new keys are validated like the others (a config error names the allowed values). `local.json`: `me`,
`sources`, `reload`, `board_commits`.

The other person's `board.json` template already uses `vcs`, `main_branch` and `ownership` with the same
values; the board tests that load the packaged `board.json` skip while it holds template tags.

## Web board

The git copy's option buttons plus the SVN copy's commits note (`commits_text` in `base.html`), the activity
touch for the idle exit and `board` in `/api/fingerprint`. The committed message is built from the backend
(`Answer saved and committed (r12).` / `Answer saved and committed locally (a1b2c3d); push with git push.`), the
not-committed hint from `commit_by_hand`. `base.html`'s update banner title says "Periodic update of the
checkout". `static/base.css` (the colour tokens) is not shipped here: the other person writes it; the two tests
that need it skip when it is absent (static-file tests use `board.css`).

## Tests

Both suites merged (700 passed, 4 skipped on this machine with git 2.56 and svn 1.14 on PATH; the 4 skips are
the packaged-config tests, which skip while `board.json` holds template tags, and the `base.css` token test):

- `test_update.py`: git classes (`TestGitUpdate`, `TestFailures`) and svn classes (`TestSvnUpdate`,
  `TestSvnFailures`) with `HAVE_GIT` / `HAVE_SVN` skips, shared config / write-lock / watcher / serve tests on a
  git board plus svn variants where the output differs (INCOMING header, banner, log lines, lock column);
- `test_cli.py`: the git copy's tests plus `TestSvnGuards` (svn repository), `TestReviewFieldPerVcs` (`--pr` /
  `--rev`, `set pr= / rev=`, `show`, `board`), `TestOpenOwnership`, `TestBoardCommitsOptIn`;
- `test_web.py`: `TestGitGuard` and `TestSvnGuard` (monkeypatched backends), `run_board` serve tests;
- `test_coordinator.py`: the SVN copy's watcher and board-commit tests (`TestBoardCommitsSvn`) plus
  `TestBoardCommitsGit` (local commit, unpushed pause, failed commit, write lock);
- `test_lifecycle.py`, `test_winproc.py` (git and svn children, no import outside the package),
  `test_model.py` (+ `TestReviewField`), `test_reload_hook.py` (finds `../../hooks/reload.py` relative to the
  tests folder, which is `template/_claude/hooks/reload.py`).

Fixtures in `tests/fixtures/tasks/` were rewritten with neutral titles and the people `Alex` / `Robin`; they keep
the old formats the tests exercise (no owner field, the old answered-question form, the `owner: Robin` decision).

`template/_claude/svn/tests/test_sv.py`: 37 passed (throw-away repository with svnadmin).

## Smoke test

A temp git repo (with a bare origin) and a temp svn repo + checkout, each with a board copy and a minimal
`board.json`: `tb me`, `new`, `section`, `ask --to`, `answer`, `move --branch`, `move codereview` with the wrong
flag (refused, names the vcs) and the right one, `check`, `board`, `show`, `watch --once`, `serve --idle-exit 1`
(`TB_IDLE_SECONDS=3`: answered `/api/fingerprint` with the board id, served the board page, stopped by itself with
exit 0) all behaved on both.

## Left open

- `static/base.css` must come from the other person's theme (the templates link it; without it the board has no
  colour tokens).
- `template/_claude/board/gitignore` is the git copy's `.gitignore` (the build renames it); an svn project needs
  the equivalent `svn:ignore` on `.claude/board` (`local.json`, `log`), which nothing here sets.
- The svn backend answers "unknown" for `own_commits_only` / `changed_tasks`, so an svn supervisor's own task,
  committed and updated within one poll, is reported as INCOMING once (the SVN copy's known limitation).
- git board commits are local only (no push); the periodic update pauses until the coordinator pushes, as for
  any own commit. A git redo (out-of-date) never happens.
- `config._handover` now also rejects a `handover.path` starting with `/` or `\` or a drive letter: Python 3.13's
  `os.path.isabs` no longer treats `/etc/x.md` as absolute on Windows, which made the git copy's test fail here.
- The board's test suite takes about 3.5 minutes (real git and svn repositories, real server processes).
