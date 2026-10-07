"""End-to-end tests for the command-queue poll loop (issue #28, aligned to
#27's full design): enqueue via `commands.py`, then
poll/claim/execute(mocked)/result via `agent.py`, against the real
`redis-server` fixtures in `conftest.py`.

`subprocess.run` is mocked -- there's no real `loopctl`/`systemd-run` in
this sandbox -- but argv construction, signature verification, claiming,
and queue/result bookkeeping all run for real against Redis.
"""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest
import redis as redis_lib

from lupin import agent, commands, slots

KEY = "secret"
ACTOR_KW = {"actor": "grace", "issuer": "test-host"}


def _fake_run(returncode=0, stdout="ok", stderr=""):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    run.calls = calls
    return run


def test_valid_command_runs_via_systemd_run_and_produces_ok_result(redis_port, flush_redis, monkeypatch):
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    cmd_id = commands.enqueue("jesus", "loop.stop", {"repo": "lupin"}, key=KEY, **ACTOR_KW, **kw)

    fake = _fake_run(returncode=0)
    monkeypatch.setattr(agent.subprocess, "run", fake)

    touched = agent.poll_once("jesus", KEY, **kw)

    assert fake.calls == [[
        "systemd-run", f"--unit=lupin-cmd-{cmd_id[:8]}", "--collect", "--wait", "loopctl", "stop", "lupin",
    ]]
    assert touched == [{"id": cmd_id, "state": "ok"}]
    status = commands.get_status(cmd_id, **kw)
    assert status["state"] == "ok"
    assert status["host"] == "jesus"
    assert status["exit_code"] == 0
    assert status["output"] == "ok"
    assert status["truncated"] is False
    raw = redis_lib.Redis(host="127.0.0.1", port=redis_port, decode_responses=True)
    assert raw.zrange("lupin:v1:cmdq:jesus", 0, -1) == []


def test_loop_run_also_wraps_in_systemd_run(redis_port, flush_redis, monkeypatch):
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    cmd_id = commands.enqueue("jesus", "loop.run", {"repo": "lupin"}, key=KEY, **ACTOR_KW, **kw)

    fake = _fake_run(returncode=0)
    monkeypatch.setattr(agent.subprocess, "run", fake)

    agent.poll_once("jesus", KEY, **kw)

    assert fake.calls == [[
        "systemd-run", f"--unit=lupin-cmd-{cmd_id[:8]}", "--collect", "loopctl", "run", "lupin",
    ]]


def test_failed_run_reports_nonzero_exit_code(redis_port, flush_redis, monkeypatch):
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    cmd_id = commands.enqueue("jesus", "loop.stop", {"repo": "lupin"}, key=KEY, **ACTOR_KW, **kw)

    fake = _fake_run(returncode=1, stdout="", stderr="boom")
    monkeypatch.setattr(agent.subprocess, "run", fake)

    touched = agent.poll_once("jesus", KEY, **kw)

    assert touched == [{"id": cmd_id, "state": "failed"}]
    status = commands.get_status(cmd_id, **kw)
    assert status["exit_code"] == 1
    assert status["output"] == "boom"


def test_bad_signature_is_rejected_without_executing(redis_port, flush_redis, monkeypatch):
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    cmd_id = commands.enqueue(
        "jesus", "loop.stop", {"repo": "lupin"}, key="right-secret", **ACTOR_KW, **kw
    )

    fake = _fake_run()
    monkeypatch.setattr(agent.subprocess, "run", fake)

    touched = agent.poll_once("jesus", "wrong-secret", **kw)

    assert fake.calls == []
    assert touched == [{"id": cmd_id, "state": "rejected"}]
    status = commands.get_status(cmd_id, **kw)
    assert status["state"] == "rejected"
    assert "bad signature" in status["reason"]


