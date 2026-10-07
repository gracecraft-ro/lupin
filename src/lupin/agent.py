"""The per-machine poll loop: claims and runs commands from `commands.py`'s
queue (issue #28, implementing #27's design). `cli.py`'s `lupin agent`
subcommand is the long-running process; this module is what it runs.

Security model, checked in this order, and nothing after a failed check
ever reaches `subprocess.run`:
1. HMAC signature over the canonical payload (`commands.verify`).
2. `target` must equal this machine -- belt and suspenders. A command
   only reaches this machine's queue by key already, but a forged payload
   naming the wrong `target` field should still be caught, not trusted.
3. Not past its TTL (`issued_at + ttl_ms`).
4. `action` must be in `ACTIONS`, a fixed, explicit table. An action not
   in it is rejected, never run as a best-effort guess.

`ACTIONS` wraps `loopctl` rather than reimplementing it -- #27's decision
comment: `loopctl` already has the hard-won edge cases (scrollback save,
keepalive session, exact tmux target match). Every handler returns a list
argv, never a shell string -- execution is always
`subprocess.run(argv, shell=False)`.
"""

from __future__ import annotations

import json
import subprocess
import time

import redis

from . import commands
from .slots import CoordinatorUnreachable
from .slots_redis import _call_with_retry, _client

DEFAULT_BATCH = 10
DEFAULT_POLL_INTERVAL = 2.0
EXEC_TIMEOUT_S = 120.0
OUTPUT_CAP = 4000  # characters kept per stream -- a runaway command can't bloat cmdres forever


class RejectedCommand(Exception):
    """A command whose params don't fit its action -- caught before
    `subprocess.run`, turned into a `rejected` result, never executed."""


def _handle_loop_stop(params: dict) -> list[str]:
    repo = params.get("repo")
    if not repo:
        raise RejectedCommand("loop.stop needs a 'repo' param")
    return ["loopctl", "stop", repo]


def _handle_loop_run(params: dict) -> list[str]:
    repo = params.get("repo")
    if not repo:
        raise RejectedCommand("loop.run needs a 'repo' param")
    return ["loopctl", "run", repo]


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


def _log(client, fields: dict) -> None:
    flat = {k: str(v) for k, v in fields.items()}
    _call_with_retry(lambda: client.xadd(commands.LOG_KEY, flat, maxlen=1000, approximate=True))


def _reject(client, machine: str, cmd_id: str, action: str | None, reason: str) -> dict:
    _write_result(client, cmd_id, {"id": cmd_id, "status": "rejected", "machine": machine, "error": reason})
    _call_with_retry(lambda: client.zrem(commands.queue_key(machine), cmd_id))
    _log(client, {"id": cmd_id, "machine": machine, "status": "rejected", "action": action, "reason": reason})
    return {"id": cmd_id, "status": "rejected"}


def _mark_expired(client, machine: str, cmd_id: str) -> dict:
    _write_result(client, cmd_id, {"id": cmd_id, "status": "expired", "machine": machine})
    _call_with_retry(lambda: client.zrem(commands.queue_key(machine), cmd_id))
    _log(client, {"id": cmd_id, "machine": machine, "status": "expired", "reason": "ttl"})
    return {"id": cmd_id, "status": "expired"}


def _process_one(client, machine: str, key: str, cmd_id: str, now: int) -> dict:
    raw = _call_with_retry(lambda: client.get(commands.cmd_key(cmd_id)))
    if raw is None:
        # Expired between the prune pass and here -- same outcome.
        return _mark_expired(client, machine, cmd_id)
    cmd = json.loads(raw)
    action = cmd.get("action")

    if not commands.verify(cmd, key):
        return _reject(client, machine, cmd_id, action, "bad signature")
    if cmd.get("target") != machine:
        return _reject(client, machine, cmd_id, action, f"addressed to {cmd.get('target')!r}")
    if now >= cmd.get("issued_at", 0) + cmd.get("ttl_ms", 0):
        return _mark_expired(client, machine, cmd_id)
    if action not in ACTIONS:
        return _reject(client, machine, cmd_id, action, f"unknown action {action!r}")

    # Build the argv (and so validate the params) before claiming -- a
    # rejection has to land while no `cmdres` exists yet, so `_reject`'s
    # `SET ... NX` actually writes "rejected" instead of silently losing to
    # a "running" claim already sitting there.
    params = cmd.get("params") or {}
    try:
        argv = ACTIONS[action](params)
    except RejectedCommand as exc:
        return _reject(client, machine, cmd_id, action, str(exc))

    claimed = _call_with_retry(
        lambda: client.set(
            commands.res_key(cmd_id),
            json.dumps({"id": cmd_id, "status": "running", "machine": machine, "action": action, "claimed_at": now}),
            nx=True,
            px=int(commands.RESULT_TTL_S * 1000),
        )
    )
    if not claimed:
        # Another poller racing on the same id claimed it first. Don't
        # touch the queue or run anything -- the claimant finishes the
        # job, including the dequeue.
        return {"id": cmd_id, "status": "lost-race"}

    try:
        proc = subprocess.run(argv, shell=False, capture_output=True, text=True, timeout=EXEC_TIMEOUT_S)
        payload = {
            "id": cmd_id,
            "status": "ok" if proc.returncode == 0 else "failed",
            "machine": machine,
            "action": action,
            "returncode": proc.returncode,
            "stdout": proc.stdout[-OUTPUT_CAP:],
            "stderr": proc.stderr[-OUTPUT_CAP:],
            "finished_at": commands.now_ms(),
        }
    except Exception as exc:  # subprocess failed to even start, or timed out
        payload = {
            "id": cmd_id,
            "status": "failed",
            "machine": machine,
            "action": action,
            "error": str(exc),
            "finished_at": commands.now_ms(),
        }

    _write_result(client, cmd_id, payload, overwrite=True)
    _call_with_retry(lambda: client.zrem(commands.queue_key(machine), cmd_id))
    _log(client, {"id": cmd_id, "machine": machine, "status": payload["status"], "action": action})
    return {"id": cmd_id, "status": payload["status"]}


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
            if result.get("status") != "running" or result.get("machine") != machine:
                continue
            result["status"] = "failed"
            result["error"] = "orphaned: still running when the agent restarted"
            result["finished_at"] = commands.now_ms()
            _write_result(client, cmd_id, result, overwrite=True)
            _call_with_retry(lambda c=cmd_id: client.zrem(commands.queue_key(machine), c))
            _log(client, {"id": cmd_id, "machine": machine, "status": "failed", "reason": "startup-scan"})
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
    """One pass: prune anything past its TTL, then claim and run up to
    `batch` of the oldest remaining pending commands. Returns a summary per
    id touched, in the order handled.
    """
    client = _client(redis_host, redis_port, redis_username, redis_password)
    qkey = commands.queue_key(machine)
    now = commands.now_ms()
    touched: list[dict] = []
    try:
        # Prune: an id whose cmd:<id> key is already gone (Redis's own PX
        # TTL did it) is expired and will never be processed.
        all_ids = _call_with_retry(lambda: client.zrange(qkey, 0, -1))
        for cmd_id in all_ids:
            exists = _call_with_retry(lambda c=cmd_id: client.get(commands.cmd_key(c)))
            if exists is None:
                touched.append(_mark_expired(client, machine, cmd_id))

        pending = _call_with_retry(lambda: client.zrange(qkey, 0, batch - 1))
        for cmd_id in pending:
            touched.append(_process_one(client, machine, key, cmd_id, now))
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
