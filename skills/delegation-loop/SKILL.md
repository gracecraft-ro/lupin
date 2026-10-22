---
name: delegation-loop
description: >-
  Coordinate an issue backlog with Lupin. Use this skill when starting or
  continuing an issue loop, selecting work for agents, watching a fleet, or
  handing off a loop run.
compatibility: >-
  Requires Lupin, GitHub CLI, an agent runner, and the `delegation-loop`,
  `triage`, `ship`, `code-review`, `handoff`, and `ui-pressure-test` skills on
  each worker host. Redis is required for fleet claims.
---

# Run a Lupin delegation loop

Read the root `AGENTS.md`, `docs/delegation-loop.md` (if any), and the latest
issue comments. Follow the repo's branch, worktree, test, and release rules.

A loop pushes its branch to the `fork` remote, never to `origin` (`OWNER/REPO`)
or `main`. It opens the PR on upstream against `main`, with `--head
FORK_OWNER:<branch>`. Never create a new fork. Never open a PR or an issue on a
fork.

Give each worker its own worktree under `.claude/worktrees/`. Parallel workers
must not edit the same checkout. A worker that opens a new branch makes its
worktree from upstream `main`:

```sh
git fetch origin
git worktree add <path> -b <branch> origin/main
```

On a Herdr worker, make the worktree as "Dispatch work" says instead. Record the
machine, worktree path, branch, and issue in the dispatch and the repository's
handoff record. For a worker in a Herdr pane, also record its agent name,
workspace ID, pane ID, tab ID, and cwd (`herdr agent list` shows them).

## Pull requests come first

In each cycle, dispatch PR work before work on a new issue. The owner merges
every PR into `main`. No loop, worker, or reviewer merges. An approved PR that
waits for the owner does not block the loop. A sync merges `main` into the PR
branch to fix a conflict. A sync is a push. It changes the head SHA (the newest
commit on the PR branch), so the PR needs a new review.

Before it reads the backlog, the orchestrator (the agent that dispatches work)
handles each open PR into `main`. Ignore draft PRs. Steps 2 and 3 apply only to
a PR that the loop owns. The loop owns a PR when both are true:

- The owner of its head repo is the fleet fork account (`FORK_OWNER`, the owner
  of the `fork` remote), or the ledger has a dispatch event for its branch.
- No other holder has a live claim on it. Run `lupin claim` for the PR. Exit 0:
  you hold the claim. Release it if you dispatch nothing and no worker of yours
  runs on the PR. Exit 2: another holder has it. Exit 1 (bad target) or 3 (Redis
  unreachable or login refused): do not dispatch on the PR.

Read the head repo owner (`headRepositoryOwner.login`) and all ledger events:

```sh
gh pr list --repo OWNER/REPO --base main --state open \
  --json number,headRefOid,headRefName,headRepositoryOwner,isDraft
```

```sh
lupin ledger read OWNER/REPO --limit 100000 --json
```

A PR is ready for production when all three are true:

- It is open on upstream, `OWNER/REPO`, with base `main`.
- Its latest formal review is an approval, and that review's `commit_id` is the
  current head SHA (`headRefOid`). A review of an older SHA does not count.
- The repo's full gate, as its `AGENTS.md` defines it, passes at that SHA.

It does not need to contain the current `main`. Skip a PR when the latest
`Loop:` comment says it is blocked at this head SHA and no owner comment came
after it. For each other PR, do the first step that applies:

1. The PR is ready for production. Take no action.
2. `mergeable` is `CONFLICTING` (see "Review each pull request"): dispatch a
   sync worker. It merges `origin/main` into the PR branch and resolves the
   conflicts. If it cannot, it stops and reports them. Then dispatch a fix
   worker to resolve them. Do not repeat that sync.
