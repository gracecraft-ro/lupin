"""Tests for the cross-machine command queue's Redis plumbing and signing
(issue #28, aligned to #27's full design). Uses the real `redis-server`
fixtures in `conftest.py`, same as `test_claims.py`/`test_slots_redis.py`.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import redis as redis_lib

from lupin import cli, commands, slots

ACTOR_KW = {"actor": "grace", "issuer": "test-host"}


def test_sign_verify_roundtrip():
    fields = {
        "v": 1, "id": "x", "target": "jesus", "action": "loop.stop", "params": {},
        "actor": "grace", "issuer": "pihome", "issued_at": 1.0, "expires_at": 121.0,
    }
    sig = commands.sign(fields, "secret")
    cmd = {**fields, "sig": sig}
    assert commands.verify(cmd, "secret") is True


def test_verify_rejects_wrong_key():
    fields = {
        "v": 1, "id": "x", "target": "jesus", "action": "loop.stop", "params": {},
        "actor": "grace", "issuer": "pihome", "issued_at": 1.0, "expires_at": 121.0,
    }
    cmd = {**fields, "sig": commands.sign(fields, "secret")}
    assert commands.verify(cmd, "wrong-secret") is False


def test_verify_rejects_tampered_field():
    fields = {
        "v": 1, "id": "x", "target": "jesus", "action": "loop.stop", "params": {},
        "actor": "grace", "issuer": "pihome", "issued_at": 1.0, "expires_at": 121.0,
    }
    cmd = {**fields, "sig": commands.sign(fields, "secret")}
    cmd["action"] = "loop.run"  # tampered after signing
    assert commands.verify(cmd, "secret") is False


def test_parse_params_basic():
    assert commands.parse_params(["repo=owner/name", "flag=1"]) == {"repo": "owner/name", "flag": "1"}


def test_parse_params_rejects_missing_equals():
    with pytest.raises(ValueError):
        commands.parse_params(["not-a-pair"])


def test_enqueue_writes_signed_cmd_and_queue_entry(redis_port, flush_redis):
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    cmd_id = commands.enqueue(
        "jesus", "loop.stop", {"repo": "gracecraft/lupin"}, key="secret", **ACTOR_KW, **kw
    )

    raw = redis_lib.Redis(host="127.0.0.1", port=redis_port, decode_responses=True)
    stored = json.loads(raw.get(f"lupin:v1:cmd:{cmd_id}"))
    assert stored["v"] == 1
    assert stored["target"] == "jesus"
    assert stored["action"] == "loop.stop"
    assert stored["params"] == {"repo": "gracecraft/lupin"}
    assert stored["actor"] == "grace"
    assert stored["issuer"] == "test-host"
    assert stored["expires_at"] == pytest.approx(stored["issued_at"] + commands.DEFAULT_PICKUP_S)
    assert commands.verify(stored, "secret") is True

    members = raw.zrange("lupin:v1:cmdq:jesus", 0, -1)
    assert members == [cmd_id]


def test_enqueue_respects_custom_pickup_window(redis_port, flush_redis):
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    cmd_id = commands.enqueue("jesus", "loop.stop", {}, key="secret", pickup_window=5.0, **ACTOR_KW, **kw)
    raw = redis_lib.Redis(host="127.0.0.1", port=redis_port, decode_responses=True)
    stored = json.loads(raw.get(f"lupin:v1:cmd:{cmd_id}"))
    assert stored["expires_at"] == pytest.approx(stored["issued_at"] + 5.0)


def test_enqueue_writes_a_cmdlog_entry(redis_port, flush_redis):
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    cmd_id = commands.enqueue("jesus", "loop.stop", {"repo": "a"}, key="secret", **ACTOR_KW, **kw)
    raw = redis_lib.Redis(host="127.0.0.1", port=redis_port, decode_responses=True)
    entries = raw.xrange("lupin:v1:cmdlog")
    assert len(entries) == 1
    _entry_id, fields = entries[0]
    assert fields["id"] == cmd_id
    assert fields["event"] == "enqueued"
    assert fields["actor"] == "grace"


def test_enqueue_unreachable_redis_raises(closed_port):
    kw = {"redis_host": "127.0.0.1", "redis_port": closed_port}
    with pytest.raises(slots.CoordinatorUnreachable):
        commands.enqueue("jesus", "loop.stop", {}, key="secret", **ACTOR_KW, **kw)


def test_get_status_unknown_id_returns_none(redis_port, flush_redis):
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    assert commands.get_status("nope", **kw) is None


def test_get_status_queued_when_only_cmd_exists(redis_port, flush_redis):
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    cmd_id = commands.enqueue("jesus", "loop.stop", {}, key="secret", **ACTOR_KW, **kw)
    status = commands.get_status(cmd_id, **kw)
    assert status["state"] == "queued"
    assert status["target"] == "jesus"


def test_get_status_returns_cmdres_when_present(redis_port, flush_redis):
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    cmd_id = commands.enqueue("jesus", "loop.stop", {}, key="secret", **ACTOR_KW, **kw)
    raw = redis_lib.Redis(host="127.0.0.1", port=redis_port, decode_responses=True)
    raw.set(f"lupin:v1:cmdres:{cmd_id}", json.dumps({"id": cmd_id, "state": "ok"}))
    status = commands.get_status(cmd_id, **kw)
    assert status["state"] == "ok"


def test_get_queue_lists_pending_oldest_first(redis_port, flush_redis, monkeypatch):
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    timestamps = iter((1_800_000_000.001, 1_800_000_000.002))
    monkeypatch.setattr(commands, "time", SimpleNamespace(time=lambda: next(timestamps)))
    first = commands.enqueue("jesus", "loop.stop", {"repo": "a"}, key="secret", **ACTOR_KW, **kw)
    second = commands.enqueue("jesus", "loop.run", {"repo": "b"}, key="secret", **ACTOR_KW, **kw)
    entries = commands.get_queue("jesus", **kw)
    assert [e["id"] for e in entries] == [first, second]
    assert entries[0]["action"] == "loop.stop"
    assert entries[1]["params"] == {"repo": "b"}


def test_get_queue_unreachable_redis_raises(closed_port):
    kw = {"redis_host": "127.0.0.1", "redis_port": closed_port}
    with pytest.raises(slots.CoordinatorUnreachable):
        commands.get_queue("jesus", **kw)


def test_cli_cmd_send_status_queue_roundtrip(redis_port, flush_redis, capsys, monkeypatch):
    # Same reason as test_claims.py: clear this sandbox's real-deployment
    # Redis auth env vars so the CLI's defaults don't try to auth against
    # the test's plain (no-ACL) redis-server fixture.
    monkeypatch.delenv("LUPIN_REDIS_USERNAME", raising=False)
    monkeypatch.delenv("LUPIN_REDIS_PASSWORD", raising=False)
    common = ["--redis-host", "127.0.0.1", "--redis-port", str(redis_port)]

    code = cli.main([
        "cmd", "send", "jesus", "loop.stop", "repo=gracecraft/lupin",
        "--signing-key", "secret", *common,
    ])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    cmd_id = captured.out.strip()

    code = cli.main(["cmd", "status", cmd_id, *common])
    captured = capsys.readouterr()
    assert code == 0
    assert "queued" in captured.out

    code = cli.main(["cmd", "queue", "jesus", *common])
    captured = capsys.readouterr()
    assert code == 0
    assert cmd_id in captured.out


def test_cli_cmd_send_without_signing_key_exits_1(redis_port, flush_redis, capsys, monkeypatch):
    monkeypatch.delenv("LUPIN_REDIS_USERNAME", raising=False)
    monkeypatch.delenv("LUPIN_REDIS_PASSWORD", raising=False)
    monkeypatch.delenv("LUPIN_CMD_SIGNING_KEY", raising=False)
    common = ["--redis-host", "127.0.0.1", "--redis-port", str(redis_port)]
    code = cli.main(["cmd", "send", "jesus", "loop.stop", *common])
    captured = capsys.readouterr()
    assert code == 1
    assert "signing-key" in captured.err


def test_cli_cmd_send_bad_param_format_exits_1(redis_port, flush_redis, capsys, monkeypatch):
    monkeypatch.delenv("LUPIN_REDIS_USERNAME", raising=False)
    monkeypatch.delenv("LUPIN_REDIS_PASSWORD", raising=False)
    common = ["--redis-host", "127.0.0.1", "--redis-port", str(redis_port)]
    code = cli.main(["cmd", "send", "jesus", "loop.stop", "not-a-pair", "--signing-key", "secret", *common])
    captured = capsys.readouterr()
    assert code == 1


def test_cli_cmd_status_unknown_id_exits_1(redis_port, flush_redis, capsys, monkeypatch):
    monkeypatch.delenv("LUPIN_REDIS_USERNAME", raising=False)
    monkeypatch.delenv("LUPIN_REDIS_PASSWORD", raising=False)
    common = ["--redis-host", "127.0.0.1", "--redis-port", str(redis_port)]
    code = cli.main(["cmd", "status", "nope", *common])
    captured = capsys.readouterr()
    assert code == 1


def test_cli_cmd_send_unreachable_redis_exits_3(closed_port, capsys, monkeypatch):
    monkeypatch.delenv("LUPIN_REDIS_USERNAME", raising=False)
    monkeypatch.delenv("LUPIN_REDIS_PASSWORD", raising=False)
    common = ["--redis-host", "127.0.0.1", "--redis-port", str(closed_port)]
    code = cli.main(["cmd", "send", "jesus", "loop.stop", "--signing-key", "secret", *common])
    captured = capsys.readouterr()
    assert code == 3
    assert "cannot reach the redis coordinator" in captured.err
