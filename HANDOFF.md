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

Done, including a follow-up fix round. Merged locally into
`issue-28-cmd-queue-agent` (off `main` at 4e614ce). Commits: 153737f
(implementation), e476112 (merge commit), f7cf268 (first HANDOFF), plus
this round's fix commit (see git log). Reported to the issue both times.

### Follow-up round: 5 gaps from an independent review

Issue #27's design comment wasn't posted when the first round above was
built; it is now (issue #28 comment), and an independent review of the
first merge found 5 gaps against it. All 5 fixed:

1. **Repo format validation** — `agent.py`'s `_validate_repo` now checks
   `^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$` before any handler builds argv
   (defense in depth, independent of whatever the dashboard checks).
   Note: the design's own example payload uses a bare repo name
   (`"repo": "field-trip"`, no owner prefix) — the regex has no `/`. An
   `owner/repo` string is rejected by design, not a bug.
2. **`systemd-run` detachment** — both `loop.stop` and `loop.run` now
   build `["systemd-run", f"--unit=lupin-cmd-{id[:8]}", "--collect",
   "loopctl", <action>, repo]` instead of a bare `loopctl` call, so an
   agent restart (`KillMode=process`, #29) can't kill an in-flight action.
   Note: the design's own action-mapping table only shows `loop.run`
   wrapped this way, not `loop.stop` — wrapped both per the coordinator's
   explicit instruction ("not a bare `loopctl run`/`stop` call"), since
   wrapping `stop` too is strictly safer and the instruction was direct.
3. **Audit trail** — `commands.py`'s signed payload now carries
   `actor`/`issuer`; `cmdlog` (a capped stream, `XADD ... MAXLEN ~ 2000`)
   gets one entry at enqueue and one per terminal outcome
   (ok/failed/rejected/expired), via `commands.log_event`.
4. **Field names realigned to the design** — `cmdres` now uses
   `state`/`host` (was `status`/`machine`), matching what #21/#22/#23 will
   be built against.
5. **TTL vs. expiry separated** — `cmd:<id>`'s Redis `PX` (`CMD_RETENTION_S`,
   fixed 1h) is now purely retention; the pickup deadline is the JSON
   field `expires_at` (`issued_at + pickup_window`, default 120s), checked
   by the executor with a 30s `CLOCK_SKEW_S` grace allowance. Queue pruning
   uses `ZREMRANGEBYSCORE` against the 1h cutoff as a safety net; the real
   runnability check is per-item `expires_at`.

Deliberately left out of this round (not among the 5 named findings, no
scope creep): in-memory anti-replay id tracking, "draining machine"
rejection, optional `platform`/`note` params, `c_`-prefixed ids.

## Files Changed

- `src/lupin/commands.py` — key helpers, enqueue EVAL (now signs
  `actor`/`issuer` too, writes a `cmdlog` entry), HMAC sign/verify,
  status/queue reads (`state` field).
- `src/lupin/agent.py` — poll loop, fixed ACTIONS table (now
  `systemd-run`-wrapped), startup scan, repo-regex validation,
  `expires_at` + clock-skew staleness check, `cmdlog` writes on every
  terminal outcome.
- `src/lupin/cli.py` — `cmd send|status|queue` (`--actor`/`--issuer`/
  `--pickup-window` replacing `--ttl`), `agent` subcommands.
- `docs/redis-schema.md` — key shapes updated to match (`actor`/`issuer`/
  `state`/`host`, TTL-vs-`expires_at` explanation).
- `tests/test_commands.py`, `tests/test_agent.py` — extended for all 5
  fixes: custom pickup window, cmdlog entry at enqueue, `systemd-run` argv
  for both actions (asserting the exact unit-name pattern), invalid/valid
  repo formats, clock-skew allowance (just past nominal expiry still
  runs; well past the skew is marked expired), cmdlog entry at finish.

## Verification Status

`nix develop . -c pytest` run on this branch after the fix round:
- Before this round's fixes (first-round merge, previously reported):
  360 passed, 4 pre-existing failures, 0 errors.
- After this round's fixes and test updates: **368 passed, same 4
  pre-existing failures, 0 errors** — net 8 new/extended tests passing,
  no regressions. The 4 pre-existing failures are unchanged from the
  first round and were independently reproduced against baseline `main`
  then: `test_quest.py::test_cli_quest_focus_prints_exact_copy_text`,
  `test_quest.py::test_cli_quest_release_prints_exact_copy_text`,
  `test_quest.py::test_cli_quest_release_no_focus_error`,
  `test_slots_redis.py::test_status_json_matches_real_sorted_set_contents`.
- Caught one bug in my own first test draft: I wrote several `test_agent.py`
  cases using `"repo": "gracecraft/lupin"` (contains `/`), which the new
  regex correctly rejects — fixed by using a bare repo name (`"lupin"`),
  matching the design's own example. Also found my first expiry test used
  too short a gap (10ms past a 10ms pickup window) to clear the 30s
  clock-skew allowance — rewrote it to construct a command 80s past
  `expires_at` directly.

## Next Action

None. Shipped: fix round committed and merged locally, reported to the
issue.

## Known Blockers

None.
