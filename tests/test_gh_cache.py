"""Tests for the shared read-only-`gh`-lookup cache (issue #35).

Uses the real `redis-server` fixtures in `conftest.py` (`redis_port`/
`flush_redis`/`closed_port`), not a mock -- same convention as
test_slots_redis.py/test_machines.py.
"""

from __future__ import annotations

import json
import threading
import time
from unittest import mock

import redis as redis_lib
import pytest

from lupin import gh_cache, machines

PINNED_FETCHER = "fetcher-host"


@pytest.fixture(autouse=True)
def isolated_fleet_config(monkeypatch, tmp_path):
    monkeypatch.setattr(machines, "DEFAULT_CONFIG_PATH", tmp_path / "fleet.json")


@pytest.fixture(autouse=True)
def pinned_fetcher(monkeypatch):
    monkeypatch.setenv("LUPIN_GH_CANONICAL_FETCHER", PINNED_FETCHER)


def _kw(redis_port):
    return {"redis_host": "127.0.0.1", "redis_port": redis_port}


def _raw_client(redis_port):
    return redis_lib.Redis(host="127.0.0.1", port=redis_port, decode_responses=True)


def _lock_key(cache_key, repo="acme/repo"):
    return f"{gh_cache.PREFIX}gh-lock:{repo}:{cache_key}"


def _hold_lock(redis_port, cache_key, holder):
    """Hold the fetch lock as another caller, the same way `gh_cache` does."""
    _raw_client(redis_port).set(
        _lock_key(cache_key), holder, nx=True, px=int(gh_cache.LOCK_TTL * 1000)
    )


def test_cache_hit_never_calls_fetch_fn(redis_port, flush_redis):
    key = f"{gh_cache.PREFIX}gh-cache:acme/repo:issues:open"
    _raw_client(redis_port).set(key, json.dumps({"data": [{"number": 1}]}))
    fetch = mock.Mock()

    data, error = gh_cache.cached_gh_json(
        "acme", "repo", "issues:open", fetch, connection=_kw(redis_port)
    )

    assert data == [{"number": 1}]
    assert error is None
    fetch.assert_not_called()


def test_cached_none_is_a_hit_not_a_miss(redis_port, flush_redis):
    """A fetch that legitimately returns `(None, None)` -- e.g.
    `roadmap_cli._issue_state` on a deleted issue -- must still be cacheable,
    distinguishable from "nothing cached yet".
    """
    key = f"{gh_cache.PREFIX}gh-cache:acme/repo:issue-state:9"
    _raw_client(redis_port).set(key, json.dumps({"data": None}))
    fetch = mock.Mock()

    data, error = gh_cache.cached_gh_json(
        "acme", "repo", "issue-state:9", fetch, connection=_kw(redis_port)
    )

    assert data is None
    assert error is None
    fetch.assert_not_called()


def _record(name: str, state: str = "online") -> dict:
    return {"name": name, "state": state, "version": "1", "heartbeat": "1970-01-01T00:00:00Z"}


def test_non_canonical_machine_never_fetches_on_a_miss(redis_port, flush_redis):
    fetch = mock.Mock()
    with mock.patch.object(machines, "hostname", return_value="jesus"):
        with mock.patch.object(machines, "machines", return_value=[_record(PINNED_FETCHER)]):
            data, error = gh_cache.cached_gh_json(
                "acme", "repo", "issues:open", fetch, connection=_kw(redis_port)
            )

    assert data is None
    assert PINNED_FETCHER in error
    fetch.assert_not_called()


def test_non_canonical_machine_never_fetches_when_redis_is_down(closed_port):
    fetch = mock.Mock()
    with mock.patch.object(machines, "hostname", return_value="jesus"):
        data, error = gh_cache.cached_gh_json(
            "acme", "repo", "issues:open", fetch, connection=_kw(closed_port)
        )

    assert data is None
    assert "jesus" in error
    fetch.assert_not_called()


