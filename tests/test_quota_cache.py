"""Tests for `quota_cache.py` -- the fleet-shared quota cache (issue #38).

Uses the real `redis-server` fixtures in `conftest.py` (`redis_port`/
`flush_redis`/`closed_port`), not a mock for Redis itself -- same
convention as `test_gh_cache.py`/`test_benchmark_fetch.py`. The local
`quota.quota_usage()` call is always mocked: real credentials are not
available in CI, and this module's own job (publish/merge/lock) is what's
under test, not `quota.py`'s readers (covered by `test_quota.py`).
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

import redis as redis_lib

from lupin import cli, quota, quota_cache, slots_redis, usage_cache


def _kw(redis_port):
    return {"redis_host": "127.0.0.1", "redis_port": redis_port}


def _raw_client(redis_port):
    return redis_lib.Redis(host="127.0.0.1", port=redis_port, decode_responses=True)


class HasRealDataTests(unittest.TestCase):
    def test_a_row_with_used_pct_is_real(self):
        self.assertTrue(quota_cache._has_real_data([{"provider": "claude", "used_pct": 10}]))

    def test_note_only_row_is_not_real(self):
        self.assertFalse(quota_cache._has_real_data([{"provider": "claude", "note": "no key"}]))

    def test_empty_or_none_is_not_real(self):
        self.assertFalse(quota_cache._has_real_data([]))
        self.assertFalse(quota_cache._has_real_data(None))


class GroupByProviderTests(unittest.TestCase):
    def test_groups_rows_keeping_order(self):
        rows = [
            {"provider": "claude", "used_pct": 1},
            {"provider": "openai", "used_pct": 2},
            {"provider": "claude", "used_pct": 3},
        ]
        groups = quota_cache.group_by_provider(rows)
        self.assertEqual(list(groups), ["claude", "openai"])
        self.assertEqual(len(groups["claude"]), 2)

    def test_rows_without_a_provider_are_dropped(self):
        self.assertEqual(quota_cache.group_by_provider([{"note": "x"}, "not a dict"]), {})


class RestoreDurationsTests(unittest.TestCase):
    def test_string_duration_round_trips_back_to_the_enum(self):
        # json.dumps of a QuotaDuration (a str subclass) writes its raw
        # value, not "QuotaDuration.FIVE_HOURS" -- json.loads then hands
        # back a plain str. Confirmed directly here, not just asserted.
        raw = json.loads(json.dumps(quota.QuotaDuration.FIVE_HOURS))
        self.assertEqual(raw, "PT5H")
        self.assertNotIsInstance(raw, quota.QuotaDuration)

        restored = quota_cache._restore_durations([{"provider": "claude", "duration": raw, "used_pct": 1}])
        self.assertIsInstance(restored[0]["duration"], quota.QuotaDuration)
        self.assertEqual(restored[0]["duration"], quota.QuotaDuration.FIVE_HOURS)

    def test_unknown_string_is_left_alone(self):
        restored = quota_cache._restore_durations([{"provider": "x", "duration": "not-a-real-one"}])
        self.assertEqual(restored[0]["duration"], "not-a-real-one")


# The cache/lock tests need the real-redis-server fixtures (`redis_port`,
# `flush_redis`, `closed_port`) `conftest.py` defines for pytest, not
# unittest -- plain functions, same as `test_benchmark_fetch.py`.

def test_read_snapshot_missing_key_is_empty(redis_port, flush_redis):
    assert quota_cache.read_snapshot(**_kw(redis_port)) == {}


def test_read_snapshot_corrupt_value_is_empty(redis_port, flush_redis):
    _raw_client(redis_port).set(quota_cache.REDIS_KEY, "{not json")
    assert quota_cache.read_snapshot(**_kw(redis_port)) == {}


def test_read_snapshot_unreachable_redis_is_empty_not_a_crash(closed_port):
    assert quota_cache.read_snapshot(redis_host="127.0.0.1", redis_port=closed_port) == {}


def test_read_snapshot_restores_duration_enums(redis_port, flush_redis):
    stored = {
        "claude": {
            "rows": [{"provider": "claude", "duration": "PT5H", "used_pct": 10, "resets_at": 1}],
            "fetched_at": "2026-01-01T00:00:00+00:00",
            "fetched_by": "jesus",
        }
    }
    _raw_client(redis_port).set(quota_cache.REDIS_KEY, json.dumps(stored))

    snapshot = quota_cache.read_snapshot(**_kw(redis_port))

    self_duration = snapshot["claude"]["rows"][0]["duration"]
    assert self_duration == quota.QuotaDuration.FIVE_HOURS
    assert isinstance(self_duration, quota.QuotaDuration)


def test_refresh_publishes_real_rows_only(redis_port, flush_redis):
    rows = [
        {"provider": "claude", "duration": quota.QuotaDuration.FIVE_HOURS, "used_pct": 10, "resets_at": 1},
        {"provider": "openai", "note": "no credentials"},
        {"provider": "opencode-go", "account": "free", "duration": quota.QuotaDuration.MONTHLY,
         "used_pct": 100, "resets_at": 2},
        {"provider": "opencode-go", "account": "plus", "duration": quota.QuotaDuration.FIVE_HOURS,
         "used_pct": 1, "resets_at": 1},
    ]
    with mock.patch.object(quota, "quota_usage", return_value=rows):
        result = quota_cache.refresh_snapshot(**_kw(redis_port))

    assert "claude" in result
    assert result["openai"]["rows"] == [rows[1]]  # note only: shown, never published
    assert result["claude"]["rows"] == [rows[0]]
    assert result["claude"]["fetched_by"]
    stored = json.loads(_raw_client(redis_port).get(quota_cache.REDIS_KEY))
    assert "claude" in stored
    # Account labels and durations come back unchanged.
    read_back = quota_cache.read_snapshot(**_kw(redis_port))["opencode-go"]["rows"]
    assert [(row["account"], row["duration"]) for row in read_back] == [
        ("free", quota.QuotaDuration.MONTHLY),
        ("plus", quota.QuotaDuration.FIVE_HOURS),
    ]
    # The lock is released afterward -- nothing still holds it.
    assert slots_redis.status(**_kw(redis_port))["quota-fetch/claude"]["holders"] == 0


# The key that `lupin` versions before issue #169 read and write.
OLD_KEY = f"{slots_redis.PREFIX}quota-snapshot"


def test_new_reader_does_not_trust_rows_from_an_old_writer(redis_port, flush_redis):
    """Issue #169, case 2. An old writer has two logins but writes no labels.
    The rows look like one login that is blocked. A new reader must not use
    them to choose an orchestrator.
    """
    later = 4_102_444_800_000  # 2100-01-01
    old_entry = {
        "openai": {
            "rows": [
                {"provider": "openai", "duration": "P30D", "used_pct": 100, "resets_at": later},
                {"provider": "openai", "duration": "PT5H", "used_pct": 1, "resets_at": later},
                {"provider": "openai", "duration": "P7D", "used_pct": 0, "resets_at": later},
            ],
            "fetched_at": quota_cache._now_iso(),
            "fetched_by": "old-machine",
        }
    }
    _raw_client(redis_port).set(OLD_KEY, json.dumps(old_entry))

    snapshot = quota_cache.read_snapshot(**_kw(redis_port))

    # The old rows are still there to show on the /usage page.
    assert snapshot["openai"]["fetched_by"] == "old-machine"
    # But they are never fresh, so a loop start does not use them.
    assert not quota_cache.is_fresh(snapshot["openai"])


def test_new_writer_does_not_write_labeled_rows_where_an_old_reader_looks(redis_port, flush_redis):
    """Issue #169, case 2, other direction. An old reader treats all rows of a
    provider as one login. It must never see labeled rows.
    """
    rows = [
        {"provider": "openai", "account": "free", "duration": quota.QuotaDuration.MONTHLY,
         "used_pct": 100, "resets_at": 2},
        {"provider": "openai", "account": "plus", "duration": quota.QuotaDuration.FIVE_HOURS,
         "used_pct": 1, "resets_at": 1},
    ]
    with mock.patch.object(quota, "quota_usage", return_value=rows):
        quota_cache.refresh_snapshot(**_kw(redis_port))

    old_view = _raw_client(redis_port).get(OLD_KEY)
    assert old_view is None or '"account"' not in old_view


def test_new_writer_does_not_copy_old_rows_into_its_key(redis_port, flush_redis):
    old_entry = {
        "claude": {
            "rows": [{"provider": "claude", "duration": "PT5H", "used_pct": 5, "resets_at": 1}],
            "fetched_at": quota_cache._now_iso(),
            "fetched_by": "old-machine",
        }
    }
    _raw_client(redis_port).set(OLD_KEY, json.dumps(old_entry))
    rows = [{"provider": "openai", "duration": quota.QuotaDuration.FIVE_HOURS, "used_pct": 20, "resets_at": 2}]

    with mock.patch.object(quota, "quota_usage", return_value=rows):
        result = quota_cache.refresh_snapshot(**_kw(redis_port))

    assert result["claude"]["fetched_by"] == "old-machine"
    assert not quota_cache.is_fresh(result["claude"])
    stored = json.loads(_raw_client(redis_port).get(quota_cache.REDIS_KEY))
    assert list(stored) == ["openai"]


def test_cli_quota_wrong_password_exits_3_and_names_the_setting(auth_redis_port, capsys):
    """Issue #169, item 5. A refused login is not hidden behind local rows."""
    rows = [{"provider": "claude", "duration": quota.QuotaDuration.FIVE_HOURS, "used_pct": 10, "resets_at": 1}]
    with (
        mock.patch.object(quota, "quota_usage", return_value=rows),
        mock.patch.object(usage_cache, "refresh_snapshot"),
    ):
        code = cli.main([
            "quota", "--redis-host", "127.0.0.1", "--redis-port", str(auth_redis_port),
            "--redis-password", "wrong-secret",
        ])

    err = capsys.readouterr().err
    assert code == 3
    assert "password" in err.lower()
    assert "wrong-secret" not in err