def test_forged_target_field_is_rejected_without_executing(redis_port, flush_redis, monkeypatch):
    """Defense in depth: even if a command somehow ends up in this machine's
    queue with a `target` field naming a different machine, the explicit
    field check must still catch it -- not just the key-based routing.
    """
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    now = time.time()
    fields = {
        "v": 1, "id": "forged1", "target": "someone-else", "action": "loop.stop",
        "params": {"repo": "lupin"}, "actor": "grace", "issuer": "test-host",
        "issued_at": now, "expires_at": now + 120,
    }
    cmd = {**fields, "sig": commands.sign(fields, KEY)}
    raw = redis_lib.Redis(host="127.0.0.1", port=redis_port, decode_responses=True)
    raw.set("lupin:v1:cmd:forged1", json.dumps(cmd), px=3600000)
    raw.zadd("lupin:v1:cmdq:jesus", {"forged1": commands.now_ms()})

    fake = _fake_run()
    monkeypatch.setattr(agent.subprocess, "run", fake)

    touched = agent.poll_once("jesus", KEY, **kw)

    assert fake.calls == []
    assert touched == [{"id": "forged1", "state": "rejected"}]


def test_invalid_repo_format_is_rejected_without_executing(redis_port, flush_redis, monkeypatch):
    """Defense in depth required by the design even though nothing upstream
    validates `repo` yet: anything not matching
    `^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$` must never reach `loopctl`'s argv.
    """
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    cmd_id = commands.enqueue("jesus", "loop.stop", {"repo": "; rm -rf /"}, key=KEY, **ACTOR_KW, **kw)

    fake = _fake_run()
    monkeypatch.setattr(agent.subprocess, "run", fake)

    touched = agent.poll_once("jesus", KEY, **kw)

    assert fake.calls == []
    assert touched == [{"id": cmd_id, "state": "rejected"}]
    status = commands.get_status(cmd_id, **kw)
    assert "invalid repo" in status["reason"]


def test_valid_repo_formats_are_accepted(redis_port, flush_redis, monkeypatch):
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    cmd_id = commands.enqueue("jesus", "loop.stop", {"repo": "field-trip_2.0"}, key=KEY, **ACTOR_KW, **kw)

    fake = _fake_run()
    monkeypatch.setattr(agent.subprocess, "run", fake)

    touched = agent.poll_once("jesus", KEY, **kw)

    assert touched == [{"id": cmd_id, "state": "ok"}]
    assert fake.calls == [[
        "systemd-run", f"--unit=lupin-cmd-{cmd_id[:8]}", "--collect", "--wait", "loopctl", "stop", "field-trip_2.0",
    ]]


def test_command_for_a_different_machine_is_never_picked_up(redis_port, flush_redis, monkeypatch):
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    commands.enqueue("ralpha", "loop.stop", {"repo": "lupin"}, key=KEY, **ACTOR_KW, **kw)

    fake = _fake_run()
    monkeypatch.setattr(agent.subprocess, "run", fake)

    touched = agent.poll_once("jesus", KEY, **kw)

    assert touched == []
    assert fake.calls == []


def test_expired_command_is_marked_expired_and_never_executes(redis_port, flush_redis, monkeypatch):
    """Past `expires_at` by more than the clock-skew allowance (30s) -- a
    short `pickup_window` alone isn't enough to prove this, since the skew
    allowance covers a few seconds of staleness on purpose.
    """
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    now = time.time()
    fields = {
        "v": 1, "id": "expired1", "target": "jesus", "action": "loop.stop",
        "params": {"repo": "lupin"}, "actor": "grace", "issuer": "test-host",
        "issued_at": now - 200, "expires_at": now - 80,  # 80s past expiry, well beyond the 30s skew
    }
    cmd = {**fields, "sig": commands.sign(fields, KEY)}
    raw = redis_lib.Redis(host="127.0.0.1", port=redis_port, decode_responses=True)
    raw.set("lupin:v1:cmd:expired1", json.dumps(cmd), px=3600000)
    raw.zadd("lupin:v1:cmdq:jesus", {"expired1": commands.now_ms()})
    cmd_id = "expired1"

    fake = _fake_run()
    monkeypatch.setattr(agent.subprocess, "run", fake)

    touched = agent.poll_once("jesus", KEY, **kw)

    assert fake.calls == []
    assert touched == [{"id": cmd_id, "state": "expired"}]
    status = commands.get_status(cmd_id, **kw)
    assert status["state"] == "expired"


