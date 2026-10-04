# Brief: lupin repo prep (sub-issues #202, #203, #205 of #198's plan)

## Context

`lupin` is a planned new public repo (`gracecraft/lupin`) that doesn't exist
on GitHub yet — creating it needs the owner's own credentials, tracked as
issue #201, blocked on the owner. Everything downstream of #201 is blocked
on it too. This job is prep work done *before* #201 lands: build the repo's
actual content as a complete, tested, standalone git repo at
`/home/ghosta/jobs/lupin-prep` (already `git init`'d, branch `main`, no
remote), so that once the owner finishes #201, pushing this is a copy + one
`git push`, not a build-from-scratch.

This is **not** part of the `ghostbook.nix` repo and has no relationship to
its git history. Read source files out of `/code/ghostbook.nix` (absolute
paths — that mount is readable from here) to see what's being moved and
copy/adapt from it, but never edit anything under `/code/ghostbook.nix`
itself. Everything you write goes in `/home/ghosta/jobs/lupin-prep`.

Full design context: `gh issue view 198 --repo gracecraft/nix --json
body,comments` (the architecture plan and its conflict-resolution follow-up
comment — read both). Also read issues `202`, `203`, and `205` directly
(`gh issue view 202/203/205 --repo gracecraft/nix`) — this brief covers all
three combined.

## The task

Build three things in `/home/ghosta/jobs/lupin-prep`:

### 1. Repo skeleton and packaging (issue #202)

- `pyproject.toml` for a package named `lupin`, Python 3, standard library
  only for now (no `redis` dependency yet — that's a later sub-issue).
- `flake.nix` with:
  - `packages.<system>.default` for `aarch64-linux`, `x86_64-linux`, and
    `aarch64-darwin`, built via `buildPythonApplication` (no pip)
  - `checks.<system>.default` running the test suite
  - an empty `nixosModules.default` (just a stub — later sub-issues fill it
    in, don't invent config options that don't have a consumer yet)
- `AGENTS.md` at the repo root, with `CLAUDE.md` as a symlink to it (match
  how `/code/ghostbook.nix/CLAUDE.md` is a symlink — check it with `ls -la
  /code/ghostbook.nix/CLAUDE.md`).
- `docs/delegation-loop.md` with the same no-push-to-GitHub-from-the-sandbox
  rule `/code/ghostbook.nix/docs/delegation-loop.md` states in its rule 1 —
  adapt the wording for a repo that (for now) has no GitHub remote at all,
  since `origin` won't point anywhere until the owner pushes this.

### 2. Move the routing code (issue #203)

Copy and adapt (don't blindly `cp` — these files assume they live three
directories under `modules/ai/` in `ghostbook.nix`, that assumption needs to
change for a standalone package):

- `/code/ghostbook.nix/hosts/jesus/loopgui/route.py`
- `/code/ghostbook.nix/hosts/jesus/loopgui/classify.py`
- `/code/ghostbook.nix/hosts/jesus/loopgui/test_route.py`
- `/code/ghostbook.nix/hosts/jesus/loopgui/test_classify.py`
- `/code/ghostbook.nix/modules/ai/model-tiers.json`

`classify.py` imports `_size`/`_label_names` from
`/code/ghostbook.nix/hosts/jesus/loopgui/roadmap.py` — find them (`grep -n
"_size\|_label_names" roadmap.py`) and copy just those functions in, not the
whole 65K-line file; note in a comment where they came from and the source
commit (`git -C /code/ghostbook.nix log -1 --format=%H -- hosts/jesus/loopgui/roadmap.py`).

Build a single `lupin` CLI entry point (one executable, subcommands — not
separate scripts) with this exact contract, decided in #198's plan:

```
lupin route <category> <size> [--no-bmo] [--primary-effort E] [--json]   -> {"model", "effort"}
lupin classify --issue-json FILE [--diff-stat FILE] [--json]             -> {"category", "size"}
```

Look at `/code/ghostbook.nix/hosts/jesus/loopgui/review_dispatch.py`'s
`_parse_args`/`main` for the existing argparse style this project uses
(it currently wraps `route`/`classify` as a one-off CLI for issue #185 —
your version is the general-purpose one, with subcommands, that #204
will later point at instead of #185's wrapper).

### 3. Slot commands, local backend (issue #205)

Add `lupin acquire`, `lupin hold`, `lupin release`, `lupin status` as
subcommands of the same CLI, exactly as specified in #198's plan:

```
lupin acquire <slot> --holder H [--wait SECONDS]                   -> lease id
lupin hold (--lease ID | <slot> --holder H --wait S) -- <command>   renew while <command> runs, release on exit
lupin release --lease ID
lupin status [--json]
```

Keep this generic — **do not** hardcode anything about `loop-claude-*`
systemd units or `ghostbook.nix`'s specific lock files. That wiring is a
separate, later sub-issue (#206) that happens inside `ghostbook.nix` once
this package is a real dependency; this package only needs to know "a slot
has a name and a max concurrent holder count."

Design for the `local` backend:
- A slot is a directory under a configurable state root (default
  `~/.lupin/slots/<name>/`), one file per active holder, holder's PID and a
  monotonic deadline (now + TTL) in the file content.
- `acquire`: open with `fcntl.flock` for the directory's lock file, prune
  any holder file past its deadline, count what's left; if under the slot's
  max, write a new holder file and return its lease id (exit 0); otherwise
  exit 2.
- `hold`: acquire (blocking up to `--wait` seconds, polling), then run
  `<command>` as a subprocess, renewing the lease periodically while it
  runs, releasing on exit (success or failure) — release must run even if
  the child is killed by a signal.
- `release`: remove the holder file for that lease id. Exit 3 if the
  coordinator (here, just the local filesystem) can't be reached — not
  really applicable for `local`, but keep the exit code reserved so the
  future `redis` backend (a later sub-issue, not this one) can reuse the
  same contract without a breaking change.
- `status`: list every slot under the state root with its current
  holder count and max (max isn't known locally without a config — read it
  from a per-slot config file or a `--max` flag on `acquire`'s first call
  that creates the slot; use your judgment, document the choice).

## Tests

- `route`/`classify`: port the existing tests, adjust import paths, confirm
  they still pass unmodified in behavior (you're moving the code, not
  changing its logic).
- Slot commands: a slot's max is respected (third `acquire` on a max-2 slot
  exits 2); a holder past its TTL is pruned and its spot freed; `hold`
  releases on normal command exit and on the command being killed
  (`SIGTERM`/`SIGKILL`) — test this for real, don't assume `finally` runs
  under every kill signal, verify it; `status --json` reports accurate
  counts.

## Acceptance

```bash
cd /home/ghosta/jobs/lupin-prep
nix flake check 2>&1 | tail -40
```

Must pass. Run the Python tests directly too and show the output:

```bash
nix shell nixpkgs#python3Packages.pytest -c pytest -v
```

## Deliverable

Commit everything on `main` in `/home/ghosta/jobs/lupin-prep` (this repo has
no remote — do not try to push anywhere, there's nowhere to push to yet).
End the commit message with:

```
Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
```

Report back: what you built, the full test output, the `nix flake check`
output, exactly what you copied vs. wrote fresh, and anything you had to
guess about or decide without a clear spec (the `status` max-tracking
question above, especially). Do not comment on or close issues #202, #203,
or #205 yourself — I'll review the actual code and do that.
