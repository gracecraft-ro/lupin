"""The per-machine poll loop: claims and runs commands from `commands.py`'s
queue. Implements #27's design -- see that issue's design comment for the
full spec. `cli.py`'s `lupin agent` subcommand is the long-running process;
this module is what it runs.

Security model, checked in this order, and nothing after a failed check
ever reaches `subprocess.run`:
1. HMAC signature over the canonical payload (`commands.verify`).
2. `target` must equal this machine -- belt and suspenders. A command
   only reaches this machine's queue by key already, but a forged payload
   naming the wrong `target` field should still be caught, not trusted.
3. Not past `expires_at` (plus `commands.CLOCK_SKEW_S` grace).
4. `action` must be in `ACTIONS`, a fixed, explicit table. An action not
   in it is rejected, never run as a best-effort guess.
5. Per-action parameter validation (e.g. `repo` against a strict regex,
   defense in depth independent of whatever a future dashboard checks).

`ACTIONS` wraps `loopctl` rather than reimplementing it -- the design's
decision: `loopctl` already has the hard-won edge cases (scrollback save,
keepalive session, exact tmux target match). Every handler returns a list
argv, never a shell string -- execution is always
`subprocess.run(argv, shell=False)`.

Two shapes of action, per issue #2's phase A plan:
- Mutating (`loop.stop`, `loop.run`, `schedule.set`, `schedule.pause`,
  `schedule.resume`): run as `sudo -n systemd-run --unit=lupin-cmd-<id8>
  --collect --wait --pipe loopctl ...` -- `sudo -n` because every other
  `systemd-run` call in ghostbook.nix needs it too (plain `systemd-run` as
  this process's own user is not authorized); `--wait --pipe` so the
  reported exit code is the actual action's, not just "did the transient
  unit start" (a bug in the first cut of this table -- `systemd-run`
  without `--wait` always looked like success).
- Read-only (`loop.peek`, `schedule.show`): run `loopctl` directly, no
  `systemd-run` wrapper -- nothing to isolate or wait synchronously for
  that `subprocess.run`'s own timeout doesn't already cover.

`schedule.set`'s `cal` mode is validated before it ever reaches `loopctl`:
`loopctl schedule cal "<expr>"` (ghostbook.nix) writes `<expr>` straight
into a systemd timer drop-in with a bare `printf`, no escaping -- a `expr`
containing a newline could add arbitrary extra directives to that file.
`validate_cal_expr` rejects a newline/control character or an
overlength string unconditionally (pure, no subprocess, so it is testable
without `systemd-analyze` installed); `_check_cal_expr_with_systemd_analyze`
adds a real syntax check through `systemd-analyze calendar` when that
binary is reachable.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time

import redis

from . import commands
from .slots import CoordinatorUnreachable
from .slots_redis import _call_with_retry, _client

DEFAULT_BATCH = 20  # design: "ZRANGE the oldest 20"
DEFAULT_POLL_INTERVAL = 2.0
EXEC_TIMEOUT_S = 120.0
OUTPUT_CAP = 8192  # 8 KiB, combined stdout+stderr -- design's "last 8 KiB combined"

# Stricter than loopctl's own `check_repo_name` (which still also runs) --
# the design's explicit defense-in-depth requirement, checked here even
# though nothing upstream validates yet either.
_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")

# Length caps are generous for a real value, tight enough to stop anyone
# using these fields to smuggle something else through.
_CAL_EXPR_MAX_LEN = 256
_SCHEDULE_TOKEN_MAX_LEN = 64
_DEFAULT_PEEK_LINES = 60
_MAX_PEEK_LINES = 5000


class RejectedCommand(Exception):
    """A command whose params don't fit its action -- caught before
    `subprocess.run`, turned into a `rejected` result, never executed."""


def _validate_repo(repo) -> str:
    if not isinstance(repo, str) or not _REPO_RE.match(repo):
        raise RejectedCommand(f"invalid repo {repo!r}")
    return repo


def _validate_peek_lines(value) -> int:
    if value is None:
        return _DEFAULT_PEEK_LINES
    try:
        lines = int(value)
    except (TypeError, ValueError):
        raise RejectedCommand(f"invalid lines {value!r}")
    if not (1 <= lines <= _MAX_PEEK_LINES):
        raise RejectedCommand(f"lines must be 1-{_MAX_PEEK_LINES}, got {lines}")
    return lines


def _reject_unsafe_text(label: str, value, max_len: int) -> str:
    """Shared by every string field that ends up written verbatim into a
    config file or passed as one `loopctl` argument -- a newline or other
    control character in any of them is the same injection shape as the
    `cal` expression this was written for (see module docstring)."""
    if not isinstance(value, str) or not value:
        raise RejectedCommand(f"{label} must be a non-empty string")
    if len(value) > max_len:
        raise RejectedCommand(f"{label} longer than {max_len} characters")
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        raise RejectedCommand(f"{label} contains a newline or control character")
    return value


def validate_cal_expr(expr) -> str:
    """Character/length check only -- pure, no subprocess, so this runs
    (and is tested) even where `systemd-analyze` isn't installed. See
    `_check_cal_expr_with_systemd_analyze` for the syntax check on top."""
    return _reject_unsafe_text("cal expression", expr, _CAL_EXPR_MAX_LEN)


def validate_schedule_token(label: str, value) -> str:
    return _reject_unsafe_text(f"schedule {label}", value, _SCHEDULE_TOKEN_MAX_LEN)


def _check_cal_expr_with_systemd_analyze(expr: str) -> None:
    """Ask systemd itself whether `expr` parses as a calendar expression --
    the same check a human would run by hand before trusting one. Runs
    unconditionally; a no-op (not a pass) when the binary isn't on PATH, so
    a sandbox without it still exercises `validate_cal_expr` above rather
    than silently skipping all cal validation.
    """
    binary = shutil.which("systemd-analyze")
    if binary is None:
        return
    proc = subprocess.run([binary, "calendar", expr], capture_output=True, text=True, timeout=5.0)
    if proc.returncode != 0:
        raise RejectedCommand(f"not a valid calendar expression: {expr!r}")


def _unit_name(cmd_id: str) -> str:
    return f"lupin-cmd-{cmd_id[:8]}"


def _sudo_systemd_run(cmd_id: str, *loopctl_args: str) -> list[str]:
    """A mutating action's argv: `sudo -n systemd-run ... --wait --pipe
    loopctl <loopctl_args>`. `sudo -n` matches every other `systemd-run`
    call in ghostbook.nix (this process's own user has no bare
    `systemd-run` rights); `--wait --pipe` makes `subprocess.run`'s
    returncode the actual action's exit code, not just "did the transient
    unit start" -- the first cut of this table had neither.
    """
    return [
        "sudo", "-n", "systemd-run",
        f"--unit={_unit_name(cmd_id)}", "--collect", "--wait", "--pipe",
        "loopctl", *loopctl_args,
    ]


def _handle_loop_stop(params: dict, cmd_id: str) -> list[str]:
    repo = _validate_repo(params.get("repo"))
    return _sudo_systemd_run(cmd_id, "stop", repo)


def _handle_loop_run(params: dict, cmd_id: str) -> list[str]:
    repo = _validate_repo(params.get("repo"))
    return _sudo_systemd_run(cmd_id, "run", repo)


def _handle_loop_peek(params: dict, cmd_id: str) -> list[str]:
    # Read-only -- run loopctl directly, no systemd-run wrapper (see module
    # docstring's "two shapes of action").
    repo = _validate_repo(params.get("repo"))
    lines = _validate_peek_lines(params.get("lines"))
    return ["loopctl", "peek", repo, str(lines)]


def _handle_schedule_show(params: dict, cmd_id: str) -> list[str]:
    return ["loopctl", "schedule"]


def _handle_schedule_set(params: dict, cmd_id: str) -> list[str]:
    mode = params.get("mode")
    if mode == "cal":
        expr = validate_cal_expr(params.get("expr"))
        _check_cal_expr_with_systemd_analyze(expr)
        return _sudo_systemd_run(cmd_id, "schedule", "cal", expr)
    if mode == "first":
        when = validate_schedule_token("when", params.get("when"))
        interval = validate_schedule_token("interval", params.get("interval"))
        return _sudo_systemd_run(cmd_id, "schedule", "first", when, "every", interval)
    raise RejectedCommand(f"unknown schedule mode {mode!r}")


def _handle_schedule_pause(params: dict, cmd_id: str) -> list[str]:
    return _sudo_systemd_run(cmd_id, "pause")


def _handle_schedule_resume(params: dict, cmd_id: str) -> list[str]:
    return _sudo_systemd_run(cmd_id, "resume")


# Fixed, explicit allowlist -- the only actions this process will ever run.
# An action not in this table is rejected, not attempted.
ACTIONS = {
    "loop.stop": _handle_loop_stop,
    "loop.run": _handle_loop_run,
    "loop.peek": _handle_loop_peek,
    "schedule.show": _handle_schedule_show,
    "schedule.set": _handle_schedule_set,
    "schedule.pause": _handle_schedule_pause,
    "schedule.resume": _handle_schedule_resume,
}


def _write_result(client, cmd_id: str, payload: dict, *, overwrite: bool = False) -> bool:
    value = json.dumps(payload)
    key = commands.res_key(cmd_id)
    px = int(commands.RESULT_TTL_S * 1000)
    if overwrite:
        return bool(_call_with_retry(lambda: client.set(key, value, px=px)))
    return bool(_call_with_retry(lambda: client.set(key, value, nx=True, px=px)))


def _reject(client, machine: str, cmd_id: str, action: str | None, reason: str) -> dict:
    _write_result(client, cmd_id, {"id": cmd_id, "state": "rejected", "host": machine, "reason": reason})
    _call_with_retry(lambda: client.zrem(commands.queue_key(machine), cmd_id))
    commands.log_event(
        client, {"id": cmd_id, "machine": machine, "state": "rejected", "action": action, "reason": reason}
    )
    return {"id": cmd_id, "state": "rejected"}


def _mark_expired(client, machine: str, cmd_id: str) -> dict:
    _write_result(client, cmd_id, {"id": cmd_id, "state": "expired", "host": machine})
    _call_with_retry(lambda: client.zrem(commands.queue_key(machine), cmd_id))
    commands.log_event(client, {"id": cmd_id, "machine": machine, "state": "expired", "reason": "ttl"})
    return {"id": cmd_id, "state": "expired"}


def _process_one(client, machine: str, key: str, cmd_id: str) -> dict:
    raw = _call_with_retry(lambda: client.get(commands.cmd_key(cmd_id)))
    if raw is None:
        # Gone from Redis already (its own retention TTL, or evicted under
        # memory pressure) -- same outcome either way.
        return _mark_expired(client, machine, cmd_id)
    cmd = json.loads(raw)
    action = cmd.get("action")

    if not commands.verify(cmd, key):
        return _reject(client, machine, cmd_id, action, "bad signature")
    if cmd.get("target") != machine:
        return _reject(client, machine, cmd_id, action, f"addressed to {cmd.get('target')!r}")
    if time.time() >= cmd.get("expires_at", 0) + commands.CLOCK_SKEW_S:
        return _mark_expired(client, machine, cmd_id)
    if action not in ACTIONS:
        return _reject(client, machine, cmd_id, action, f"unknown action {action!r}")

    # Build the argv (and so validate the params) before claiming -- a
    # rejection has to land while no `cmdres` exists yet, so `_reject`'s
    # `SET ... NX` actually writes "rejected" instead of silently losing to
    # a "running" claim already sitting there.
    params = cmd.get("params") or {}
    try:
        argv = ACTIONS[action](params, cmd_id)
    except RejectedCommand as exc:
        return _reject(client, machine, cmd_id, action, str(exc))

    started_at = time.time()
    claimed = _call_with_retry(
        lambda: client.set(
            commands.res_key(cmd_id),
            json.dumps({"id": cmd_id, "state": "running", "host": machine, "action": action, "started_at": started_at}),
            nx=True,
            px=int(commands.RESULT_TTL_S * 1000),
        )
    )
    if not claimed:
        # Another poller racing on the same id claimed it first. Don't
        # touch the queue or run anything -- the claimant finishes the
        # job, including the dequeue.
        return {"id": cmd_id, "state": "lost-race"}

    try:
        proc = subprocess.run(argv, shell=False, capture_output=True, text=True, timeout=EXEC_TIMEOUT_S)
        combined = (proc.stdout or "") + (proc.stderr or "")
        payload = {
            "id": cmd_id,
            "state": "ok" if proc.returncode == 0 else "failed",
            "host": machine,
            "action": action,
            "started_at": started_at,
            "finished_at": time.time(),
            "exit_code": proc.returncode,
            "output": combined[-OUTPUT_CAP:],
            "truncated": len(combined) > OUTPUT_CAP,
        }
    except Exception as exc:  # subprocess failed to even start, or timed out
        payload = {
            "id": cmd_id,
            "state": "failed",
            "host": machine,
            "action": action,
            "started_at": started_at,
            "finished_at": time.time(),
            "reason": str(exc),
        }

    _write_result(client, cmd_id, payload, overwrite=True)
    _call_with_retry(lambda: client.zrem(commands.queue_key(machine), cmd_id))
    commands.log_event(client, {"id": cmd_id, "machine": machine, "state": payload["state"], "action": action})
    return {"id": cmd_id, "state": payload["state"]}


def startup_scan(
    machine: str,
    *,
    redis_host: str | None = None,
    redis_port: int | None = None,
    redis_username: str | None = None,
    redis_password: str | None = None,
) -> list[str]:
    """Run once before polling starts. A command this host claimed (its own
    `cmdres` still says `running`) but never finished means the previous
    process crashed mid-run -- mark it `failed` rather than silently
    re-running it on restart. Returns the ids it marked failed.
    """
    client = _client(redis_host, redis_port, redis_username, redis_password)
    try:
        ids = _call_with_retry(lambda: client.zrange(commands.queue_key(machine), 0, -1))
        marked = []
        for cmd_id in ids:
            raw = _call_with_retry(lambda c=cmd_id: client.get(commands.res_key(c)))
            if raw is None:
                continue
            result = json.loads(raw)
            if result.get("state") != "running" or result.get("host") != machine:
                continue
            result["state"] = "failed"
            result["reason"] = "orphaned: still running when the agent restarted"
            result["finished_at"] = time.time()
            _write_result(client, cmd_id, result, overwrite=True)
            _call_with_retry(lambda c=cmd_id: client.zrem(commands.queue_key(machine), c))
            commands.log_event(client, {"id": cmd_id, "machine": machine, "state": "failed", "reason": "startup-scan"})
            marked.append(cmd_id)
    except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as exc:
        raise CoordinatorUnreachable(machine) from exc
    return marked


def poll_once(
    machine: str,
    key: str,
    *,
    batch: int = DEFAULT_BATCH,
    redis_host: str | None = None,
    redis_port: int | None = None,
    redis_username: str | None = None,
    redis_password: str | None = None,
) -> list[dict]:
    """One pass: prune queue entries past the retention window, then claim
    and run up to `batch` of the oldest remaining pending commands. Returns
    a summary per id touched, in the order handled.
    """
    client = _client(redis_host, redis_port, redis_username, redis_password)
    qkey = commands.queue_key(machine)
    touched: list[dict] = []
    try:
        # Safety-net prune: entries older than the retention window are
        # long past `expires_at` too (120s default vs. 1h here) -- this
        # just stops the queue growing forever if something is never
        # polled. The real "is this still runnable" check is `expires_at`,
        # done per-item below.
        cutoff_ms = commands.now_ms() - int(commands.CMD_RETENTION_S * 1000)
        _call_with_retry(lambda: client.zremrangebyscore(qkey, "-inf", cutoff_ms))

        pending = _call_with_retry(lambda: client.zrange(qkey, 0, batch - 1))
        for cmd_id in pending:
            touched.append(_process_one(client, machine, key, cmd_id))
    except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as exc:
        raise CoordinatorUnreachable(machine) from exc
    return touched


def run_forever(
    machine: str,
    key: str,
    *,
    batch: int = DEFAULT_BATCH,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    redis_host: str | None = None,
    redis_port: int | None = None,
    redis_username: str | None = None,
    redis_password: str | None = None,
) -> None:
    """The `lupin agent` process: one crash-recovery pass, then poll forever.
    Doesn't return under normal operation."""
    conn = dict(redis_host=redis_host, redis_port=redis_port, redis_username=redis_username, redis_password=redis_password)
    startup_scan(machine, **conn)
    while True:
        poll_once(machine, key, batch=batch, **conn)
        time.sleep(poll_interval)
