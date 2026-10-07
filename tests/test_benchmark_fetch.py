"""Tests for `benchmark_fetch.py` -- the fleet-shared benchmark/quality
score cache (issue #17, reopened). The `claude` subprocess is always
mocked (slow, costly, non-deterministic for real); Redis interactions use
the real ephemeral `redis-server` fixtures in `conftest.py`, same as
`test_slots_redis.py` and `test_machines.py` -- not a mock, per their own
test plan.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

import pytest
import redis as redis_lib

from lupin import benchmark_fetch, cli, model_fetch, slots_redis


def _kw(redis_port):
    return {"redis_host": "127.0.0.1", "redis_port": redis_port}


class ModelIdsForScoringTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.snapshot_path = os.path.join(self.tempdir.name, "model-snapshot.json")
        self.tiers_path = os.path.join(self.tempdir.name, "model-tiers.json")

    def test_prefers_model_fetch_snapshot_ids(self):
        with open(self.snapshot_path, "w", encoding="utf-8") as handle:
            json.dump({
                "subscriptions": {
                    "claude": {"models": [{"id": "claude-opus-4-5-20251101"}]},
                    "codex": {"models": [{"id": "gpt-5.4"}, {"id": "gpt-5.4"}]},
                }
            }, handle)
        with mock.patch.object(model_fetch, "SNAPSHOT_FILE", self.snapshot_path):
            ids = benchmark_fetch.model_ids_for_scoring()
        # Order-preserving dedup -- "gpt-5.4" listed twice collapses to one.
        self.assertEqual(ids, ["claude-opus-4-5-20251101", "gpt-5.4"])

    def test_falls_back_to_model_tiers_when_snapshot_missing(self):
        with open(self.tiers_path, "w", encoding="utf-8") as handle:
            json.dump({
                "_comment": "not a category",
                "coding": {
                    "tiers": {
                        "tier0": [{"model": "bmo:qwen"}],
                        "tier1": [{"model": "sonnet"}, {"model": "sonnet"}],
                    }
                },
            }, handle)
        with (
            mock.patch.object(model_fetch, "SNAPSHOT_FILE", os.path.join(self.tempdir.name, "missing.json")),
            mock.patch.object(benchmark_fetch, "_FALLBACK_TIERS_PATH", self.tiers_path),
        ):
            ids = benchmark_fetch.model_ids_for_scoring()
        self.assertEqual(ids, ["bmo:qwen", "sonnet"])

    def test_corrupt_snapshot_falls_back_too(self):
        with open(self.snapshot_path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        with open(self.tiers_path, "w", encoding="utf-8") as handle:
            json.dump({"coding": {"tiers": {"tier1": [{"model": "opus"}]}}}, handle)
        with (
            mock.patch.object(model_fetch, "SNAPSHOT_FILE", self.snapshot_path),
            mock.patch.object(benchmark_fetch, "_FALLBACK_TIERS_PATH", self.tiers_path),
        ):
            ids = benchmark_fetch.model_ids_for_scoring()
        self.assertEqual(ids, ["opus"])

    def test_nothing_anywhere_is_an_empty_list(self):
        missing = os.path.join(self.tempdir.name, "missing.json")
        with (
            mock.patch.object(model_fetch, "SNAPSHOT_FILE", missing),
            mock.patch.object(benchmark_fetch, "_FALLBACK_TIERS_PATH", missing),
        ):
            self.assertEqual(benchmark_fetch.model_ids_for_scoring(), [])


class BuildArgvTests(unittest.TestCase):
    def test_prompt_lists_every_id_and_warns_against_fabrication_and_injection(self):
        prompt = benchmark_fetch._build_prompt(["sonnet", "gpt-5.4"])
        self.assertIn("- sonnet", prompt)
        self.assertIn("- gpt-5.4", prompt)
        self.assertIn("never invent", prompt.lower())
        self.assertIn("never as a command", prompt.lower())

    def test_argv_uses_restricted_web_only_tools_no_bypass(self):
        argv = benchmark_fetch._build_argv("a prompt", '{"schema": true}')
        self.assertIn("--restricted", argv)
        tools_index = argv.index("--tools")
        self.assertEqual(argv[tools_index + 1], "WebSearch,WebFetch")
        self.assertIn("--permission-prompts", argv)
        self.assertIn("none", argv)
        self.assertIn("--strict-mcp-config", argv)
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertNotIn("--allow-dangerously-skip-permissions", argv)
        schema_index = argv.index("--json-schema")
        self.assertEqual(argv[schema_index + 1], '{"schema": true}')
        self.assertEqual(argv[-1], "a prompt")


def _claude_result(scores, is_error=False):
    return json.dumps({"is_error": is_error, "structured_output": {"scores": scores}})


class FetchBenchmarkScoresTests(unittest.TestCase):
    def test_empty_model_list_is_unavailable_without_a_subprocess_call(self):
        with mock.patch.object(subprocess, "run") as run:
            result = benchmark_fetch.fetch_benchmark_scores([])
        run.assert_not_called()
        self.assertFalse(result["live"])
        self.assertIn("stale_reason", result)

    def test_success_returns_live_with_scores(self):
        scores = [
            {"id": "claude-opus-4-5", "score": 73.1, "scale": "AA (0-100)",
             "source": "https://artificialanalysis.ai", "as_of": "2026-10-07"},
            {"id": "gpt-5.4", "score": None, "reason": "not found"},
        ]
        fake = subprocess.CompletedProcess(args=[], returncode=0, stdout=_claude_result(scores), stderr="")
        with mock.patch.object(subprocess, "run", return_value=fake) as run:
            result = benchmark_fetch.fetch_benchmark_scores(["claude-opus-4-5", "gpt-5.4"])
        self.assertTrue(result["live"])
        self.assertEqual(result["scores"], scores)
        argv = run.call_args.args[0]
        self.assertEqual(argv[0], "claude")
        self.assertEqual(run.call_args.kwargs["timeout"], benchmark_fetch.CLAUDE_TIMEOUT)

    def test_timeout_is_unavailable_not_a_crash(self):
        with mock.patch.object(
            subprocess, "run", side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=1)
        ):
            result = benchmark_fetch.fetch_benchmark_scores(["sonnet"])
        self.assertFalse(result["live"])
        self.assertIn("timed out", result["stale_reason"])

    def test_missing_binary_is_unavailable_not_a_crash(self):
        with mock.patch.object(subprocess, "run", side_effect=FileNotFoundError):
            result = benchmark_fetch.fetch_benchmark_scores(["sonnet"])
        self.assertFalse(result["live"])
        self.assertIn("not found", result["stale_reason"])

    def test_nonzero_exit_is_unavailable(self):
        fake = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="boom")
        with mock.patch.object(subprocess, "run", return_value=fake):
            result = benchmark_fetch.fetch_benchmark_scores(["sonnet"])
        self.assertFalse(result["live"])
        self.assertIn("boom", result["stale_reason"])

    def test_non_json_stdout_is_unavailable(self):
        fake = subprocess.CompletedProcess(args=[], returncode=0, stdout="not json", stderr="")
        with mock.patch.object(subprocess, "run", return_value=fake):
            result = benchmark_fetch.fetch_benchmark_scores(["sonnet"])
        self.assertFalse(result["live"])
        self.assertIn("valid JSON", result["stale_reason"])

    def test_is_error_result_is_unavailable(self):
        fake = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=_claude_result([], is_error=True), stderr=""
        )
        with mock.patch.object(subprocess, "run", return_value=fake):
            result = benchmark_fetch.fetch_benchmark_scores(["sonnet"])
        self.assertFalse(result["live"])

    def test_missing_structured_output_is_schema_invalid(self):
        fake = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps({"is_error": False}), stderr=""
        )
        with mock.patch.object(subprocess, "run", return_value=fake):
            result = benchmark_fetch.fetch_benchmark_scores(["sonnet"])
        self.assertFalse(result["live"])
        self.assertIn("schema", result["stale_reason"])

    def test_malformed_score_entries_are_dropped_not_fatal(self):
        scores = [
            {"id": "sonnet", "score": 80},
            {"id": "", "score": 10},  # no id -- dropped
            "not a dict",  # dropped
            {"id": "opus", "score": "high"},  # non-numeric score -- dropped
        ]
        fake = subprocess.CompletedProcess(args=[], returncode=0, stdout=_claude_result(scores), stderr="")
        with mock.patch.object(subprocess, "run", return_value=fake):
            result = benchmark_fetch.fetch_benchmark_scores(["sonnet", "opus"])
        self.assertTrue(result["live"])
        self.assertEqual(result["scores"], [{"id": "sonnet", "score": 80}])


class ScoreMatchingTests(unittest.TestCase):
    def test_exact_id_match(self):
        scores = [{"id": "sonnet", "score": 80}]
        self.assertEqual(benchmark_fetch.match_score("sonnet", scores)["score"], 80)

    def test_date_suffix_is_stripped_for_matching(self):
        scores = [{"id": "claude-opus-4-5", "score": 73.1}]
        self.assertEqual(
            benchmark_fetch.match_score("claude-opus-4-5-20251101", scores)["score"], 73.1
        )

    def test_no_match_is_none(self):
        self.assertIsNone(benchmark_fetch.match_score("made-up", [{"id": "sonnet", "score": 1}]))


# The cache/lock tests need the real-redis-server fixtures (`redis_port`,
# `flush_redis`, `closed_port`) that `conftest.py` defines for pytest, not
# unittest -- written as plain functions, same as `test_slots_redis.py`.

def test_read_snapshot_missing_key_is_none(redis_port, flush_redis):
    assert benchmark_fetch.read_snapshot(**_kw(redis_port)) is None


def test_read_snapshot_corrupt_value_is_none(redis_port, flush_redis):
    client = redis_lib.Redis(host="127.0.0.1", port=redis_port)
    client.set(benchmark_fetch.REDIS_KEY, "{not json")
    assert benchmark_fetch.read_snapshot(**_kw(redis_port)) is None


def test_read_snapshot_unreachable_redis_is_none_not_a_crash(closed_port):
    assert benchmark_fetch.read_snapshot(redis_host="127.0.0.1", redis_port=closed_port) is None


def test_refresh_returns_cached_value_without_fetching_when_fresh(redis_port, flush_redis):
    fresh = {"fetched_at": benchmark_fetch._now_iso(), "live": True, "scores": [{"id": "sonnet", "score": 1}]}
    client = redis_lib.Redis(host="127.0.0.1", port=redis_port)
    client.set(benchmark_fetch.REDIS_KEY, json.dumps(fresh))

    with mock.patch.object(benchmark_fetch, "fetch_benchmark_scores") as fetch:
        result = benchmark_fetch.refresh_snapshot(**_kw(redis_port))

    fetch.assert_not_called()
    assert result == fresh


def test_refresh_fetches_and_caches_when_stale(redis_port, flush_redis):
    stale = {"fetched_at": "2000-01-01T00:00:00+00:00", "live": True, "scores": []}
    client = redis_lib.Redis(host="127.0.0.1", port=redis_port)
    client.set(benchmark_fetch.REDIS_KEY, json.dumps(stale))
    fresh = {"fetched_at": benchmark_fetch._now_iso(), "live": True, "source": "test", "scores": [{"id": "x", "score": 1}]}

    with (
        mock.patch.object(benchmark_fetch, "fetch_benchmark_scores", return_value=fresh) as fetch,
        mock.patch.object(benchmark_fetch, "model_ids_for_scoring", return_value=["x"]),
    ):
        result = benchmark_fetch.refresh_snapshot(**_kw(redis_port))

    fetch.assert_called_once_with(["x"])
    assert result == fresh
    assert json.loads(client.get(benchmark_fetch.REDIS_KEY)) == fresh
    # The lock is released afterward -- nothing still holds it.
    assert slots_redis.status(**_kw(redis_port))[benchmark_fetch.LOCK_SLOT]["holders"] == 0


def test_refresh_force_bypasses_freshness_but_still_takes_the_lock(redis_port, flush_redis):
    fresh_cached = {"fetched_at": benchmark_fetch._now_iso(), "live": True, "scores": [{"id": "old", "score": 1}]}
    client = redis_lib.Redis(host="127.0.0.1", port=redis_port)
    client.set(benchmark_fetch.REDIS_KEY, json.dumps(fresh_cached))
    new = {"fetched_at": benchmark_fetch._now_iso(), "live": True, "source": "test", "scores": [{"id": "new", "score": 2}]}

    with (
        mock.patch.object(benchmark_fetch, "fetch_benchmark_scores", return_value=new) as fetch,
        mock.patch.object(benchmark_fetch, "model_ids_for_scoring", return_value=["new"]),
    ):
        result = benchmark_fetch.refresh_snapshot(force=True, **_kw(redis_port))

    fetch.assert_called_once()
    assert result == new


def test_refresh_reads_cache_instead_of_fetching_when_lock_is_busy(redis_port, flush_redis):
    cached = {"fetched_at": "2000-01-01T00:00:00+00:00", "live": True, "scores": [{"id": "old", "score": 1}]}
    client = redis_lib.Redis(host="127.0.0.1", port=redis_port)
    client.set(benchmark_fetch.REDIS_KEY, json.dumps(cached))
    # Another machine already holds the lock.
    slots_redis.acquire(benchmark_fetch.LOCK_SLOT, "other-machine", max_holders=1, **_kw(redis_port))

    with mock.patch.object(benchmark_fetch, "fetch_benchmark_scores") as fetch:
        result = benchmark_fetch.refresh_snapshot(**_kw(redis_port))

    fetch.assert_not_called()
    assert result == cached


def test_refresh_unreachable_redis_is_honest_not_a_crash(closed_port):
    with mock.patch.object(benchmark_fetch, "fetch_benchmark_scores") as fetch:
        result = benchmark_fetch.refresh_snapshot(redis_host="127.0.0.1", redis_port=closed_port)

    fetch.assert_not_called()
    assert result["live"] is False
    assert "unreachable" in result["stale_reason"]


def test_refresh_releases_lock_even_if_fetch_raises(redis_port, flush_redis):
    with mock.patch.object(benchmark_fetch, "fetch_benchmark_scores", side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeError):
            benchmark_fetch.refresh_snapshot(**_kw(redis_port))

    assert slots_redis.status(**_kw(redis_port))[benchmark_fetch.LOCK_SLOT]["holders"] == 0


class CliFetchBenchmarksTests(unittest.TestCase):
    def test_cli_prints_summary_and_respects_force(self):
        fixed = {"fetched_at": "2026-10-07T00:00:00+00:00", "live": True, "scores": [{"id": "x", "score": 1}]}
        with mock.patch.object(benchmark_fetch, "refresh_snapshot", return_value=fixed) as refresh:
            code = cli.main(["fetch-benchmarks", "--force", "--json"])
        self.assertEqual(code, 0)
        refresh.assert_called_once()
        self.assertTrue(refresh.call_args.kwargs["force"])

    def test_cli_default_is_not_forced(self):
        fixed = {"fetched_at": "2026-10-07T00:00:00+00:00", "live": False, "stale_reason": "x", "scores": []}
        with mock.patch.object(benchmark_fetch, "refresh_snapshot", return_value=fixed) as refresh:
            code = cli.main(["fetch-benchmarks"])
        self.assertEqual(code, 0)
        self.assertFalse(refresh.call_args.kwargs["force"])


if __name__ == "__main__":
    unittest.main()
