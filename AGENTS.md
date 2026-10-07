# lupin

`lupin` is a tool for multi-machine task loops. It does four jobs:

1. It picks a model and effort level for a task. Commands: `route`,
   `classify`, `review-route`.
2. It controls slots. A slot is a resource with a limit on how many
   callers can use it at one time. Commands: `acquire`, `hold`, `release`,
   `status`.
3. It serves a read-only dashboard for the loops. Command: `serve`.
4. It coordinates a fleet of machines: GitHub-issue claims, a machine
   registry, and quests (a set of issues worked together on one machine).
   Commands: `claim`, `renew-claim`, `release-claim`, `join`, `heartbeat`,
   `drain`, `undrain`, `machines`, `place`, `quest`, `reconcile`, `roadmap`.

One program, `lupin`, with subcommands. Run `lupin --help` for the full list.

## Commands

```
lupin route <category> <size> [--no-bmo] [--primary-effort E] [--json]
lupin classify --issue-json FILE [--diff-stat FILE] [--json]
lupin review-route (--category C --size S | --issue-json FILE) [--mode M] [--json]
lupin review-route --prefetch N[,N...] [--repo OWNER/REPO]
lupin acquire <slot> --holder H [--wait SECONDS] [--max N] [--ttl SECONDS]
lupin hold (--lease ID | <slot> --holder H --wait S) [--ttl SECONDS] -- <command>
lupin release --lease ID
lupin status [--json]
lupin serve [--bind 127.0.0.1] [--port 8788] [--roadmap REPO]
```

`route` and `classify` print a plain result by default (`model effort`, or
`category size`). Add `--json` for a JSON object instead.

`review-route --prefetch` fetches issue/PR text up front -- body, comments,
and (for a PR) reviews and a diff stat (changed files, additions/deletions,
no diff text) -- one JSON blob keyed by number, always printed as JSON (no
plain form; the output is nested data, not a one-line result). A number
that is neither an issue nor a PR gets `{"error": ...}` in its own slot
instead of failing the whole batch.

`acquire` prints a lease ID on success. Exit code 2 means the slot is full
— the caller should skip and try again later. Exit code 3 means the `redis`
backend cannot reach the coordinator and the slot has no local fallback.
The `local` backend's coordinator is the filesystem, so it never returns 3.

`hold` acquires a lease (or reuses one from `--lease`), runs `<command>`,
renews the lease while the command runs, and releases it when the command
ends — on a normal exit, a non-zero exit, or the command being killed by a
signal.

## How a slot's limit (`max`) works

A slot does not know its own limit until something tells it. The first
`acquire` call for a new slot sets the limit, from `--max` (default: 1).
Every later `acquire` call for that slot keeps the stored limit — `--max` on
a later call does nothing. `status` reads the same stored limit.

## State

The `local` backend stores slot state as files, under
`$LUPIN_STATE_ROOT` or `~/.lupin/slots/` by default. Each slot is one
directory; each active holder is one file inside it, holding a PID and an
expiry time. The `redis` backend (`slots_redis.py`) covers leases that
several machines must see. Its schema is in `docs/redis-schema.md`.

The dashboard caches GitHub data in `~/.local/state/lupin/cache.json`.

## Code layout

- `src/lupin/route.py`, `src/lupin/classify.py`, `src/lupin/model-tiers.json`
  — moved from `ghostbook.nix`. See the comments at the top of each file for
  the source commit.
- `src/lupin/slots.py` — the `local` slot backend. New code.
- `src/lupin/slots_redis.py` — the `redis` slot backend, with a fallback to
  `local` for the `bmo` slot.
- `src/lupin/_lease_runtime.py` — the `hold` subprocess/lease-renewal code
  shared by both slot backends above.
- `src/lupin/review_dispatch.py` — picks which lock a routed model needs.
- `src/lupin/serve.py`, `src/lupin/roadmap.py` — the dashboard. Moved from
  `ghostbook.nix`'s `hosts/jesus/loopgui/` (issue #204).
- `src/lupin/quota.py` — the `/usage` page's quota reads, split out of
  `serve.py`.
- `src/lupin/claims.py` — `claim`/`renew-claim`/`release-claim`: one GitHub
  issue claimed by one host at a time.
- `src/lupin/machines.py` — `join`/`heartbeat`/`drain`/`undrain`/`machines`:
  the fleet's machine registry.
- `src/lupin/quest.py` — `quest start`/`stop`/`focus`/`release`: a set of
  issues worked together on one machine.
- `src/lupin/place.py` — `place`: picks which fleet machine should run a
  task.
- `src/lupin/reconcile.py` — `reconcile`: applies the automatic claim/
  focus/quest release rules.
- `src/lupin/roadmap_cli.py` — the `roadmap` subcommand's own CLI surface
  (distinct from `roadmap.py`, which the dashboard also uses).
- `src/lupin/cli.py` — the `lupin` command. It owns the argument parsing for
  every subcommand; the modules above take an argument list instead of
  parsing their own.
- `tests/` — one test file per module above.

## Build and test

```
nix flake check                                    # build + test, all systems
nix shell nixpkgs#python3Packages.pytest -c pytest -v
```