def test_clock_skew_allowance_lets_a_just_expired_command_still_run(redis_port, flush_redis, monkeypatch):
    """A command past its nominal `expires_at` but within the 30s skew
    allowance must still execute -- the allowance exists precisely so a
    small clock difference between sender and executor doesn't reject a
    command that's actually still fresh.
    """
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    now = time.time()
    fields = {
        "v": 1, "id": "skew1", "target": "jesus", "action": "loop.stop",
        "params": {"repo": "lupin"}, "actor": "grace", "issuer": "test-host",
        "issued_at": now - 130, "expires_at": now - 10,  # 10s past nominal expiry, well within 30s skew
    }
    cmd = {**fields, "sig": commands.sign(fields, KEY)}
    raw = redis_lib.Redis(host="127.0.0.1", port=redis_port, decode_responses=True)
    raw.set("lupin:v1:cmd:skew1", json.dumps(cmd), px=3600000)
    raw.zadd("lupin:v1:cmdq:jesus", {"skew1": commands.now_ms()})

    fake = _fake_run()
    monkeypatch.setattr(agent.subprocess, "run", fake)

    touched = agent.poll_once("jesus", KEY, **kw)

    assert touched == [{"id": "skew1", "state": "ok"}]
    assert len(fake.calls) == 1


def test_double_claim_only_runs_once(redis_port, flush_redis, monkeypatch):
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    cmd_id = commands.enqueue("jesus", "loop.stop", {"repo": "lupin"}, key=KEY, **ACTOR_KW, **kw)

    calls = []
    lock = threading.Lock()

    def slow_run(argv, **kwargs):
        with lock:
            calls.append(argv)
        time.sleep(0.2)  # widen the race window between claim and finish
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(agent.subprocess, "run", slow_run)

    results = []

    def poll():
        results.append(agent.poll_once("jesus", KEY, **kw))

    t1 = threading.Thread(target=poll)
    t2 = threading.Thread(target=poll)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert len(calls) == 1
    states = [r["state"] for batch in results for r in batch if r["id"] == cmd_id]
    assert states.count("ok") == 1
    final = commands.get_status(cmd_id, **kw)
    assert final["state"] == "ok"


def test_startup_scan_marks_orphaned_running_entry_as_failed(redis_port, flush_redis, monkeypatch):
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    cmd_id = commands.enqueue("jesus", "loop.stop", {"repo": "lupin"}, key=KEY, **ACTOR_KW, **kw)

    # Simulate a previous `lupin agent` process that claimed this command
    # and crashed before finishing: cmdres says "running", but the id is
    # still sitting in the queue (never reached the dequeue step).
    raw = redis_lib.Redis(host="127.0.0.1", port=redis_port, decode_responses=True)
    raw.set(
        f"lupin:v1:cmdres:{cmd_id}",
        json.dumps({"id": cmd_id, "state": "running", "host": "jesus"}),
    )

    marked = agent.startup_scan("jesus", **kw)
    assert marked == [cmd_id]

    status = commands.get_status(cmd_id, **kw)
    assert status["state"] == "failed"
    assert raw.zrange("lupin:v1:cmdq:jesus", 0, -1) == []

    # Not silently re-run: a poll after the scan sees nothing left to do.
    fake = _fake_run()
    monkeypatch.setattr(agent.subprocess, "run", fake)
    touched = agent.poll_once("jesus", KEY, **kw)
    assert touched == []
    assert fake.calls == []


def test_startup_scan_ignores_running_entries_for_other_machines(redis_port, flush_redis):
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    cmd_id = commands.enqueue("jesus", "loop.stop", {"repo": "lupin"}, key=KEY, **ACTOR_KW, **kw)
    raw = redis_lib.Redis(host="127.0.0.1", port=redis_port, decode_responses=True)
    raw.set(
        f"lupin:v1:cmdres:{cmd_id}",
        json.dumps({"id": cmd_id, "state": "running", "host": "some-other-host"}),
    )

    marked = agent.startup_scan("jesus", **kw)
    assert marked == []
    status = commands.get_status(cmd_id, **kw)
    assert status["state"] == "running"


def test_poll_once_unreachable_redis_raises(closed_port):
    kw = {"redis_host": "127.0.0.1", "redis_port": closed_port}
    with pytest.raises(slots.CoordinatorUnreachable):
        agent.poll_once("jesus", KEY, **kw)


def test_startup_scan_unreachable_redis_raises(closed_port):
    kw = {"redis_host": "127.0.0.1", "redis_port": closed_port}
    with pytest.raises(slots.CoordinatorUnreachable):
        agent.startup_scan("jesus", **kw)


