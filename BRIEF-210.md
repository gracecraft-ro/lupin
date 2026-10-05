# Brief: lupin `redis` backend (issue #210 of #198's plan)

## Context

You're continuing work in `/home/ghosta/jobs/lupin-prep` — a standalone
staging repo for the future `gracecraft/lupin` (not pushed anywhere yet,
no remote, waiting on issue #201). It already has, on `main`:
- `src/lupin/{route,classify,slots,cli}.py` (#202/#203/#205 — routing,
  classification, the `local` slot-lease backend)
- `docs/redis-schema.md` (#207 — the exact key shapes, Lua script
  behavior, TTLs, ACL command list, and fallback rules you must implement)

Read `docs/redis-schema.md` first — it is the spec for this task, already
decided, not something to redesign. Read `src/lupin/slots.py` and
`src/lupin/cli.py` second — the `redis` backend you're adding must satisfy
the exact same CLI contract the `local` backend already does:

```
lupin acquire <slot> --holder H [--wait SECONDS]                   -> lease id
lupin hold (--lease ID | <slot> --holder H --wait S) -- <command>   renew while <command> runs, release on exit
lupin release --lease ID
lupin status [--json]
```

Also read `gh issue view 198 --repo gracecraft/nix --json comments` section
3 ("Topology") and section 4 ("Auth and network") for the fallback
reasoning in prose, if `docs/redis-schema.md`'s table alone leaves anything
ambiguous.

## The task

Add a `redis` backend alongside the existing `local` one in
`src/lupin/slots.py` (or split into `src/lupin/slots_redis.py` if that
reads cleaner — your call, but keep one shared CLI surface in `cli.py`;
don't duplicate the argument parsing). A `--backend local|redis` flag (or
an env var `LUPIN_BACKEND`, your call, document whichever you pick)
selects it. Implement:

- **Acquire**: the Lua script described in `docs/redis-schema.md` — drop
  expired holders, renew if the caller already holds it, add if under max,
  else return 0 (busy, same exit code 2 the `local` backend already uses
  for "full").
- **Release**: the compare-and-delete Lua script — only the current holder
  can release its own entry.
- **Fallback behavior from the schema's table**: when Redis is
  unreachable (2s connect timeout, 1 retry — use `redis.Redis(socket_connect_timeout=2)`
  and catch `redis.exceptions.ConnectionError`/`TimeoutError`), the `bmo`
  slot falls back to the `local` backend's own lock file, with a warning
  written to stderr (this package has no journal access — the brief for a
  later ghostbook.nix-side issue wires that warning into the actual
  systemd journal; here, a clear stderr line is the full requirement).
  Claims aren't implemented yet (that's #212) so don't build claim fallback
  logic — just leave a `NotImplementedError` with a comment citing #212 if
  `cli.py` doesn't already have a `claim` subcommand (it shouldn't).
- **`status --json`** on the `redis` backend reports the same shape the
  `local` one does (holder count, max), read from the sorted set, not
  mutating it.

## Dependency

Add `python3Packages.redis` to `pyproject.toml` (this is the first
dependency beyond the standard library — `docs/redis-schema.md`'s own
plan source says phase 3 is where this lands, and that's what you're
building). Update `flake.nix`'s `buildPythonApplication` to list it in
`dependencies`.

## Files you may change

`src/lupin/`, `pyproject.toml`, `flake.nix`, `tests/`. Do not touch
`docs/redis-schema.md`, `docs/delegation-loop.md`, or anything under `.git`.

## Tests

Per #210: tests spin up a temporary `redis-server` from nixpkgs inside
`checks`, not a mock. Use `pytest`'s fixture machinery to start a real
`redis-server --port <ephemeral>` subprocess per test module (or session
scope — your call), pointed at a throwaway unix socket or a high port, and
tear it down after. Cover:
- acquire respects max (third acquire on a max-2 slot busies out)
- a holder past TTL is pruned, its spot freed
- release is compare-and-delete: a non-holder's release attempt fails
  without touching the real holder's entry
- `status --json` matches real sorted-set contents
- unreachable Redis (point the client at a port nothing listens on) falls
  back to the `local` backend for the `bmo` slot, with a warning on stderr
  — assert on both the fallback actually working (slot still acquired) and
  the warning text, don't just assert no exception was raised

`nix flake check` must stay green — if `python3Packages.redis` needs a
pinned nixpkgs revision check (some versions lag), say so in your report if
you hit it; don't silently downgrade an unrelated dependency to dodge it.

## Deliverable

Commit on `main` in `/home/ghosta/jobs/lupin-prep` (still no remote — don't
try to push). End the commit message with:
```
Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
```
Report: what you built, the `--backend`/env-var choice and why, full test
output, `nix flake check` output, and anything you had to guess about
(especially: what "warning in the journal" should mean for a package with
no systemd access — say what you did instead and why it's enough for this
issue, leaving the real journal wiring to whichever ghostbook.nix issue
switches jesus's backend). Do not comment on or close #210 yourself.
