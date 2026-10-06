"""Tests for `quota.py` -- the quota/usage readers moved out of `serve.py`
(issue #8). `serve.py`'s rendering of this data is tested in
`test_serve.py`'s `QuotaRenderingTests`; these tests cover the reading
layer only.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from datetime import date, timedelta
from unittest import mock

from lupin import quota


class UsageReaderFixtures(unittest.TestCase):
    """Shared setup for the two local 7-day usage readers."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.claude_path = os.path.join(self.tempdir.name, "stats-cache.json")
        self.omp_path = os.path.join(self.tempdir.name, "stats.db")

    def write_claude(self, data):
        with open(self.claude_path, "w", encoding="utf-8") as stats_file:
            json.dump(data, stats_file)

    def write_omp(self, rows):
        with closing(sqlite3.connect(self.omp_path)) as db:
            db.execute(
                "CREATE TABLE messages (provider TEXT, model TEXT, timestamp INTEGER, "
                "input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, "
                "cache_write_tokens INTEGER, cost_total REAL)"
            )
            db.executemany("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
            db.commit()


class ClaudeUsageTests(UsageReaderFixtures):
    def test_totals_cover_seven_calendar_days(self):
        # dailyModelTokens holds one flat token total per model per day --
        # confirmed against the real ~/.claude/stats-cache.json on this box,
        # not a nested input/output/cost breakdown. No daily cost exists at
        # all; that only exists as an all-time total under modelUsage.
        today = date.today()
        self.write_claude({
            "lastComputedDate": today.isoformat(),
            "dailyModelTokens": [
                {
                    "date": (today - timedelta(days=6)).isoformat(),
                    "tokensByModel": {"model-a": 100, "model-b": 30},
                },
                {
                    "date": (today - timedelta(days=7)).isoformat(),
                    "tokensByModel": {"old": 900},
                },
            ],
        })
        with mock.patch.object(quota, "CLAUDE_STATS_FILE", self.claude_path):
            rows = quota.claude_usage()

        self.assertEqual(rows, [{
            "provider": "claude",
            "input_tokens": 130,
            "output_tokens": None,
            "cost": None,
            "period": "last 7 days",
            "source": self.claude_path,
            "last_update": today.isoformat(),
        }])

    def test_missing_file_is_unavailable(self):
        with mock.patch.object(quota, "CLAUDE_STATS_FILE", self.claude_path + ".missing"):
            rows = quota.claude_usage()
        self.assertEqual(rows[0]["provider"], "claude")
        self.assertIn("unavailable (FileNotFoundError)", rows[0]["error"])

    def test_bad_json_is_unavailable(self):
        with open(self.claude_path, "w", encoding="utf-8") as stats_file:
            stats_file.write("{")
        with mock.patch.object(quota, "CLAUDE_STATS_FILE", self.claude_path):
            rows = quota.claude_usage()
        self.assertIn("unavailable (JSONDecodeError)", rows[0]["error"])


class OmpUsageTests(UsageReaderFixtures):
    def test_window_totals_and_last_update(self):
        now = 1_800_000_000
        self.write_omp([
            ("openai-codex", "m", int((now - 3600) * 1000), 12, 8, 0, 0, 0.4),
            ("openai-codex", "m", int((now - 7200) * 1000), 3, 2, 0, 0, 0.1),
            ("opencode-go", "m", int((now - 9 * 86400) * 1000), 700, 800, 0, 0, 5.0),
        ])
        with (
            mock.patch.object(quota, "OMP_STATS_FILE", self.omp_path),
            mock.patch.object(quota.time, "time", return_value=now),
        ):
            rows = quota.omp_usage()

        by_provider = {row["provider"]: row for row in rows}
        self.assertEqual(
            (by_provider["openai-codex"]["input_tokens"], by_provider["openai-codex"]["output_tokens"],
             by_provider["openai-codex"]["cost"]),
            (15, 10, 0.5),
        )
        self.assertEqual(
            (by_provider["opencode-go"]["input_tokens"], by_provider["opencode-go"]["output_tokens"],
             by_provider["opencode-go"]["cost"]),
            (0, 0, 0),
        )
        self.assertEqual(
            by_provider["opencode-go"]["last_update"],
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - 9 * 86400)),
        )

    def test_missing_file_is_unavailable(self):
        with mock.patch.object(quota, "OMP_STATS_FILE", self.omp_path + ".missing"):
            rows = quota.omp_usage()
        self.assertIn("unavailable (OperationalError)", rows[0]["error"])

    def test_empty_db_is_unavailable(self):
        with closing(sqlite3.connect(self.omp_path)):
            pass
        with mock.patch.object(quota, "OMP_STATS_FILE", self.omp_path):
            rows = quota.omp_usage()
        self.assertIn("unavailable (OperationalError)", rows[0]["error"])


class QuotaUsageTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        # Default to unavailable local tools; quota API adapters are mocked
        # so tests do not read credentials or contact providers.
        run_patcher = mock.patch.object(quota, "run", return_value=(127, "not found: omp"))
        run_patcher.start()
        self.addCleanup(run_patcher.stop)
        fallbacks = {
            "claude_oauth_quota": "claude",
            "opencode_go_quota": "opencode-go",
        }
        for name, provider in fallbacks.items():
            patcher = mock.patch.object(
                quota,
                name,
                return_value=[{"provider": provider, "note": "quota unavailable"}],
            )
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_omits_ollama_and_keeps_opencode_limits(self):
        # Real-shaped omp usage --json: one provider with limits, one with
        # only notes (no standalone quota API) -- confirmed against the
        # live CLI on this box before writing the parser.
        reset_at_ms = 1_790_547_474_348
        generated_at_ms = 1_790_529_505_926
        report = {
            "generatedAt": generated_at_ms,
            "reports": [
                {
                    "provider": "opencode-go",
                    "limits": [
                        {
                            "id": "monthly",
                            "label": "Monthly limit",
                            "window": {"id": "monthly", "resetsAt": reset_at_ms},
                            "amount": {"used": 64, "usedFraction": 0.64},
                        }
                    ],
                },
                {
                    "provider": "ollama",
                    "limits": [],
                    "notes": ["Ollama does not expose a standalone quota usage API."],
                },
            ],
        }
        with mock.patch.object(quota, "run", return_value=(0, json.dumps(report))):
            rows = quota.quota_usage()

        self.assertFalse(any(row["provider"] == "ollama" for row in rows))
        opencode_row = next(row for row in rows if row["provider"] == "opencode-go" and "used_pct" in row)
        self.assertEqual(opencode_row["duration"], quota.QuotaDuration.MONTHLY)
        self.assertEqual(opencode_row["used_pct"], 64.0)
        self.assertEqual(opencode_row["resets_at"], reset_at_ms)

    def test_uses_latest_codex_snapshot_when_omp_fails(self):
        sessions = os.path.join(self.tempdir.name, "sessions")
        session_dir = os.path.join(sessions, "2026", "10", "03")
        os.makedirs(session_dir)
        rollout = os.path.join(session_dir, "rollout-test.jsonl")
        with open(rollout, "w", encoding="utf-8") as file:
            file.write(json.dumps({
                "payload": {
                    "type": "token_count",
                    "rate_limits": {
                        "primary": {"used_percent": 42, "resets_at": 1_790_547_474},
                        "secondary": {"used_percent": 75, "resets_at": 1_791_000_000},
                    },
                }
            }) + "\n")
        newer_rollout = os.path.join(session_dir, "rollout-newer.jsonl")
        with open(newer_rollout, "w", encoding="utf-8") as file:
            file.write(json.dumps({"payload": {"type": "session_meta"}}) + "\n")
        newer_mtime = os.path.getmtime(rollout) + 1
        os.utime(newer_rollout, (newer_mtime, newer_mtime))

        with (
            mock.patch.object(quota, "run", return_value=(1, "omp failed")),
            mock.patch.object(quota, "CODEX_SESSIONS_DIR", sessions),
        ):
            rows = quota.quota_usage()

        codex_rows = [row for row in rows if row["provider"] == "openai"]
        self.assertEqual(
            [(row["duration"], row["used_pct"]) for row in codex_rows],
            [
                (quota.QuotaDuration.FIVE_HOURS, 42),
                (quota.QuotaDuration.WEEKLY, 75),
            ],
        )
        self.assertEqual(codex_rows[0]["resets_at"], 1_790_547_474_000)

    def test_maps_omp_codex_provider_to_openai(self):
        report = {
            "generatedAt": 1_790_529_505_926,
            "reports": [{
                "provider": "openai-codex",
                "limits": [{
                    "label": "5 hours",
                    "window": {"resetsAt": 1_790_547_474_348},
                    "amount": {"usedFraction": 0.42},
                }],
            }],
        }
        with mock.patch.object(quota, "run", return_value=(0, json.dumps(report))):
            rows = quota.quota_usage()

        self.assertEqual(rows[0]["duration"], quota.QuotaDuration.FIVE_HOURS)
        self.assertEqual(rows[0]["resets_at"], 1_790_547_474_348)

    def test_unavailable_on_nonzero_exit_or_bad_json(self):
        with mock.patch.object(quota, "run", return_value=(1, "boom")):
            rows = quota.quota_usage()
        omp_row = next(row for row in rows if row["provider"] == "omp")
        self.assertIn("unavailable (RuntimeError)", omp_row["error"])

        with mock.patch.object(quota, "run", return_value=(0, "not json")):
            rows = quota.quota_usage()
        omp_row = next(row for row in rows if row["provider"] == "omp")
        self.assertIn("unavailable (JSONDecodeError)", omp_row["error"])

    def test_claude_fallback_always_present(self):
        rows = quota.quota_usage()
        self.assertTrue(any(row["provider"] == "claude" for row in rows))


