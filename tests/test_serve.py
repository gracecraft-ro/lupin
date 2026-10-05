import contextlib
import io
import sys

import json
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from datetime import date, timedelta
from importlib import resources
from unittest import mock

from lupin import serve


class TimerTests(unittest.TestCase):
    def test_lists_main_and_one_off_timers_not_watchdogs(self):
        rows = [
            {"unit": "delegation-loop-claude-watchdog.timer", "next": 10_000_000},
            {"unit": "delegation-loop-watchdog.timer", "next": 15_000_000},
            {"unit": "delegation-loop.timer", "next": 20_000_000},
            {"unit": "delegation-loop-once-123.timer", "next": 30_000_000},
        ]
        with mock.patch.object(serve, "run", return_value=(0, json.dumps(rows))):
            timers = serve.timers()

        self.assertEqual(
            [timer["unit"] for timer in timers],
            ["delegation-loop.timer", "delegation-loop-once-123.timer"],
        )

    def test_render_shows_repository_for_recurring_and_one_off_timers(self):
        state = {
            "sessions": [],
            "enabled": ["repo-enabled"],
            "timers": [
                {"unit": "delegation-loop.timer", "next": 1_800_000_000, "last": None},
                {
                    "unit": "delegation-loop-once-123.timer",
                    "next": 1_800_000_060,
                    "last": None,
                },
            ],
            "timer_active": True,
            "repos": [],
        }
        exec_start = (
            "{ path=/nix/store/bin/delegation-launch ; "
            "argv[]=/nix/store/bin/delegation-launch repo-one ; "
            "ignore_errors=no ; }"
        )
        with mock.patch.object(serve, "run", return_value=(0, exec_start)) as run:
            page = serve.render_dashboard(state).decode()

        self.assertIn("all enabled repos", page)
        self.assertIn("repo-one", page)
        self.assertNotIn("<td>delegation-loop.timer</td>", page)
        run.assert_called_once_with(
            [
                "systemctl",
                "show",
                "delegation-loop-once-123.timer",
                "--property=ExecStart",
                "--value",
            ]
        )

    def test_render_parses_one_off_flags_before_repo(self):
        state = {
            "sessions": [],
            "enabled": [],
            "timers": [
                {
                    "unit": "delegation-loop-once-123.timer",
                    "next": 1_800_000_000,
                    "last": None,
                }
            ],
            "timer_active": False,
            "repos": [],
        }
        exec_start = (
            '{ path=/nix/store/bin/delegation-launch ; '
            'argv[]=/nix/store/bin/delegation-launch --note "follow up" '
            "--platform claude repo-two ; ignore_errors=no ; }"
        )
        with mock.patch.object(serve, "run", return_value=(0, exec_start)):
            page = serve.render_dashboard(state).decode()

        self.assertIn("<td>repo-two</td>", page)
        self.assertNotIn("--note", page)

    def test_render_falls_back_to_one_off_unit_for_bad_execstart(self):
        state = {
            "sessions": [],
            "enabled": [],
            "timers": [
                {
                    "unit": "delegation-loop-once-123.timer",
                    "next": 1_800_000_000,
                    "last": None,
                }
            ],
            "timer_active": False,
            "repos": [],
        }
        for exec_start in ("", "not an ExecStart", "{ argv[]='unterminated ; }"):
            with self.subTest(exec_start=exec_start), mock.patch.object(
                serve, "run", return_value=(0, exec_start)
            ):
                page = serve.render_dashboard(state).decode()
                self.assertIn("<td>delegation-loop-once-123.timer</td>", page)

    def test_rendered_next_run_timestamps_include_timezone(self):
        state = {
            "sessions": [],
            "enabled": [],
            "timers": [
                {
                    "unit": "delegation-loop.timer",
                    "next": 1_800_000_000,
                    "last": None,
                }
            ],
            "timer_active": True,
            "repos": [],
        }
        with mock.patch.object(serve.time, "strftime", wraps=serve.time.strftime) as fmt:
            page = serve.render_dashboard(state).decode()

        self.assertEqual(fmt.call_count, 2)
        self.assertTrue(all(call.args[0].endswith("%Z") for call in fmt.call_args_list))
        self.assertRegex(page, r"\d{2}:\d{2}:\d{2} [A-Z]{2,5}")

    def test_dashboard_shows_once_command_only_for_enabled_loopable_repos(self):
        state = {
            "sessions": [],
            "enabled": ["repo-enabled", "repo-disabled"],
            "timers": [],
            "timer_active": False,
            "repos": [
                {"repo": "repo-enabled", "state": "enabled", "loopable": True},
                {"repo": "repo-disabled", "state": "disabled", "loopable": True},
                {"repo": "repo-no-doc", "state": "no-doc", "loopable": False},
            ],
        }

        page = serve.render_dashboard(state).decode()

        self.assertIn("loopctl once repo-enabled now", page)
        self.assertIn("data-once-repo='repo-enabled'", page)
        self.assertIn("<th>one-off command</th>", page)
        self.assertNotIn("data-once-repo='repo-disabled'", page)
        self.assertNotIn("data-once-repo='repo-no-doc'", page)
        self.assertNotIn("loopctl once repo-disabled", page)
        self.assertNotIn("loopctl once repo-no-doc", page)


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.claude_path = os.path.join(self.tempdir.name, "stats-cache.json")
        self.omp_path = os.path.join(self.tempdir.name, "stats.db")
        self.real_claude_oauth_quota = serve.claude_oauth_quota
        self.real_opencode_go_quota = serve.opencode_go_quota
        # Default to unavailable local tools; quota API adapters are mocked
        # so tests do not read credentials or contact providers.
        run_patcher = mock.patch.object(serve, "run", return_value=(127, "not found: omp"))
        run_patcher.start()
        self.addCleanup(run_patcher.stop)
        fallbacks = {
            "claude_oauth_quota": "claude",
            "opencode_go_quota": "opencode-go",
        }
        for name, provider in fallbacks.items():
            patcher = mock.patch.object(
                serve,
                name,
                return_value=[{"provider": provider, "note": "quota unavailable"}],
            )
            patcher.start()
            self.addCleanup(patcher.stop)

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

    def test_claude_totals_and_last_update_cover_seven_calendar_days(self):
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
        with mock.patch.object(serve, "CLAUDE_STATS_FILE", self.claude_path):
            page = serve.render_usage().decode()

        self.assertIn("<td>claude</td><td>130</td><td>-</td><td>not tracked</td>", page)
        self.assertIn(f"<td>{today.isoformat()}</td>", page)
        self.assertIn("last 7 days", page)
        self.assertIn(self.claude_path, page)

    def test_omp_shows_window_totals_and_whole_table_last_update(self):
        now = 1_800_000_000
        self.write_omp([
            ("openai-codex", "m", int((now - 3600) * 1000), 12, 8, 0, 0, 0.4),
            ("openai-codex", "m", int((now - 7200) * 1000), 3, 2, 0, 0, 0.1),
            ("opencode-go", "m", int((now - 9 * 86400) * 1000), 700, 800, 0, 0, 5.0),
        ])
        with (
            mock.patch.object(serve, "OMP_STATS_FILE", self.omp_path),
            mock.patch.object(serve.time, "time", return_value=now),
        ):
            page = serve.render_usage().decode()

        self.assertIn("<td>openai-codex</td><td>15</td><td>10</td><td>$0.50</td>", page)
        self.assertIn("<td>opencode-go</td><td>0</td><td>0</td><td>$0.00</td>", page)
        self.assertIn(
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime((now - 9 * 86400))),
            page,
        )
        self.assertIn(self.omp_path, page)

    def test_unavailable_sources_do_not_hide_the_other_source(self):
        now = int(time.time() * 1000)
        self.write_omp([("openai-codex", "m", now, 1, 2, 0, 0, 0.1)])
        with (
            mock.patch.object(serve, "CLAUDE_STATS_FILE", self.claude_path + ".missing"),
            mock.patch.object(serve, "OMP_STATS_FILE", self.omp_path),
        ):
            page = serve.render_usage().decode()
        self.assertIn("unavailable (FileNotFoundError)", page)
        self.assertIn("<td>openai-codex</td>", page)

        with (
            mock.patch.object(serve, "CLAUDE_STATS_FILE", self.claude_path),
            mock.patch.object(serve, "OMP_STATS_FILE", self.omp_path + ".missing"),
        ):
            page = serve.render_usage().decode()
        self.assertIn("unavailable (FileNotFoundError)", page)
        self.assertIn("<td>claude</td>", page)

        self.write_claude({
            "lastComputedDate": date.today().isoformat(),
            "dailyModelTokens": [],
        })
        with open(self.claude_path, "w", encoding="utf-8") as stats_file:
            stats_file.write("{")
        with (
            mock.patch.object(serve, "CLAUDE_STATS_FILE", self.claude_path),
            mock.patch.object(serve, "OMP_STATS_FILE", self.omp_path),
        ):
            page = serve.render_usage().decode()
        self.assertIn("unavailable (JSONDecodeError)", page)
        self.assertIn("<td>openai-codex</td>", page)
        empty_db_path = self.omp_path + ".empty"
        with closing(sqlite3.connect(empty_db_path)):
            pass
        with (
            mock.patch.object(serve, "CLAUDE_STATS_FILE", self.claude_path),
            mock.patch.object(serve, "OMP_STATS_FILE", empty_db_path),
        ):
            page = serve.render_usage().decode()
        self.assertIn("unavailable (OperationalError)", page)
        self.assertIn("<td>claude</td>", page)

    def test_time_until_reset_formats_remaining_time(self):
        self.assertEqual(serve.time_until_reset(90_060_000, now_ms=0), "1d 1h 1m")
        self.assertEqual(serve.time_until_reset(30_000, now_ms=0), "<1m")
        self.assertEqual(serve.time_until_reset(0, now_ms=0), "now")
        self.assertEqual(serve.time_until_reset(None, now_ms=0), "-")

    def test_quota_bar_marks_window_time_and_quota_progress(self):
        row = {
            "provider": "openai",
            "duration": serve.QuotaDuration.WEEKLY,
            "used_pct": 42,
            "resets_at": 302_400_000,
        }
        rendered = serve.render_quota_row(row, now_ms=0)

        self.assertEqual(serve.time_remaining_pct(row, now_ms=0), 50)
        self.assertEqual(serve.quota_elapsed_pct(row, now_ms=0), 50)
        self.assertIn("width:42.0%", rendered)
        self.assertIn("left:50.0%", rendered)
        self.assertIn("3d 12h", rendered)
        self.assertIn("42% used", rendered)


    def test_quota_gauges_precede_totals_and_omit_ollama(self):
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
        with mock.patch.object(serve, "run", return_value=(0, json.dumps(report))):
            page = serve.render_usage().decode()

        self.assertIn(
            f"data-window-duration='P30D' data-resets-at-ms='{reset_at_ms}'",
            page,
        )
        self.assertIn("<h3>opencode-go</h3>", page)
        self.assertIn(
            f"<div class=quota-reset-at>"
            f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(reset_at_ms / 1000))}"
            f"</div>",
            page,
        )
        self.assertIn("<span><strong>36%</strong> available</span>", page)
        self.assertIn("64% used", page)
        self.assertIn("quota-meter-elapsed", page)
        self.assertIn("Needs attention", page)
        self.assertIn("Most used", page)
        self.assertIn("Next reset", page)
        self.assertLess(page.index("<h2>Quota</h2>"), page.index("<h2>7-day totals</h2>"))
        quota_section = page.split("<h2>Quota</h2>", 1)[1].split("<h2>7-day totals</h2>", 1)[0]
        self.assertNotIn("ollama", quota_section.lower())
        self.assertIn(
            f"Data timestamp: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(generated_at_ms / 1000))}",
            page,
        )

    def test_quota_uses_latest_codex_snapshot_when_omp_fails(self):
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
            mock.patch.object(serve, "run", return_value=(1, "omp failed")),
            mock.patch.object(serve, "CODEX_SESSIONS_DIR", sessions),
        ):
            rows = serve.quota_usage()

        codex_rows = [row for row in rows if row["provider"] == "openai"]
        self.assertEqual(
            [
                (row["duration"], row["used_pct"])
                for row in codex_rows
            ],
            [
                (serve.QuotaDuration.FIVE_HOURS, 42),
                (serve.QuotaDuration.WEEKLY, 75),
            ],
        )
        self.assertEqual(codex_rows[0]["resets_at"], 1_790_547_474_000)

    def test_quota_maps_omp_codex_provider_to_openai(self):
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
        with mock.patch.object(serve, "run", return_value=(0, json.dumps(report))):
            rows = serve.quota_usage()

        self.assertEqual(rows[0]["duration"], serve.QuotaDuration.FIVE_HOURS)
        self.assertEqual(rows[0]["resets_at"], 1_790_547_474_348)

    def test_opencode_go_fallback_reads_usage_api(self):
        payload = {
            "usage": {
                "rolling": {"percent": 12, "resetsAt": "2030-01-01T00:00:00Z"},
                "weekly": {"percent": 34, "resetsAt": "2030-01-02T00:00:00Z"},
                "monthly": {"percent": 56, "resetsAt": "2030-01-03T00:00:00Z"},
            }
        }
        with (
            mock.patch.dict(os.environ, {"OPENCODE_API_KEY": ""}),
            mock.patch.object(serve, "OPENCODE_GO_AUTH_FILE", self.omp_path),
            mock.patch("builtins.open", mock.mock_open(read_data=json.dumps({
                "opencode-go": {"type": "api", "key": "test-key"}
            }))),
            mock.patch.object(
                serve.urllib.request,
                "urlopen",
                return_value=io.BytesIO(json.dumps(payload).encode()),
            ) as fetch,
        ):
            rows = self.real_opencode_go_quota()

        self.assertEqual(
            [row["duration"] for row in rows],
            [
                serve.QuotaDuration.FIVE_HOURS,
                serve.QuotaDuration.WEEKLY,
                serve.QuotaDuration.MONTHLY,
            ],
        )
        self.assertTrue(all(isinstance(row["resets_at"], int) for row in rows))
        self.assertEqual(
            fetch.call_args.args[0].get_header("Authorization"), "Bearer test-key"
        )

    def test_claude_oauth_fallback_reads_live_usage(self):
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
                serve.urllib.request,
                "urlopen",
                return_value=io.BytesIO(json.dumps(payload).encode()),
            ) as fetch,
        ):
            rows = self.real_claude_oauth_quota()

        self.assertEqual(
            [row["duration"] for row in rows],
            [serve.QuotaDuration.FIVE_HOURS, serve.QuotaDuration.WEEKLY],
        )
        self.assertTrue(all(isinstance(row["resets_at"], int) for row in rows))
        self.assertEqual(
            fetch.call_args.args[0].get_header("Authorization"), "Bearer test-token"
        )

    def test_quota_unavailable_on_nonzero_exit_or_bad_json(self):
        with mock.patch.object(serve, "run", return_value=(1, "boom")):
            page = serve.render_usage().decode()
        self.assertIn("<h3>omp</h3>", page)
        self.assertIn("<p class=dim>unavailable (RuntimeError)</p>", page)

        with mock.patch.object(serve, "run", return_value=(0, "not json")):
            page = serve.render_usage().decode()
        self.assertIn("<h3>omp</h3>", page)
        self.assertIn("<p class=dim>unavailable (JSONDecodeError)</p>", page)

    def test_claude_direct_quota_row_always_present(self):
        page = serve.render_usage().decode()
        self.assertIn("<h3>claude</h3>", page)
        self.assertIn("quota unavailable", page)

    def test_usage_route_and_dashboard_link(self):
        state = {"sessions": [], "enabled": [], "timers": [], "timer_active": False, "repos": []}
        self.assertIn("href='/usage'", serve.render_dashboard(state).decode())
        handler = serve.Handler.__new__(serve.Handler)
        handler.path = "/usage"
        handler.host_ok = mock.Mock(return_value=True)
        handler.reply = mock.Mock()
        with mock.patch.object(serve, "render_usage", return_value=b"usage page"):
            handler.do_GET()
        handler.reply.assert_called_once_with(b"usage page")

    def test_favicon_route_returns_svg(self):
        handler = serve.Handler.__new__(serve.Handler)
        handler.path = "/favicon.ico"
        handler.host_ok = mock.Mock(return_value=True)
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        handler.wfile = mock.Mock()

        handler.do_GET()

        handler.send_response.assert_called_once_with(200)
        self.assertIn(
            mock.call("Content-Type", "image/svg+xml"), handler.send_header.call_args_list
        )
        self.assertIn(
            mock.call("Cache-Control", "public, max-age=86400"),
            handler.send_header.call_args_list,
        )
        handler.wfile.write.assert_called_once_with(serve.FAVICON)
        head = serve.page("test", "").decode()
        self.assertIn("href='/favicon.ico' type='image/svg+xml'", head)
        self.assertIn("name=viewport", head)


class ModelTierTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.tiers_path = os.path.join(self.tempdir.name, "model-tiers.json")

    def write_tiers(self, data, raw=None):
        with open(self.tiers_path, "w", encoding="utf-8") as handle:
            handle.write(raw if raw is not None else json.dumps(data))

    def render(self):
        with mock.patch.object(serve, "MODEL_TIERS_PATH", self.tiers_path):
            return serve.render_model_tiers().decode()

    def test_full_three_tier_category_shows_every_model_and_effort_in_order(self):
        self.write_tiers({
            "_comment": "not a category",
            "coding": {
                "source": "Artificial Analysis Coding Agent Index",
                "last_verified": "2026-10-03",
                "tiers": {
                    "tier0": [{"model": "bmo:qwen", "effort": "low"}],
                    "tier1": [
                        {"model": "sonnet", "effort": "medium"},
                        {"model": "sonnet", "effort": "high"},
                    ],
                    "tier2": [{"model": "opus", "effort": "xhigh"}],
                },
                "note": "pick a tier and go",
            },
        })
        page = self.render()

        self.assertIn("<h3>coding</h3>", page)
        self.assertIn("verified 2026-10-03", page)
        self.assertIn("Artificial Analysis Coding Agent Index", page)
        self.assertIn("pick a tier and go", page)
        # The "_comment" key is prose, not a category card.
        self.assertNotIn("not a category", page)
        self.assertIn("xhigh", page)

    def test_tier_picks_keep_the_file_order_as_a_fallback_chain(self):
        rendered = serve.render_tier_picks({
            "tier1": [
                {"model": "sonnet", "effort": "medium"},
                {"model": "sonnet", "effort": "high"},
            ],
        })
        self.assertEqual(rendered.count("<span class=tier-pick>"), 2)
        self.assertLess(
            rendered.index("sonnet</span><span class=dim>medium"),
            rendered.index("sonnet</span><span class=dim>high"),
        )
        self.assertEqual(rendered.count("&rarr;"), 1)
        # A tier the row omits still gets a row, marked none.
        self.assertIn("<span class='tier-pick dim'>none</span>", rendered)

    def test_category_without_a_score_or_with_a_short_tier_list(self):
        # The real file has no numeric per-model score at all, and
        # frontend-ui/prose have no tier0 -- neither may be assumed.
        self.write_tiers({
            "frontend-ui": {
                "source": "manual (mirrors ship/SKILL.md step 8)",
                "last_verified": "2026-10-03",
                "tiers": {
                    "tier1": [{"model": "sonnet", "effort": "high"}],
                    "tier2": [{"model": "opus", "effort": "high"}],
                },
                "note": "no tier0: UI review always escalates to tier2",
            },
        })
        page = self.render()

        self.assertIn("<h3>frontend-ui</h3>", page)
        self.assertIn("manual (mirrors ship/SKILL.md step 8)", page)
        # Every tier key still gets a row; the absent one reads "none".
        for tier in serve.MODEL_TIER_ORDER:
            self.assertIn(f"<span class=tier-name>{tier}</span>", page)
        self.assertIn("<span class='tier-pick dim'>none</span>", page)
        self.assertNotIn("score", page)

    def test_file_sourced_text_is_escaped(self):
        self.write_tiers({
            "prose": {
                "source": "LMArena <b>Creative</b> Writing",
                "last_verified": "2026-10-03",
                "tiers": {"tier2": [{"model": "a<b", "effort": "x'high"}]},
                "note": 'Opus leads both, at "5.5"',
            },
        })
        page = self.render()

        self.assertIn("LMArena &lt;b&gt;Creative&lt;/b&gt; Writing", page)
        self.assertIn("a&lt;b", page)
        self.assertIn("x&#x27;high", page)
        self.assertIn("Opus leads both, at &quot;5.5&quot;", page)
        self.assertNotIn("<b>Creative</b>", page)

    def test_missing_or_malformed_file_reports_instead_of_crashing(self):
        with mock.patch.object(serve, "MODEL_TIERS_PATH", self.tiers_path + ".missing"):
            page = serve.render_model_tiers().decode()
        self.assertIn("unavailable (FileNotFoundError)", page)
        self.assertIn(self.tiers_path, page)

        self.write_tiers(None, raw="{")
        self.assertIn("unavailable (JSONDecodeError)", self.render())

        # Valid JSON that is not an object of categories.
        self.write_tiers(None, raw="[1, 2, 3]")
        self.assertIn("no categories in the file", self.render())

        self.write_tiers({"coding": {"tiers": {"tier0": [{"model": "opus"}]}}})
        page = self.render()
        self.assertIn("verified -", page)
        self.assertIn("not recorded", page)

    def test_shipped_model_tiers_file_renders_every_category(self):
        real_path = str(resources.files("lupin").joinpath("model-tiers.json"))
        with mock.patch.object(serve, "MODEL_TIERS_PATH", real_path):
            rows = serve.model_tiers()
            page = serve.render_model_tiers().decode()

        categories = {row["category"] for row in rows}
        self.assertEqual(
            categories,
            {"coding", "general", "frontend-ui", "translation", "prose", "cad-spatial"},
        )
        for category in categories:
            self.assertIn(f"<h3>{category}</h3>", page)

    def test_model_tiers_route_and_dashboard_link(self):
        state = {"sessions": [], "enabled": [], "timers": [], "timer_active": False, "repos": []}
        self.assertIn("href='/model-tiers'", serve.render_dashboard(state).decode())
        handler = serve.Handler.__new__(serve.Handler)
        handler.path = "/model-tiers"
        handler.host_ok = mock.Mock(return_value=True)
        handler.reply = mock.Mock()
        with mock.patch.object(serve, "render_model_tiers", return_value=b"tiers page"):
            handler.do_GET()
        handler.reply.assert_called_once_with(b"tiers page")