3. The PR has findings or a failed repo gate (the checks that `AGENTS.md`
   defines) at its current head SHA. A loop verdict with findings is at the head
   and nothing runs: start a fix worker, as "Fix worker models" says. Otherwise
   (the findings come from an owner comment or the gate): dispatch a reviewer
   with the findings and the gate output. The reviewer runs the fix cycle and
   starts the fix worker. See "Run the fix cycle" in the `code-review` skill.
   - A fix worker cannot start, or `lupin route` fails or gives `wait_seconds`:
     post the `Loop:` comment that step 3 of "Fix worker models" names, unless
     the reviewer posted it. Do not call the PR blocked in this comment. Post it
     once for each head SHA. Take the next item. Retry next cycle.
   - The reviewer reports that the fix worker's LEVEL is above its own: do not
     start a fix worker. Dispatch a reviewer from `lupin review-route --category
CATEGORY --size size-l --mode separate`.
4. The PR has no review at its current head SHA: dispatch a reviewer. Do not
   wait for the worker to request one.
5. The PR waits for a gate that is still running: wait for the result.

Each reviewer brief says whether the loop owns the PR. A reviewer that posted a
verdict in the last 30 minutes has a result for the 30-minute check on workers.
A worker is a fix worker, a sync worker, or a reviewer. Do not dispatch a worker
for a PR that already has one running. A reviewer in a fix cycle is running. Do
not push to the branch of a PR that the loop does not own.

A sync worker or a fix worker starts from the PR branch on the fork. Give it the
branch name. It uses these commands, not the other worktree and push commands in
this skill. The first three make its worktree. The last one pushes:

```sh
git fetch origin
git fetch fork <branch>
git worktree add --detach <path> fork/<branch>
git push fork HEAD:<branch>
```

Read the reviews:

```sh
gh api --paginate repos/OWNER/REPO/pulls/<number>/reviews
```

Post a verdict as a review, through the API, with the SHA that you read. `gh pr
review` binds the review to the head at the time of posting, and the head can
move first:

```sh
gh api repos/OWNER/REPO/pulls/<number>/reviews \
  -f event=APPROVE -f commit_id=<full SHA> -F body=@verdict.md
```

Use `-f event=REQUEST_CHANGES` when the review asks for changes. GitHub refuses
an approval or a change request from the PR's author. If your login opened the
PR, use a separate reviewer account. If you have none, post the verdict with `-f
event=COMMENT`. If your verdict is `Ship`, ask the owner in a `Loop:` PR comment
to approve the PR.

Every comment that the loop or its workers post starts with `Loop:`. An owner
comment has `author_association` `OWNER` or `MEMBER` and does not start with
`Loop:`. A loop review starts with `Round` and is not an owner comment. In each
cycle, read the new comments on each PR that the loop owns:

```sh
gh api --paginate repos/OWNER/REPO/issues/<number>/comments
gh api --paginate repos/OWNER/REPO/pulls/<number>/comments
```

An owner comment that names a problem is a finding. A PR comment is never an
approval. A review with findings is not an end state. The fix cycle repeats
until the PR is ready for production. A round is one verdict. There is no round
limit. Do not report, ask, or stop between rounds, except as "Run the fix cycle"
says.

