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
`subprocess.run(argv, shell=False)`. Both actions run inside a detached
transient unit (`systemd-run --unit=lupin-cmd-<id8> --collect ...`) so a
restart of the `lupin-agent` service (`KillMode=process`, #29) can't kill
an in-flight `loopctl` run.
"""

from __future__ import annotations

import json
import re
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


class RejectedCommand(Exception):
    """A command whose params don't fit its action -- caught before
    `subprocess.run`, turned into a `rejected` result, never executed."""


def _validate_repo(repo) -> str:
    if not isinstance(repo, str) or not _REPO_RE.match(repo):
        raise RejectedCommand(f"invalid repo {repo!r}")
    return repo


def _unit_name(cmd_id: str) -> str:
    return f"lupin-cmd-{cmd_id[:8]}"


def _handle_loop_stop(params: dict, cmd_id: str) -> list[str]:
    repo = _validate_repo(params.get("repo"))
    return ["systemd-run", f"--unit={_unit_name(cmd_id)}", "--collect", "loopctl", "stop", repo]


def _handle_loop_run(params: dict, cmd_id: str) -> list[str]:
    repo = _validate_repo(params.get("repo"))
    return ["systemd-run", f"--unit={_unit_name(cmd_id)}", "--collect", "loopctl", "run", repo]


# Fixed, explicit allowlist -- the only actions this process will ever run.
# An action not in this table is rejected, not attempted.
ACTIONS = {
    "loop.stop": _handle_loop_stop,
    "loop.run": _handle_loop_run,
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