class RoadmapCliTests(unittest.TestCase):
    def setUp(self):
        self.model = {
            "nodes": [
                {
                    "number": 42,
                    "priority": "P1",
                    "size": "size-m",
                    "title": "Example issue",
                    "body": "Full issue body",
                    "comments": [{"body": "Full comment text"}],
                },
                {
                    "number": 41,
                    "priority": "P2",
                    "size": "size-s",
                    "title": "Prerequisite",
                    "body": "",
                    "comments": [],
                },
            ],
            "edges": [{"from": 41, "to": 42, "kind": "depends"}],
            "stages": [{"name": "Next batch", "numbers": [42, 41]}],
        }

    def run_cli(self, *args):
        output = io.StringIO()
        errors = io.StringIO()
        with (
            mock.patch.object(sys, "argv", ["serve", *args]),
            mock.patch.object(serve, "code_repos", return_value=[{"repo": "sample", "loopable": True}]),
            mock.patch.object(serve.roadmap, "cached_model", return_value=self.model),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            status = serve.main()
        return status, output.getvalue(), errors.getvalue()

    def test_default_shows_stage_and_issue_summary(self):
        status, output, _ = self.run_cli("--roadmap", "sample")
        self.assertEqual(status, 0)
        self.assertIn("#42 Next batch P1 size-m 1 comments deps:#41", output)
        self.assertNotIn("Full issue body", output)
        self.assertNotIn("Full comment text", output)

    def test_verbose_shows_body_and_comment(self):
        status, output, _ = self.run_cli("--roadmap", "sample", "--verbose")
        self.assertEqual(status, 0)
        self.assertIn("Full issue body", output)
        self.assertIn("Full comment text", output)

    def test_json_is_valid(self):
        status, output, _ = self.run_cli("--roadmap", "sample", "--json")
        self.assertEqual(status, 0)
        data = json.loads(output)
        self.assertEqual(data["issues"][0]["bucket"], "Next batch")
        self.assertEqual(data["issues"][0]["deps"], [41])

    def test_unknown_repo_exits_two_without_traceback(self):
        with mock.patch.object(sys, "argv", ["serve", "--roadmap", "missing"]), mock.patch.object(
            serve, "code_repos", return_value=[]
        ):
            errors = io.StringIO()
            with contextlib.redirect_stderr(errors):
                status = serve.main()
        self.assertEqual(status, 2)
        self.assertIn("unknown repository", errors.getvalue())
        self.assertNotIn("Traceback", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
