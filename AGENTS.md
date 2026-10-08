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
4. It coordinates a fleet of machines: issue claims, machine records, quests
   (sets of issues worked together on one machine), and shared repository
   events. Commands: `claim`, `renew-claim`, `release-claim`, `join`,
   `heartbeat`, `drain`, `undrain`, `machines`, `place`, `quest`, `reconcile`,
   `roadmap`, `ledger`.
   Loop lifecycle: `run`, `once`, `enable`, `disable`, `loops`, `stop`, `peek`,
   `attach`, `schedule`, `pause`, `resume`.

One program, `lupin`, with subcommands. Run `lupin --help` for the full list.

## Commands

```
lupin route <category> <size> [--no-bmo] [--primary-effort E] [--json]
lupin classify --issue-json FILE [--diff-stat FILE] [--json]
lupin fetch-models [--snapshot-file FILE] [--no-write] [--json]
lupin fetch-benchmarks [--force] [--json]
lupin quota [--json]
lupin review-route (--category C --size S | --issue-json FILE) [--mode M]
lupin review-route --prefetch N[,N...] [--repo OWNER/REPO]
lupin acquire <slot> --holder H [--wait SECONDS] [--max N] [--ttl SECONDS]
lupin hold (--lease ID | <slot> --holder H --wait S) [--ttl SECONDS] -- <command>
lupin release --lease ID
lupin status [--json]
lupin ledger append OWNER/REPO --event EVENT [--issue N] [--status S] \
  [--branch B] [--summary TEXT] [--highlights TEXT] [--evidence TEXT] \
  [--decisions TEXT] [--next TEXT] [--child N] [--json]
lupin ledger read OWNER/REPO [--limit N] [--json]
lupin serve [--bind 127.0.0.1] [--port 8788] [--roadmap REPO]
lupin agent [--machine M] [--batch N] [--poll-interval S]
lupin run <repo> [--machine M] [--platform claude|omp] [--note TEXT] [--resume]
lupin run --all [--machine M] [--note TEXT]
lupin once <when> [repo ...] [--platform claude|omp] [--note TEXT] [--resume]
lupin enable <repo> [--platform claude|omp]
lupin disable <repo>
lupin loops [repo] [--machine M] [--json]
lupin stop <repo> [--machine M] [--wait S] [--json]
lupin peek <repo> [LINES] [--machine M] [--json]
lupin attach <repo> [--machine M] [--print]
lupin schedule [--machine M] [--json]
lupin schedule cal <expr> [--machine M] [--json]
lupin schedule first <when> every <interval> [--machine M] [--json]
lupin pause [--machine M | --all] [--json]
lupin resume [--machine M | --all] [--json]
```

`route` and `classify` print a plain result by default (`model effort`, or
`category size`). Add `--json` for a JSON object instead.

`ledger append` stores events in Redis. It records the time and host. Repeat
digest options to add more than one item. Repeat `--child` for each split
issue. `ledger read --json` returns the latest 10 events, oldest first.
Use `--limit N` to choose another positive count. Both commands exit 3 if
Redis is unavailable. The roadmap shows no ledger annotations and a warning.
It ignores `.loop/loop-state.json`.

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

`fetch-models` checks which model IDs each subscription can call today and
what they cost. It saves the result to
`~/.local/state/lupin/model-snapshot.json` unless `--no-write` is given. It
also publishes the snapshot to Redis, so the PiHome dashboard shows pulls
from other fleet machines. A Redis error leaves the local file in place and
prints a warning. Model lists for opencode-go and claude come from live
provider calls. Codex uses `omp models` when available; otherwise it uses
models.dev and is marked `live: false`. Prices
come from models.dev, matched by model ID. A model with no match has
`price: null`. Promo pricing has no live source, so `promo` stays `null`.

`run <repo>` starts a Lupin worker and a Herdr workspace. `run --all`
starts every enabled repo. Add `--machine M` to use the signed fleet queue
on another machine; `run --all --machine M` starts its enabled repos.
`once now` starts the listed repos. If you omit repos, it starts every
enabled repo. A future `once` run uses the same default. It fails if no repo
is enabled. `schedule first` sets the first timer run. Use `+2h` for a
relative time or a calendar expression such as `tomorrow 09:00`. The
interval controls later runs.
`enable` and `disable` edit the local repo list.

