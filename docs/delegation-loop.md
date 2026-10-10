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

## Rule 1: push to the fork, not to the upstream repo

The sandbox `gh` account is `gracecraft-ro`. It cannot push to
`gracecraft-software/lupin` (`gh api repos/gracecraft-software/lupin --jq
.permissions` -> `push: false, triage: false`). It owns a fork,
`gracecraft-ro/lupin`, and can push there. So a worker pushes to the fork. It opens
the PR on the fork, against `release/next`. Closing or labeling an issue can
fail without triage access. Check before you write it into a report.

1. Fetch the fork. From the repo checkout, run:

   ```bash
   git fetch fork
   ```

2. Create a worktree for your branch. Keep its path under `.claude/worktrees/`.
   Run:

   ```bash
   git worktree add <path> -b <branch> fork/release/next
   ```

3. Use `/ship`. It runs `git push -u fork <branch>` and opens the PR. The PR
   base is `release/next` on the fork.

On a Herdr worker, use the Herdr worktree commands:

```bash
herdr worktree list --cwd /code/lupin
git -C /code/lupin fetch fork
herdr worktree create --branch <branch> --base fork/release/next --cwd /code/lupin
```

A linked worktree shares its remotes with the checkout it came from. If that
checkout has no `fork` remote, stop. Then report the missing remote.
Do not add a remote.

`lupin run` still starts each loop from `origin/HEAD`, which is upstream `main`.
It does not start from `fork/release/next`. Until the owner changes `lupin run`,
do not use it for feature work. Create feature worktrees with the Herdr
worktree commands above, or the manual steps in rule 1.

## Pull request and review

Every feature change goes through a pull request. The shared steps are in
"Review and merge each pull request" in the `delegation-loop` skill. In this
repo:

1. The base branch is the integration branch, `release/next`, on the fork
   (`gracecraft-ro/lupin`). A feature PR never targets upstream `main`.
2. A worker uses `/ship` (see rule 1). If the push fails, the worker stops.
   Report the branch name and commit range. Do not review or merge the branch.
3. The orchestrator dispatches `/code-review` for every PR,
   including docs-only changes. The reviewer is not the worker. The
   reviewer's model tier is not lower than the worker's.
4. If the review finds a problem, dispatch a fix worker with the exact
   finding. It uses `/ship` on the same PR. Repeat until the reviewer
   approves.
5. The orchestrator posts the verdict on the PR.
6. The orchestrator merges the PR on the fork into `release/next` only after
   the reviewer approves it and the gate passes. The gate is `nix flake check`
   (see "Build and test" in `AGENTS.md`). Merge with
   `gh pr merge --repo gracecraft-ro/lupin --merge`. Do not rebase. Do not
   force-push. Before merge, the branch must contain the current `release/next`.
   See the merge check in the `delegation-loop` skill. A worker or reviewer
   never merges a pull request. Close the issue in the same pass as the merge.

Changes to this policy go to upstream `main` for the owner to merge. The owner
opens that PR. Feature work goes to fork `release/next`.

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
applies is rule 1 above — push to the fork, not to the upstream repo.

Before you merge or dispatch anything, check for work already done:

```bash
git -C /code/lupin fetch fork
git -C /code/lupin branch --no-merged fork/release/next
```
