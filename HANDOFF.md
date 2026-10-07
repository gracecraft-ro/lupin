# HANDOFF: Issue #35 — centralize GitHub issue-status reads behind a shared cache

## Goal

One machine fetches a repo's read-only GitHub issue data (via `gh`); every
other machine reads a shared, TTL'd Redis cache instead of calling `gh`
directly. Covers 7 call sites across `place.py`, `quest.py`,
`roadmap_cli.py`, `roadmap.py`. No mutating `gh` calls are touched.

Mid-task correction from the repo owner: the fetcher is not "whichever
machine wins the Redis lock race" — it's a hard pin to one named machine,
`pihome` (the fleet's always-on dashboard host). A non-pihome machine never
calls `gh` directly for these 7 reads, even if Redis is down — it returns
an honest "no data" result instead. pihome still calls `gh` directly
regardless of Redis state (it just can't publish to the cache if Redis is
down).

## Files Changed

- `src/lupin/gh_cache.py` (new) — the shared helper, `cached_gh_json`.
  Gates the live fetch on `machines.hostname() == CANONICAL_GH_FETCHER`
  ("pihome"), then (if that passes) on a Redis lock via `slots_redis`.
  Cache envelope is `{"data": ...}` so a cached `None` is a real hit, not
  a miss. See "pihome check" section below for the exact logic and its
  one known risk.
- `src/lupin/place.py` — `_fetch_issue` (the `gh issue view
  --json title,body,labels` call) goes through `gh_cache.cached_gh_json`.
  `_resolve_task`, `quest_focus_for`, `place` thread `connection` down to
  it.
- `src/lupin/quest.py` — `_read_quests` (the quest-labeled-issues GraphQL
  call) and `_locate_issue` (the `gh issue view --json number,state` call)
  go through `gh_cache.cached_gh_json`. `load_quests`, `_resolve_issues`,
  `focus`, `release`, `start` thread `connection` down to them.
- `src/lupin/roadmap.py` — `_read_comments` (issue-comments GraphQL),
  `load_github`'s issue-list call, and `_read_dependencies` (dependency
  GraphQL) go through `gh_cache.cached_gh_json`. `load_github` and
  `load_dependencies` thread `connection` through and keep their
  pre-existing "never return bare None" guarantee even when a cache miss
  on a non-pihome machine returns `(None, error)`.
  `cached_github`/`cached_dependencies`/`cached_dependency_dag` (the
  pre-existing in-process file cache layer *above* the new Redis cache)
  deliberately do **not** take a `connection` param — they're not among
  the issue's 7 call sites, and threading `connection` through them would
  have forced fixes to ~20 unrelated test mocks (`test_quest.py`,
  `test_reconcile.py`, `test_serve.py`, `test_roadmap_cli.py`) that stand
  in for "the roadmap data layer" in tests that have nothing to do with
  this cache. They call `load_github`/`load_dependencies` with no
  `connection` kwarg, which defaults to `None` and resolves via
  `machines.resolve_connection()` same as if it were threaded.
- `src/lupin/roadmap_cli.py` — `_issue_state` (the `gh issue view
  --json state` call) goes through `gh_cache.cached_gh_json`. Gained
  `owner`/`name` params (the caller already has them, no need to
  re-resolve identity inside `_issue_state`). `build_roadmap`/`run` do
  **not** take a `connection` param, same reasoning as above — nothing
  calls them with one in production (`cli.py`'s `_cmd_roadmap` doesn't
  either), so adding it was pure speculative plumbing with real test
  cost and no real benefit.
- `docs/redis-schema.md` — new `gh-cache:<owner>/<repo>:<cache-key>` key,
  its TTL (5 min) and the fetch lock's TTL (2 min), and a fallback-table
  row documenting the pihome-only direct-call fallback.