@pytest.mark.parametrize("state", ["draining", "offline"])
def test_non_canonical_machine_fetches_when_the_pinned_fetcher_is_gone(
    redis_port, flush_redis, state
):
    """The pin has no holder: the pinned fetcher is draining or offline, so nobody is
    maintaining the shared cache. jesus fetches and publishes instead of
    showing the whole fleet an empty roadmap -- the case that left jesus's
    own dashboard blank.
    """
    fetch = mock.Mock(return_value=([{"number": 1}], None))
    with mock.patch.object(machines, "hostname", return_value="jesus"):
        with mock.patch.object(machines, "machines", return_value=[_record(PINNED_FETCHER, state)]):
            data, error = gh_cache.cached_gh_json(
                "acme", "repo", "issues:open", fetch, connection=_kw(redis_port)
            )

    assert data == [{"number": 1}]
    assert error is None
    fetch.assert_called_once()
    raw = _raw_client(redis_port).get(f"{gh_cache.PREFIX}gh-cache:acme/repo:issues:open")
    assert json.loads(raw) == {"data": [{"number": 1}]}


def test_non_canonical_machine_fetches_when_the_pinned_fetcher_never_joined(
    redis_port, flush_redis
):
    fetch = mock.Mock(return_value=([{"number": 1}], None))
    with mock.patch.object(machines, "hostname", return_value="jesus"):
        with mock.patch.object(machines, "machines", return_value=[_record("ralpha")]):
            data, error = gh_cache.cached_gh_json(
                "acme", "repo", "issues:open", fetch, connection=_kw(redis_port)
            )

    assert data == [{"number": 1}]
    assert error is None
    fetch.assert_called_once()


def test_unreadable_registry_keeps_the_conservative_answer(redis_port, flush_redis):
    """A registry that cannot be read is not evidence the pinned fetcher is
    gone, so the miss stays a miss rather than turning into a fetch we
    cannot justify."""
    fetch = mock.Mock()
    with mock.patch.object(machines, "hostname", return_value="jesus"):
        with mock.patch.object(machines, "machines", side_effect=machines.CoordinatorUnreachable("x")):
            data, error = gh_cache.cached_gh_json(
                "acme", "repo", "issues:open", fetch, connection=_kw(redis_port)
            )

    assert data is None
    assert PINNED_FETCHER in error
    fetch.assert_not_called()


def test_canonical_fetcher_check_refuses_when_the_registry_is_partly_unreadable(redis_port, flush_redis):
    """The pinned fetcher's own record is corrupt. The check must read that as
    unknown and report the fetcher live, not absent."""
    _raw_client(redis_port).set(f"{machines.PREFIX}machine:{PINNED_FETCHER}", "not json")

    assert gh_cache._canonical_fetcher_live(_kw(redis_port)) is True


def test_fallback_fetcher_serializes_on_the_shared_lock(redis_port, flush_redis):
    """Two machines that both fall back still fetch once: the second finds
    the first's published result after waiting on the lock."""
    _hold_lock(redis_port, "issues:open", "other-machine")
    _raw_client(redis_port).set(
        f"{gh_cache.PREFIX}gh-cache:acme/repo:issues:open",
        json.dumps({"data": [{"number": 1}]}),
    )
    fetch = mock.Mock()
    with mock.patch.object(machines, "hostname", return_value="jesus"):
        with mock.patch.object(machines, "machines", return_value=[_record(PINNED_FETCHER, "draining")]):
            data, error = gh_cache.cached_gh_json(
                "acme", "repo", "issues:open", fetch, connection=_kw(redis_port)
            )
    assert data == [{"number": 1}]
    assert error is None
    fetch.assert_not_called()


