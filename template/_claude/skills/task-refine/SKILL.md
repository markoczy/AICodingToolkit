---
name: task-refine
description: Take a task from idea to 02-Ready - work out approach and acceptance, ask and record questions and answers, commission and approve a plan for non-trivial work. Use for tasks in 00-Conception or 01-Refinement, and whenever the user answers a question or makes a decision about a task in chat.
---

# Refinement

`tb` = `{{python}} .claude/board/tb.py`. {{#if git}}On `{{main}}`{{else}}In the trunk checkout{{/if}} only.

## 1. Start

- `tb show <id> --full`. Read the related tasks and, briefly, the code area (or have the researcher{{#if analyst}}, analyst{{/if}} or
  executor look, if it's more than a few files).
- From 00: `tb move <id> refinement`.

## 2. Work out the solution

- `tb section <id> approach --file <tmp>`: how it will be solved, and what is **out of scope**.
- `tb section <id> acceptance --file <tmp>`: how we know it's done, as checkable statements, including what to
  verify {{#if has_deploy}}after deployment{{else}}once it landed{{/if}} and what result to expect.
- Use `### ` sub-headings inside a section if needed (never `## `).

## 3. Questions

- One question per decision the user must make, answerable with a letter:
  `tb ask <id> --for user "<question> (a) ... (recommended, because ...) (b) ... (c) ..."`. Ask several at once
  rather than one by one. The web board turns the options into buttons.
- Questions you'll answer yourself, or that an agent must answer: `--for supervisor` / `--for executor`.
- Tell the user only where the questions are ("T012 Q3-Q5 on the board"), not their text (ground rule 1); the
  watcher wakes you when they answer there.

## 4. Record answers and decisions

{{#if strict}}
- Anyone may answer any open question; the answer is recorded under the answerer's name. `--answered-by` and
  `--decided-by` are mandatory (a person or a role); there is no default, so a missing flag cannot attribute your
  entry to the wrong person.
- Answer given in chat: `tb answer <id> Q<n> "<answer as it was said, expanded: 'a: <option text>'>" --via
  chat --answered-by <who answered>`. (Answers from the web board are already recorded under the answerer's
  name; just read them.)
- Your own answer to a supervisor question: `tb answer <id> Q<n> "..." --answered-by supervisor --no-decision`.
- A decision that wasn't a numbered question: `tb decide <id> "<what was decided>" --decided-by <who
  decided>`.
{{else}}
- Anyone may answer any open question; the answer is recorded under the answerer's name. `--answered-by` and
  `--decided-by` default to this checkout's person (`tb me`, {{user}} in the supervisor's checkout).
- Answer given in chat: `tb answer <id> Q<n> "<answer as it was said, expanded: 'a: <option text>'>" --via
  chat`{{#if multiuser}}; when another dev answered, name them: `--answered-by {{dev}}`{{/if}}. (Answers from the web board are
  already recorded under the answerer's name; just read them.)
- Your own answer to a supervisor question: `tb answer <id> Q<n> "..." --answered-by supervisor --no-decision`.
- A decision that wasn't a numbered question: `tb decide <id> "<what was decided>"` (add `--decided-by <name>`
  when someone other than `tb me` decided).
{{/if}}
- Never ask again what a task file already answers. If an answer is ambiguous, ask a new, sharper question.

## 5. Plan (non-trivial work only)

Non-trivial = new behavior, changed output or format, data migration, more than one area, or a real design
choice (see `AI/Methodology.md`, "Plans").

1. Delegate the plan to the **executor** (`task-delegate` skill, "plan only" brief), while the task stays in 01.
2. When it returns: move its plan file from `AI/Temp/plans/` to `AI/Plans/`{{#if svn}} (`mv`, then `svn add`){{/if}}. Read its
   summary sections (design, alternatives, impact, effort), not more. `tb set <id> plan=<path>`; copy the plan's
   open questions into the task (`tb ask`, with options).
3. Summarize for the user in a few lines (approach, impact, effort) and ask for approval:
   `tb ask <id> --for user "Approve plan <path>? (a) yes (b) changes: ..."`.
4. On approval: `tb answer <id> Q<n> "a: approved" ...` (or `tb decide`). Set the plan's status line to
   "approved <date> by <person>" and commit it with the task file.

## 6. Move to Ready

`tb move <id> ready`. The CLI refuses while a question is open, a section is empty or the task is blocked; fix
what it lists. Then `tb check`, {{commit_push}}: `tasks: T012 01-Refinement → 02-Ready`.

02-Ready is not a start: the task starts only after {{#if open}}someone's start approval (anyone's, ground rule 2){{else}}its owner's start approval (ground rule 2){{/if}}. You may
ask for it together with the plan approval ("(a) approve and start now (b) approve, start later (c) changes: ...").

If the user wants to drop it: `tb move <id> dropped --reason "<why, user decision>"`.
