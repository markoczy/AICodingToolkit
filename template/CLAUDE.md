# {{project}}

## How we work

Before doing anything in this repo, read [AI/Context.md](AI/Context.md) (the project: system design,
conventions and the rules for changing this codebase) and [AI/Methodology.md](AI/Methodology.md) (roles, ground
rules, task board, branches and reviews, plans). The main session acts as the **supervisor** (`/supervisor`
skill); detail work goes to the agents in `.claude/agents/`.{{#if multiuser}} A developer working without the
supervisor runs a **coordinator** session instead (`/coordinator` skill): board, refinement and research for
their own tasks, no code executors.{{/if}}

{{#if git}}
Version control is **git**. The main checkout stays on `{{main}}`; task branches are git branches, agents work
in git worktrees under `.claude/worktrees/`, and code reaches `{{main}}` only through a reviewed and merged pull
request.
{{else}}
Version control is **Subversion**. This checkout is the main checkout and stays on `^/trunk`. Task branches live
under `^/branches/`, agents work in SVN working copies in `../<checkout>.worktrees/` (created by the
WorktreeCreate hook), and code reaches trunk only through `{{python}} .claude/svn/sv.py land` after review: one
trunk commit per task. Reference: [.claude/svn/README.md](.claude/svn/README.md).
{{/if}}

Tasks live in [AI/Tasks/](AI/Tasks/README.md), one file per task and one folder per state. Task files change
{{on_main}} only, through `{{python}} .claude/board/tb.py`; a task branch reports its result in the
{{#if git}}pull request and the agent's final message{{else}}agent's final message{{/if}} instead. The
supervisor's live state is in [AI/SupervisorHandover.md](AI/SupervisorHandover.md).

Ground rules (details in `AI/Methodology.md`): task questions are not repeated in chat unless asked; every task
needs {{#if open}}a person's{{else}}the user's{{/if}} approval before it starts; questions offer options (a), (b),
(c) so a letter answers them; the supervisor updates `AI/SupervisorHandover.md` at every step of a running task.

## Compact Instructions

If you are the supervisor (you invoked the `supervisor` skill, or the line "Role: supervisor" is in your
context; the reload hook re-injects `.claude/skills/supervisor/SKILL.md` and `AI/SupervisorHandover.md` after
compaction):

Write a minimal summary: at most 2500 characters, plain bullets, no headings. Do not use the usual summary
template (primary request, key technical concepts, files and code sections, errors and fixes, problem solving,
all user messages, pending tasks, current work, next step): none of those sections. `AI/SupervisorHandover.md`
is authoritative; never restate it, never list past user messages or landed tasks.

Keep, verbatim where possible:
- The line "Role: supervisor".
- The current task and the exact step in progress.
- Unresolved errors: the exact message, plus the file and line.
- Decisions, findings, and subagent results that came after the last edit to `AI/SupervisorHandover.md`, each
  with its one-line reason.
- Subagents still running or awaiting review, with their task and agent id.
- Standing user constraints given in chat that no {{tracked}} file records (one line each).

Drop:
- Anything already in `AI/SupervisorHandover.md` or the supervisor skill.
- Completed work, superseded plans, file contents, tool output, and exploration that led nowhere.

End with: "First action: sync the items above into AI/SupervisorHandover.md, and into any further handover file
that the reload hook injected after it."

For all other sessions: summarize the current task, open errors (verbatim), and next steps; omit file contents
and tool output.
