---
name: harness-setup
description: Generate the AI coding harness (CLAUDE.md, AI/ methodology and task board, agents, skills, hooks, board tool, svn tool) for one project as a zip - asks the project questions (git or svn, single or multi-user, ownership strictness, analyst agent, deploy target, board colours, port, python command, confidentiality), builds from the templates and hands over the zip with the setup steps. Use when the user wants to set up, create, generate or rebuild the harness for a project ("create the harness for X", "set up the workflow for my repo", "build the kit", "new project harness").
---

# Harness setup

You build a project-specific harness from this toolkit's templates. The questions, their order and their
defaults are in `build/questions.json`; the builder is `build/build.py`. Never answer a question for the user.

## 1. Ask the questions

1. Print the question list: `python build/build.py --questions` (JSON: `id`, `prompt`, `type`, `options`,
   `default`, `when`). Ask them in that order, in the user's language. Skip a question whose `when` is false
   for the answers so far (e.g. `people` and `ownership` only for `mode == 'multi'`, `main_branch` only for
   git). Offer the options as (a), (b), (c) and name the default, so one letter or "default" answers.
2. Ask several questions per message where they are independent (project name, vcs and mode together; then
   the people and ownership; then analyst, deploy and task types; then colours, port, python and
   confidentiality). Keep each message short.
3. If the user already gave answers in their request or has a `harness-answers.json` from an earlier build,
   use those and ask only what is missing or what they want to change.

## 2. Build

1. Write the answers to the scratchpad as `answers.json` (keys = question ids; lists as JSON lists, booleans as
   true/false).
2. Validate: `python build/build.py --check <answers.json>`; on an error, fix the answer with the user.
3. Build into the scratchpad and zip it into the working directory's `dist/` folder:

```bash
python build/build.py --answers <scratchpad>/answers.json --out <scratchpad>/harness --zip dist/<project-slug>-harness.zip --force
```

   (`dist/` is git-ignored; `<project-slug>` is the project name in lowercase with dashes.)
4. Check the build: `python -m pytest <scratchpad>/harness/.claude/board/tests -q` must pass (and
   `.claude/svn/tests` for svn); `grep -r "{{"` over the `.md` and `.json` files of the output must find
   nothing outside `.claude/board/taskboard/web/templates/`.

## 3. Hand over

Tell the user, in a few lines: the zip path, what the bundle contains for their answers (vcs, mode, ownership,
agents, deploy target), and that `SETUP.md` inside the zip lists the steps (unzip into the main checkout,
ignore rules, `pip install`, fill `AI/Context.md`, `tb me`, first commit, `/supervisor`). Offer to walk through
the setup in the target checkout if they name it. Keep `answers.json` next to the zip (copy it into `dist/`
as `<project-slug>-answers.json`) so the harness can be rebuilt after a toolkit update.