def test_refresh_does_not_republish_a_fresh_provider(redis_port, flush_redis):
    existing = {
        "claude": {
            "rows": [{"provider": "claude", "duration": "PT5H", "used_pct": 5, "resets_at": 1}],
            "fetched_at": quota_cache._now_iso(),
            "fetched_by": "other-machine",
        }
    }
    _raw_client(redis_port).set(quota_cache.REDIS_KEY, json.dumps(existing))
    rows = [{"provider": "claude", "duration": quota.QuotaDuration.FIVE_HOURS, "used_pct": 99, "resets_at": 2}]

    with mock.patch.object(quota, "quota_usage", return_value=rows):
        result = quota_cache.refresh_snapshot(**_kw(redis_port))

    # Still the old, fresh reading -- not overwritten with this machine's.
    assert result["claude"]["fetched_by"] == "other-machine"
    assert result["claude"]["rows"][0]["used_pct"] == 5


def test_refresh_force_republishes_even_if_fresh(redis_port, flush_redis):
    existing = {
        "claude": {
            "rows": [{"provider": "claude", "duration": "PT5H", "used_pct": 5, "resets_at": 1}],
            "fetched_at": quota_cache._now_iso(),
            "fetched_by": "other-machine",
        }
    }
    _raw_client(redis_port).set(quota_cache.REDIS_KEY, json.dumps(existing))
    rows = [{"provider": "claude", "duration": quota.QuotaDuration.FIVE_HOURS, "used_pct": 99, "resets_at": 2}]

    with mock.patch.object(quota, "quota_usage", return_value=rows):
        result = quota_cache.refresh_snapshot(force=True, **_kw(redis_port))

    assert result["claude"]["rows"][0]["used_pct"] == 99