- `tests/test_gh_cache.py` (new) — hit/miss/lock-contention/
  redis-unreachable paths, for both the canonical and non-canonical
  machine, using real `redis_port`/`flush_redis`/`closed_port` fixtures
  (no mocked Redis client — matches this repo's own test convention).
- `tests/test_place.py`, `tests/test_quest.py`, `tests/test_roadmap.py`,
  `tests/test_roadmap_cli.py`, `tests/test_serve.py` — updated existing
  mocks that fully replace a wrapped function with a fixed-arity lambda,
  so they still work now that call sites pass new args/kwargs. Two
  separate `_fake_locate` helpers (`tests/test_quest.py` and
  `tests/test_serve.py` each define their own copy) needed the same
  `**kw` fix.

## The pihome check, precisely

`gh_cache.cached_gh_json` compares `machines.hostname()` (which wraps
`socket.gethostname()`) against a module-level constant,
`CANONICAL_GH_FETCHER = "pihome"`. Exact match, case-sensitive, no
normalization. This is a real, not-yet-eliminated risk: if pihome's
`gethostname()` ever returns something other than the bare string
`"pihome"` — a FQDN like `pihome.local`, a rename after a reimage, a
container hostname — the comparison silently fails and the live-fetch
path goes cold fleet-wide (every machine, pihome included under this
check's logic, would see itself as "not the canonical fetcher" for that
one call... except pihome itself still gets a carve-out, see below). The
code does not detect or warn about this mismatch; it only works correctly
today because `docs/redis-schema.md`'s own existing example data already
uses the bare string `"pihome"` as `machines.py`'s `"issuer"` value, which
means `hostname()` on that box is already confirmed to return exactly
that string in this fleet's convention — but nothing enforces it stays
true.

## Review round 2: double-fetch bug (fixed)

An independent reviewer found a real bug with two real threads against a
real `redis-server`: `cached_gh_json` only re-checked the cache in the
`except SlotFull` branch. When `acquire()` succeeded *after waiting* (the
normal case — the first caller finishes inside `LOCK_WAIT` and the second
gets the lock once it's released), the code fell straight through to
`fetch_fn()` again instead of checking whether the first caller's result
was already cached. Two calls a few hundred ms apart double-fetched `gh`.

Fix: added an `else` clause on the `try/except` around `acquire()` that
re-reads the cache on a *successful* acquire too, releasing the lease and
returning the cached value if the holder waited behind already finished.

While building a real-concurrency test for this (two actual threads, not
a pre-held lease for the whole test), found a second, deeper, pre-existing
bug underneath it: the lock's slot name was `f"gh-fetch:{owner}/{name}"`
— a colon inside the slot name. `_lease_runtime.split_lease` rebuilds
`(slot, holder)` from the lease string by splitting on the *first* colon
only, so that slot name was parsed wrong by `release()`/`renew()`, which
silently acted on the wrong key — the real lock entry was never released
and just sat until `LOCK_TTL` (2 min) expired. This meant any second
caller would always hit `SlotFull` and only ever reach the `except`
branch's cache re-check, masking the first bug in any test that didn't
control timing precisely. Fixed by changing the slot name's separator
from `:` to `/` (`gh-fetch/{owner}/{name}`). Also found and fixed a third,
related issue: the lock's `holder` was just `cache_key`, so two different
real callers for the *same* cache_key shared one holder identity —
`_ACQUIRE_SCRIPT` treats a repeat acquire from the same holder as a renew
(no contention), so the lock gave no real mutual exclusion for exactly the
race it exists to prevent. Fixed by making `holder` unique per call
(`f"{cache_key}:{uuid4().hex[:8]}"`).

New test: `tests/test_gh_cache.py::
test_concurrent_caller_that_waits_reuses_the_result_instead_of_refetching`
— two real `threading.Thread`s, synchronized with an `Event` (not a sleep)
so the second thread's `acquire()` is guaranteed to find the lock already
held, not racing to get it first. Verified by hand that it fails against
each of the three bugs above (reverted the `else` branch alone: fails in
0.4s calling `fetch_fn()` directly; the colon-separator alone, without the
`else` fix: fails after the full 2s `SlotFull` wait instead of the fast
path) before restoring the fix.

## Verification Status

Project gate (`nix flake check -L`, this worktree, aarch64-linux):
**before** any of my changes, 551 passed / 1 failed; **after** the
original implementation, 561 passed / 1 failed; **after** this review
round's fix, confirmed 562 passed / 1 failed (one more test than before:
the new concurrency test). Same single pre-existing failure throughout
(`test_serve.py::TestLoopsPageIntegration::
test_claimed_elsewhere_shows_up_as_remote_in_by_machine_grouping`),
confirmed pre-existing because it already failed in the very first,
unmodified-code baseline run.

Separately, ad-hoc `pytest -q` runs on the host (not the hermetic nix
sandbox) consistently surface a *different* subset of 4 pre-existing
failures matching the issue's own claim exactly by name and count
(`test_quest.py` x3, `test_slots_redis.py` x1) — order/state-dependent
flakiness in shared fixtures across the two environments, not something
this change causes (none of the 4 touch `gh_cache.py` or the 7 call
sites' new code).

## Next Action

Done. `nix flake check -L` confirmed 562 passed / 1 failed (same single
pre-existing failure). Committed; already visible in the mount
(`/code/lupin`, worktree shares the git object store, no separate push
needed). Follow-up issue report posted, issue left open per the
coordinator's instruction.

## Known Blockers

`nix shell nixpkgs#python3Packages.pytest` alone fails at collection
(`ModuleNotFoundError: No module named 'redis'`) — `nix shell` doesn't
reliably compose more than one package's site-packages onto one
interpreter. Workaround used throughout this session:
`PYTHONPATH=$(nix build --no-link --print-out-paths 'nixpkgs#python3Packages.redis')/lib/python3.14/site-packages`
set before `nix shell nixpkgs#python3Packages.pytest nixpkgs#redis nixpkgs#gh -c pytest -q`.