def test_canonical_machine_fetches_and_publishes_on_a_miss(redis_port, flush_redis):
    fetch = mock.Mock(return_value=([{"number": 1}], None))
    with mock.patch.object(machines, "hostname", return_value=PINNED_FETCHER):
        data, error = gh_cache.cached_gh_json(
            "acme", "repo", "issues:open", fetch, connection=_kw(redis_port)
        )

    assert data == [{"number": 1}]
    assert error is None
    fetch.assert_called_once()

    raw = _raw_client(redis_port).get(f"{gh_cache.PREFIX}gh-cache:acme/repo:issues:open")
    assert json.loads(raw) == {"data": [{"number": 1}]}


def test_canonical_machine_does_not_cache_a_failed_fetch(redis_port, flush_redis):
    fetch = mock.Mock(return_value=(None, "gh: rate limited"))
    with mock.patch.object(machines, "hostname", return_value=PINNED_FETCHER):
        data, error = gh_cache.cached_gh_json(
            "acme", "repo", "issues:open", fetch, connection=_kw(redis_port)
        )

    assert error == "gh: rate limited"
    assert _raw_client(redis_port).get(f"{gh_cache.PREFIX}gh-cache:acme/repo:issues:open") is None


def test_canonical_machine_still_fetches_live_when_redis_is_down(closed_port):
    fetch = mock.Mock(return_value=([{"number": 1}], None))
    with mock.patch.object(machines, "hostname", return_value=PINNED_FETCHER):
        data, error = gh_cache.cached_gh_json(
            "acme", "repo", "issues:open", fetch, connection=_kw(closed_port)
        )

    assert data == [{"number": 1}]
    assert error is None
    fetch.assert_called_once()


def test_canonical_machine_lock_contention_rereads_cache_instead_of_refetching(
    redis_port, flush_redis
):
    """Two `lupin` invocations on the canonical fetcher race for the same
    repo's lock. The second one should find the first's result already
    cached and skip its own fetch -- not run `gh` twice.
    """
    _hold_lock(redis_port, "issues:open", "other-caller")
    _raw_client(redis_port).set(
        f"{gh_cache.PREFIX}gh-cache:acme/repo:issues:open",
        json.dumps({"data": [{"number": 1}]}),
    )
    fetch = mock.Mock()
    with mock.patch.object(machines, "hostname", return_value=PINNED_FETCHER):
        data, error = gh_cache.cached_gh_json(
            "acme", "repo", "issues:open", fetch, connection=_kw(redis_port)
        )
    assert data == [{"number": 1}]
    assert error is None
    fetch.assert_not_called()


def test_canonical_machine_fetches_anyway_if_lock_busy_and_cache_still_cold(
    redis_port, flush_redis
):
    """If another holder has the lock and the cache is still empty (the
    other fetch hasn't landed yet), the canonical fetcher still answers its
    own caller rather than blocking indefinitely -- a duplicate `gh` call
    is wasted work, not a correctness problem.
    """
    _hold_lock(redis_port, "issues:open", "other-caller")
    fetch = mock.Mock(return_value=([{"number": 2}], None))
    with mock.patch.object(machines, "hostname", return_value=PINNED_FETCHER):
        data, error = gh_cache.cached_gh_json(
            "acme", "repo", "issues:open", fetch, connection=_kw(redis_port)
        )
    assert data == [{"number": 2}]
    assert error is None
    fetch.assert_called_once()
    # This caller did not hold the lock, so it must not delete it.
    assert _raw_client(redis_port).get(_lock_key("issues:open")) == "other-caller"


