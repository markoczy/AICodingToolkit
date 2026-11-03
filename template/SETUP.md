# Setting up the AI coding harness in {{project}}

This bundle was generated for: **{{vcs_name}}**, main line `{{main}}`, {{#if multiuser}}multi-user ({{#if strict}}strict{{else}}open{{/if}} ownership; people: {{people_json}}){{else}}single user ({{user}}){{/if}},
{{#if analyst}}with{{else}}without{{/if}} the analyst agent, deploy target: {{deploy_where}}. The answers are in `harness-answers.json`
(keep it: it rebuilds the same harness from the toolkit after an update).

## 1. Copy the files

Unzip into the root of the project's main checkout ({{checkout_phrase}}). Nothing is overwritten
unless the project already has a file of the same name; `CLAUDE.md` is the one to merge by hand if it exists.
Files:

- `CLAUDE.md` (entry point for Claude Code), `AI/` (Context, Methodology, Tasks, Plans, SupervisorHandover),
- `.claude/settings.json` (permissions and hooks), `.claude/hooks/reload.py`, `.claude/agents/`, `.claude/skills/`,
- `.claude/board/` (the task board tool `tb`){{#if svn}}, `.claude/svn/` (the SVN workflow tool `sv`){{/if}}.

Delete `SETUP.md` and this bundle's `harness-answers.json` from the checkout when done, or keep the answers file
somewhere outside the repository.

## 2. Ignore the local files

{{#if git}}
Append the lines of `harness.gitignore` to the project's `.gitignore`, then delete `harness.gitignore`. The
board's own `.claude/board/.gitignore` is already in place (local.json, log/).
{{else}}
Run the `svn propset` commands in `svn-ignore.txt` once in the trunk checkout and commit; then delete the file.
{{/if}}

## 3. Install the board's dependencies

```bash
pip install -r .claude/board/requirements.txt
```

(Flask and markdown-it-py for the web board, pytest for the tests. The CLI and the watcher need only the standard
library; `{{python}}` must be Python 3.10 or newer.{{#if git}} `git` and the GitHub CLI `gh` (logged in) on PATH.{{else}} The `svn`
command line client on PATH (TortoiseSVN's command line tools are enough).{{/if}})

Check: `{{python}} -m pytest .claude/board/tests -q`{{#if svn}} and `{{python}} -m pytest .claude/svn/tests -q` (the latter
creates a throw-away repository with `svnadmin`){{/if}}.

## 4. Fill in the project context

`AI/Context.md` is a skeleton with TODO markers: the project description, structure, core design ideas, coding
guidelines, testing, documentation rules, build/run/deploy{{#if has_deploy}} (how {{main_ref}} reaches {{deploy_where}} and how the result is
checked){{/if}}. Every executor and reviewer reads it completely at the start of every run; keep it under ~20 KB.
`/init` in Claude Code gives a first draft; shorten it to the rules an agent must not break.

Review `.claude/agents/*.md` once: the templates speak of tests, docs and data in general terms; add the one or
two project-specific lines that matter (e.g. the test command, the reference data agents may use read-only).

## 5. First commit and first session

1. Set your name once per checkout: `{{python}} .claude/board/tb.py me {{user}}`.
{{#if git}}
2. `git add CLAUDE.md AI .claude .gitignore && git commit -m "AI coding harness" && git push`.
{{else}}
2. `svn add --force CLAUDE.md AI .claude && svn commit -m "AI coding harness"`.
{{/if}}
3. `{{python}} .claude/board/tb.py me {{user}} --role supervisor` (the reload hook then injects the supervisor role
   into every Claude Code session of this checkout, also after `/clear`; the environment variable
   `CLAUDE_ROLE=supervisor` does the same for one launch).
   Start Claude Code in the checkout and run `/supervisor`. It reads Context, Methodology and the handover,
   starts the watcher (which starts the web board at http://127.0.0.1:{{port}}/) and works the board. Record the
   first tasks with `/task-new` or by telling the supervisor what needs doing.
{{#if multiuser}}
4. Each other developer: {{own_checkout}}, `pip install` as above, `tb me <their name>`,
   `tb me <their name> --board-commits on`, then `/coordinator` in Claude Code. New developers are added to
   `people` in `.claude/board/board.json` by the supervisor's user first.
{{/if}}

Token usage: `.claude/settings.json` caps every session's automatic compaction window at {{compact_window_k}} tokens
(`autoCompactWindow`) and lets an idle session compact itself; a long-lived supervisor whose context is re-read
at every wake-up costs a multiple of its context size, and the handover plus the reload hook make compaction and
`/clear` safe at any time. Change the cap with `/autocompact <tokens>` (per user) or in the setting.

## 6. Keeping the harness current

The harness is generated from a toolkit. To change a rule for every project, change it in the toolkit's
templates and rebuild with `harness-answers.json`; to change it for this project only, edit the files here (the
next rebuild then needs a merge). The board tool and its tests are plain code: fix them here and port the fix
back to the toolkit.