def test_refresh_adds_a_new_provider_without_disturbing_an_existing_fresh_one(redis_port, flush_redis):
    """Two-machine-shaped: one machine already published "claude"; this
    machine has credentials only for "openai" (a different provider) and
    must be able to add its own entry without touching claude's.
    """
    existing = {
        "claude": {
            "rows": [{"provider": "claude", "duration": "PT5H", "used_pct": 5, "resets_at": 1}],
            "fetched_at": quota_cache._now_iso(),
            "fetched_by": "machine-a",
        }
    }
    _raw_client(redis_port).set(quota_cache.REDIS_KEY, json.dumps(existing))
    rows = [{"provider": "openai", "duration": quota.QuotaDuration.WEEKLY, "used_pct": 20, "resets_at": 2}]

    with mock.patch.object(quota, "quota_usage", return_value=rows):
        result = quota_cache.refresh_snapshot(**_kw(redis_port))

    assert result["claude"]["fetched_by"] == "machine-a"
    assert result["openai"]["rows"] == rows


def test_refresh_lock_busy_leaves_existing_entry_alone(redis_port, flush_redis):
    existing = {
        "claude": {
            "rows": [{"provider": "claude", "duration": "PT5H", "used_pct": 5, "resets_at": 1}],
            "fetched_at": "2000-01-01T00:00:00+00:00",  # stale, would normally be republished
            "fetched_by": "other-machine",
        }
    }
    _raw_client(redis_port).set(quota_cache.REDIS_KEY, json.dumps(existing))
    # Another process already holds this provider's publish lock.
    slots_redis.acquire("quota-fetch/claude", "someone-else", max_holders=1, **_kw(redis_port))
    rows = [{"provider": "claude", "duration": quota.QuotaDuration.FIVE_HOURS, "used_pct": 99, "resets_at": 2}]

    with mock.patch.object(quota, "quota_usage", return_value=rows):
        result = quota_cache.refresh_snapshot(**_kw(redis_port))

    assert result["claude"]["fetched_by"] == "other-machine"


