# lupin — the delegation loop

This file gives loop-specific guidance for this repo. It is optional; Lupin
can run without it. Read the root `AGENTS.md` first.

## Where this repo lives

This repo is `gracecraft-software/lupin`. The owner clones it to
`~/Code/Projects/lupin` and OrbStack mounts it into the `jesus` sandbox at
`/code/lupin`, the same way `ghostbook.nix` is mounted at
`/code/ghostbook.nix`.

A loop agent does not edit the main checkout in `/code/lupin`. Each run gets
its own Git worktree in the loop state directory. See "State" in `AGENTS.md`.

## Lupin commands for a pass

The `delegation-loop` skill gives the full steps. The short list:

| Job | Command |
| --- | --- |
| Read the latest events | `lupin ledger read OWNER/REPO --json` |
| Rank open issues | `lupin roadmap --repo lupin --stage all --json` |
| Show dependencies | `lupin roadmap --repo lupin --dag` |
| Select a model and a machine | `lupin place N --explain` |
| Show provider quota | `lupin quota` |
| Claim an issue | `lupin claim OWNER/REPO#N --holder SESSION` |
| Claim related issues together | `lupin quest start --issue N --issue M` |
| See machines and loops | `lupin machines`, `lupin loops`, `lupin peek lupin` |
| Write a handoff | `/handoff`. It runs `lupin ledger append`. |

`lupin roadmap --repo` reads the checkout in `LUPIN_LOOP_CODE_DIR` (default
`/code`). Give the folder name of the checkout.

## Rule 1: push the branch, open the PR upstream

The PR goes to upstream `OWNER/REPO`, against `release/next`. A worker pushes its
branch to `origin`. If upstream denies the push, it pushes to the fork
`FORK_OWNER/REPO` and opens the PR upstream from the fork. Do not test access
with `gh api repos/OWNER/REPO --jq .permissions.push`. It can print `true` for
a token that cannot push. Try the push. Use the existing fork. Do not create a
new one. Do not open a PR or an issue on a fork.

1. Fetch upstream. From the repo checkout, run:

   ```bash
   git fetch origin
   ```

2. Create a worktree for your branch. Keep its path under `.claude/worktrees/`.
   Run:

   ```bash
   git worktree add <path> -b <branch> origin/release/next
   ```

3. Push the branch. First run `git push -u origin <branch>`. If upstream denies
   the push (for example, with a `403` error), push to the fork. Add the fork
   remote if it is missing, then push:

   ```bash
   git remote add fork https://github.com/FORK_OWNER/REPO.git
   git push -u fork <branch>
   ```

   Never push `main` or `release/next`. You may use `/ship` for steps 3 and 4.
   Check that it does not push `main` or `release/next`.

4. Open the PR on upstream. From a fork branch, add `--head FORK_OWNER:<branch>`:

   <!-- markdownlint-disable MD013 -->
   ```sh
   gh pr create --repo OWNER/REPO --base release/next --title "<title>" --body "Closes #N"
   ```
   <!-- markdownlint-enable MD013 -->

   If the PR opens, report its number to the orchestrator (the agent that
   dispatches and merges work). The orchestrator dispatches `/code-review`.

On a Herdr worker, use the Herdr worktree commands:

```bash
herdr worktree list --cwd <checkout>
git -C <checkout> fetch origin
herdr worktree create --branch <branch> --base origin/release/next --cwd <checkout>
```

A linked worktree shares its remotes with the checkout it came from. If that
checkout has neither an `origin` nor a `fork` remote, stop. Then report the
missing remote. Add `fork` only as step 3 says.

`lupin run` starts each loop from `origin/release/next` when the remote has it.
Otherwise it starts from the default branch: `origin/HEAD`, or else
`origin/main` or `origin/master`.

## Pull request and review

Every feature change goes through a pull request. The shared steps are in
"Review and merge each pull request" in the `delegation-loop` skill. In this
repo:

1. The base branch is `release/next` on upstream (`OWNER/REPO`). A feature PR
   targets that branch.
2. A worker follows rule 1 to push and open the PR. It can use `/ship` for
   the work and the report. If neither push works, the worker stops.
   Report the branch name and commit range. Do not review or merge the branch.
3. The orchestrator (the agent that dispatches and merges work) dispatches
   `/code-review` for every PR, including docs-only changes. The reviewer is
   not the worker. The reviewer's model tier is not lower than the worker's.
4. If the review finds a problem, dispatch a fix worker with the exact
   finding. It pushes the fix to the same branch with `git push origin <branch>`.
   Repeat until the reviewer approves the current head SHA. The head SHA is the
   newest commit ID on the branch.
5. The orchestrator posts the verdict on the PR.
6. The orchestrator merges into `release/next` only when all four are true:
   1. The PR is open on `OWNER/REPO`, with base `release/next`.
   2. A reviewer approves the current head SHA.
   3. The PR branch contains the current `release/next`. The ancestor check
      asks git whether one branch contains another. The check is in the
      `delegation-loop` skill.
   4. The repo's full gate, as its `AGENTS.md` defines it, passes.
   Record the approved SHA before you merge. Merge with
   `gh pr merge <PR-number> --repo OWNER/REPO --merge
   --match-head-commit <approved-SHA>`. Do not rebase. Do not force-push.
   See the paragraph that starts "Before a branch is merged" in the
   `delegation-loop` skill. A worker or reviewer never merges a pull request.
   The PR body has `Closes #N`. The merge into `release/next` does not close
   the issue. The orchestrator (the agent that dispatches and merges work)
   closes it manually right after the merge into `release/next`.
7. On every orchestrator cycle, merge each approved PR that passes step 6. Follow
   "Continuous merge of approved PRs" in the `delegation-loop` skill.

Changes to this policy go to upstream `main`, which the owner merges. Feature
work goes to upstream `release/next`. The owner merges `release/next` into
`main` when they choose.

At the end of a session, run `/handoff`.

### Preview server

This repo follows "Preview server" in the `delegation-loop` skill. For this
repo:

- The start command and port are in "Preview server" in `AGENTS.md`. The
  machine is not named yet.
- Set `LUPIN_LOOP_STATE_DIR` to a preview-only path. The default is
  `/var/lib/delegation-loop`, which is the real fleet state.
- Do not set `LUPIN_REDIS_HOST` to the fleet Redis unless the test needs it.
- The state directory does not change the Herdr session. New runs use the
  `lupin-loops` session (`SESSION_NAME` in `src/lupin/loop_runtime.py`). Older
  loops may use an old session until they stop.
- Do not use the start, stop, or run controls on the preview dashboard.

## Other rules

This repo is a plain Python package and a Nix flake, not a live system
config. It carries none of `ghostbook.nix`'s extra danger rules (no
activation scripts, no machine state to break). The one rule that still
applies is rule 1 above — push the branch and open the PR upstream.

Before you merge or dispatch anything, check for work already done:

```bash
git -C <checkout> fetch origin
git -C <checkout> branch --no-merged origin/release/next
```