def test_unknown_action_is_rejected_without_executing(redis_port, flush_redis, monkeypatch):
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    cmd_id = commands.enqueue("jesus", "loop.frobnicate", {}, key=KEY, **ACTOR_KW, **kw)

    fake = _fake_run()
    monkeypatch.setattr(agent.subprocess, "run", fake)

    touched = agent.poll_once("jesus", KEY, **kw)

    assert fake.calls == []
    assert touched == [{"id": cmd_id, "state": "rejected"}]


def test_missing_required_param_is_rejected_without_executing(redis_port, flush_redis, monkeypatch):
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    cmd_id = commands.enqueue("jesus", "loop.stop", {}, key=KEY, **ACTOR_KW, **kw)  # no 'repo'

    fake = _fake_run()
    monkeypatch.setattr(agent.subprocess, "run", fake)

    touched = agent.poll_once("jesus", KEY, **kw)

    assert fake.calls == []
    assert touched == [{"id": cmd_id, "state": "rejected"}]
    status = commands.get_status(cmd_id, **kw)
    assert status["state"] == "rejected"


def test_draining_machine_rejects_loop_run_without_executing(redis_port, flush_redis, monkeypatch):
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    raw = redis_lib.Redis(host="127.0.0.1", port=redis_port, decode_responses=True)
    raw.set("lupin:v1:machine:jesus", json.dumps({"state": "draining"}))
    cmd_id = commands.enqueue("jesus", "loop.run", {"repo": "lupin"}, key=KEY, **ACTOR_KW, **kw)

    fake = _fake_run()
    monkeypatch.setattr(agent.subprocess, "run", fake)

    touched = agent.poll_once("jesus", KEY, **kw)

    assert fake.calls == []
    assert touched == [{"id": cmd_id, "state": "rejected"}]
    status = commands.get_status(cmd_id, **kw)
    assert "draining" in status["reason"]


def test_draining_machine_still_runs_loop_stop(redis_port, flush_redis, monkeypatch):
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    raw = redis_lib.Redis(host="127.0.0.1", port=redis_port, decode_responses=True)
    raw.set("lupin:v1:machine:jesus", json.dumps({"state": "draining"}))
    cmd_id = commands.enqueue("jesus", "loop.stop", {"repo": "lupin"}, key=KEY, **ACTOR_KW, **kw)

    fake = _fake_run(returncode=0)
    monkeypatch.setattr(agent.subprocess, "run", fake)

    touched = agent.poll_once("jesus", KEY, **kw)

    assert touched == [{"id": cmd_id, "state": "ok"}]
    assert len(fake.calls) == 1


def test_non_draining_machine_runs_loop_run_normally(redis_port, flush_redis, monkeypatch):
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    raw = redis_lib.Redis(host="127.0.0.1", port=redis_port, decode_responses=True)
    raw.set("lupin:v1:machine:jesus", json.dumps({"state": "online"}))
    cmd_id = commands.enqueue("jesus", "loop.run", {"repo": "lupin"}, key=KEY, **ACTOR_KW, **kw)

    fake = _fake_run(returncode=0)
    monkeypatch.setattr(agent.subprocess, "run", fake)

    touched = agent.poll_once("jesus", KEY, **kw)

    assert touched == [{"id": cmd_id, "state": "ok"}]
    assert len(fake.calls) == 1


def test_poll_once_writes_cmdlog_entries_for_terminal_outcomes(redis_port, flush_redis, monkeypatch):
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    cmd_id = commands.enqueue("jesus", "loop.stop", {"repo": "lupin"}, key=KEY, **ACTOR_KW, **kw)

    raw = redis_lib.Redis(host="127.0.0.1", port=redis_port, decode_responses=True)
    before = raw.xrange("lupin:v1:cmdlog")
    assert len(before) == 1  # the enqueue event
    assert before[0][1]["event"] == "enqueued"

    fake = _fake_run(returncode=0)
    monkeypatch.setattr(agent.subprocess, "run", fake)
    agent.poll_once("jesus", KEY, **kw)

    after = raw.xrange("lupin:v1:cmdlog")
    assert len(after) == 2  # enqueue + terminal outcome
    assert after[1][1]["id"] == cmd_id
    assert after[1][1]["state"] == "ok"
