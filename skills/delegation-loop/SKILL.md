---
name: delegation-loop
description: >-
  Coordinate an issue backlog with Lupin. Use this skill when starting or
  continuing an issue loop, selecting work for agents, watching a fleet, or
  handing off a loop run.
compatibility: >-
  Requires Lupin, GitHub CLI, an agent runner, and the `delegation-loop`,
  `triage`, `ship`, and `code-review` skills on each worker host. Redis is
  required for fleet claims.
---

# Run a Lupin delegation loop

Read the repository's `AGENTS.md` and delegation notes first. Follow its
branch, worktree, test, and release rules. Read the latest issue comments.

Use an isolated worktree for each worker. Do not let parallel workers edit the
same checkout. Record the machine, worktree path, branch, and issue in the
dispatch and the repository's handoff record.

## Install skills on loop hosts

The `skills/` folders are source files. Installing Lupin does not install
them. Each host that runs a loop must make all four skills available to its
agent runner:

- Claude Code: `~/.claude/skills/`
- Pi: `~/.pi/agent/skills/`
- OMP: `~/.omp/agent/skills/`

Manage these paths in the host's config. For another runner, use its native
skill path. After deployment, check that each required `SKILL.md` is readable
on every target host. Start a new agent session and confirm it loads
`/triage`, `/ship`, and `/code-review` before dispatch. Do not start a worker
if one of these skills is missing.

## Read the backlog

From a configured checkout, run:

```sh
lupin roadmap --stage all --json
lupin roadmap --dag --json
lupin review-route --prefetch 123 --repo OWNER/REPO
lupin place 123 --json
```

The roadmap ranks issues. The DAG view shows dependencies. Prefetch gets the
latest issue or pull request text and comments. Placement recommends a model,
effort, and machine. It does not claim work or start an agent.

Use `/triage` to decide what is ready. Read the latest comments and repo
instructions. Check likely files and live work before you run issues in
parallel. Run issues together only when their work does not overlap.

`lupin quota` displays the shared Redis snapshot and refreshes it with local
provider data when this machine has credentials. `route()` still reads quota
from the local machine, so the two results may differ.

## Claim and label work

Use the same issue and holder to claim, renew, and release work:

```sh
lupin claim OWNER/REPO#123 --holder SESSION
lupin renew-claim OWNER/REPO#123 --holder SESSION
lupin release-claim OWNER/REPO#123 --holder SESSION
```

Lupin stores claims in Redis. A claim does not add a GitHub label, comment,
or assignment. It also does not store the worktree path.

After a claim succeeds, the claim owner must mark the issue in GitHub. Use the
repo's `claimed` label. Add it during repo setup if it does not exist. If you
cannot add it, post the comment and report that the label is missing. Include
the machine, worktree, branch, and holder:

```sh
gh issue edit 123 --repo OWNER/REPO --add-label claimed
gh issue comment 123 --repo OWNER/REPO \
  --body "Claimed: machine=HOST; worktree=PATH; branch=BRANCH; holder=SESSION"
```

When the claim ends, remove the label and post the result, including the PR
number or reason the work stopped:

```sh
gh issue edit 123 --repo OWNER/REPO --remove-label claimed
gh issue comment 123 --repo OWNER/REPO --body "Released: PR #123"
```

The claim owner makes these updates; workers must not post duplicate claim
comments. If the Redis claim fails, do not mark it as claimed. Follow the repo's
local coordination rule instead. If a lease expires or `lupin reconcile`
releases it, check that the GitHub label and comment also match.

A claim needs Redis. If Redis is not available, Lupin has no local claim
fallback. Do not report a Lupin claim as active.

For related issues, `lupin quest start --issue 123 --issue 124` claims and
orders the issues, then registers a quest on a machine. It does not start an
agent. Do not claim those issues again.

## Dispatch work

Give each worker a short brief with:

- Issue goal and acceptance checks.
- Latest comments and open dependencies.
- Likely files and known parallel work.
- Model, effort, and machine recommendation.
- Claim or quest ID.
- Machine, absolute worktree path, and branch.
- Required tests and smoke checks.

Tell the worker to use `/ship`. Do not repeat its implementation checklist.
Use the runner's worktree isolation feature when it has one. Otherwise, create
a separate worktree and verify the worker's working directory.

## Communicate with workers

Use the agent runner's prompt and message tools for its own workers. Lupin's
remote commands control loops. They do not send free-form messages.

For workers started in Herdr-managed panes, Herdr can prompt, read, and wait:

```sh
herdr agent prompt NAME "Please report progress" --wait --timeout 120000
herdr agent wait NAME --until idle --timeout 120000
herdr agent read NAME --source recent-unwrapped --lines 120
herdr --machine MACHINE agent list
```

For a remote worker, add `--machine MACHINE` after `herdr` in each command.

Use Herdr only for agents that run in Herdr panes. Lupin loops run in tmux
panes; Herdr cannot see those sessions.

Use Lupin to watch or control a loop on another machine:

```sh
lupin peek REPO --machine MACHINE
lupin attach REPO --machine MACHINE
lupin stop REPO --machine MACHINE
lupin schedule --machine MACHINE
lupin pause --machine MACHINE
lupin resume --machine MACHINE
```

`peek` reads output. `attach` opens the loop terminal. The other commands
control the loop or its schedule. Use the agent runner or Herdr for messages.

## Review and merge each pull request

Dispatch `/code-review` for every pull request, including docs-only changes.
Review the current PR diff, not only the issue or a worker's report. Re-fetch
the latest PR comments and reviews before merge.

Use `lupin review-route --category CATEGORY --size SIZE --mode separate` for
a reviewer recommendation. Compare it with the issue's implementation route
and the worker's model tier.

To save tokens, one reviewer may review several small, independent PRs in one
dispatch. Keep a separate verdict for each PR. Review complex or high-risk
changes alone. Keep visual or 3D reviews to one or two PRs per dispatch.

The reviewer must be capable of doing the issue and must not be below the
worker's model tier. Prefer a reviewer one tier higher when quota allows.
Never dispatch Fable without the user's approval. Use Opus sparingly because
it costs more.

If the review finds a problem, dispatch a `fix` worker with the exact finding.
Tell it to use `/ship` and update the same PR. Review the latest PR commit.
Repeat until the reviewer approves it.
The orchestrator posts the verdict and findings on the PR.

After approval and required checks pass, the orchestrator merges locally using
the repo's merge rules. The reviewer and worker do not merge their own PR.

## Monitor and finish

Use `lupin machines`, `lupin peek`, and `lupin attach` to check live work.
Read the final diff and run the repo's required gate and a smoke check. A
worker's success report is not proof. Re-read issue and PR comments before
closure.

Append dispatch and handoff events to the shared Redis ledger:

```sh
lupin ledger append OWNER/REPO --event dispatch --issue N \
  --status running --branch BRANCH
lupin ledger append OWNER/REPO --event handoff --issue N --status STATUS \
  --summary TEXT --highlights TEXT --evidence TEXT --decisions TEXT --next TEXT
lupin ledger read OWNER/REPO --json
```

Add `--child N` for each split issue. If Redis is unavailable, ledger
commands exit 3. The roadmap shows no ledger annotations and adds a warning.
Do not use `.loop/loop-state.json`.

Keep working while requested, unblocked work remains. Hand off when the
backlog is done, work is dispatched up to capacity, or the rest is blocked.
State the exact next action and any missing owner input.