`loops` reads state from the Herdr API. It reports the agent state, backend,
session, workspace and pane IDs. It does not infer state from pane text.
Each repo has one Herdr session. Each run has one workspace. A completed
workspace stays open for review and blocks another run until you stop it.
A Herdr state of `needs_attention` means the workspace needs review. If
Herdr cannot provide state, Lupin reports `unknown`. Both states block a
new run until you resolve the state.

`run`, `stop`, `peek`, `schedule`, `pause`, and `resume` can target any
fleet machine. Local actions run through Lupin. Remote actions use the signed
Redis queue and wait up to `--wait` seconds (default 20). The target must run
`lupin agent` with Redis access and its own signing key. The sender also needs
that key. Inject it only into the Lupin process with protected secret
management. Do not pass it in command arguments or save it in global
environment settings. Exit code 4 means the result is unknown after the
wait; check it with `lupin cmd status <id>`. `stop`, `peek`, and `attach`
need `--machine` if Lupin cannot find one machine for the repo.

`attach` never uses the queue. It opens Herdr here or connects to the remote
Herdr server over SSH. `--print` shows the command instead of running it.
Remote attach needs a local SSH target in `~/.config/lupin/ssh-targets`, one
`<machine> <target>` pair per line. Blank lines and `#` comments are ignored:

```
jesus   ghosta@jesus.local
mini    ghosta@mini.tailnet.ts.net
```

This file is local config, not fleet state. Herdr must be installed on both
machines. SSH must allow key-based access. A missing target makes `attach`
fail instead of guessing a hostname.

`fetch-benchmarks` gets a quality score for each model. It does not call a
benchmark API. Instead it runs a sandboxed Claude agent once a day. That
agent searches the web and reports back a score, a source, and a date —
never a guess. The result goes into one shared Redis key, not a file on
disk, so every machine sees the same score and only one machine does the
work each day. Use `--force` to pull fresh data right now, skipping the
daily cache (it still waits its turn if another machine is mid-pull).
Plain output shows numeric score count and snapshot age. "Live" means the
agent returned a valid snapshot. It does not mean every model has a score or
that this command fetched the snapshot.

`quota` shows how much of each provider's quota is left: percent left,
which window (5 hours, 7 days, 30 days), and time to reset. Quota is one
account per provider, shared by the whole fleet, not a per-machine fact —
a machine with real provider logins reads it live and publishes it to one
shared Redis key; every other machine reads that key instead of calling
the provider itself. A provider with no reading yet (no machine with its
logins has run `quota` recently) shows "no quota data cached yet".

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

Loop state is stored under `$LUPIN_LOOP_STATE_DIR` or
`/var/lib/delegation-loop`. Lupin keeps repo metadata in `herdr-loops/`,
run prompts in `notes/`, stop reports in `reports/`, and one-off schedules
in `once/`. `locks/` serializes local start and stop actions. Herdr keeps
its own session and workspace state.
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
- `src/lupin/free_gate.py` — routes, then reserves the fleet `bmo` Redis
  slot for a `bmo:` pick before calling it final (issue #37).
- `src/lupin/serve.py`, `src/lupin/roadmap.py` — the dashboard. Moved from
  `ghostbook.nix`'s `hosts/jesus/loopgui/` (issue #204).
- `src/lupin/quota.py` — the `/usage` page's quota reads, split out of
  `serve.py`.
- `src/lupin/quota_cache.py` — `lupin quota`'s shared Redis cache: one
  machine's real quota reading, published for the whole fleet to read
  (issue #38).
- `src/lupin/model_fetch.py` — `fetch-models`: a daily snapshot of model
  IDs and prices per subscription.
- `src/lupin/claims.py` — `claim`/`renew-claim`/`release-claim`: one GitHub
  issue claimed by one host at a time.
- `src/lupin/ledger.py` — appends and reads shared repository events in Redis.
- `src/lupin/machines.py` — `join`/`heartbeat`/`drain`/`undrain`/`machines`:
  the fleet's machine registry.
- `src/lupin/loops.py` — signed local-or-remote dispatch for loop controls.
  Attach uses Herdr directly over SSH.
- `src/lupin/loop_runtime.py` — Herdr sessions, workspaces, state, reports,
  schedules, and systemd worker lifecycle.
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
