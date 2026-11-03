# AI Coding Toolkit

A toolkit that generates a **multi-agent software development harness for Claude Code** for one project: a
supervisor session that keeps the big picture with the user, a file-based task board with a local web UI and a
watcher, executor / reviewer / researcher (and optionally analyst) agents, skills for every step of the flow,
and the version-control plumbing for **git** (branches, worktrees, pull requests) or **Subversion** (branches,
working copies, landing with `sv`).

The harness itself is documented inside what it generates (`AI/Methodology.md` is the full definition). This
repository holds the templates, the builder and the setup skill.

## Use it

In Claude Code, in this repository: `/harness-setup`. The skill asks the project questions (listed in
`build/questions.json`), builds the harness and writes `dist/<project>-harness.zip`. The zip's `SETUP.md`
explains the steps in the target checkout.

Without Claude Code:

```bash
python build/build.py --example git > answers.json        # or svn; edit the answers
python build/build.py --answers answers.json --out out/myproject --zip dist/myproject-harness.zip
```

`python build/build.py --questions` prints the question schema, `--check answers.json` validates answers and
prints the derived template context.

## What the questions decide

| Question | Effect |
|---|---|
| `vcs` git / svn | branch, worktree, review and landing vocabulary in every document and skill; `.claude/svn/` (the `sv` tool and its hooks) only for svn; `pr` or `rev` as the task header's review field; the board's `vcs` backend |
| `main_branch` | `master`, `main`, ... (git only; svn is always `^/trunk`) |
| `mode` single / multi | multi adds the `coordinator` skill (a developer's session next to the supervisor), the ownership rules, board commits and the coordinator watcher to the documents |
| `ownership` strict / open | strict: the owner decides, only the owner transfers, `--answered-by` is mandatory; open: anyone may refine, approve and answer any task and change its owner (the board's `ownership` key) |
| `analyst` | adds the analyst agent and the "data impact" measurement to the reviewer, the plans and the delegation templates |
| `deploy` production / test / none | what 06-Deploy means in the state table, the landing skill and the supervisor's queue |
| `task_types`, `port`, `python`, colours, `confidential` | board config, CSS tokens, the command every skill uses, the confidentiality rule's wording |
| advanced: `compact_window`, `idle_compact_tokens`, `watch_quiet_seconds`, `watch_update_seconds`, `board_idle_minutes`, `reload_budget_bytes` | the compaction window and idle compaction in `settings.json` (every session re-reads its whole context at each turn, so the window is the main cost lever), the watcher timings and the reload budget in `board.json`; the skill shows them as one table with their defaults at the end and asks for an OK |

## Layout

```
build/
  build.py            the builder: template engine, answer validation, derived context, zip
  questions.json      the questions (id, prompt, type, options, default, when)
  tests/              pytest for the engine and smoke builds of both variants
template/             the harness, with {{...}} tags in the rendered files (.md, .json, .css, .txt, gitignore)
  CLAUDE.md, SETUP.md, harness.gitignore (git), svn-ignore.txt (svn)
  AI/                 Context.md (skeleton), Methodology.md, SupervisorHandover.md, Plans/, Tasks/
  _claude/            settings.json, hooks/reload.py, agents/, skills/, board/ (tb), svn/ (sv, svn only); becomes .claude/ in the build
.claude/skills/harness-setup/   the skill that asks and builds
dist/                 generated zips (ignored)
```

Template syntax: `{{name}}` for a value, `{{#if expr}} ... {{else}} ... {{/if}}` for conditional blocks (a tag
alone on its line takes the line with it; tags also work inline). The flags and values are built in
`build.context()`; an unknown name fails the build. Files that are not rendered (Python, JavaScript, Jinja
templates) are copied as they are; the board reads its configuration from `board.json` at run time.

## Develop the toolkit

```bash
python -m pytest build/tests -q                       # engine + smoke builds
python -m pytest template/_claude/board/tests -q      # the board tool (git and svn backends; needs git, svn, svnadmin)
python -m pytest template/_claude/svn/tests -q        # the svn tool (creates a throw-away repository)
```

Rules: change a workflow rule in `template/` once, for both variants (use `{{#if git}}` only where the
mechanics differ); no project names in templates (use the context's `{{user}}` and `{{dev}}`); the board
package stays VCS-neutral through `taskboard/vcs/`; keep `build/BOARD-MERGE-NOTES.md` current when the board
changes. Line endings are LF everywhere (`.gitattributes`).
