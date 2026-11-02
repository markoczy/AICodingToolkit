---
name: task-new
description: Record a new task on the board - an idea or problem from the user, a bug{{#if analyst}} or data issue{{/if}} found in passing, or a task an executor{{#if analyst}}, reviewer or analyst{{else}} or reviewer{{/if}} proposed. Use whenever something needs doing that isn't done right now in this conversation, so it is never lost and never silently done.
---

# New task

`tb` = `{{python}} .claude/board/tb.py`. {{#if git}}On `{{main}}`{{else}}In the trunk checkout{{/if}} only; `{{vcs_update}}` first (ids come
from the files you have).

1. **Check for duplicates first:** `tb board` (and the search on the web board). If a task covers it, add to
   that one instead (`tb log <id> "..."`, `tb section <id> problem --file ...`).
2. **Choose:**
   - `--type`: one of the configured types ({{types_list}}; `research` for a question answered by an agent
     without code, `ops` for deployment and operations).
   - `--priority`: `high` (blocks something or produces wrong output now), `medium` (next in line), `low`
     (nice to have). If unsure, `medium` and say so to the user.
   - `--state`: `00-Conception` (default: only the problem is known) or `01-Refinement` (you are about to work
     out the solution).
   - `--title`: short and specific, what is wrong or wanted (not how). Quoting is handled for you.
{{#if multiuser}}
   - `--owner <person>` when the task belongs to another dev (default: the user).
{{/if}}
3. **Write the Problem with evidence:** what is wrong or missing, where (file:line, config, command, URL), how
   it shows (numbers, dates, screenshots described), who reported it. For a proposal from an agent, copy its
   evidence and name the source ("proposed by the reviewer of {{#if git}}PR #81{{else}}branch t041-export{{/if}}").

```bash
tb new --title "Weekly export of an unfinished week is stored as final" --type bug --priority high \
  --problem "Reviewer of {{#if git}}PR #81{{else}}t041-export{{/if}}, 2026-10-24: export.py:88 takes the open week; 3 of 64 files in the sample end mid-week."
```

   For a longer Problem, write it to a temp file and use `--problem-file <path>`. Keep it publishable: no
   {{confidential}}.
4. **Link it:** `tb set <id> blocked_by=T010` if it can't start before another task; mention related tasks in the
   Problem.
5. **Questions you already know:** `tb ask <id> --for user "<question> (a) ... (recommended) (b) ..."`.
6. `tb check`, {{commit_push}} (`{{vcs_commit_tasks}}` with the message `tasks: T013 created (...)`; several
   new tasks can share one commit).
7. Tell the user in one line (id, title, priority), unless they asked for it themselves just now.

Never implement a new task on the side "because it's small": it goes through the board like everything else,
including the start approval, unless the user explicitly says to just do it.