def test_concurrent_caller_that_waits_reuses_the_result_instead_of_refetching(
    redis_port, flush_redis
):
    """Real two-thread race against a real redis-server, not a held-lease
    stand-in: caller A acquires the lock and is mid-fetch (simulated by a
    brief sleep) when caller B starts and waits in the lock poll loop.
    A finishes, writes the cache, and releases. B must re-check the cache
    and reuse A's result, not call its own `fetch_fn`.

    The other lock-contention tests above pre-hold the lock for the whole
    test, so the lock is never free for them -- they never exercise a
    caller that waits while another fetch ends, which is the normal (not
    the exceptional) case and the one the bug was in.
    """
    fetch_b = mock.Mock()
    a_holds_lock = threading.Event()

    def fetch_a():
        # Only reached once A's `acquire()` has already succeeded, so
        # setting this tells B it is safe to start -- B's own `acquire()`
        # is then guaranteed to find the lock held, not racing to get it
        # first. `cached_gh_json` does real work (a cache read, hostname
        # and redis checks) before it ever calls `acquire()`, so a plain
        # head-start sleep before starting B isn't enough to guarantee
        # ordering -- this event is.
        a_holds_lock.set()
        time.sleep(0.4)
        return [{"number": 1}], None

    results = {}

    def call_a():
        results["a"] = gh_cache.cached_gh_json(
            "acme", "repo", "issues:open", fetch_a, connection=_kw(redis_port)
        )

    def call_b():
        a_holds_lock.wait(timeout=5)
        results["b"] = gh_cache.cached_gh_json(
            "acme", "repo", "issues:open", fetch_b, connection=_kw(redis_port)
        )

    with mock.patch.object(machines, "hostname", return_value=PINNED_FETCHER):
        thread_a = threading.Thread(target=call_a)
        thread_b = threading.Thread(target=call_b)
        thread_a.start()
        thread_b.start()
        thread_a.join(timeout=5)
        thread_b.join(timeout=5)

    assert results["a"] == ([{"number": 1}], None)
    assert results["b"] == ([{"number": 1}], None)
    fetch_b.assert_not_called()


def test_different_cache_keys_for_the_same_repo_do_not_collide(redis_port, flush_redis):
    fetch_issues = mock.Mock(return_value=([{"number": 1}], None))
    fetch_deps = mock.Mock(return_value=({1: {"blockedBy": []}}, None))
    with mock.patch.object(machines, "hostname", return_value=PINNED_FETCHER):
        gh_cache.cached_gh_json("acme", "repo", "issues:open", fetch_issues, connection=_kw(redis_port))
        gh_cache.cached_gh_json("acme", "repo", "dependencies", fetch_deps, connection=_kw(redis_port))

    client = _raw_client(redis_port)
    issues_raw = client.get(f"{gh_cache.PREFIX}gh-cache:acme/repo:issues:open")
    deps_raw = client.get(f"{gh_cache.PREFIX}gh-cache:acme/repo:dependencies")
    assert json.loads(issues_raw) == {"data": [{"number": 1}]}
    assert json.loads(deps_raw) == {"data": {"1": {"blockedBy": []}}}


def test_canonical_fetcher_reads_the_setting_on_each_call(monkeypatch):
    assert gh_cache.canonical_fetcher() == PINNED_FETCHER

    monkeypatch.setenv("LUPIN_GH_CANONICAL_FETCHER", "other-host")

    assert gh_cache.canonical_fetcher() == "other-host"


