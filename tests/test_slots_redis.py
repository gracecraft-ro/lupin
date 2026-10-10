"""Tests for the `redis` slot-lease backend (issue #210).

Uses the real `redis-server` fixtures in `conftest.py`, not a mock -- per
#210's test plan. `redis_port`/`flush_redis`/`closed_port` are defined
there and shared with any future redis-backed test module.
"""

from __future__ import annotations

import json
import time

import pytest
import redis as redis_lib

from lupin import cli, slots, slots_redis


def test_acquire_respects_max_third_call_is_full(redis_port, flush_redis):
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    slots_redis.acquire("bmo", "a", max_holders=2, **kw)
    slots_redis.acquire("bmo", "b", **kw)
    # Third holder on a max-2 slot: full.
    with pytest.raises(slots.SlotFull):
        slots_redis.acquire("bmo", "c", **kw)


def test_holder_past_ttl_is_pruned_and_its_spot_freed(redis_port, flush_redis):
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    first = slots_redis.acquire("bmo", "a", ttl=0.05, max_holders=1, **kw)
    with pytest.raises(slots.SlotFull):
        slots_redis.acquire("bmo", "b", ttl=10, **kw)
    time.sleep(0.2)
    second = slots_redis.acquire("bmo", "b", ttl=10, **kw)
    assert second != first
    status = slots_redis.status(**kw)
    assert status["bmo"]["holders"] == 1


def test_release_is_compare_and_delete(redis_port, flush_redis):
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    real_lease = slots_redis.acquire("bmo", "real-holder", max_holders=2, **kw)

    # A non-holder's release attempt (same slot, a holder name that never
    # acquired) must not touch the real holder's entry.
    assert slots_redis.release("bmo:impostor", **kw) is False
    assert slots_redis.status(**kw)["bmo"]["holders"] == 1

    assert slots_redis.release(real_lease, **kw) is True
    assert slots_redis.status(**kw)["bmo"]["holders"] == 0
    # Releasing twice is not an error -- already gone is the end state anyway.
    assert slots_redis.release(real_lease, **kw) is False


def test_status_json_matches_real_sorted_set_contents(redis_port, flush_redis, capsys):
    common = ["--backend", "redis", "--redis-host", "127.0.0.1", "--redis-port", str(redis_port)]
    cli.main(["acquire", "bmo", "--holder", "a", "--max", "2", *common])
    cli.main(["acquire", "bmo", "--holder", "b", *common])
    capsys.readouterr()

    code = cli.main(["status", "--json", *common])
    captured = capsys.readouterr()
    assert code == 0
    assert json.loads(captured.out) == {"bmo": {"holders": 2, "max": 2}}

    raw = redis_lib.Redis(host="127.0.0.1", port=redis_port, decode_responses=True)
    assert raw.zcard("lupin:v1:slot:bmo") == 2
    assert set(raw.zrange("lupin:v1:slot:bmo", 0, -1)) == {"a", "b"}


def test_set_max_overwrites_an_already_set_max(redis_port, flush_redis):
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    slots_redis.acquire("bmo", "a", max_holders=2, **kw)
    assert slots_redis.status(**kw)["bmo"]["max"] == 2

    slots_redis.set_max("bmo", 5, **kw)

    assert slots_redis.status(**kw)["bmo"]["max"] == 5


def test_set_max_does_not_evict_holders_above_the_new_max(redis_port, flush_redis):
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    slots_redis.acquire("bmo", "a", max_holders=2, **kw)
    slots_redis.acquire("bmo", "b", **kw)

    slots_redis.set_max("bmo", 1, **kw)

    # Both existing holders are still counted -- lowering the max doesn't
    # touch the holder sorted set, only the separate `:max` key.
    assert slots_redis.status(**kw)["bmo"] == {"holders": 2, "max": 1}
    # A third, brand new acquire is turned away by the lowered max.
    with pytest.raises(slots.SlotFull):
        slots_redis.acquire("bmo", "c", **kw)
    # A fourth acquire frees up once a holder's lease is released.
    assert slots_redis.release("bmo:a", **kw) is True
    with pytest.raises(slots.SlotFull):
        slots_redis.acquire("bmo", "c", **kw)  # still 1 holder ("b"), max 1
    assert slots_redis.release("bmo:b", **kw) is True
    slots_redis.acquire("bmo", "c", **kw)  # now under the max


def test_set_max_unreachable_redis_raises_coordinator_unreachable(closed_port):
    with pytest.raises(slots.CoordinatorUnreachable):
        slots_redis.set_max("bmo", 3, redis_host="127.0.0.1", redis_port=closed_port)


def test_unreachable_redis_falls_back_to_local_for_bmo(closed_port, tmp_path, capsys):
    root = str(tmp_path)
    lease = slots_redis.acquire(
        "bmo", "a", max_holders=1, redis_host="127.0.0.1", redis_port=closed_port, state_root=root
    )
    captured = capsys.readouterr()

    assert "redis unreachable" in captured.err
    assert "'bmo'" in captured.err
    # The fallback actually acquired the slot, via the local backend's lock file.
    assert lease.startswith("bmo:")
    assert slots.status(state_root=root)["bmo"]["holders"] == 1


