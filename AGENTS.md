# lupin

`lupin` is a tool for multi-machine task loops. It does four jobs:

1. It picks a model and effort level for a task. Commands: `route`,
   `classify`, `review-route`. `fetch-models` fetches a daily snapshot of
   which models each subscription can call today, and their prices.
2. It controls slots. A slot is a resource with a limit on how many
   callers can use it at one time. Commands: `acquire`, `hold`, `release`,
   `status`.
3. It serves a dashboard for the loops, for both viewing and controlling
   them (start/stop/close a loop, add or remove a repo, trigger a run,
   adjust a machine's slots). Command: `serve`.
4. It coordinates a fleet of machines: GitHub-issue claims, a machine
   registry, and quests (a set of issues worked together on one machine).
   Commands: `claim`, `renew-claim`, `release-claim`, `join`, `heartbeat`,
   `drain`, `undrain`, `machines`, `place`, `quest`, `reconcile`, `roadmap`.

One program, `lupin`, with subcommands. Run `lupin --help` for the full list.

## Commands

```
lupin route <category> <size> [--no-bmo] [--primary-effort E] [--json]
lupin classify --issue-json FILE [--diff-stat FILE] [--json]
lupin fetch-models [--snapshot-file FILE] [--no-write] [--json]
lupin fetch-benchmarks [--force] [--json]
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

`fetch-models` checks, per subscription (opencode-go, claude, codex), which
model IDs it can call today and what each one costs. It saves the result to
`--snapshot-file` (default: `~/.local/state/lupin/model-snapshot.json`)
unless `--no-write` is given. Model lists for opencode-go and claude come
from a live call to each provider; codex has no live source reachable from
this tool, so it falls back to a public catalog (models.dev) and is marked
`live: false`. Prices come from that same public catalog for all three,
matched by model ID — a model with no match gets `price: null`, never a
guessed number. Promo pricing (a free or discounted period with its own
start and end) has no live source yet either; the `promo` field stays
`null` until issue #18 adds one.

`fetch-benchmarks` gets a quality score for each model. It does not call a
benchmark API. Instead it runs a sandboxed Claude agent once a day. That
agent searches the web and reports back a score, a source, and a date —
never a guess. The result goes into one shared Redis key, not a file on
disk, so every machine sees the same score and only one machine does the
work each day. Use `--force` to pull fresh data right now, skipping the
daily cache (it still waits its turn if another machine is mid-pull).

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
- `src/lupin/model_fetch.py` — `fetch-models`: a daily snapshot of model
  IDs and prices per subscription.
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
