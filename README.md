# lupin

Lupin runs coding-agent loops on a group of machines (a fleet). It does
four jobs:

1. **Routing.** It selects a model and an effort level for a task based on available quota. (Supports Claude, OpenAI, and OpenCode Go subscription quota readouts)
2. **Slots.** It limits how many callers use a resource at the same time.
3. **Dashboard.** It shows the loops in a web page, and lets you start and
   stop them.
4. **Fleet.** It shares issue claims, quests, machine records, quota, and
   events between machines through Redis. It ranks open issues and starts
   loops on workers.

> **Status:** Lupin is early software. One owner uses it on one fleet. Some
> defaults and data files are specific to that fleet. Read
> [Known limits](#known-limits) before you install it.

## Requirements

| Item | Necessary for | Notes |
| --- | --- | --- |
| Nix with flakes | All | The supported way to build Lupin. |
| Python 3.11 or later | All | Nix supplies it. |
| Redis | Fleet commands, `redis` slot backend, dashboard data | Not necessary for `route` or the `local` slot backend. |
| `gh` (GitHub CLI) | Claims, roadmap, dashboard | The Nix package adds it to `PATH`. |
| `git` | Loops, roadmap | The Nix package adds it to `PATH`. |
| systemd (Linux) | Loop workers, timers | Not available on macOS. |
| Herdr | Loop workers | The Lupin package adds it to `PATH`. Set `LUPIN_HERDR_BIN` to use another binary. |
| `omp` | OMP loops, `fetch-benchmarks`, some quota data | The default adds it to `PATH` from a pinned input. `without-omp` saves about 228 MiB; install `omp` separately for OMP tasks. |

## Install

Run Lupin one time, without installing it:

```sh
nix run github:gracecraft-software/lupin -- --help
```

Build it from a checkout:

```sh
nix build .
./result/bin/lupin --help
```

To install it on a NixOS or nix-darwin machine, add this flake as an input
and add `packages.<system>.default` to `environment.systemPackages`.

## Quick start

These commands do not need Redis.

Select a model for a medium-size prose task:

```sh
$ lupin route prose m
sonnet high
```

Get a slot lease, then look at the slot:

```sh
$ lupin acquire demo --holder me --max 1
demo:7d023867bcbed45f
$ lupin status
demo: 1/1
$ lupin release --lease demo:7d023867bcbed45f
```

Run a command while you hold a lease. Lupin releases the lease when the
command stops:

```sh
lupin hold demo --holder me --wait 5 -- echo inside-lease
```

The route categories are `coding`, `general`, `frontend-ui`, `translation`,
`prose`, and `cad-spatial`. If `LUPIN_MODEL_TIERS` is not set, Lupin uses
the bundled `src/lupin/model-tiers.json` file.

Set `LUPIN_MODEL_TIERS` to a JSON file to use your own model tiers. The
category selected by `route` must exist in the file. Each category has a
`tiers` object with one or more of `tier0`, `tier1`, and `tier2`. Each tier
is a list of ordered `{model, effort}` choices. The first choice is tried
first. Effort must be `low`, `medium`, `high`, or `xhigh`. You can omit a tier.
If a route needs it, Lupin uses the next higher tier. If no higher tier exists,
the route reports an error. Optional fields are `source`, `last_verified`, and
`note`. A top-level key that starts with `_` is ignored. The Models page shows
the file path.

```json
{
  "coding": {
    "tiers": {
      "tier1": [
        {"model": "opencode-go/glm-5.3", "effort": "high"}
      ]
    }
  }
}
```

## Fleet setup

A fleet has:

- **One coordinator.** This is a Redis server that all machines can reach.
  Do not bind it to `0.0.0.0`. Use a private network, for example a
  tailnet.
- **Workers.** Each worker runs `lupin agent` and runs loops in Herdr.
- **Optional: one dispatch machine.** It runs `lupin serve` and the
  `fleet-run` timer. It does not run loops.

Set the Redis connection for each machine:

| Variable | Use |
| --- | --- |
| `LUPIN_REDIS_HOST` | Redis host name. |
| `LUPIN_REDIS_PORT` | Redis port. |
| `LUPIN_REDIS_USERNAME` | Redis ACL user. |
| `LUPIN_REDIS_PASSWORD` | Redis password. Give it through a secret manager. Do not put it in a command line. |

Remote loop controls use signed commands. Each worker needs its own signing
key. Lupin reads the keys from `LUPIN_CMD_SIGNING_KEYS_DIR`, or from the
systemd `CREDENTIALS_DIRECTORY`. The file name is the worker name.

The Redis key layout is in `docs/redis-schema.md`.

## NixOS module

`nixosModules.default` writes the service user, `lupin-agent.service`,
`lupin-serve.service`, the `lupin-quota` timer, and the `delegation-loop`
timer. Set `role` to `worker`, `dispatch`, or `both`.

```nix
services.lupin = {
  enable = true;
  role = "worker";
  redis.host = "COORDINATOR_HOST";
  redis.passwordFile = "/run/secrets/redis-password";
  signingKeysDir = "/run/secrets/lupin-keys";
};
```

The module needs these secret files:

- `redis.passwordFile`: a file with the Redis password. The module loads it
  as the systemd credential `redis-password`. No unit gets the password in
  its environment. Skip this option if the coordinator has no password.
- `signingKeysDir`: a directory with one command-queue key for each worker.
  The file name is the worker name and the file holds that worker's key.
  The module reads the key of the local machine from this directory.

The default `serve.bind` is `127.0.0.1` and cannot be `0.0.0.0`. The default
`serve.port` is 8788. The default package is the flake's
`packages.<system>.default`.

Without `fleetConfig`, the module writes `/etc/lupin/fleet.json` from the
`redis` options. To use one file for several machines, set `fleetConfig` to
that file.

The module also writes the sudo rules for the loop control path. The loop
user can start and stop `lupin-*` units and the `delegation-loop` timer.

## Dashboard

```sh
lupin serve --bind 127.0.0.1 --port 8788
```

Then open `http://localhost:8788`. To see it from another computer, use an
SSH tunnel:

```sh
ssh -N -L 8788:127.0.0.1:8788 MACHINE
```

The Repos and Roadmap pages read the Git checkouts in `LUPIN_LOOP_CODE_DIR`
(default `/code`).

## Commands

Run `lupin --help` for all commands. Run `lupin <command> --help` for the
options of one command. `AGENTS.md` gives the full behavior of each
command.

| Job | Commands |
| --- | --- |
| Select a model | `route`, `classify`, `review-route`, `place` |
| Model data | `fetch-models`, `fetch-benchmarks` |
| Provider quota | `quota` |
| Slots | `acquire`, `hold`, `release`, `status` |
| Rank open issues | `roadmap` |
| Claim an issue | `claim`, `renew-claim`, `release-claim` |
| Work a set of issues on one machine | `quest` |
| Release old claims and quests | `reconcile` |
| Shared events and handoffs | `ledger` |
| Machines | `join`, `heartbeat`, `drain`, `undrain`, `machines` |
| Loops | `enable`, `disable`, `run`, `once`, `fleet-run`, `loops`, `stop`, `peek`, `attach`, `schedule`, `pause`, `resume` |
| Remote commands | `agent`, `cmd` |
| Dashboard | `serve` |

Examples:

```sh
lupin roadmap --repo my-repo --stage all    # rank open issues
lupin place 123 --explain                   # select a machine for issue 123
lupin quota                                 # show provider quota
lupin quest start --issue 123 --issue 124   # claim related issues as a quest
lupin quest                                 # list quests and their progress
lupin ledger read OWNER/REPO                # read the latest events
```

## Development

Enter the development shell. It has Python, pytest, and Redis:

```sh
nix develop
```

Run the tests from the repo root:

```sh
pytest -q
```

Run the full gate before you open a pull request:

```sh
nix flake check
```

Some tests start a local `redis-server`. Do not set `LUPIN_REDIS_HOST` to a
shared Redis when you run the tests.

## Contributing

Open pull requests against the `main` branch. The owner merges approved pull
requests into `main`. The loop orchestrator can also merge, when the four
conditions in `AGENTS.md` are true. A reviewer who did not write the change must
approve it. `AGENTS.md` gives the full rules.

## Known limits

- There is no license file yet. Until there is one, you have no right to
  copy or change this code.
- Some defaults name the owner's machines and user name.
- Lupin is tested only on Linux. The flake builds for macOS, but loop
  workers need systemd.

## Documentation

| File | Content |
| --- | --- |
| `AGENTS.md` | Full command behavior and rules for agents that work on this repo. |
| `docs/redis-schema.md` | Redis keys and their format. |
| `docs/delegation-loop.md` | The loop policy for this repo. |
| `skills/` | Shared skills for loops. `delegation-loop` is the main one. |
| `docs/ui-mockups.md` | Screenshots of the dashboard mockups. |
| `THIRD_PARTY_NOTICES.md` | Licenses of adapted third-party text. |