A PR is blocked on an owner decision when it needs a sync or a fix and the loop
does not own it, when its fix cycle is stalled (the stall rule in "Fix worker
models"), or when a worker for it stops to ask the owner. On each blocked PR,
post the `Loop:` comment that step 3 of "Fix worker models" names, unless the
reviewer posted it. Then take the next item. In each cycle, read the new
comments on each blocked PR. List the blocked PRs in the handoff. Dispatch syncs
and reviews first. Then use the free worker slots for new issues. A PR in a fix
cycle does not stop work on a new issue. Work that is already running continues.

### Fix worker models

1. Make FILE with `gh issue view <number> --repo OWNER/REPO --json
title,body,labels > FILE` for the linked issue, or `gh pr view` with the same
   flags if no issue is linked. Run `lupin classify --issue-json FILE`. If it
   fails, use category `coding` and LEVEL `size-m`.
2. Take LEVEL from the latest `Round` review on the PR. If there is none, LEVEL
   is `size-l` for a `size-l` or `size-xl` PR and `size-m` for every other PR.
   Run `lupin route CATEGORY LEVEL --no-bmo --json`. Use the `model` and
   `effort` that it gives. If the command fails, or the output has
   `wait_seconds`, the fix worker cannot start now. Stop and report it.
3. Stall rule ("raise the fix worker one model tier"). At LEVEL `size-m`: when
   the same finding or failing check is in 3 verdicts in a row, change LEVEL to
   `size-l`. At LEVEL `size-l`: when the same finding or failing check is in 3
   verdicts in a row, the PR is blocked on an owner decision. Two `Round`
   reviews with the same round number at the same head SHA count as one verdict.
   Count only verdicts after the first `Round` line that says `size-l`, or all
   verdicts if the PR started at `size-l`. Do not start a fix worker on a
   blocked PR. Post the reason and the head SHA as a `Loop:` PR comment.
4. If there is no push 30 minutes after the fix worker starts, ask it for the
   result (Herdr: `herdr agent prompt NAME "report the result" --wait --timeout
120000`, with the agent name or its pane ID). If it has none, stop it (Herdr:
   `herdr pane close <pane_id>`).
5. If the fix worker ended with no push, whoever started it posts a `Round N.`
   verdict review at the same head SHA, with the same findings and the line `The
fix worker did not push.` Its first line is `Round N. LEVEL: <LEVEL>.`, as in
   "Output format" in `code-review`. Check the stall rule (step 3) before you
   post. That is the next round.
6. `size-l` usually gives Opus. That is allowed here. Never use Fable without
   the user's approval.

## Install skills on loop hosts

The `skills/` tree is the source. The Lupin command does not install it. A Nix
host flake links the six required directories from its pinned Lupin input into
each runner's skill directory: `~/.claude/skills/` (Claude Code),
`~/.pi/agent/skills/` (Pi), and `~/.omp/agent/skills/` (OMP). Do not copy the
files by hand. Add all six names to each host's skill set, including any curated
subset. After a rebuild, check that every required `SKILL.md` is readable:

```sh
for root in "$HOME/.claude/skills" "$HOME/.pi/agent/skills" "$HOME/.omp/agent/skills"; do
  for name in delegation-loop triage ship code-review handoff ui-pressure-test; do
    test -r "$root/$name/SKILL.md" || {
      echo "Missing $root/$name/SKILL.md" >&2
      exit 1
    }
  done
done
```

Start a new agent session and confirm it loads the `delegation-loop`, `triage`,
`ship`, `code-review`, `handoff`, and `ui-pressure-test` skills. In Claude Code,
run `/name` for each. In another agent, read each `SKILL.md`. Do not start a
worker if a skill it needs is missing.

## Read the backlog

Read the backlog only after you dispatch the PR work for this cycle. From a
configured checkout, run:

```sh
lupin roadmap --stage all --json
lupin roadmap --dag --json
lupin review-route --prefetch 123 --repo OWNER/REPO
lupin place 123 --json
```

The roadmap ranks issues. The DAG view shows dependencies. Prefetch gets the
latest issue or PR text and comments as markdown (`--json` gives the keyed
object). Placement recommends a model, effort, and machine. It does not claim
work or start an agent. Delegate a subagent to run the `triage` skill to help prioritize or groom the backlog as needed - lupin roadmap depends on issues being properly triaged.
Check likely files, comments, and live work before you run issues in parallel.
Run issues together only when their work does not overlap. `lupin quota` shows the shared Redis snapshot, refreshed with local provider data when this machine has
credentials. `route()` reads quota from the local machine, so the two results
may differ.

## Claim and label work

Use the same target and holder to claim, renew, and release work. The target is
always `OWNER/REPO#N` with the full repo name. Never pass a bare number. Claim
an issue when a worker starts its implementation. The orchestrator claims a PR
before it dispatches a reviewer or a fix worker. It renews the claim every 2
minutes during the fix cycle. It releases the claim only when no worker of its
own runs on the PR: the reviewer reported the final verdict, or the worker
stopped.

```sh
lupin claim OWNER/REPO#123 --holder SESSION         # issue 123
lupin claim OWNER/REPO#599 --holder SESSION         # PR 599, same repo
lupin renew-claim OWNER/REPO#599 --holder SESSION
lupin release-claim OWNER/REPO#599 --holder SESSION
```

Exit code 2 from `claim` means another holder has the item. The error text names
the holder. Exit code 3 means Redis is not reachable. Do not dispatch on the
item in either case. A claim needs Redis. Lupin has no local claim fallback. Do
not report a Lupin claim as active when Redis is not available. A claim expires
after 10 minutes by default. Use `--ttl SECONDS` to change this limit. Renew
every 2 minutes while work continues. Each renewal restarts the timer. `lupin
reconcile` runs once per call. It releases claims with no renewal for 10
minutes, even when `--ttl` is longer. Its schedule sets the cleanup delay.

The label and comment steps below apply to issue claims only. A PR claim has no
label. Record it in the ledger and in the PR verdict. Issue #43 tracks automatic
`claimed` label updates. Until it ships, the claim owner must update the issue
manually. Use the repo's `claimed` label. Add it during repo setup if it does
not exist. If you cannot add it, post the comment and report that the label is
missing. Include the machine, worktree, branch, and holder:

```sh
gh issue edit 123 --repo OWNER/REPO --add-label claimed
gh issue comment 123 --repo OWNER/REPO \
  --body "Loop: Claimed: machine=HOST; worktree=PATH; branch=BRANCH; holder=SESSION"
```

When the claim ends, remove the label and post the result, including the PR
number or reason the work stopped:

```sh
gh issue edit 123 --repo OWNER/REPO --remove-label claimed
gh issue comment 123 --repo OWNER/REPO --body "Loop: Released: PR #123"
```

The claim owner makes these updates. Workers must not post duplicate claim
comments. If the Redis claim fails, do not mark it as claimed. Follow the repo's
local coordination rule instead. If a lease expires or `lupin reconcile`
releases it, check that the GitHub label and comment also match. For related
issues, `lupin quest start --issue 123 --issue 124` claims and orders the
issues, then registers a quest on a machine. It does not start an agent. Do not
claim those issues again.

## Dispatch work

A reviewer, a sync worker, or a fix worker for an open PR is not new work. For
one issue or PR, run:

```sh
lupin dispatch OWNER/REPO#N
```

It claims the item, writes a markdown brief with the body and every comment
(and, for a PR, every review and the changed files), and starts the loop with
that brief. Add `--no-start` to write the brief only. Pass `--holder` or set
`LUPIN_CLAIM_HOLDER` as for `lupin claim`. Give each worker a short brief with:
issue goal and acceptance checks; latest comments and open dependencies; likely
files and known parallel work; model, effort, and machine recommendation; claim
or quest ID; machine, absolute worktree path, and branch; required tests and
smoke checks. For a visual change, the brief must tell the worker to attach
before and after screenshots to the PR, or the new screen if there is no before.
See "Attach screenshots to the PR" in the `ship` skill. Tell implementation
workers to follow `AGENTS.md`, rule 1 under "Pull requests, review, and
handoff". Do not repeat the `/ship` implementation checklist.

When an issue makes a major change to a web app's user interface or key user
journey, and a preview is ready, dispatch a separate QA task with
`ui-pressure-test`. Keep the test agent read-only for source code, with its own
browser data. Ask for evidence, prioritized tickets, and a list of untested
items. Do not ask it to fix findings. See "Run a standing pressure test".

A worker that opens a new branch uses the runner's worktree isolation feature
when it has one. Otherwise, it creates a separate worktree and verifies its
working directory. On a Herdr worker, it uses the Herdr worktree commands:

```sh
herdr worktree list --cwd "$PWD"
git fetch origin
herdr worktree create --branch BRANCH --base origin/main --cwd "$PWD"
herdr worktree open --path PATH --cwd "$PWD"
```

A sync worker or a fix worker makes its worktree with the git commands in "Pull
requests come first". It must not use `herdr worktree create`. On a Herdr
worker, it then runs `herdr worktree open --path PATH --cwd "$PWD"`.
`--trust-repository` gives Git trust for one command. Use it only after the
owner verified the repository. Do not use it to retry a failed worktree command.

## Communicate with workers

Use the agent runner's prompt and message tools for its own workers. Lupin's
remote commands control loops. They do not send free-form messages.

### Talk to an agent in a Herdr pane

A Lupin loop runs in the `lupin-loops` Herdr session on its machine. Each loop
is one workspace with one named agent. A worker that the runner starts itself
runs in the runner's own session. Herdr is the only way to send a message to a
running agent. Read the agent name from `herdr agent list`. Do not guess it. A
name is valid only while the agent runs.

```sh
herdr agent list                                   # read the name from the output
herdr agent prompt REPO_NAME "report progress" --wait --timeout 120000
herdr agent read REPO_NAME --source recent-unwrapped --lines 120
```

The timeout is in milliseconds. Omit `--timeout` only for a wait that can last a
long time. A prompt without it can wait forever. Use `--wait` for normal work.
It returns when the agent is ready for input. Add `--until blocked` only to wait
for a question or an approval. Use `--source detection` only to check why Herdr
does not recognize the agent.

Agent states:

| State     | Meaning                                        | What to do                                                                 |
| --------- | ---------------------------------------------- | -------------------------------------------------------------------------- |
| `idle`    | Ready for input                                | Send the next prompt                                                       |
| `done`    | Ready for input                                | Send the next prompt. The CLI state decides this, not the badge in the app |
| `working` | The agent is busy                              | Wait or read output                                                        |
| `blocked` | The agent stopped on a question or an approval | Read the output, then ask the owner. Do not answer for the owner           |
| `unknown` | Herdr cannot tell what the agent does          | Not proof that it finished. Check the pane yourself                        |

A timeout does not prove the text arrived, and it does not prove the text did
not arrive. A prompt that reports a stall can also be delivered. Read the agent
before you send the text again. To read output from a pane, or to run a test or
a smoke check beside the worker, use the pane commands. `pane run` sends one
command. `pane wait-output` returns when the output matches:

```sh
herdr pane run <pane_id> "pytest -q"
herdr pane wait-output <pane_id> --match "passed" --timeout 120000
herdr pane read <pane_id> --source recent-unwrapped --lines 120
```

Address panes by pane ID, or with `--current` for your own pane. Never use
another client's focused pane. To start a worker in a Herdr pane, make the pane
first, then start the agent in it. `agent start` needs a shell that waits at its
prompt. It never makes, splits, or moves a pane:

```sh
herdr pane split --current --direction right --cwd "$PWD" --no-focus
herdr agent start worker-1 --kind omp --pane <pane-id-from-above>
```

The kind must be an installed one. `herdr integration status` lists them. An
agent with a kind that Herdr does not know shows as `unknown`.

### Read or control a loop on another machine

```sh
herdr --machine MACHINE agent list
lupin peek REPO --machine MACHINE
lupin attach REPO --machine MACHINE
lupin stop REPO --machine MACHINE
lupin schedule --machine MACHINE
lupin pause --machine MACHINE
lupin resume --machine MACHINE
```

`peek` reads output. `attach` opens the loop terminal. The other commands
control the loop or its schedule. `herdr --machine MACHINE` must come before the
subcommand on every command, including the discovery command. Do not use it with
`--session` or `--remote`. It uses the saved machine profile and its default
session, so it does not reach the `lupin-loops` session. To reach the loops on
another machine, use `lupin attach REPO --machine MACHINE`. Locally, address a
loop with the session name:

```sh
herdr --session lupin-loops agent list
```

## Review each pull request

Dispatch `/code-review` for every pull request, including docs-only changes.
Review the current PR diff, not only the issue or a worker's report. Re-fetch
the latest PR comments and reviews before you post a verdict. The reviewer posts
each verdict and its findings on the PR. A worker that opens a new branch pushes
only that branch to the fork, with `git push -u fork <branch>`. The PR base is
`main` on upstream:

<!-- markdownlint-disable MD013 -->

```sh
gh pr create --repo OWNER/REPO --base main --head FORK_OWNER:<branch> --title "<title>" --body "Closes #N"
```

<!-- markdownlint-enable MD013 -->

The PR body has one line for each issue. Use `Closes #N` when the PR finishes
the issue, and `Refs #N` only when the PR leaves work open. If the fork push
fails, the worker stops. It reports the branch name and commit range. Do not
review the branch. After the PR opens, report its number to the orchestrator.

For a visual change, you must check that the PR has screenshots before you
dispatch the reviewer. A PR has screenshots when a comment on it shows images
from `user-attachments`. A `./evidence/` path does not count. A worker whose
token cannot upload reports the path of each file and its machine. Copy the
images before you run `lupin stop`. Stop deletes the worktree and `evidence/`
with it. To copy from another machine, run `mkdir -p ./evidence/<issue-number>`.
Then run `scp <target>:<absolute path> ./evidence/<issue-number>/`. Take
`<target>` from `~/.config/lupin/ssh-targets`. Open each image and check it, as
in step 2 of "Attach screenshots to the PR" in the `ship` skill. Then attach the
files yourself, with your own token, using the command in that section. Write
your own `report.md` in the same folder. If the upload fails, post the report as
a `Loop:` PR comment before `lupin stop`. Do not leave it only in the ignored
`evidence/<issue-number>/report.md`. Do not dispatch the reviewer on a visual PR
with no screenshots. If the worker says it could not capture a screenshot, the
PR is blocked on an owner decision. Ask the owner how to go on in a `Loop:` PR
comment. Then take the next item.

Use `lupin review-route --category CATEGORY --size SIZE --mode separate` for a
reviewer recommendation. Compare it with the issue's implementation route and
the worker's model tier. One reviewer may review several small, independent PRs
in one dispatch, with a separate verdict for each. Review complex or high-risk
changes alone. Keep visual or 3D reviews to one or two PRs per dispatch. The
reviewer must be capable of doing the issue and must not be below the worker's
model tier. On a PR the loop owns, the reviewer uses `--size LEVEL` with the fix
worker's LEVEL or higher. Prefer a reviewer one tier higher when quota allows, but
use frontier models sparingly. If the fix changes anything a person can see, the brief must
tell the fix worker to capture new screenshots, even when the finding is not
labeled visual. The fix worker attaches them after it pushes (see "Attach
screenshots to the PR" in the `ship` skill).

Sync a PR only when GitHub reports a conflict with `main`. Check it:

```sh
gh pr view <number> --repo OWNER/REPO --json mergeable --jq .mergeable
```

`CONFLICTING` means a sync is needed. `MERGEABLE` means it is not. `UNKNOWN`
means read it again next cycle. The sync worker, or the fix worker that resolves
a conflict, works in its worktree and follows these steps. Do not rebase:

1. Run `git merge origin/main` and fix each conflict in the file.
2. Run `git add FILE` for each file you changed.
3. If you edit a file after `git add`, run `git add FILE` again before the
   commit. A commit holds the text that was staged. The gate reads the working
   tree, not the commit, so a missing stage looks like a pass.
4. Run `git diff --stat`. It must print nothing. If it prints a file, run `git
add FILE` for that file and run the check again.
5. Run `git diff --stat HEAD`. It must list only the files you changed.
6. Run `git commit`.
7. Run `git diff --stat HEAD` again. It must print nothing. If it prints a file,
   run `git add FILE`, commit again, and run the check again.
8. Run `git push fork HEAD:<branch>`.

The reviewer may limit the new review to the files the sync changed. The gate
runs in every case. The reviewer is a different agent from the worker. The gate
command is in the repo's `AGENTS.md`. If the repo has no gate command, report
that. A branch with no open upstream PR is not merged. Do not force-push.

### Check that a PR is ready

The latest formal review is the newest review in state `APPROVED` or
`CHANGES_REQUESTED`. This command prints its `commit_id` only when it is
`APPROVED`. The PR is ready only when that value equals the head (`headRefOid`):

```sh
gh api --paginate --slurp repos/OWNER/REPO/pulls/<PR-number>/reviews \
  --jq '[.[][] | select(.state == "APPROVED"
    or .state == "CHANGES_REQUESTED")] | last
    | if .state == "APPROVED" then .commit_id else null end'
```

The output is `null` when the latest formal review is not an approval, or when
no reviewer made one. `--paginate --slurp` reads every page. A `COMMENTED`
review does not count.

### Preview server

Each repo runs one preview server. It serves upstream `main` only. Do not run a
server for a feature branch.

1. Create the preview worktree once. From the repo root, run:

   ```sh
   git fetch origin
   git worktree add --detach .claude/worktrees/preview origin/main
   ```

2. After each merge into `main`, the orchestrator updates the preview. From the
   repo root, run:

   ```sh
   git -C .claude/worktrees/preview fetch origin
   git -C .claude/worktrees/preview checkout --detach origin/main
   ```

   Then it restarts the server.

3. Bind the server to `127.0.0.1` or to the Tailscale IP address. Never bind to
   `0.0.0.0`.
4. Set `LUPIN_LOOP_STATE_DIR` to a scratch path. Do not point the preview at the
   real fleet Redis unless the test needs it.
5. Keep the server running in a Herdr pane or with `systemd-run --user`. It must
   keep running after an agent session ends.
6. The repo's `AGENTS.md` has a "Preview server" section. It names the start
   command and the port. The machine is in the owner's own machine notes. The
   tunnel command is `ssh -N -L PORT:127.0.0.1:PORT MACHINE`. The local URL is
   `http://localhost:PORT`. The port must be free on that machine. Check with
   `ss -ltn`. Check the scratch Redis port the same way. If the repo has no
   runnable app, that section says `Preview server: none` and gives the reason.
7. A PR does not need a tunnel command. The PR body says whether the change is
   visible only after the merge.

## Run a standing pressure test

This is a queue item, not an option. Do it for every repo with a web UI. A UI
pull request changes a web UI. The repo's `frontend-ui` capability label marks
it. If the repo has no such label, a change under the UI paths that the repo
names marks it.

Start a run when one of these is true:

- The repo has no `pressure-test` event. This is the first run.
- A QA register row has no `Last QA date`, or the date is 14 days old or older,
  whether or not a UI pull request merged.
- 5 or more UI pull requests merged since the last run.
- The last run is more than 24 hours old, and a UI pull request merged since it.
- A release pull request is about to open.

The release trigger applies only when the repo names a release signal: a release
pull request that the repo's `AGENTS.md` release rules name, or an owner `Loop:`
comment. Before the due check, read the repo's QA register issue. In Lupin, use
issue #88. See "Keep the repo QA register" in the `ui-pressure-test` skill. Use
its rows for the 14-day trigger and the sample pool.

Every run also samples long-tail rows. The pool has rows whose `Last QA date` is
30 days old or older, or whose `Changed since?` value is greater than zero. Do
not sample rows already selected as due. Choose 3 rows at random without
replacement, or choose the whole pool if it has fewer than 3 rows. Record the
sample count and random seed in the `pressure-test` ledger event. If the pool is
empty, record `sample_n=0; seed=none`.

A `blocked` event records an attempt, not a test. It does not update the
register or clear an overdue row. The 14-day trigger uses `Last QA date`, not
the event time. Retry blocked work when a host can launch the browser. The row
stays due. Find the newest `pressure-test` event in the ledger. The default
limit is 10 events, so always give `--limit 100000`:

```sh
lupin ledger read OWNER/REPO --limit 100000 --json
```

The event `status` is `running`, `done`, or `blocked`. A `running` event younger
than 4 hours means a director is active. Do not start a second one. The director
writes the `running` event again before 4 hours pass. If the orchestrator cannot
see the director's live session, it does not treat an old `running` event as
inactive. `lupin loops` and `herdr agent list` show live sessions. If the ledger
exits 3, do not start a run. Report it and take the next queue item. Record the
start:

```sh
lupin ledger append OWNER/REPO --event pressure-test --status running \
  --summary "director=SESSION; reason=first|14d|5-prs|24h|release; sample_n=N; seed=SEED"
```

Check that the browser starts before you dispatch. If it does not start, try to
file or update an environment issue in the environment repo with label
`environment`. If `gh` cannot write, run this command. Say in the `blocked`
event summary or the handoff that the loop did not file the issue.
`ENV_OWNER/REPO` is the environment repo that rule 6 of the repo's `AGENTS.md`
names. The summary text names the repo that you test:

```sh
lupin ledger append ENV_OWNER/REPO --event env-friction \
  --summary "browser did not start for OWNER/REPO pressure test: <detail>"
```

Then run the test on a host where the browser works. If no host can launch the
browser, write the blocked event, report it, and take the next queue item:

```sh
lupin ledger append OWNER/REPO --event pressure-test --status blocked \
  --summary "sample_n=N; seed=SEED; browser did not start; see <issue or ledger entry>"
```

Dispatch `/ui-pressure-test` as the only director for that repo. It writes
prioritized tickets and a coverage report. Write the report to the QA register
issue. The `ui-pressure-test` skill gives the report sections and the register
row rules. After the agent posts the tickets, record the end:

```sh
lupin ledger append OWNER/REPO --event pressure-test --status done \
  --summary "tickets=N; untested=M; sample_n=N; seed=SEED" \
  --evidence "<report link>"
```

Then dispatch fix workers for the tickets. A pressure-test ticket has no pull
request at first. Dispatch a normal implementation worker from "Dispatch work"
for the first fix. Then use the review-fix cycle in `/code-review` on the pull
request that it opens.

## Monitor and finish

Use `lupin machines`, `lupin peek`, and `lupin attach` to check live work. On a
Herdr worker, `herdr --session lupin-loops agent list` shows each loop's state,
workspace, and pane in one call. Read the final diff and run the repo's required
gate and a smoke check. A worker's success report is not proof. Re-read issue
and PR comments before closure. Use `lupin quota` to pace your spending.

After the merge and the preview restart, check that the preview port answers.
Run `curl -fsS -o /dev/null http://127.0.0.1:PORT/` on the preview machine. Use
the Tailscale IP address instead if the server binds to one. Exit code 0 means
the port answers. Report the result in the handoff. Append dispatch and handoff
events to the shared Redis ledger. Add `--child N` for each split issue:

```sh
lupin ledger append OWNER/REPO --event dispatch --issue N \
  --status running --branch BRANCH
lupin ledger append OWNER/REPO --event handoff --issue N --status STATUS \
  --summary TEXT --highlights TEXT --evidence TEXT --decisions TEXT --next TEXT
lupin ledger read OWNER/REPO --json
```

If Redis is unavailable, ledger commands exit 3. The roadmap then shows no
ledger annotations and adds a warning. The `handoff` skill gives the format and
the `.loop/loop-state.json` fallback. At the start of a session, read the ledger
and that file.

A worker's task ends when its issue is merged into `main`. A task also ends when
it is blocked on an owner decision. Waiting for the owner does not end the
queue. Write the question as a `Loop:` comment on the issue or the PR. Then take
the next item. Policy changes to this skill go to upstream `main` as a pull
request, which the owner merges.

Work the queue until nothing is left. Nothing is left when all of these are
true:

- No open issue that the loop may take is unclaimed.
- Every PR that the loop owns is ready for production.
- No PR waits for a review.
- No research, docs, or triage item is open that a worker can do.
- No repo with a web UI is due a pressure test. See "Run a standing pressure
  test".

Then run `/handoff`. State the exact next action and any missing owner input.
Before that, do not ask the owner whether to go on. Never write "tell me if you
want me to continue" or "if you want me to".
