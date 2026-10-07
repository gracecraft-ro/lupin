"""Tests for the shared read-only-`gh`-lookup cache (issue #35).

Uses the real `redis-server` fixtures in `conftest.py` (`redis_port`/
`flush_redis`/`closed_port`), not a mock -- same convention as
test_slots_redis.py/test_machines.py.
"""

from __future__ import annotations

import json
from unittest import mock

import redis as redis_lib

from lupin import gh_cache, machines


def _kw(redis_port):
    return {"redis_host": "127.0.0.1", "redis_port": redis_port}


def _raw_client(redis_port):
    return redis_lib.Redis(host="127.0.0.1", port=redis_port, decode_responses=True)


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


def test_non_canonical_machine_never_fetches_on_a_miss(redis_port, flush_redis):
    fetch = mock.Mock()
    with mock.patch.object(machines, "hostname", return_value="jesus"):
        data, error = gh_cache.cached_gh_json(
            "acme", "repo", "issues:open", fetch, connection=_kw(redis_port)
        )

    assert data is None
    assert "pihome" in error
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


def test_canonical_machine_fetches_and_publishes_on_a_miss(redis_port, flush_redis):
    fetch = mock.Mock(return_value=([{"number": 1}], None))
    with mock.patch.object(machines, "hostname", return_value=gh_cache.CANONICAL_GH_FETCHER):
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
    with mock.patch.object(machines, "hostname", return_value=gh_cache.CANONICAL_GH_FETCHER):
        data, error = gh_cache.cached_gh_json(
            "acme", "repo", "issues:open", fetch, connection=_kw(redis_port)
        )

    assert error == "gh: rate limited"
    assert _raw_client(redis_port).get(f"{gh_cache.PREFIX}gh-cache:acme/repo:issues:open") is None


def test_canonical_machine_still_fetches_live_when_redis_is_down(closed_port):
    fetch = mock.Mock(return_value=([{"number": 1}], None))
    with mock.patch.object(machines, "hostname", return_value=gh_cache.CANONICAL_GH_FETCHER):
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
    from lupin import slots_redis

    held = slots_redis.acquire("gh-fetch:acme/repo", holder="other-caller", **_kw(redis_port))
    try:
        _raw_client(redis_port).set(
            f"{gh_cache.PREFIX}gh-cache:acme/repo:issues:open",
            json.dumps({"data": [{"number": 1}]}),
        )
        fetch = mock.Mock()
        with mock.patch.object(machines, "hostname", return_value=gh_cache.CANONICAL_GH_FETCHER):
            data, error = gh_cache.cached_gh_json(
                "acme", "repo", "issues:open", fetch, connection=_kw(redis_port)
            )
        assert data == [{"number": 1}]
        assert error is None
        fetch.assert_not_called()
    finally:
        slots_redis.release(held, **_kw(redis_port))


def test_canonical_machine_fetches_anyway_if_lock_busy_and_cache_still_cold(
    redis_port, flush_redis
):
    """If another holder has the lock and the cache is still empty (the
    other fetch hasn't landed yet), the canonical fetcher still answers its
    own caller rather than blocking indefinitely -- a duplicate `gh` call
    is wasted work, not a correctness problem.
    """
    from lupin import slots_redis

    held = slots_redis.acquire("gh-fetch:acme/repo", holder="other-caller", **_kw(redis_port))
    try:
        fetch = mock.Mock(return_value=([{"number": 2}], None))
        with mock.patch.object(machines, "hostname", return_value=gh_cache.CANONICAL_GH_FETCHER):
            data, error = gh_cache.cached_gh_json(
                "acme", "repo", "issues:open", fetch, connection=_kw(redis_port)
            )
        assert data == [{"number": 2}]
        assert error is None
        fetch.assert_called_once()
    finally:
        slots_redis.release(held, **_kw(redis_port))


def test_different_cache_keys_for_the_same_repo_do_not_collide(redis_port, flush_redis):
    fetch_issues = mock.Mock(return_value=([{"number": 1}], None))
    fetch_deps = mock.Mock(return_value=({1: {"blockedBy": []}}, None))
    with mock.patch.object(machines, "hostname", return_value=gh_cache.CANONICAL_GH_FETCHER):
        gh_cache.cached_gh_json("acme", "repo", "issues:open", fetch_issues, connection=_kw(redis_port))
        gh_cache.cached_gh_json("acme", "repo", "dependencies", fetch_deps, connection=_kw(redis_port))

    client = _raw_client(redis_port)
    issues_raw = client.get(f"{gh_cache.PREFIX}gh-cache:acme/repo:issues:open")
    deps_raw = client.get(f"{gh_cache.PREFIX}gh-cache:acme/repo:dependencies")
    assert json.loads(issues_raw) == {"data": [{"number": 1}]}
    assert json.loads(deps_raw) == {"data": {"1": {"blockedBy": []}}}
