# {{project}} — Agent Context

This is the entry point for an AI agent (or any contributor) working on this codebase. Read this first. It
covers the system's design in short, its conventions, and the rules for changing it; the detail sits next to the
area it describes and is linked from here. Keep it under about 20 KB: the executor and the reviewer read it
completely at the start of every run, so every paragraph costs tokens on every task.

> **TODO (project setup):** the sections below are the skeleton the harness expects. Fill them in for this
> project (the `/init` command of Claude Code gives a first draft of the structure and conventions; shorten it
> to what an agent must not break) and delete this note. The headings are referenced by the agents and skills:
> keep "Coding guidelines", "Testing", "Documentation", "Build, run and deploy" and "Confidentiality".

## What {{project}} is

One paragraph: what the software does, for whom, and what matters most when changing it (correctness, speed,
compatibility, ...).

## Project structure

```
{{project}}/
├── .claude/                # agents/, skills/, hooks/ (session reload), board/ (the task board tool `tb`){{#if svn}}, svn/ (the SVN workflow tool `sv`){{/if}}
├── AI/
│   ├── Context.md            # this file
│   ├── Methodology.md        # roles, ground rules, task flow, branches and reviews, plans
│   ├── SupervisorHandover.md # the supervisor's live state (work in progress, agents, open decisions)
│   ├── Plans/                # point-in-time design documents (not living docs)
│   └── Tasks/                # one file per task, one folder per state (README.md has the rules)
└── ...                     # TODO: the source, test, doc and config folders of this project
```

## Core design ideas

Each idea in a few lines, with the rule an agent must not break and a link to the detail.

### TODO: first idea

## Coding guidelines

- TODO: the conventions that are not obvious from the code (naming, error handling, logging, dependencies,
  what never goes into the repository).
- **No new tool versions**: don't create `my_tool_v2` next to `my_tool`; modify the existing one in place unless
  explicitly asked to keep both.
- **Comments**: sparse, only for non-obvious logic; never a comment that restates the code or refers to the
  current chat or task.
- **Ask before implementing** when a request is ambiguous or leaves a real design choice open, rather than
  guessing and building the wrong thing.

## Testing

- TODO: where tests live, how they are organized, how to run the whole suite and one file.
- Untested new functionality is not acceptable: every behavior change has a test, every bug fix a regression
  test.
- No live network calls and no dependency on real production data in tests.

```bash
# TODO: the command that runs the full suite
```

## Documentation

- TODO: which documents must be updated in the same change as which kind of code change.
- `AI/Tasks/` holds the tasks: one Markdown file per task in one folder per state (`00-Conception` to
  `08-Dropped`). `AI/Tasks/README.md` defines the states, the file format and who edits what;
  `{{python}} .claude/board/tb.py board` lists the board and checks the files. Check the tasks of an area before
  touching it. Task files change {{on_main}} only: a task branch never edits, adds or moves them. Something you
  find but won't fix now goes into your hand-back as a proposed task; the supervisor records it.
- **Working model with agents.** `AI/Methodology.md` defines it: a supervising session keeps the big picture
  with the user and delegates to the **executor** / **executor-light** (implement one task on its branch, never
  land), the **reviewer** (verdict with evidence){{#if analyst}}, the **analyst** (data questions on a read-only copy){{/if}} and
  the **researcher** (reading-heavy questions). Agent definitions: `.claude/agents/`.

## Build, run and deploy

- TODO: how to build and run the software locally.
{{#if deploy_prod}}
- TODO: how `{{main}}` is deployed to production and how the result is checked (the private details, host names
  and credentials stay outside the repository; this section names only the steps and the check).
{{/if}}
{{#if deploy_test}}
- TODO: how {{main_ref}} is deployed to the test server and how the result is checked there (host names and
  credentials stay outside the repository). Production releases are separate `ops` tasks.
{{/if}}
{{#if deploy_none}}
- Nothing is deployed from this repository; a landed task is verified on {{main_ref}}.
{{/if}}

## Confidentiality

Material that must never appear in {{tracked}} files, commit messages or agent reports: {{confidential}}. The
supervisor may give such material to an agent in its prompt for context, with the instruction never to write it
anywhere {{tracked}}. Credentials and passwords are never given to AI tools at all.
