"""Shared local-or-remote dispatch for loop-control actions (issue #2,
phase A): given a target machine, either run `loopctl` directly (the
target is this machine) or enqueue a signed command on the Redis queue
from `commands.py` for that machine's `lupin agent` to run. One decision,
used by both `cli.py`'s `stop`/`peek`/`schedule`/`pause`/`resume` verbs and
`serve.py`'s dashboard loop controls, so the two surfaces can't drift.

`attach` is not here -- it execs a terminal directly (local `loopctl
attach`, or `ssh -t <target> loopctl attach`), never through the Redis
queue (see `ssh_target_for`'s docstring below for the mapping file
`attach` reads to find a remote target).
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Callable

from . import commands, machines

SSH_TARGETS_PATH = Path.home() / ".config" / "lupin" / "ssh-targets"

# A command's terminal states -- same set `commands.py`'s `STATES` defines,
# minus "queued"/"running" (still in flight, not terminal).
_TERMINAL_STATES = {"ok", "failed", "rejected", "expired"}


class AmbiguousMachine(Exception):
    """A repo's running machine couldn't be resolved to exactly one --
    `cli.py` turns this into exit code 5 ("use --machine")."""

    def __init__(self, repo: str, candidates: list[str]):
        self.repo = repo
        self.candidates = candidates
        super().__init__(f"{repo!r} is running on {len(candidates)} machine(s) {candidates!r} -- use --machine")


class MissingSigningKey(Exception):
    """A remote action was asked for, but no signing key was given --
    `commands.enqueue` needs one for every target."""

    def __init__(self, machine: str):
        self.machine = machine
        super().__init__(f"no signing key given to reach {machine!r}")


def resolve_machine_for_repo(repo: str, connection: dict) -> str:
    """The one machine whose heartbeat says it is running `repo`'s loop
    right now. Raises `AmbiguousMachine` for 0 or more than 1 match --
    `machines.py`'s heartbeat `loops` field (issue #2 phase A) is the
    single source of truth here, not a guess.
    """
    try:
        records = machines.machines(connection)
    except machines.CoordinatorUnreachable:
        records = []
    candidates = [
        record["name"]
        for record in records
        if any(loop.get("repo") == repo for loop in record.get("loops", []))
    ]
    if len(candidates) != 1:
        raise AmbiguousMachine(repo, candidates)
    return candidates[0]


def _default_run_local(argv: list[str], timeout: float = 20.0) -> tuple[int, str]:
    """`subprocess.run(argv)`, never a shell -- the default local runner for
    a caller that doesn't already have its own (`serve.py` passes its own
    `run()` instead, so the dashboard's existing behavior doesn't change).
    """
    try:
        proc = subprocess.run(argv, shell=False, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return 127, f"not found: {argv[0]}"
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s: {' '.join(argv)}"
    out = proc.stdout
    if proc.stderr:
        out = out + ("\n" if out and not out.endswith("\n") else "") + proc.stderr
    return proc.returncode, out


def dispatch_loop_action(
    *,
    machine: str,
    local_host: str,
    local_argv: list[str],
    queue_action: str,
    queue_params: dict,
    connection: dict,
    signing_key: str | None = None,
    actor: str = "lupin",
    issuer: str | None = None,
    wait_s: float | None = None,
    run_local: Callable[[list[str]], tuple[int, str]] | None = None,
) -> dict:
    """Run one loop-control action on `machine`.

    `machine == local_host`: runs `local_argv` directly (`run_local`, or
    `_default_run_local` if not given) and returns
    `{"mode": "local", "returncode": int, "output": str}`.

    Otherwise: enqueues `queue_action`/`queue_params` on the Redis queue for
    `machine`'s `lupin agent` to run, and returns
    `{"mode": "queued", "id": str, "result": dict | None}`. `result` is
    `None` if `wait_s` wasn't given (fire-and-forget -- `serve.py`'s
    dashboard doesn't block an HTTP reply on a remote machine) or if it was
    given but no terminal state (`ok`/`failed`/`rejected`/`expired`) landed
    before the deadline (`cli.py`'s synchronous verbs treat that as "sent,
    result unknown" -- exit code 4).

    Raises `MissingSigningKey` if `machine != local_host` and no
    `signing_key` was given, and `commands.enqueue`'s own
    `CoordinatorUnreachable` if Redis can't be reached.
    """
    if machine == local_host:
        runner = run_local or _default_run_local
        returncode, output = runner(local_argv)
        return {"mode": "local", "returncode": returncode, "output": output}

    if not signing_key:
        raise MissingSigningKey(machine)

    cmd_id = commands.enqueue(
        machine, queue_action, queue_params,
        key=signing_key, actor=actor, issuer=issuer or local_host,
        **connection,
    )
    result = None
    if wait_s is not None and wait_s > 0:
        deadline = time.monotonic() + wait_s
        while True:
            result = commands.get_status(cmd_id, **connection)
            if result and result.get("state") in _TERMINAL_STATES:
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(0.5)
    return {"mode": "queued", "id": cmd_id, "result": result}


def ssh_target_for(machine: str, path: Path | str | None = None) -> str | None:
    """`machine`'s ssh target (a `user@host`, or an ssh_config alias), read
    from a plain text file -- one line per machine, `<machine> <target>`;
    blank lines and `#` comments are ignored. `None` if the file is
    missing or has no line for `machine`.

    Default path: `~/.config/lupin/ssh-targets`. This is local, per-machine
    config (in practice, written by Nix on each caller), not fleet state
    -- unlike the Redis command queue, `attach` needs this before it can
    even open a connection, so it can't itself come from the far side of
    that connection.
    """
    p = Path(path) if path else SSH_TARGETS_PATH
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(maxsplit=1)
        if len(parts) == 2 and parts[0] == machine:
            return parts[1]
    return None
