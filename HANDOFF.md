# HANDOFF — issue #2 Phase A

## Goal

Build `schedule`/`peek`/`attach`/`pause`/`resume`/`stop` as real `lupin` CLI
subcommands, routed over the existing Redis command queue (#28:
`src/lupin/agent.py`, `src/lupin/commands.py`). Herdr integration is out of
scope (cross-repo, owner-only, blocked on #29). Full spec: see the task
prompt / issue #2's last comment (architecture plan, posted by opus).

Scope recap:
1. Fix 3 bugs in agent.py's loop-stop/loop-run handling: missing `sudo -n`
   on `systemd-run`, missing `--wait --pipe`, unvalidated cal-expression
   injection risk.
2. New `src/lupin/loops.py`: shared local-or-remote dispatch
   (`dispatch_loop_action`), used by both `cli.py`'s new verbs and
   `serve.py`'s existing duplicated local/remote branches.
3. `machines.py` heartbeat gains `loops`, `session_backend`, `actions`.
   `serve.py`'s `remote_loop_hosts` updated to use it, or left as a
   documented fallback if updating callers is too big.
4. New queue actions in `agent.py`: `loop.peek`, `schedule.show`,
   `schedule.set`, `schedule.pause`, `schedule.resume`.
5. New `cli.py` subcommands: `stop`, `peek`, `attach`, `schedule`, `pause`,
   `resume`. New exit codes 4 (timeout, unknown result) and 5 (ambiguous
   machine). `attach` execs directly (never via Redis), using a local
   mapping file (`~/.config/lupin/ssh-targets`), documented in this repo's
   CLAUDE.md and in `loops.py`'s docstring.

Signing key scheme: explicitly NOT touched — left as-is. A comment at the
call site in `serve.py` (`cmd_signing_key: str | None = None`) flags the
known gap (one shared key, no per-machine scoping) and points at issue #2.

Not doing: anything in /code/ghostbook.nix, Nix wiring for ssh-targets,
live multi-machine testing (blocked on #29), a new key scheme.

## Current Step

Done. All 5 scope items implemented, tested, and gated. Ready to commit
and push.

## Files Changed

- `src/lupin/agent.py` — 3 bug fixes (`sudo -n`, `--wait --pipe`, cal-expr
  validation) + 5 new queue actions (`loop.peek`, `schedule.show/set/pause/
  resume`). New public `validate_cal_expr`/`validate_schedule_token`.
- `src/lupin/loops.py` — new module: `dispatch_loop_action`,
  `resolve_machine_for_repo`, `ssh_target_for`, `SSH_TARGETS_PATH`.
- `src/lupin/machines.py` — heartbeat gains `loops`/`session_backend`/
  `actions`; `join`/`heartbeat` take an optional `loops=` param.
- `src/lupin/serve.py` — `do_loops_close`/`do_loops_start`/`do_schedule_run`
  now call `loops.dispatch_loop_action` instead of duplicated branches;
  `remote_loop_hosts` kept as a documented fallback for old-heartbeat-shape
  machines (see Known Blockers / Decisions below); signing-key gap comment
  added.
- `src/lupin/cli.py` — 6 new subcommands: `stop`, `peek`, `attach`,
  `schedule` (show/cal/first), `pause`, `resume`. New exit codes 4/5
  documented in the module docstring.
- `tests/test_agent.py`, `tests/test_loops.py` (new), `tests/test_machines.py`,
  `tests/test_cli.py` (new) — unit tests for all of the above. `test_serve.py`
  unchanged (refactor is behavior-preserving, confirmed by the existing
  suite passing with no edits).
- `AGENTS.md` (symlinked from `CLAUDE.md`) — documented the 6 new commands,
  the two new exit codes, the `~/.config/lupin/ssh-targets` file format,
  and `loops.py` in Code layout.

## Verification Status

- Baseline `nix develop . -c pytest -q` (before any change): 542 passed, 4
  known-bad failures (`test_quest.py` x3, `test_slots_redis.py` x1).
- Per-module runs during implementation: `test_agent.py` 32 passed,
  `test_loops.py` 14 passed, `test_machines.py` 15 passed, `test_serve.py`
  177 passed + 3 subtests (zero regressions from the refactor), `test_cli.py`
  28 passed.
- Final full-suite gate `nix develop . -c pytest -q`: 603 passed, 9 subtests
  passed, 4 failed — the same 4 known-bad tests, byte-for-byte (same names,
  same assertions failing). No new failures.
- Not tested: real multi-machine remote dispatch (needs a deployed
  `lupin agent` on a second machine — blocked on #29, not attempted or
  faked). CLI `--help` output and a few mocked functional paths were
  smoke-tested by hand via `python -c` with `sys.path.insert(0, 'src')`.

## Next Action

1. Commit.
2. Push the branch to `origin` (this worktree's remote is the `/code/lupin`
   mount, not GitHub — no `git push` to GitHub from this sandbox, per
   `docs/delegation-loop.md` rule 1).
3. Report: name the branch, summarize the 5 scope items, call out the
   `remote_loop_hosts` fallback decision and the signing-key non-change
   explicitly, and state that live multi-machine testing is unverified
   (blocked on #29).

## Known Blockers / Decisions

- #29 (lupin-agent deployment) not done — no live remote verb testing
  possible. Noted as unverified in report, not faked.
- Signing-key scheme change is explicitly out of scope (Grace's decision).
  One comment added at the call site pointing at issue #2's gap.
- `serve.py`'s `remote_loop_hosts()` (guess-based) is kept as a fallback
  rather than removed: `gather_loops()` now prefers the heartbeat-derived
  `_loop_hosts_from_heartbeat()` and only falls back to the old guess for
  machines whose heartbeat predates this change (no `loops` field).
  Replacing `remote_loop_hosts` outright would mean auditing every caller
  across `serve.py` for one that still depends on its guess-based shape —
  a bigger refactor than this issue's scope. Flagged here rather than done
  silently.
