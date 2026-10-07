# HANDOFF: issue #28 — cross-machine command queue + agent executor

## Goal

Implement #27's design: a per-machine signed command queue in Redis
(`src/lupin/commands.py`) and a poll/execute loop (`src/lupin/agent.py`)
with a fixed `loop.stop`/`loop.run` action table wrapping `loopctl`. Add
`lupin cmd send|status|queue` and `lupin agent` to `cli.py`. Document the
new keys in `docs/redis-schema.md`. Test end-to-end against the real
ephemeral-Redis fixture in `tests/conftest.py`.

Note: issue #27 has only one comment (the orchestrator's decision), not
two — the "full design" comment it links to was never actually posted
(dead link, no comment id). Verified via `gh api
repos/gracecraft/lupin/issues/27/comments` (1 comment) and the timeline.
Proceeding from issue #28's own body (which has the concrete key shapes)
plus #27's one decision comment — together these are sufficient.

No push access: local-merge fallback. Branch off local `main` — tip moved
during this session (a70e5d4 -> 4e614ce, another sandbox session's merge
landing concurrently); final merge target is whatever `main` is at merge
time, not `origin/main` (b952cb5, stale).

Found and fixed a real bug during testing, before any commit: in
`agent.py`'s `_process_one`, a command rejected for bad params (missing
`repo`) was rejected *after* the `SET cmdres ... NX` claim write already
succeeded, so the rejection's own `NX` write silently no-op'd and the
stored result stayed `"running"` forever even though the function
returned `"rejected"`. Fixed by building/validating the handler's argv
*before* claiming, so a rejection always lands while no `cmdres` key
exists yet.

## Current Step

Done. Merged locally into `issue-28-cmd-queue-agent` (off `main` at
4e614ce). Commits: 153737f (implementation), e476112 (merge commit).
Reported to the issue.

## Files Changed

- `src/lupin/commands.py` (new) — key helpers, enqueue EVAL, HMAC sign/verify, status/queue reads.
- `src/lupin/agent.py` (new) — poll loop, fixed ACTIONS table, startup scan.
- `src/lupin/cli.py` — `cmd send|status|queue`, `agent` subcommands.
- `docs/redis-schema.md` — new key families under `v1`.
- `tests/test_commands.py`, `tests/test_agent.py` (new).

## Verification Status

`nix develop . -c pytest` run twice:
- On baseline `main` (4e614ce, via a tracked `git stash`/checkout/restore,
  not a reset): 4 pre-existing failures, unrelated to this change --
  `test_quest.py::test_cli_quest_focus_prints_exact_copy_text`,
  `test_quest.py::test_cli_quest_release_prints_exact_copy_text`,
  `test_quest.py::test_cli_quest_release_no_focus_error`,
  `test_slots_redis.py::test_status_json_matches_real_sorted_set_contents`.
  Same 4 failures, same assertions, reproduced identically on baseline.
- On this branch (`issue-28-cmd-queue-agent`, after merge): same 4
  pre-existing failures + 360 passed, 0 errors (all new
  `test_commands.py`/`test_agent.py` tests pass). One earlier run on this
  branch hit 26 `ConnectionError`s against the session Redis fixture;
  re-ran clean (360 passed, same 4 known failures, 0 errors) -- a one-off
  sandbox flake, not reproducible, not related to this change.

## Next Action

None. Shipped: merged locally, reported to the issue.

## Known Blockers

None.
