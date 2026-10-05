# lupin — the delegation loop

This file is the loop's entry point for this repo. Read the root
`AGENTS.md` first.

## Where this repo lives

This repo is `gracecraft/lupin`. The owner clones it to
`~/Code/Projects/lupin` and OrbStack mounts it into the `jesus` sandbox at
`/code/lupin`, the same way `ghostbook.nix` is mounted at
`/code/ghostbook.nix`.

## Rule 1: do not push to GitHub from the sandbox

A sandbox agent's `gh` token cannot push code to GitHub. This holds for
`lupin` the same way it holds for `ghostbook.nix` (see that repo's
`docs/delegation-loop.md`, rule 1).

To work on an issue:

1. Clone it to a task directory, and work on a branch there, not in the
   mount itself:

   ```bash
   test ! -e ~/jobs/lupin-<task> || { echo "job dir exists"; exit 1; }
   git clone --shared /code/lupin ~/jobs/lupin-<task>
   ```

2. `origin` in the clone points back at the mount. When the work is done,
   push the branch there, not to GitHub:

   ```bash
   git push origin <branch>
   ```

3. Say the branch name in the report. The owner pushes it to GitHub from
   the Mac.

Branch from `origin/main` — check it is the current tree first:

```bash
git ls-remote /code/lupin
```

## Other rules

This repo is a plain Python package and a Nix flake, not a live system
config. It carries none of `ghostbook.nix`'s extra danger rules (no
activation scripts, no machine state to break). The one rule that still
applies is rule 1 above — no push to GitHub.

Before you merge or dispatch anything, check for work already done:

```bash
git -C /code/lupin branch --no-merged main
```