@pytest.mark.parametrize("value", [None, ""])
def test_canonical_fetcher_is_none_when_unset_or_empty(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("LUPIN_GH_CANONICAL_FETCHER")
    else:
        monkeypatch.setenv("LUPIN_GH_CANONICAL_FETCHER", value)

    assert gh_cache.canonical_fetcher() is None


def test_unset_setting_pins_no_machine_so_a_miss_fetches_here(redis_port, flush_redis, monkeypatch):
    monkeypatch.delenv("LUPIN_GH_CANONICAL_FETCHER")
    fetch = mock.Mock(return_value=([{"number": 1}], None))
    with mock.patch.object(machines, "hostname", return_value="jesus"):
        with mock.patch.object(machines, "machines", return_value=[_record(PINNED_FETCHER)]):
            data, error = gh_cache.cached_gh_json(
                "acme", "repo", "issues:open", fetch, connection=_kw(redis_port)
            )

    assert data == [{"number": 1}]
    assert error is None
    fetch.assert_called_once()


@pytest.mark.parametrize("value", [None, ""])
def test_unset_setting_three_callers_on_one_empty_key_fetch_once(
    redis_port, flush_redis, monkeypatch, value
):
    """With no machine pinned, three callers miss the same key while one
    fetch runs for 3 seconds. Only one `gh` fetch may run. The other two
    callers get an "in progress" error, not a second fetch. An empty value
    means the same as an unset one.
    """
    if value is None:
        monkeypatch.delenv("LUPIN_GH_CANONICAL_FETCHER")
    else:
        monkeypatch.setenv("LUPIN_GH_CANONICAL_FETCHER", value)
    calls = []

    def slow_fetch():
        calls.append(1)
        time.sleep(3)
        return [{"number": 1}], None

    results = []

    def call():
        results.append(
            gh_cache.cached_gh_json(
                "acme", "repo", "issues:open", slow_fetch, connection=_kw(redis_port)
            )
        )

    with mock.patch.object(machines, "hostname", return_value="jesus"):
        threads = [threading.Thread(target=call) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

    assert len(calls) == 1
    assert sorted(error is None for _data, error in results) == [False, False, True]
    losers = [error for _data, error in results if error is not None]
    assert all("in progress" in error for error in losers)


def test_unset_setting_two_callers_on_different_keys_both_fetch(
    redis_port, flush_redis, monkeypatch
):
    """With no machine pinned, one repo has a slow fetch for `issues:open`.
    A caller for `dependencies` of the same repo must still fetch. It must
    not get an "in progress" error because of the other key.
    """
    monkeypatch.delenv("LUPIN_GH_CANONICAL_FETCHER")
    calls = []

    def slow_fetch(label):
        def fetch():
            calls.append(label)
            time.sleep(3)
            return [{"label": label}], None

        return fetch

    results = {}

    def call(cache_key):
        results[cache_key] = gh_cache.cached_gh_json(
            "acme", "repo", cache_key, slow_fetch(cache_key), connection=_kw(redis_port)
        )

    with mock.patch.object(machines, "hostname", return_value="jesus"):
        threads = [
            threading.Thread(target=call, args=(cache_key,))
            for cache_key in ("issues:open", "dependencies")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

    assert sorted(calls) == ["dependencies", "issues:open"]
    assert results["issues:open"] == ([{"label": "issues:open"}], None)
    assert results["dependencies"] == ([{"label": "dependencies"}], None)


def test_many_issue_lookups_make_no_slots(redis_port, flush_redis, monkeypatch):
    """The fetch lock is not a slot. 50 lookups of different issues must
    add no `slot:*` key and no row to the slot status (the Machines page).
    """
    from lupin import slots_redis

    monkeypatch.delenv("LUPIN_GH_CANONICAL_FETCHER")
    with mock.patch.object(machines, "hostname", return_value="jesus"):
        for number in range(50):
            gh_cache.cached_gh_json(
                "acme", "repo", f"issue:{number}",
                lambda: ({"number": 1}, None), connection=_kw(redis_port),
            )

    assert list(_raw_client(redis_port).scan_iter(f"{gh_cache.PREFIX}slot:*")) == []
    assert [slot for slot in slots_redis.status(**_kw(redis_port)) if "gh-fetch" in slot] == []


def test_lock_key_has_a_ttl_while_held_and_is_gone_after(redis_port, flush_redis):
    """A holder that crashes must free the lock after LOCK_TTL. So the key
    has a TTL during the fetch. After the fetch, the key is gone."""
    client = _raw_client(redis_port)
    seen = {}

    def fetch():
        seen["value"] = client.get(_lock_key("issues:open"))
        seen["pttl"] = client.pttl(_lock_key("issues:open"))
        return [{"number": 1}], None

    with mock.patch.object(machines, "hostname", return_value=PINNED_FETCHER):
        gh_cache.cached_gh_json("acme", "repo", "issues:open", fetch, connection=_kw(redis_port))

    assert seen["value"]
    assert 0 < seen["pttl"] <= gh_cache.LOCK_TTL * 1000
    assert client.exists(_lock_key("issues:open")) == 0


def test_release_keeps_a_lock_that_another_holder_has(redis_port, flush_redis):
    _hold_lock(redis_port, "issues:open", "other-caller")
    client = gh_cache._client(_kw(redis_port))

    gh_cache._release(client, _lock_key("issues:open"), "not-the-holder")

    assert _raw_client(redis_port).get(_lock_key("issues:open")) == "other-caller"


# The command list from docs/redis-schema.md, "ACL command list". It has no
# MULTI and no WATCH.
_FLEET_ACL_COMMANDS = (
    "PING GET SET DEL EXPIRE PEXPIRE TTL ZADD ZREM ZSCORE ZCARD ZRANGE "
    "ZREMRANGEBYSCORE XADD XRANGE XREVRANGE SCAN EVAL EVALSHA SCRIPT|LOAD SCRIPT|EXISTS"
).split()


def test_lock_works_with_the_fleet_acl(auth_redis_port, monkeypatch):
    """A user with only the fleet's commands takes the lock, fetches,
    publishes, and releases. No ACL error."""
    admin = redis_lib.Redis(
        host="127.0.0.1", port=auth_redis_port, password="test-pass", decode_responses=True
    )
    admin.execute_command(
        "ACL", "SETUSER", "fleet", "reset", "on", ">fleet-pw", "~lupin:*",
        *(f"+{command.lower()}" for command in _FLEET_ACL_COMMANDS),
    )
    monkeypatch.delenv("LUPIN_GH_CANONICAL_FETCHER")
    connection = {
        "redis_host": "127.0.0.1", "redis_port": auth_redis_port,
        "redis_username": "fleet", "redis_password": "fleet-pw",
    }
    lock_key = _lock_key("issues:open", repo="acl/repo")
    seen = {}

    def fetch():
        seen["pttl"] = admin.pttl(lock_key)
        return [{"number": 1}], None

    try:
        with mock.patch.object(machines, "hostname", return_value="jesus"):
            data, error = gh_cache.cached_gh_json(
                "acl", "repo", "issues:open", fetch, connection=connection
            )
        assert (data, error) == ([{"number": 1}], None)
        assert seen["pttl"] > 0
        assert admin.exists(lock_key) == 0
        assert admin.get(f"{gh_cache.PREFIX}gh-cache:acl/repo:issues:open") is not None
    finally:
        admin.delete(lock_key, f"{gh_cache.PREFIX}gh-cache:acl/repo:issues:open")
        admin.execute_command("ACL", "DELUSER", "fleet")


def test_unset_setting_never_fetches_when_redis_is_down(closed_port, monkeypatch):
    monkeypatch.delenv("LUPIN_GH_CANONICAL_FETCHER")
    fetch = mock.Mock()
    with mock.patch.object(machines, "hostname", return_value="jesus"):
        data, error = gh_cache.cached_gh_json(
            "acme", "repo", "issues:open", fetch, connection=_kw(closed_port)
        )

    assert data is None
    assert "LUPIN_GH_CANONICAL_FETCHER is unset" in error
    fetch.assert_not_called()


def test_a_refused_login_is_reported_once(auth_redis_port, monkeypatch, capsys):
    """Issue #115: a refused login is an answer, not an empty cache.

    The read still falls back to "no cache" (the caller must keep working),
    but the user sees one line that names the setting.
    """
    monkeypatch.delenv("LUPIN_GH_CANONICAL_FETCHER")
    monkeypatch.setattr(gh_cache, "_AUTH_WARNED", False)
    monkeypatch.setattr(machines, "hostname", lambda: "worker")
    connection = {
        "redis_host": "127.0.0.1",
        "redis_port": auth_redis_port,
        "redis_password": "wrong",
    }

    for _ in range(2):
        data, error = gh_cache.cached_gh_json(
            "o", "r", "issues:open", lambda: ([], None), connection=connection
        )
        assert data is None and "unreachable" in error

    err = capsys.readouterr().err
    assert err.count("Redis refused the GitHub cache login") == 1
    assert "--redis-password" in err