def test_unreachable_redis_raises_for_a_non_bmo_slot(closed_port):
    with pytest.raises(slots.CoordinatorUnreachable):
        slots_redis.acquire("not-bmo", "a", redis_host="127.0.0.1", redis_port=closed_port)


def test_missing_password_against_a_requirepass_server_falls_back_like_unreachable(
    auth_redis_port, tmp_path, capsys, no_client_retry
):
    # redis-py's AuthenticationError is a ConnectionError subclass. A bad or
    # missing password takes the same fallback path as an unreachable server.
    # The warning names the refused login instead.
    root = str(tmp_path)
    lease = slots_redis.acquire(
        "bmo", "a", max_holders=1, redis_host="127.0.0.1", redis_port=auth_redis_port, state_root=root
    )
    assert lease.startswith("bmo:")
    assert slots.status(state_root=root)["bmo"]["holders"] == 1

    err = capsys.readouterr().err
    assert "refused the login" in err
    assert "Check the Redis password" in err
    assert "unreachable" not in err


def test_acquire_with_the_right_password_uses_redis_not_the_fallback(auth_redis_port):
    kw = {"redis_host": "127.0.0.1", "redis_port": auth_redis_port, "redis_password": "test-pass"}
    lease = slots_redis.acquire("bmo", "auth-holder", **kw)
    assert lease == "bmo:auth-holder"
    raw = redis_lib.Redis(host="127.0.0.1", port=auth_redis_port, password="test-pass", decode_responses=True)
    assert raw.zcard("lupin:v1:slot:bmo") == 1
    slots_redis.release(lease, **kw)


# A refused login is not an unreachable server. `auth_redis_port` is a real
# server with a password. The tests below connect without one.


@pytest.mark.parametrize(
    "call",
    [
        lambda kw: slots_redis.acquire("not-bmo", "a", **kw),
        lambda kw: slots_redis.release("not-bmo:a", **kw),
        lambda kw: slots_redis.set_max("not-bmo", 3, **kw),
    ],
    ids=["acquire", "release", "set_max"],
)
def test_refused_login_on_a_non_bmo_slot_raises_and_is_not_unreachable(call, auth_redis_port, no_client_retry):
    with pytest.raises(slots_redis.CoordinatorAuthFailed) as caught:
        call({"redis_host": "127.0.0.1", "redis_port": auth_redis_port})

    message = str(caught.value)
    assert caught.value.password_refused
    assert "unreachable" not in message
    assert "Check the Redis password" in message


def test_refused_login_warning_for_bmo_status_names_the_password(auth_redis_port, tmp_path, capsys, no_client_retry):
    slots_redis.status(redis_host="127.0.0.1", redis_port=auth_redis_port, state_root=str(tmp_path))

    err = capsys.readouterr().err
    assert "unreachable" not in err
    assert "Check the Redis password" in err
    assert "local backend" in err


def test_command_the_acl_denies_names_the_acl_not_unreachable(auth_redis_port):
    admin = redis_lib.Redis(host="127.0.0.1", port=auth_redis_port, password="test-pass")
    admin.execute_command("ACL", "SETUSER", "no-eval", "on", ">no-eval-pw", "~lupin:*", "+get", "+set", "+ping")
    try:
        with pytest.raises(slots_redis.CoordinatorAuthFailed) as caught:
            slots_redis.acquire(
                "not-bmo", "a", redis_username="no-eval", redis_password="no-eval-pw",
                redis_host="127.0.0.1", redis_port=auth_redis_port,
            )
    finally:
        admin.execute_command("ACL", "DELUSER", "no-eval")

    message = str(caught.value)
    assert not caught.value.password_refused
    assert "unreachable" not in message
    assert "ACL" in message
    assert "no-eval-pw" not in message


REDIS_ARGS = ["--redis-host", "127.0.0.1", "--redis-port", "{port}"]


@pytest.mark.parametrize(
    "argv, where",
    [
        (["acquire", "not-bmo", "--holder", "a", "--backend", "redis", *REDIS_ARGS], "For slot 'not-bmo'"),
        (["hold", "not-bmo", "--holder", "a", "--backend", "redis", *REDIS_ARGS, "--", "true"], "For slot 'not-bmo'"),
        (["release", "--lease", "not-bmo:a", "--backend", "redis", *REDIS_ARGS], "For lease 'not-bmo:a'"),
        (["reconcile", *REDIS_ARGS], "For the reconcile slot"),
    ],
    ids=["acquire", "hold", "release", "reconcile"],
)
def test_cli_refused_login_exits_three_and_names_the_password_setting(
    argv, where, auth_redis_port, no_client_retry, monkeypatch, capsys
):
    monkeypatch.setattr(cli.serve, "enabled_repos", lambda: [])
    code = cli.main([arg.format(port=auth_redis_port) for arg in argv])

    err = capsys.readouterr().err
    assert code == 3
    assert "cannot reach" not in err
    assert "unreachable" not in err
    assert "Set --redis-password or LUPIN_REDIS_PASSWORD." in err
    assert where in err
