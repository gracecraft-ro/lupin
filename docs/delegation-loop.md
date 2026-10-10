# lupin — the delegation loop

This file gives loop-specific guidance for this repo. It is optional; Lupin
can run without it. Read the root `AGENTS.md` first.

## Where this repo lives

This repo is `gracecraft-software/lupin`. The owner clones it to
`~/Code/Projects/lupin` and OrbStack mounts it into the `jesus` sandbox at
`/code/lupin`, the same way `ghostbook.nix` is mounted at
`/code/ghostbook.nix`.

A loop agent does not work in `/code/lupin`. Each run gets its own Git
worktree in the loop state directory. See "State" in `AGENTS.md`.

## Rule 1: push to the fork, not to the upstream repo

The sandbox `gh` account is `gracecraft-ro`. It cannot push to
`gracecraft-software/lupin` (`gh api repos/gracecraft-software/lupin --jq
.permissions` -> `push: false, triage: false`). It owns a fork,
`gracecraft-ro/lupin`, and can push there. So a worker pushes to the fork and
opens a PR to the upstream repo. Closing or labeling an issue can fail
without triage access. Check before you write it into a report.

To work on an issue:

1. Clone it to a task directory, and work on a branch there, not in the
   mount itself:

   ```bash
   test ! -e ~/jobs/lupin-<task> || { echo "job dir exists"; exit 1; }
   git clone --shared /code/lupin ~/jobs/lupin-<task>
   git -C ~/jobs/lupin-<task> remote add fork https://github.com/gracecraft-ro/lupin.git
   ```

   `origin` in the clone points back at the mount. Do not push to it for a
   PR.

2. Branch from the current `main`. Check that it is current first:

   ```bash
   git ls-remote /code/lupin
   ```

3. Use `/ship`. It runs `git push -u fork <branch>` and opens the PR.

On a Herdr worker you can use a Herdr worktree instead of a clone:

```bash
herdr worktree list --cwd /code/lupin
herdr worktree create --branch <branch> --base main --cwd /code/lupin
```

The worktree is a linked Git worktree, not a clone. It has no `fork` remote,
so `/ship` must add one before it pushes.

## Pull request and review

Every change goes through a pull request. The shared steps are in "Review and
merge each pull request" in the `delegation-loop` skill. In this repo:

1. The base branch is the integration branch, `release/next`, on the fork
   (`gracecraft-ro/lupin`). A PR never targets upstream `main`.
2. A worker uses `/ship` (see rule 1). If the push to the fork fails, it
   reports the branch name and commit range. The reviewer reviews that
   branch. Do not merge it until a PR exists on the fork.
3. The orchestrator dispatches `/code-review` for every PR or branch,
   including docs-only changes. The reviewer is not the worker. The
   reviewer's model tier is not lower than the worker's.
4. If the review finds a problem, dispatch a fix worker with the exact
   finding. It uses `/ship` on the same PR or branch. Repeat until the
   reviewer approves.
5. The orchestrator posts the verdict on the PR (or on the issue, for a
   branch).
6. The orchestrator merges into `release/next` only after the reviewer
   approves and the gate passes. The gate is `nix flake check` (see "Build
   and test" in `AGENTS.md`). Merge with
   `gh pr merge --repo gracecraft-ro/lupin --merge`. Do not rebase. Do not
   force-push. A worker or reviewer never merges. Close the issue in the same
   pass as the merge.

At the end of a session, run `/handoff`.

### Preview server

This repo follows "Preview server" in the `delegation-loop` skill. For this
repo:

- The start command, port, and machine are in "Preview server" in `AGENTS.md`.
- The dashboard command is `lupin serve --bind 127.0.0.1 --port PORT`.
- Set `LUPIN_LOOP_STATE_DIR` to a preview-only path. The default is
  `/var/lib/delegation-loop`, which is the real fleet state.
- Do not set `LUPIN_REDIS_HOST` to the fleet Redis unless the test needs it.
- The state directory does not change the Herdr session. Herdr always uses the
  `lupin-loops` session (`SESSION_NAME` in `src/lupin/loop_runtime.py`).
- Do not use the start, stop, or run controls on the preview dashboard.

## Other rules

This repo is a plain Python package and a Nix flake, not a live system
config. It carries none of `ghostbook.nix`'s extra danger rules (no
activation scripts, no machine state to break). The one rule that still
applies is rule 1 above — push to the fork, not to the upstream repo.

Before you merge or dispatch anything, check for work already done:

```bash
git -C /code/lupin branch --no-merged main
```
