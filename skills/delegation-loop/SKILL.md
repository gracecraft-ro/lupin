---
name: delegation-loop
description: >-
  Coordinate an issue backlog with Lupin. Use this skill when starting or
  continuing an issue loop, selecting work for agents, watching a fleet, or
  handing off a loop run.
compatibility: Requires Lupin, GitHub CLI, and an agent runner. Redis is required for fleet coordination.
---

# Run a Lupin delegation loop

Read the repository's `AGENTS.md` and delegation notes first. Follow that
repository's branch, worktree, test, and release rules. Do not assume that a
past handoff or roadmap view is still current.

## Build a current view

From a configured repository checkout, use:

```sh
lupin roadmap --stage all --json
lupin roadmap --dag --json
lupin review-route --prefetch 123 --repo OWNER/REPO
lupin place 123 --json
```

The roadmap ranks issues. The `--dag` view shows dependencies across enabled
repos.
Prefetch gets current issue or pull request text and comments. Placement
classifies an issue, recommends a model and effort, and ranks machines. Read
the issue's latest comments and repository instructions before dispatch.

`lupin quota` shows shared quota readings. Routing currently reads the quota
data available on the machine that runs it; it does not use that shared
snapshot. Treat the model and machine results as recommendations, not as a
worker launch or an issue claim. A quota wait is a reason to wait or use a
permitted fallback, not to select the blocked model by hand.

## Select and reserve work

Use the `triage` skill to decide which issue is ready. Check likely files and
live work before running issues in parallel. The roadmap's batch is an estimate
from issue text; it does not confirm worker or worktree state.

Before sending a worker, reserve its issue when fleet claims are configured.
Use the same target and holder to renew or release it:

```sh
lupin claim OWNER/REPO#123 --holder SESSION
lupin renew-claim OWNER/REPO#123 --holder SESSION
lupin release-claim OWNER/REPO#123 --holder SESSION
```

Claims need Redis. If Redis is not available, follow the repository's local
coordination rule and do not report a claim as active.

For a related issue set, `lupin quest start --issue 123 --issue 124` claims the
issues, orders them by dependencies, and registers a quest on a machine. It
does not launch an agent and it does not bypass quota pacing. Use the
repository's agent runner to start the work. Give each parallel worker an
isolated checkout unless repository instructions say to use another method.

Give a worker a short brief with the issue goal, acceptance checks, current
comments, dependencies, likely files, model and effort recommendation,
checkout path, claim or quest ID, and required verification. Name the `ship`
skill instead of repeating its implementation checklist.

## Monitor and finish

Use `lupin quest status`, `lupin machines`, and the repository's issue or PR
state to check progress. Lupin's quest and loop controls coordinate state; they
do not review code or verify a worker's report. Read the diff and run the
repository's required gate and a smoke check before accepting work. Re-read
current issue and PR comments before merge or closure.

Lupin can control a remote loop with `stop`, `peek`, `schedule`, `pause`, and
`resume`. Its signed machine queue only accepts the configured loop-control
actions. It is not a general message or shared agent-context channel. Put
handoffs in the repository's issue or handoff record.

If the repository uses `.loop/loop-state.json`, update it through its documented
handoff process. Lupin reads that file for roadmap status; it does not write
issue dispatch or handoff entries. Keep the loop active while requested,
unblocked work remains. Hand off when remaining work is done, dispatched,
closed, or blocked by a real external need. State the exact next action and any
missing owner input.