def test_refresh_unreachable_redis_does_not_crash_and_returns_local_rows(closed_port):
    # Best-effort: read_snapshot's own unreachable path returns {}, and
    # the lock attempt also raises CoordinatorUnreachable -- but this
    # machine's own real reading is still surfaced to its own caller
    # rather than disappearing over a cache-layer outage (same principle
    # gh_cache.py's canonical-fetcher path uses).
    rows = [{"provider": "claude", "duration": quota.QuotaDuration.FIVE_HOURS, "used_pct": 10, "resets_at": 1}]
    with mock.patch.object(quota, "quota_usage", return_value=rows):
        result = quota_cache.refresh_snapshot(redis_host="127.0.0.1", redis_port=closed_port)
    assert result["claude"]["rows"] == rows


def test_fetcher_publishes_then_credential_less_reader_sees_it(redis_port, flush_redis):
    """The scenario the issue's acceptance criteria name directly: one
    machine with real credentials publishes; a second machine with none
    reads the fleet cache instead of getting nothing. Real redis-server,
    not mocked.
    """
    fetcher_rows = [{"provider": "claude", "duration": quota.QuotaDuration.FIVE_HOURS, "used_pct": 30, "resets_at": 123}]
    with mock.patch.object(quota, "quota_usage", return_value=fetcher_rows):
        quota_cache.refresh_snapshot(holder="fetcher-machine:1", **_kw(redis_port))

    # The "reader" machine has no credentials at all -- its own local
    # quota_usage() would return nothing real. refresh_snapshot() on this
    # machine must still surface the fetcher's claude reading.
    with mock.patch.object(quota, "quota_usage", return_value=[{"provider": "claude", "note": "no credentials"}]):
        reader_view = quota_cache.refresh_snapshot(holder="reader-machine:1", **_kw(redis_port))

    assert reader_view["claude"]["rows"][0]["used_pct"] == 30
    # The reader never had real "claude" data of its own to publish, so
    # it must not have overwritten the fetcher's entry with its own
    # (empty) reading -- "fetched_by" still names whoever actually fetched.
    assert reader_view["claude"]["fetched_by"]
    # A plain passive read (what serve.py's /usage page does) sees the same.
    passive = quota_cache.read_snapshot(**_kw(redis_port))
    assert passive["claude"]["rows"][0]["used_pct"] == 30


def test_refresh_returns_a_note_only_provider_but_never_publishes_it(redis_port, flush_redis):
    rows = [{"provider": "opencode-go", "note": "key expired"}]
    with mock.patch.object(quota, "quota_usage", return_value=rows):
        result = quota_cache.refresh_snapshot(**_kw(redis_port))

    assert result["opencode-go"]["rows"] == rows
    assert result["opencode-go"]["fetched_by"]
    assert _raw_client(redis_port).get(quota_cache.REDIS_KEY) is None


def test_refresh_publishes_a_mixed_provider_with_its_notes_and_skips_a_note_only_one(redis_port, flush_redis):
    real = {"provider": "openai", "account": "plus", "duration": quota.QuotaDuration.FIVE_HOURS,
            "used_pct": 10, "resets_at": 1}
    note = {"provider": "openai", "account": "free", "note": "free login has no quota data"}
    only_note = {"provider": "opencode-go", "note": "key expired"}
    with mock.patch.object(quota, "quota_usage", return_value=[real, note, only_note]):
        quota_cache.refresh_snapshot(**_kw(redis_port))

    stored = json.loads(_raw_client(redis_port).get(quota_cache.REDIS_KEY))
    assert [row.get("note") for row in stored["openai"]["rows"]] == [None, note["note"]]
    assert stored["openai"]["rows"][0]["used_pct"] == 10
    assert "opencode-go" not in stored