class OpencodeGoQuotaTests(UsageReaderFixtures):
    def test_fallback_reads_usage_api(self):
        payload = {
            "usage": {
                "rolling": {"percent": 12, "resetsAt": "2030-01-01T00:00:00Z"},
                "weekly": {"percent": 34, "resetsAt": "2030-01-02T00:00:00Z"},
                "monthly": {"percent": 56, "resetsAt": "2030-01-03T00:00:00Z"},
            }
        }
        with (
            mock.patch.dict(os.environ, {"OPENCODE_API_KEY": ""}),
            mock.patch.object(quota, "OPENCODE_GO_AUTH_FILE", self.omp_path),
            mock.patch("builtins.open", mock.mock_open(read_data=json.dumps({
                "opencode-go": {"type": "api", "key": "test-key"}
            }))),
            mock.patch.object(
                quota.urllib.request,
                "urlopen",
                return_value=io.BytesIO(json.dumps(payload).encode()),
            ) as fetch,
        ):
            rows = quota.opencode_go_quota()

        self.assertEqual(
            [row["duration"] for row in rows],
            [
                quota.QuotaDuration.FIVE_HOURS,
                quota.QuotaDuration.WEEKLY,
                quota.QuotaDuration.MONTHLY,
            ],
        )
        self.assertTrue(all(isinstance(row["resets_at"], int) for row in rows))
        self.assertEqual(
            fetch.call_args.args[0].get_header("Authorization"), "Bearer test-key"
        )


class ClaudeOauthQuotaTests(unittest.TestCase):
    def test_fallback_reads_live_usage(self):
        payload = {
            "five_hour": {"utilization": 17, "resets_at": "2030-01-01T00:00:00Z"},
            "seven_day": {"utilization": 61, "resets_at": 1_893_456_000},
        }
        with (
            mock.patch(
                "builtins.open",
                mock.mock_open(read_data=json.dumps({
                    "claudeAiOauth": {"accessToken": "test-token"}
                })),
            ),
            mock.patch.object(
                quota.urllib.request,
                "urlopen",
                return_value=io.BytesIO(json.dumps(payload).encode()),
            ) as fetch,
        ):
            rows = quota.claude_oauth_quota()

        self.assertEqual(
            [row["duration"] for row in rows],
            [quota.QuotaDuration.FIVE_HOURS, quota.QuotaDuration.WEEKLY],
        )
        self.assertTrue(all(isinstance(row["resets_at"], int) for row in rows))
        self.assertEqual(
            fetch.call_args.args[0].get_header("Authorization"), "Bearer test-token"
        )


class SnapshotTests(unittest.TestCase):
    """`snapshot()` -- the one normalized entry point `machines.py`'s
    heartbeat calls to fill in the `quota` field.
    """

    def test_picks_the_five_hour_row_per_provider(self):
        rows = [
            {"provider": "claude", "duration": quota.QuotaDuration.WEEKLY, "used_pct": 10, "resets_at": 1},
            {"provider": "claude", "duration": quota.QuotaDuration.FIVE_HOURS, "used_pct": 40, "resets_at": 2},
            {"provider": "openai", "duration": quota.QuotaDuration.FIVE_HOURS, "used_pct": 25, "resets_at": 3},
        ]
        with mock.patch.object(quota, "quota_usage", return_value=rows):
            result = quota.snapshot()

        self.assertEqual(result["claude"], {
            "pct_left": 60,
            "resets_at": 2,
            "source": quota.quota_source_label("claude"),
        })
        self.assertEqual(result["openai"], {
            "pct_left": 75,
            "resets_at": 3,
            "source": quota.quota_source_label("openai"),
        })

    def test_falls_back_to_first_row_when_no_five_hour_window(self):
        rows = [{"provider": "opencode-go", "duration": quota.QuotaDuration.MONTHLY, "used_pct": 20, "resets_at": 5}]
        with mock.patch.object(quota, "quota_usage", return_value=rows):
            result = quota.snapshot()
        self.assertEqual(result["opencode-go"]["pct_left"], 80)
        self.assertEqual(result["opencode-go"]["resets_at"], 5)

    def test_note_only_row_has_no_pct_left_but_still_has_source(self):
        rows = [{"provider": "claude", "note": "quota unavailable"}]
        with mock.patch.object(quota, "quota_usage", return_value=rows):
            result = quota.snapshot()
        self.assertEqual(result["claude"]["pct_left"], None)
        self.assertEqual(result["claude"]["resets_at"], None)
        self.assertEqual(result["claude"]["source"], quota.quota_source_label("claude"))
