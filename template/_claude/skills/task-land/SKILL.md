---
name: task-land
description: Act on a reviewer's verdict and land a task - {{#if git}}merge an approved pull request into {{main}}{{else}}merge an approved SVN branch into trunk (one commit per task){{/if}}, send changes back to the executor, escalate a judgment call to the user{{#if has_deploy}}, deploy to {{deploy_where}}, verify there{{else}}, verify the result{{/if}}, and close the task in 07-Done with its token counts. Use when a reviewer reports, when a task is in 06-Deploy, or when {{#if has_deploy}}{{deploy_where}}{{else}}{{main_ref}}{{/if}} shows a problem with a landed change.
---

# Landing

`tb` = `{{python}} .claude/board/tb.py`{{#if svn}}, `sv` = `{{python}} .claude/svn/sv.py`{{/if}}. The task is in 04-CodeReview with its
`branch` and `{{review_field}}` set.

## Reviewer verdict

**`approve`**
{{#if git}}
1. Check the reviewed commit is still the PR's head (`gh pr view <n> --json headRefOid`); if the branch moved
   after the review, review again.
2. `gh pr merge <n> --merge --delete-branch`. You have merging authority after an approval; no extra user OK
   needed. If GitHub reports a conflict, send the executor a fix round ("merge origin/{{main}} and resolve"), then
   review again.
3. `git fetch -q origin && git merge --ff-only @{u}` in the main checkout (not `git pull`: no race with the
   watcher's fetch on FETCH_HEAD). Remove an agent's worktree without asking when its branch is merged into
   `{{main}}` and the tree is clean (`git worktree remove <path>`); ask the user before `--force` or when the
   branch is unmerged.
4. Nothing to deploy{{#if deploy_none}} (always, in this project){{/if}} -> `tb move <id> done --reason "merged PR #<n> into {{main}}, reviewer approved"
   --tokens "..."`.{{#if has_deploy}} Otherwise -> `tb move <id> deploy --reason "merged PR #<n> into {{main}}, reviewer approved"`.{{/if}}
5. Update the handover, commit and push the task file.
6. Tell the user in 2-3 lines: what landed, its impact, anything they must do.
{{else}}
1. Check the reviewed revision is still the branch head (`svn log -l 1 ^/branches/<branch>`); if the branch moved
   after the review, review again.
2. Commit pending task-file changes first (`sv land` needs a clean trunk checkout):
   `svn commit AI -m "tasks: ..."`.
3. `sv land <branch> -m "<ID>: <title>"`. It updates trunk, merges the branch, commits once and deletes the
   branch; it prints the trunk revision. You have landing authority after an approval; no extra user OK needed.
   On conflicts it reverts and stops: send the executor a fix round ("sync with trunk and resolve"), then review
   again.
4. Remove the agents' working copies: `sv wt remove <name>` (refused while uncommitted changes exist; look
   before using `--force`).
5. Nothing to deploy{{#if deploy_none}} (always, in this project){{/if}} -> `tb move <id> done --reason "merged into trunk r<n>, reviewer approved" --tokens "..."`.
   {{#if has_deploy}}Otherwise -> `tb move <id> deploy --reason "merged into trunk r<n>, reviewer approved"`.{{/if}}
6. Update the handover, commit the task file; tell the user in 2-3 lines: what landed, its impact, anything they
   must do.
{{/if}}

**`changes needed`**
1. `tb move <id> implementation --reason "review: <short list of findings>"`.
2. Fix round: `executor-light` with the findings as a fix list on the same branch{{#if git}} and PR{{/if}} (`task-delegate`), or
   the `executor` if a finding needs a design decision or this is the third round.
{{#if git}}
3. When it reports the new head: `tb move <id> codereview --reason "fix round <k> pushed"`, and start the
   reviewer again with the previous findings, to verify them.
{{else}}
3. When it reports the new revision: `tb set <id> rev=<n>`, `tb move <id> codereview --reason "fix round <k>
   committed"`, and start the reviewer again with the previous findings, to verify them.
{{/if}}

**`needs decision`**
1. `tb ask <id> --for user "<the judgment call> (a) ... (recommended: the reviewer's and your view) (b) ..."`.
2. `tb move <id> userreview` (required: no task waits on the user in 03/04).
3. On the answer: record it (`tb answer ... --via chat` if it came in chat), then continue as `approve` or
   `changes needed`.
{{#if has_deploy}}

## Deploy and verify (06-Deploy)

{{#if deploy_prod}}
1. Deploy `{{main}}` to production as `AI/Context.md` ("Build, run and deploy") and the private deployment notes
   describe (they live outside the repository; never write host names or paths from them into {{tracked}} files,
   commits{{#if git}} or PR texts{{/if}}). Never let an agent deploy. Credentials stay on the user's machine and the server;
   they never appear in prompts or files.
2. Run the check from the task's Acceptance on production{{#if analyst}} (delegate the numbers to the `analyst` if it is more
   than a quick look){{/if}}; record the evidence: `tb log <id> "production check: <what, result>"`.
3. Passed -> close (below). Failed -> `tb move <id> implementation --reason "production: <what is wrong>"` and
   delegate a fix on a new branch (or revert the landed change first if production is unusable, and tell the
   user at once).
{{else}}
1. Deploy {{main_ref}} to the **test server** as `AI/Context.md` describes ("Build, run and deploy"). Never let an
   agent deploy. Credentials stay with the user's machine and the server; they never appear in prompts or files.
2. Run the check from the task's Acceptance on the test server; record the evidence:
   `tb log <id> "test server check: <what was checked, result>"`.
3. Passed -> close (below). Failed -> `tb move <id> implementation --reason "test server: <what is wrong>"`,
   {{#if svn}}create a new branch for the fix (`sv branch create`), {{/if}}and delegate a fix (or revert the landed change first
   if the test server is unusable, and tell the user at once).

Production releases are separate `ops` tasks that bundle landed work, not part of each task.
{{/if}}
{{/if}}

## Close (07-Done)

```bash
tb move <id> done --tokens "executor 140k, executor-light 30k, reviewer 55k (estimate ~150k)"
```

- Tokens: each agent run's count from its completion notification (for a resumed agent, its last cumulative
  value), summed per agent type, plus the plan's estimate if there was one. Not your own tokens. `n/a` only if a
  count was truly lost.
- The CLI refuses while a question is open or tokens are missing for a task with a reviewed {{review_noun}}.
- Record follow-ups the agents proposed as new tasks (`task-new`) before closing.
- If the task had a plan, set its status line to "{{landed_status}}".
- Remove the task from the handover; `tb check`; {{commit_push}}: `tasks: T012 {{#if has_deploy}}06-Deploy{{else}}04-CodeReview{{/if}} → 07-Done`.
- Nothing else running and the user in chat? Start fresh (supervisor skill, "Context size").
