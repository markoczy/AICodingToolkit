# AI Coding Toolkit

This repository generates a project-specific AI coding harness (supervisor + task board + agents + skills) from
templates. Read `README.md` first: it explains the layout, the template syntax and the questions.

- `/harness-setup` is the skill that asks the project questions and builds the zip.
- `template/` is the product. Every `.md`, `.json`, `.css` and `.txt` file in it is rendered by
  `build/build.py` (`{{name}}`, `{{#if expr}}...{{else}}...{{/if}}`); everything else is copied verbatim. An
  unknown `{{name}}` or a leftover `{{` fails the build, so always run `python -m pytest build/tests -q` after a
  template change: it builds the git and the svn variant.
- One rule, one place: a workflow rule lives once in a template, with `{{#if git}}`/`{{#if svn}}`,
  `{{#if multiuser}}`, `{{#if strict}}`, `{{#if analyst}}`, `{{#if deploy_prod|deploy_test|deploy_none}}` only
  where the mechanics differ. The vocabulary variables (`{{vcs_update}}`, `{{vcs_mv}}`, `{{review_ref}}`,
  `{{worktree}}`, `{{on_main}}`, ...) are defined in `build.context()`; add a variable there before using it.
- No project names, people or paths in `template/`: examples use `{{user}}` and `{{dev}}` (built from the
  answers) or neutral names (`Alex`, `Robin`) in the board code.
- The board tool `template/_claude/board/` and the svn tool `template/_claude/svn/` are plain code with their
  own pytest suites; run them after changing them (git, svn and svnadmin must be on PATH for the full suites).
- Line endings are LF (`.gitattributes`). Don't convert them.
- Generated output goes to `dist/` (ignored) or the scratchpad, never into `template/`.
