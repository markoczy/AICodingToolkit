# Plans

Point-in-time design documents for non-trivial changes (see "Plans" in [../Methodology.md](../Methodology.md)).
They record why something was built a certain way. They are history, not documentation of the current code:
check the code before relying on one.

Name: `<YYMM>-<NN>-<Title>.md`, the next free number of the month (`{{python}} .claude/board/tb.py next-plan
"<title>"` prints the path). Two plans never share a number.

## Template

```markdown
# <Title>

Status: draft | approved <date> by <person> | {{landed_status}} | superseded by <plan>
Task: T<nnn>

## Problem
What is wrong or missing, with evidence. Link the task.

## Design
The chosen approach, concretely enough to implement: modules, data structures, interfaces, formats.

### Alternatives considered
Each with why it was not chosen.

## Expected impact
What changes for users, {{#if analyst}}computed values, stored data, {{/if}}outputs, APIs or performance, and why. "None: behavior
identical, covered by <test>" is a valid answer when it is true.

## Migration and configuration
Changes existing setups need; one-off steps for the deployment.

## Tests
What is tested and how; which regression tests guard which risk.

## Documentation
Which docs change in the same {{#if git}}PR{{else}}branch{{/if}}.

## Effort
- {{#if git}}PRs{{else}}Branches{{/if}}: <n> (what each contains)
- Review rounds expected: <n>
- Tokens per {{#if git}}PR{{else}}branch{{/if}} (agent terms): ~<n>k
- Fixed run times (migrations, backfills, rebuilds): <duration>

## Open questions
Questions for the user or the supervisor, each with options (a), (b), ... and a recommendation; they are copied
into the task file.
```

A plan-only executor writes the plan to the main checkout's {{untracked}} `AI/Temp/plans/`; the supervisor moves it
here, commits it (`tasks: plan <file>`) and records the approval in the task file. The implementing executor sets
the status line; "{{landed_status}}" is set by the supervisor after landing.
