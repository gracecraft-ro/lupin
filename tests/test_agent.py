"""End-to-end tests for the command-queue poll loop (issue #28, aligned to
#27's full design): enqueue via `commands.py`, then
poll/claim/execute(mocked)/result via `agent.py`, against the real
`redis-server` fixtures in `conftest.py`.

`subprocess.run` is mocked. These tests do not start Herdr or systemd units.
They check signed queue actions and results against Redis.
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


def test_valid_command_runs_as_agent_user_and_produces_ok_result(redis_port, flush_redis, monkeypatch):
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    cmd_id = commands.enqueue("jesus", "loop.stop", {"repo": "lupin"}, key=KEY, **ACTOR_KW, **kw)

    fake = _fake_run(returncode=0)
    monkeypatch.setattr(agent.subprocess, "run", fake)

    touched = agent.poll_once("jesus", KEY, **kw)

    assert fake.calls == [["lupin", "loop", "local-action", "stop", "lupin"]]
    assert touched == [{"id": cmd_id, "state": "ok"}]
    status = commands.get_status(cmd_id, **kw)
    assert status["state"] == "ok"
    assert status["host"] == "jesus"
    assert status["exit_code"] == 0
    assert status["output"] == "ok"
    assert status["truncated"] is False
    raw = redis_lib.Redis(host="127.0.0.1", port=redis_port, decode_responses=True)
    assert raw.zrange("lupin:v1:cmdq:jesus", 0, -1) == []


def test_loop_run_returns_after_starting_lupin_worker(redis_port, flush_redis, monkeypatch):
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    cmd_id = commands.enqueue("jesus", "loop.run", {"repo": "lupin"}, key=KEY, **ACTOR_KW, **kw)

    fake = _fake_run(returncode=0)
    monkeypatch.setattr(agent.subprocess, "run", fake)

    touched = agent.poll_once("jesus", KEY, **kw)

    assert fake.calls == [["lupin", "run", "lupin"]]
    assert touched == [{"id": cmd_id, "state": "ok"}]
    status = commands.get_status(cmd_id, **kw)
    assert status["state"] == "ok"
    assert status["output"] == "ok"



@pytest.mark.parametrize(
    "action, params, expected",
    [
        (
            "loop.run",
            {"repo": "lupin", "platform": "omp", "note": "review\nhandoff", "resume": True},
            ["lupin", "run", "lupin", "--platform", "omp", "--note", "review\nhandoff", "--resume"],
        ),
        (
            "loop.run-all",
            {"note": "review"},
            ["lupin", "run", "--all", "--note", "review"],
        ),
    ],
)
def test_loop_run_forwards_validated_remote_options(
    redis_port, flush_redis, monkeypatch, action, params, expected
):
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    commands.enqueue("jesus", action, params, key=KEY, **ACTOR_KW, **kw)
    fake = _fake_run(returncode=0)
    monkeypatch.setattr(agent.subprocess, "run", fake)

    agent.poll_once("jesus", KEY, **kw)

    assert fake.calls == [expected]


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
    `^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$` must never reach Lupin's argv.
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


def test_valid_repo_format_is_passed_to_lupin(redis_port, flush_redis, monkeypatch):
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    cmd_id = commands.enqueue("jesus", "loop.stop", {"repo": "field-trip_2.0"}, key=KEY, **ACTOR_KW, **kw)

    fake = _fake_run()
    monkeypatch.setattr(agent.subprocess, "run", fake)

    touched = agent.poll_once("jesus", KEY, **kw)

    assert touched == [{"id": cmd_id, "state": "ok"}]
    assert fake.calls == [["lupin", "loop", "local-action", "stop", "field-trip_2.0"]]


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



def testvalidate_cal_expr_rejects_newline():
    with pytest.raises(agent.RejectedCommand):
        agent.validate_cal_expr("*-*-* 00:00:00\n[Service]\nExecStart=rm -rf /")


def testvalidate_cal_expr_rejects_control_char():
    with pytest.raises(agent.RejectedCommand):
        agent.validate_cal_expr("*-*-* 00\x0000:00")


def testvalidate_cal_expr_rejects_overlength():
    with pytest.raises(agent.RejectedCommand):
        agent.validate_cal_expr("x" * (agent._CAL_EXPR_MAX_LEN + 1))


def testvalidate_cal_expr_accepts_a_normal_expression():
    assert agent.validate_cal_expr("*-*-* 00/5:00:00") == "*-*-* 00/5:00:00"


def test_loop_peek_runs_local_action_directly(redis_port, flush_redis, monkeypatch):
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    cmd_id = commands.enqueue("jesus", "loop.peek", {"repo": "lupin", "lines": "20"}, key=KEY, **ACTOR_KW, **kw)

    fake = _fake_run(returncode=0, stdout="pane text")
    monkeypatch.setattr(agent.subprocess, "run", fake)

    touched = agent.poll_once("jesus", KEY, **kw)

    assert fake.calls == [["lupin", "loop", "local-action", "peek", "lupin", "20"]]
    assert touched == [{"id": cmd_id, "state": "ok"}]


def test_loop_peek_defaults_lines_to_sixty(redis_port, flush_redis, monkeypatch):
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    commands.enqueue("jesus", "loop.peek", {"repo": "lupin"}, key=KEY, **ACTOR_KW, **kw)

    fake = _fake_run(returncode=0)
    monkeypatch.setattr(agent.subprocess, "run", fake)

    agent.poll_once("jesus", KEY, **kw)

    assert fake.calls == [["lupin", "loop", "local-action", "peek", "lupin", "60"]]

def test_loop_state_uses_herdr_reported_state(redis_port, flush_redis, monkeypatch):
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    cmd_id = commands.enqueue("jesus", "loop.state", {"repo": "lupin"}, key=KEY, **ACTOR_KW, **kw)
    herdr_state = '{"state":"running","backend":"herdr","session":"lupin"}'
    fake = _fake_run(returncode=0, stdout=herdr_state)
    monkeypatch.setattr(agent.subprocess, "run", fake)

    touched = agent.poll_once("jesus", KEY, **kw)

    assert fake.calls == [["lupin", "loop", "local-action", "state", "lupin"]]
    assert touched == [{"id": cmd_id, "state": "ok"}]
    assert commands.get_status(cmd_id, **kw)["output"] == herdr_state
    assert "loop.state" in agent.DRAIN_ALLOWED


def test_schedule_show_runs_lupin_directly_without_systemd_run(redis_port, flush_redis, monkeypatch):
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    commands.enqueue("jesus", "schedule.show", {}, key=KEY, **ACTOR_KW, **kw)

    fake = _fake_run(returncode=0)
    monkeypatch.setattr(agent.subprocess, "run", fake)

    agent.poll_once("jesus", KEY, **kw)

    assert fake.calls == [["lupin", "schedule"]]


def test_schedule_set_cal_runs_lupin_after_validation(redis_port, flush_redis, monkeypatch):
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    cmd_id = commands.enqueue(
        "jesus", "schedule.set", {"mode": "cal", "expr": "*-*-* 00/5:00:00"}, key=KEY, **ACTOR_KW, **kw
    )

    fake = _fake_run(returncode=0)
    monkeypatch.setattr(agent.subprocess, "run", fake)
    monkeypatch.setattr(agent.shutil, "which", lambda name: None)

    touched = agent.poll_once("jesus", KEY, **kw)

    assert fake.calls == [["lupin", "schedule", "cal", "*-*-* 00/5:00:00"]]
    assert touched == [{"id": cmd_id, "state": "ok"}]


def test_schedule_set_cal_with_newline_is_rejected_without_executing(redis_port, flush_redis, monkeypatch):
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    cmd_id = commands.enqueue(
        "jesus", "schedule.set",
        {"mode": "cal", "expr": "*-*-* 00:00:00\n[Service]\nExecStart=rm -rf /"},
        key=KEY, **ACTOR_KW, **kw,
    )

    fake = _fake_run()
    monkeypatch.setattr(agent.subprocess, "run", fake)

    touched = agent.poll_once("jesus", KEY, **kw)

    assert fake.calls == []
    assert touched == [{"id": cmd_id, "state": "rejected"}]


def test_schedule_set_cal_rejected_when_systemd_analyze_says_invalid(redis_port, flush_redis, monkeypatch):
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    cmd_id = commands.enqueue(
        "jesus", "schedule.set", {"mode": "cal", "expr": "not a real expression"}, key=KEY, **ACTOR_KW, **kw
    )

    fake = _fake_run()
    monkeypatch.setattr(agent.shutil, "which", lambda name: "/usr/bin/systemd-analyze")
    monkeypatch.setattr(
        agent.subprocess, "run",
        lambda argv, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr="bad")
        if argv[:2] == ["/usr/bin/systemd-analyze", "calendar"] else fake(argv, **kwargs),
    )

    touched = agent.poll_once("jesus", KEY, **kw)

    assert touched == [{"id": cmd_id, "state": "rejected"}]
    assert fake.calls == []


def test_schedule_set_first_runs_lupin(redis_port, flush_redis, monkeypatch):
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    cmd_id = commands.enqueue(
        "jesus", "schedule.set", {"mode": "first", "when": "+2h5m", "interval": "5h15m"},
        key=KEY, **ACTOR_KW, **kw,
    )

    fake = _fake_run(returncode=0)
    monkeypatch.setattr(agent.subprocess, "run", fake)

    touched = agent.poll_once("jesus", KEY, **kw)

    assert fake.calls == [["lupin", "schedule", "first", "+2h5m", "every", "5h15m"]]
    assert touched == [{"id": cmd_id, "state": "ok"}]


def test_schedule_pause_and_resume_run_as_agent_user(redis_port, flush_redis, monkeypatch):
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    pause_id = commands.enqueue("jesus", "schedule.pause", {}, key=KEY, **ACTOR_KW, **kw)
    fake = _fake_run(returncode=0)
    monkeypatch.setattr(agent.subprocess, "run", fake)
    pause_touched = agent.poll_once("jesus", KEY, batch=1, **kw)

    resume_id = commands.enqueue("jesus", "schedule.resume", {}, key=KEY, **ACTOR_KW, **kw)
    resume_touched = agent.poll_once("jesus", KEY, batch=1, **kw)

    assert fake.calls == [["lupin", "pause"], ["lupin", "resume"]]
    assert pause_touched == [{"id": pause_id, "state": "ok"}]
    assert resume_touched == [{"id": resume_id, "state": "ok"}]


@pytest.mark.parametrize(
    "action, params",
    [
        ("loop.run", {"repo": "lupin"}),
        ("loop.run-all", {}),
    ],
)
def test_draining_machine_rejects_loop_start_without_executing(
    redis_port, flush_redis, monkeypatch, action, params
):
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    raw = redis_lib.Redis(host="127.0.0.1", port=redis_port, decode_responses=True)
    raw.set("lupin:v1:machine:jesus", json.dumps({"state": "draining"}))
    cmd_id = commands.enqueue("jesus", action, params, key=KEY, **ACTOR_KW, **kw)

    fake = _fake_run()
    monkeypatch.setattr(agent.subprocess, "run", fake)

    touched = agent.poll_once("jesus", KEY, **kw)

    assert fake.calls == []
    assert touched == [{"id": cmd_id, "state": "rejected"}]
    status = commands.get_status(cmd_id, **kw)
    assert "draining" in status["reason"]

def test_draining_machine_rejects_schedule_set_without_executing(redis_port, flush_redis, monkeypatch):
    # schedule.set arms a timer to start a loop later -- draining blocks it
    # the same as loop.run (see DRAIN_ALLOWED's comment in agent.py).
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    raw = redis_lib.Redis(host="127.0.0.1", port=redis_port, decode_responses=True)
    raw.set("lupin:v1:machine:jesus", json.dumps({"state": "draining"}))
    cmd_id = commands.enqueue(
        "jesus", "schedule.set", {"mode": "first", "when": "+1h", "interval": "1h"}, key=KEY, **ACTOR_KW, **kw
    )

    fake = _fake_run()
    monkeypatch.setattr(agent.subprocess, "run", fake)

    touched = agent.poll_once("jesus", KEY, **kw)

    assert fake.calls == []
    assert touched == [{"id": cmd_id, "state": "rejected"}]


def test_draining_machine_rejects_schedule_resume_without_executing(redis_port, flush_redis, monkeypatch):
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    raw = redis_lib.Redis(host="127.0.0.1", port=redis_port, decode_responses=True)
    raw.set("lupin:v1:machine:jesus", json.dumps({"state": "draining"}))
    cmd_id = commands.enqueue("jesus", "schedule.resume", {}, key=KEY, **ACTOR_KW, **kw)

    fake = _fake_run()
    monkeypatch.setattr(agent.subprocess, "run", fake)

    touched = agent.poll_once("jesus", KEY, **kw)

    assert fake.calls == []
    assert touched == [{"id": cmd_id, "state": "rejected"}]


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


def test_draining_machine_still_runs_peek_show_and_pause(redis_port, flush_redis, monkeypatch):
    # Read-only actions, and schedule.pause (winds a timer down, same
    # shape as loop.stop) -- all three are in DRAIN_ALLOWED.
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    raw = redis_lib.Redis(host="127.0.0.1", port=redis_port, decode_responses=True)
    raw.set("lupin:v1:machine:jesus", json.dumps({"state": "draining"}))

    fake = _fake_run(returncode=0)
    monkeypatch.setattr(agent.subprocess, "run", fake)

    for action, params in (("loop.peek", {"repo": "lupin"}), ("schedule.show", {}), ("schedule.pause", {})):
        cmd_id = commands.enqueue("jesus", action, params, key=KEY, **ACTOR_KW, **kw)
        touched = agent.poll_once("jesus", KEY, **kw)
        assert touched == [{"id": cmd_id, "state": "ok"}], action


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
