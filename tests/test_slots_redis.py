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
