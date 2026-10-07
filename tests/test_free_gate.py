"""Tests for `free_gate.gate()` (issue #37).

Uses the same real `redis-server` fixtures as `test_slots_redis.py`
(`redis_port`/`flush_redis`, from `conftest.py`) -- a fabricated holder's
lease is real Redis state, not a mock, so "compute the real remaining
wait" is actually exercised against real expiry timestamps.
"""

from __future__ import annotations

import time

import pytest

from lupin import free_gate, slots_redis

# Mirrors test_route.py's shape: one bmo-backed tier0 row, one tier1-only
# row, so a test can pick "does this gate touch bmo" by category/size
# alone.
_TIERS = {
    "coding": {
        "tiers": {
            "tier0": [{"model": "bmo:qwen3.8-flash-next", "effort": "low"}],
            "tier1": [{"model": "sonnet", "effort": "medium"}],
            "tier2": [{"model": "opus", "effort": "high"}],
        }
    }
}

_BMO_PICK = {"model": "bmo:qwen3.8-flash-next", "effort": "low"}


def test_bmo_pick_with_free_slot_acquires_immediately(redis_port, flush_redis):
    connection = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    start = time.monotonic()
    result = free_gate.gate(
        "coding", "size-xs", "me", inline=False, tiers=_TIERS, quota_rows=[], connection=connection
    )
    elapsed = time.monotonic() - start

    assert result == {"pick": _BMO_PICK, "bmo_acquired": True, "lease": "bmo:me"}
    assert elapsed < 1.0  # wait=0: no polling loop, no buffer


def test_bmo_pick_full_slot_waits_real_remaining_ttl_then_acquires(redis_port, flush_redis):
    connection = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    slots_redis.acquire("bmo", "other", ttl=0.3, max_holders=1, **connection)

    start = time.monotonic()
    result = free_gate.gate(
        "coding",
        "size-xs",
        "me",
        inline=False,
        tiers=_TIERS,
        quota_rows=[],
        deferred_buffer_s=0.3,
        connection=connection,
    )
    elapsed = time.monotonic() - start

    assert result["pick"] == _BMO_PICK
    assert result["bmo_acquired"] is True
    assert result["lease"] == "bmo:me"
    # Didn't acquire before "other"'s real ~0.3s lease could have expired,
    # and didn't need anywhere near a guessed "typical session" number.
    assert 0.25 <= elapsed < 2.0


def test_bmo_pick_full_slot_past_wait_returns_bmo_acquired_false(redis_port, flush_redis, monkeypatch):
    connection = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    # A real holder that outlives the gate's whole wait window.
    slots_redis.acquire("bmo", "other", ttl=5.0, max_holders=1, **connection)
    # Force a short wait regardless of the real (much longer) remaining TTL,
    # so this test exercises "still full once the wait elapses" without
    # actually waiting out a 5s lease.
    monkeypatch.setattr(free_gate, "_remaining_wait_s", lambda slot, conn: 0.05)

    result = free_gate.gate(
        "coding",
        "size-xs",
        "me",
        inline=False,
        tiers=_TIERS,
        quota_rows=[],
        deferred_buffer_s=0.05,
        connection=connection,
    )

    assert result == {"pick": _BMO_PICK, "bmo_acquired": False, "lease": None}


def test_non_bmo_pick_passes_through_with_no_redis_call(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("non-bmo pick must never touch the bmo slot")

    monkeypatch.setattr(slots_redis, "acquire", _boom)
    monkeypatch.setattr(slots_redis, "_client", _boom)

    result = free_gate.gate("coding", "size-m", "me", inline=True, tiers=_TIERS, quota_rows=[])

    assert result == {
        "pick": {"model": "sonnet", "effort": "medium"},
        "bmo_acquired": False,
        "lease": None,
    }


def test_inline_caller_gives_up_sooner_than_a_deferred_caller(redis_port, flush_redis):
    connection = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    slots_redis.acquire("bmo", "other", ttl=1.2, max_holders=1, **connection)

    # Inline: a short, fixed technical cap -- well under the holder's real
    # remaining TTL, so it gives up without the slot ever freeing.
    inline_start = time.monotonic()
    inline_result = free_gate.gate(
        "coding",
        "size-xs",
        "me",
        inline=True,
        inline_wait_s=0.2,
        tiers=_TIERS,
        quota_rows=[],
        connection=connection,
    )
    inline_elapsed = time.monotonic() - inline_start

    assert inline_result["bmo_acquired"] is False
    assert inline_elapsed < 0.6

    # Deferred: reads the real remaining TTL (now shorter, time has passed)
    # and waits that long instead of a fixed ceiling, so it succeeds once
    # "other"'s real lease actually expires.
    deferred_start = time.monotonic()
    deferred_result = free_gate.gate(
        "coding",
        "size-xs",
        "me",
        inline=False,
        deferred_buffer_s=0.3,
        tiers=_TIERS,
        quota_rows=[],
        connection=connection,
    )
    deferred_elapsed = time.monotonic() - deferred_start

    assert deferred_result["bmo_acquired"] is True
    assert deferred_result["lease"] == "bmo:me"
    assert deferred_elapsed > inline_elapsed