def test_refresh_keeps_the_cached_reading_over_a_local_note(redis_port, flush_redis):
    cached = {"opencode-go": {
        "rows": [{"provider": "opencode-go", "duration": "PT5H", "used_pct": 5, "resets_at": 1}],
        "fetched_at": "2026-01-01T00:00:00+00:00", "fetched_by": "other-machine",
    }}
    _raw_client(redis_port).set(quota_cache.REDIS_KEY, json.dumps(cached))
    rows = [{"provider": "opencode-go", "note": "key expired"}]
    with mock.patch.object(quota, "quota_usage", return_value=rows):
        result = quota_cache.refresh_snapshot(**_kw(redis_port))

    assert result["opencode-go"]["fetched_by"] == "other-machine"
    assert result["opencode-go"]["rows"][0]["used_pct"] == 5


class CliQuotaTests(unittest.TestCase):
    def test_cli_json_prints_merged_snapshot(self):
        fixed = {"claude": {"rows": [{"provider": "claude", "duration": quota.QuotaDuration.FIVE_HOURS,
                                       "used_pct": 10, "resets_at": 1}],
                             "fetched_at": "2026-01-01T00:00:00+00:00", "fetched_by": "jesus"}}
        with (
            mock.patch.object(quota_cache, "refresh_snapshot", return_value=fixed) as refresh,
            mock.patch.object(usage_cache, "refresh_snapshot"),
        ):
            code = cli.main(["quota", "--json"])
        self.assertEqual(code, 0)
        refresh.assert_called_once()

    def test_cli_plain_output_shows_duration_and_pct_and_reset(self):
        import io
        import contextlib

        fixed = {
            "claude": {
                "rows": [{
                    "provider": "claude",
                    "duration": quota.QuotaDuration.FIVE_HOURS,
                    "used_pct": 40,
                    "resets_at": 99999999999999,
                }],
                "fetched_at": quota_cache._now_iso(),
                "fetched_by": "jesus",
            }
        }
        with (
            mock.patch.object(quota_cache, "refresh_snapshot", return_value=fixed),
            mock.patch.object(usage_cache, "refresh_snapshot"),
        ):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = cli.main(["quota"])
        self.assertEqual(code, 0)
        output = buf.getvalue()
        self.assertIn("claude", output)
        self.assertIn("5 hours", output)
        self.assertIn("60% left", output)
        self.assertIn("via jesus", output)

    def _run_cli(self, merged, *argv):
        import io
        import contextlib

        with (
            mock.patch.object(quota_cache, "refresh_snapshot", return_value=merged),
            mock.patch.object(usage_cache, "refresh_snapshot"),
        ):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = cli.main(["quota", *argv])
        self.assertEqual(code, 0)
        return buf.getvalue()

    @staticmethod
    def _merged_with_a_note():
        return {
            "claude": {
                "rows": [{"provider": "claude", "duration": quota.QuotaDuration.FIVE_HOURS,
                          "used_pct": 40, "resets_at": 99999999999999}],
                "fetched_at": quota_cache._now_iso(), "fetched_by": "jesus",
            },
            "opencode-go": {
                "rows": [{"provider": "opencode-go", "note": "OpenCode Go API key expired"}],
                "fetched_at": quota_cache._now_iso(), "fetched_by": "ralpha",
            },
        }

    def test_cli_plain_output_shows_the_note_of_a_provider_with_no_usage_rows(self):
        output = self._run_cli(self._merged_with_a_note())
        self.assertIn("opencode-go: OpenCode Go API key expired", output.splitlines())
        # A provider with usage rows is shown as before.
        self.assertTrue(any(
            line.startswith("claude ") and "60% left" in line for line in output.splitlines()
        ))
        self.assertNotIn("no quota data cached yet", output)

    def test_cli_json_output_shows_the_note_of_a_provider_with_no_usage_rows(self):
        merged = json.loads(self._run_cli(self._merged_with_a_note(), "--json"))
        self.assertEqual(
            merged["opencode-go"]["rows"],
            [{"provider": "opencode-go", "note": "OpenCode Go API key expired"}],
        )
        self.assertEqual(merged["claude"]["rows"][0]["used_pct"], 40)

    def test_cli_provider_with_no_rows_and_no_note_keeps_the_cached_message(self):
        merged = {"openai": {"rows": [], "fetched_at": None, "fetched_by": "h"}}
        self.assertEqual(self._run_cli(merged).strip(), "openai: no quota data cached yet")

    def test_cli_no_data_is_a_clear_message_not_a_crash(self):
        import io
        import contextlib

        with (
            mock.patch.object(quota_cache, "refresh_snapshot", return_value={}),
            mock.patch.object(usage_cache, "refresh_snapshot"),
        ):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = cli.main(["quota"])
        self.assertEqual(code, 0)
        self.assertIn("no quota data", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
