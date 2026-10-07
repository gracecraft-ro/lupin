import contextlib
import io
import socket
import sys
import threading
import urllib.request

import ipaddress
import json
import os
import tempfile
import time
import unittest
from http.server import ThreadingHTTPServer
from importlib import resources
from unittest import mock

import pytest
import redis as redis_lib

from lupin import cli, claims, machines, quest, roadmap, serve


def _kw(redis_port):
    return {"redis_host": "127.0.0.1", "redis_port": redis_port}


def _issue_json(number, state="OPEN"):
    return {"number": number, "state": state}


def _fake_locate(table):
    """Stand-in for `quest._locate_issue` -- see test_quest.py's copy of
    this same helper for the full contract."""

    def _locate(number, repos, code_dir):
        return table.get(number)

    return _locate


def _quest_handler(redis_port):
    """A `Handler` wired to the test's throwaway redis-server, with the
    same `Handler.__new__` + mocked I/O pattern `DashboardRouteTests` uses
    for GET routes -- do_POST needs `.headers`/`.rfile` too."""
    handler = serve.Handler.__new__(serve.Handler)
    handler.fleet_connection = _kw(redis_port)
    handler.host_ok = mock.Mock(return_value=True)
    handler.reply = mock.Mock()
    handler.redirect = mock.Mock()
    return handler


def _post_body(handler, path, fields: dict) -> None:
    from urllib.parse import urlencode

    body = urlencode(fields, doseq=True).encode("utf-8")
    handler.path = path
    handler.headers = {"Content-Length": str(len(body))}
    handler.rfile = io.BytesIO(body)


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


class TimeFormattingTests(unittest.TestCase):
    def test_time_until_reset_formats_remaining_time(self):
        self.assertEqual(serve.time_until_reset(90_060_000, now_ms=0), "1d 1h 1m")
        self.assertEqual(serve.time_until_reset(30_000, now_ms=0), "<1m")
        self.assertEqual(serve.time_until_reset(0, now_ms=0), "now")
        self.assertEqual(serve.time_until_reset(None, now_ms=0), "-")


class QuotaRenderingTests(unittest.TestCase):
    """`/usage` rendering only. The real quota/usage readers moved to
    `quota.py` (issue #8) and are tested in `test_quota.py` -- here,
    `serve.quota_usage`/`claude_usage`/`omp_usage` (the names `serve.py`
    imports from `quota.py`) are mocked, so these tests cover only how
    `render_usage`/`render_quota_row` turn rows into HTML.
    """

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

    def test_quota_section_groups_by_provider_and_precedes_totals(self):
        # Real-shaped row, as `quota.quota_usage()` would produce it --
        # confirmed against that module's own tests.
        reset_at_ms = 1_790_547_474_348
        rows = [{
            "provider": "opencode-go",
            "duration": serve.QuotaDuration.MONTHLY,
            "label": "Monthly limit",
            "used_pct": 64.0,
            "resets_at": reset_at_ms,
            "generated_at": "2026-01-01 00:00:00",
        }]
        with (
            mock.patch.object(serve, "quota_usage", return_value=rows),
            mock.patch.object(serve, "claude_usage", return_value=[]),
            mock.patch.object(serve, "omp_usage", return_value=[]),
        ):
            page = serve.render_usage().decode()

        self.assertIn(
            f"data-window-duration='P30D' data-resets-at-ms='{reset_at_ms}'",
            page,
        )
        self.assertIn("<h3>opencode-go</h3>", page)
        self.assertIn("<span><strong>36%</strong> available</span>", page)
        self.assertIn("64% used", page)
        self.assertIn("quota-meter-elapsed", page)
        self.assertIn("Needs attention", page)
        self.assertIn("Most used", page)
        self.assertIn("Next reset", page)
        self.assertLess(page.index("<h2>Quota</h2>"), page.index("<h2>7-day totals</h2>"))
        self.assertIn("Data timestamp: 2026-01-01 00:00:00", page)

    def test_quota_note_and_error_rows_render_as_dim_text(self):
        with (
            mock.patch.object(serve, "quota_usage", return_value=[
                {"provider": "omp", "error": "unavailable (RuntimeError)"},
            ]),
            mock.patch.object(serve, "claude_usage", return_value=[]),
            mock.patch.object(serve, "omp_usage", return_value=[]),
        ):
            page = serve.render_usage().decode()
        self.assertIn("<h3>omp</h3>", page)
        self.assertIn("<p class=dim>unavailable (RuntimeError)</p>", page)

        with (
            mock.patch.object(serve, "quota_usage", return_value=[
                {"provider": "claude", "note": "quota unavailable"},
            ]),
            mock.patch.object(serve, "claude_usage", return_value=[]),
            mock.patch.object(serve, "omp_usage", return_value=[]),
        ):
            page = serve.render_usage().decode()
        self.assertIn("<h3>claude</h3>", page)
        self.assertIn("quota unavailable", page)

    def test_seven_day_totals_table_formats_rows_and_errors(self):
        claude_rows = [{
            "provider": "claude",
            "input_tokens": 130,
            "output_tokens": None,
            "cost": None,
            "period": "last 7 days",
            "source": "/claude/stats.json",
            "last_update": "2026-01-01",
        }]
        omp_rows = [
            {
                "provider": "openai-codex",
                "input_tokens": 15,
                "output_tokens": 10,
                "cost": 0.5,
                "period": "last 7 days",
                "source": "/omp/stats.db",
                "last_update": "2026-01-01 00:00:00",
            },
            {
                "provider": "opencode-go",
                "error": "unavailable (FileNotFoundError)",
                "source": "/opencode/stats.db",
            },
        ]
        with (
            mock.patch.object(serve, "quota_usage", return_value=[]),
            mock.patch.object(serve, "claude_usage", return_value=claude_rows),
            mock.patch.object(serve, "omp_usage", return_value=omp_rows),
        ):
            page = serve.render_usage().decode()

        self.assertIn("<td>claude</td><td>130</td><td>-</td><td>not tracked</td>", page)
        self.assertIn("<td>openai-codex</td><td>15</td><td>10</td><td>$0.50</td>", page)
        self.assertIn(
            "<td>opencode-go</td><td colspan=3>unavailable (FileNotFoundError)</td>",
            page,
        )


class BindAddressTests(unittest.TestCase):
    def test_allows_loopback(self):
        self.assertTrue(serve.bind_allowed(ipaddress.ip_address("127.0.0.1")))

    def test_allows_tailnet_range(self):
        self.assertTrue(serve.bind_allowed(ipaddress.ip_address("100.64.0.11")))

    def test_rejects_lan_address(self):
        self.assertFalse(serve.bind_allowed(ipaddress.ip_address("192.168.1.5")))

    def test_rejects_any_address(self):
        self.assertFalse(serve.bind_allowed(ipaddress.ip_address("0.0.0.0")))

    def test_rejects_tailnet_range_over_ipv6(self):
        # TAILNET_RANGE is an IPv4 network; an IPv6 address never matches it
        # even if its numeric value would overlap, so this must still be
        # loopback-or-nothing for v6.
        self.assertFalse(serve.bind_allowed(ipaddress.ip_address("::1:0:0:0")))


class DashboardRouteTests(unittest.TestCase):
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


class FleetStateTests(unittest.TestCase):
    """`fleet_state` (issue #15): the dashboard's machines + claims read,
    mocked here so these run without a real Redis. The real-Redis,
    real-HTTP round trip is in `test_dashboard_shows_real_fleet_data_from_redis`
    / `test_dashboard_degrades_when_redis_unreachable` below.
    """

    def test_coordinator_unreachable_returns_empty_with_error(self):
        with mock.patch.object(
            serve.machines, "machines",
            side_effect=machines.CoordinatorUnreachable("machine registry"),
        ):
            result = serve.fleet_state({"redis_host": "127.0.0.1", "redis_port": 1})
        self.assertEqual(result["machines"], [])
        self.assertEqual(result["claims"], {})
        self.assertIn("machine registry", result["fleet_error"])

    def test_machines_and_claims_merge_when_repo_identity_resolves(self):
        machine_rows = [{"name": "jesus", "state": "online"}]
        with (
            mock.patch.object(serve.machines, "machines", return_value=machine_rows),
            mock.patch.object(serve, "enabled_repos", return_value=["widgets"]),
            mock.patch.object(serve.roadmap, "_repo_identity", return_value=("acme", "widgets", None)),
            mock.patch.object(
                serve.claims, "claims_for",
                return_value={"acme/widgets#7": {"session": "loop-widgets#1"}},
            ) as claims_for,
        ):
            result = serve.fleet_state({"redis_host": "127.0.0.1", "redis_port": 1})
        self.assertEqual(result["machines"], machine_rows)
        self.assertEqual(result["claims"], {"acme/widgets#7": {"session": "loop-widgets#1"}})
        self.assertIsNone(result["fleet_error"])
        claims_for.assert_called_once_with(
            ["acme/widgets"],
            redis_host="127.0.0.1", redis_port=1, redis_username=None, redis_password=None,
        )

    def test_repos_with_no_resolvable_owner_skip_the_claims_lookup(self):
        with (
            mock.patch.object(serve.machines, "machines", return_value=[]),
            mock.patch.object(serve, "enabled_repos", return_value=["widgets"]),
            mock.patch.object(
                serve.roadmap, "_repo_identity", return_value=(None, None, "no gh access")
            ),
            mock.patch.object(serve.claims, "claims_for") as claims_for,
        ):
            result = serve.fleet_state({})
        claims_for.assert_not_called()
        self.assertEqual(result["claims"], {})

    def test_claims_unreachable_does_not_blank_the_machines_list(self):
        machine_rows = [{"name": "jesus", "state": "online"}]
        with (
            mock.patch.object(serve.machines, "machines", return_value=machine_rows),
            mock.patch.object(serve, "enabled_repos", return_value=["widgets"]),
            mock.patch.object(serve.roadmap, "_repo_identity", return_value=("acme", "widgets", None)),
            mock.patch.object(
                serve.claims, "claims_for",
                side_effect=claims.CoordinatorUnreachable("claims_for"),
            ),
        ):
            result = serve.fleet_state({})
        self.assertEqual(result["machines"], machine_rows)
        self.assertEqual(result["claims"], {})
        self.assertIsNone(result["fleet_error"])


class FleetDashboardRenderTests(unittest.TestCase):
    """render_dashboard's new "Fleet" section. `base_state` omits
    machines/claims/fleet_error on purpose for the first test -- older
    callers (and the other `DashboardRouteTests`-style tests above) build
    state dicts without those keys, so render_dashboard must still work.
    """

    base_state = {"sessions": [], "enabled": [], "timers": [], "timer_active": True, "repos": []}

    def test_missing_fleet_keys_degrade_to_empty_sections(self):
        page = serve.render_dashboard(dict(self.base_state)).decode()
        self.assertIn("No machines registered.", page)
        self.assertIn("No claimed issues.", page)

    def test_fleet_error_shown_instead_of_machine_table(self):
        state = dict(self.base_state, fleet_error="machine registry: connection refused")
        page = serve.render_dashboard(state).decode()
        self.assertIn("Fleet registry unreachable", page)
        self.assertIn("connection refused", page)
        self.assertNotIn("No machines registered.", page)

    def test_machines_and_claims_render_as_table_rows(self):
        state = dict(
            self.base_state,
            fleet_error=None,
            machines=[
                {
                    "name": "jesus", "state": "online", "version": "1.2.3",
                    "version_mismatch": False, "heartbeat": "2026-10-06T00:00:00Z",
                },
                {
                    "name": "ralpha", "state": "offline", "version": "1.0.0",
                    "version_mismatch": True, "heartbeat": "2026-10-05T00:00:00Z",
                },
            ],
            claims={"acme/widgets#7": {"session": "loop-widgets#1", "host": "jesus"}},
        )
        page = serve.render_dashboard(state).decode()
        self.assertIn("jesus", page)
        self.assertIn("ralpha", page)
        self.assertIn("<span class='pill on'>online</span>", page)
        self.assertIn("<span class='pill off'>offline</span>", page)
        self.assertIn("mismatch", page)
        self.assertIn("acme/widgets#7", page)
        self.assertIn("loop-widgets#1", page)


class ServeArgsParsingTests(unittest.TestCase):
    """Closes the gap noted in issue #15: `cli.main()` strictly parses the
    full argv (including `serve`'s) before dispatching to `serve.main()`,
    so `lupin serve --redis-host ...` needs `_serve_args` to accept these
    flags or it fails before serve.py ever sees them.
    """

    def test_top_level_parser_accepts_redis_flags_for_serve(self):
        args = cli._build_parser().parse_args(
            [
                "serve",
                "--redis-host", "10.0.0.1",
                "--redis-port", "6380",
                "--redis-username", "u",
                "--redis-password", "p",
                "--config-path", "/tmp/fleet.json",
            ]
        )
        self.assertEqual(args.redis_host, "10.0.0.1")
        self.assertEqual(args.redis_port, 6380)


def _write_machine_record(redis_port, name, *, state="online"):
    """Write a `machine:<name>` record directly -- same minimal shape as
    `test_machines.py`'s own `_write_raw_record` helper, not shared across
    test files since it's a few lines and each file's fixtures differ.
    """
    record = {
        "version": "0.0.0+dev",
        "heartbeat": machines._now_iso(),
        "state": state,
        "slots": {},
        "providers": [],
        "quota": {},
    }
    client = redis_lib.Redis(host="127.0.0.1", port=redis_port, decode_responses=True)
    client.set(f"{machines.PREFIX}machine:{name}", json.dumps(record))


def _free_test_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextlib.contextmanager
def _live_dashboard(connection: dict):
    """Run the real `serve.Handler` on a real socket, like `serve.main()`
    does, so a test can hit it with a real HTTP request -- not the
    `Handler.__new__` + mocked-`reply` style the rest of this file uses,
    which never exercises a real socket or a real `do_GET` response body.
    Class attributes are restored afterward since `Handler` is shared
    module state.
    """
    port = _free_test_port()
    orig_connection = serve.Handler.fleet_connection
    orig_hosts = serve.Handler.allowed_hosts
    orig_peek = serve.Handler.peek_lines
    serve.Handler.fleet_connection = connection
    serve.Handler.allowed_hosts = {f"127.0.0.1:{port}"}
    serve.Handler.peek_lines = 5

    class Server(ThreadingHTTPServer):
        daemon_threads = True

    httpd = Server(("127.0.0.1", port), serve.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        serve.Handler.fleet_connection = orig_connection
        serve.Handler.allowed_hosts = orig_hosts
        serve.Handler.peek_lines = orig_peek


def test_dashboard_shows_real_fleet_data_from_redis(redis_port, flush_redis):
    """The actual regression this issue fixes: a real machine record and a
    real claim in Redis both show up on the rendered page, fetched over a
    real HTTP request against a real running server -- not a mock.
    """
    _write_machine_record(redis_port, "jesus", state="online")
    claims.claim(
        "acme/widgets#7", "loop-widgets#1", redis_host="127.0.0.1", redis_port=redis_port
    )
    connection = {
        "redis_host": "127.0.0.1", "redis_port": redis_port,
        "redis_username": None, "redis_password": None,
    }

    with (
        mock.patch.object(serve, "enabled_repos", return_value=["widgets"]),
        mock.patch.object(serve.roadmap, "_repo_identity", return_value=("acme", "widgets", None)),
        _live_dashboard(connection) as port,
    ):
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=15) as resp:
            status = resp.status
            body = resp.read().decode()

    assert status == 200
    assert "jesus" in body
    assert "<span class='pill on'>online</span>" in body
    assert "acme/widgets#7" in body
    assert "loop-widgets#1" in body


def test_dashboard_degrades_when_redis_unreachable(closed_port):
    """No Redis listening on the other end -- the page must still render
    200 with a plain "unreachable" message, not 500.
    """
    connection = {
        "redis_host": "127.0.0.1", "redis_port": closed_port,
        "redis_username": None, "redis_password": None,
    }

    with _live_dashboard(connection) as port:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=15) as resp:
            status = resp.status
            body = resp.read().decode()

    assert status == 200
    assert "Fleet registry unreachable" in body


def test_do_post_quest_start_creates_quest_writes_redis_and_redirects(
    redis_port, flush_redis, monkeypatch
):
    locate_table = {
        11: ("repo-a", "acme/repo-a", _issue_json(11)),
        12: ("repo-a", "acme/repo-a", _issue_json(12)),
    }
    monkeypatch.setattr(quest, "_locate_issue", _fake_locate(locate_table))
    monkeypatch.setattr(
        quest.roadmap, "cached_dependency_dag", lambda repos, code_dir: {"repos": {}}
    )
    monkeypatch.setattr(quest.place_mod, "place", lambda task, connection: {"pick": "jesus"})
    monkeypatch.setattr(serve, "enabled_repos", lambda: ["repo-a"])

    handler = _quest_handler(redis_port)
    _post_body(handler, "/quest/start", {"repo": "repo-a", "issue": ["11", "12"]})

    handler.do_POST()

    handler.redirect.assert_called_once()
    (location,), _kwargs = handler.redirect.call_args
    assert location.startswith("/roadmap?repo=repo-a&quest=q")
    quest_id = location.rpartition("quest=")[2]

    record = quest.read_quest(quest_id, _kw(redis_port))
    assert record["issues"] == [11, 12]
    assert record["machine"] == "jesus"
    held = claims.claims_for(["acme/repo-a"], **_kw(redis_port))
    assert held["acme/repo-a#11"]["session"] == f"quest:{quest_id}"
    assert held["acme/repo-a#12"]["session"] == f"quest:{quest_id}"


def test_do_post_quest_start_rejects_no_issues_selected(redis_port, flush_redis):
    handler = _quest_handler(redis_port)
    _post_body(handler, "/quest/start", {"repo": "repo-a"})

    handler.do_POST()

    handler.redirect.assert_not_called()
    handler.reply.assert_called_once()
    body, status = handler.reply.call_args[0]
    assert status == 400
    assert b"select at least one issue" in body


def test_do_post_quest_stop_releases_claims_deletes_record_and_redirects(
    redis_port, flush_redis, monkeypatch
):
    locate_table = {21: ("repo-a", "acme/repo-a", _issue_json(21))}
    monkeypatch.setattr(quest, "_locate_issue", _fake_locate(locate_table))
    monkeypatch.setattr(
        quest.roadmap, "cached_dependency_dag", lambda repos, code_dir: {"repos": {}}
    )
    monkeypatch.setattr(quest.place_mod, "place", lambda task, connection: {"pick": "jesus"})
    started = quest.start([21], ["repo-a"], connection=_kw(redis_port))

    handler = _quest_handler(redis_port)
    _post_body(handler, "/quest/stop", {"id": started["id"], "repo": "repo-a"})

    handler.do_POST()

    handler.redirect.assert_called_once_with("/roadmap?repo=repo-a")
    assert quest.read_quest(started["id"], _kw(redis_port)) is None
    held = claims.claims_for(["acme/repo-a"], **_kw(redis_port))
    assert "acme/repo-a#21" not in held


def test_do_post_quest_stop_unknown_id_errors_without_redirect(redis_port, flush_redis):
    handler = _quest_handler(redis_port)
    _post_body(handler, "/quest/stop", {"id": "q999", "repo": "repo-a"})

    handler.do_POST()

    handler.redirect.assert_not_called()
    handler.reply.assert_called_once()


def _raw_post(port: int, path: str, headers: dict, body: bytes) -> bytes:
    """Send a hand-built POST request over a real socket -- `urllib` would
    always compute a correct, numeric `Content-Length` and a valid utf-8
    body itself, so it can't reproduce the malformed requests below.
    Returns everything read back before the server closes the connection.
    """
    header_lines = "".join(f"{key}: {value}\r\n" for key, value in headers.items())
    request = (
        f"POST {path} HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        f"{header_lines}"
        "Connection: close\r\n\r\n"
    ).encode("ascii") + body
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(10)
        sock.connect(("127.0.0.1", port))
        sock.sendall(request)
        chunks = []
        try:
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
        except socket.timeout:
            pass
    return b"".join(chunks)


def test_do_post_non_numeric_content_length_returns_clean_400(redis_port, flush_redis):
    """A malformed `Content-Length` must not crash do_POST -- it used to
    raise an unhandled ValueError inside `int(...)`, dropping the
    connection with no HTTP response at all instead of a 400.
    """
    connection = _kw(redis_port)
    with _live_dashboard(connection) as port:
        response = _raw_post(
            port, "/quest/stop", {"Content-Length": "bogus"}, b""
        )

    assert response.startswith(b"HTTP/1.0 400") or response.startswith(b"HTTP/1.1 400")


def test_do_post_non_utf8_body_returns_clean_400(redis_port, flush_redis):
    """A body that isn't valid utf-8 must not crash do_POST -- it used to
    raise an unhandled UnicodeDecodeError inside `raw.decode("utf-8")`,
    dropping the connection with no HTTP response at all instead of a 400.
    """
    body = b"\xff\xfe\xfd"
    connection = _kw(redis_port)
    with _live_dashboard(connection) as port:
        response = _raw_post(
            port, "/quest/stop", {"Content-Length": str(len(body))}, body
        )

    assert response.startswith(b"HTTP/1.0 400") or response.startswith(b"HTTP/1.1 400")


def test_quest_state_partitions_pending_and_done(redis_port, flush_redis, monkeypatch):
    locate_table = {
        31: ("repo-a", "acme/repo-a", _issue_json(31)),
        32: ("repo-a", "acme/repo-a", _issue_json(32)),
    }
    monkeypatch.setattr(quest, "_locate_issue", _fake_locate(locate_table))
    monkeypatch.setattr(
        quest.roadmap, "cached_dependency_dag", lambda repos, code_dir: {"repos": {}}
    )
    monkeypatch.setattr(quest.place_mod, "place", lambda task, connection: {"pick": "jesus"})
    started = quest.start([31, 32], ["repo-a"], connection=_kw(redis_port))
    # Simulate #31 merging: something else (e.g. `reconcile`) releases its
    # claim the way a closed/merged issue's own claim gets released.
    claims.release_claim("acme/repo-a#31", f"quest:{started['id']}", **_kw(redis_port))

    handler = serve.Handler.__new__(serve.Handler)
    handler.fleet_connection = _kw(redis_port)

    state = handler.quest_state(started["id"])

    assert state["id"] == started["id"]
    assert state["machine"] == "jesus"
    assert state["done"] == [31]
    assert state["pending"] == [32]
    assert state["total"] == 2


def test_quest_state_returns_none_for_blank_or_missing_id(redis_port, flush_redis):
    handler = serve.Handler.__new__(serve.Handler)
    handler.fleet_connection = _kw(redis_port)
    assert handler.quest_state("") is None
    assert handler.quest_state("q999") is None


def test_render_page_has_quest_checkbox_and_start_button():
    issues = [{"number": 5, "title": "Do thing", "body": "", "labels": []}]
    model = roadmap.build_model(issues, {}, [], repo="nix")
    render = lambda title, body, css, js: body

    page = roadmap.render_page("nix", ["nix"], model, render)

    assert "<form id=quest-start" in page
    assert "action='/quest/start'" in page
    assert "Start quest" in page
    assert "form=quest-start name=issue value='5'" in page


def test_render_page_shows_progress_and_stop_button_when_quest_running():
    issues = [{"number": 5, "title": "Do thing", "body": "", "labels": []}]
    model = roadmap.build_model(issues, {}, [], repo="nix")
    render = lambda title, body, css, js: body
    quest_state = {
        "id": "q7", "machine": "jesus", "state": "running",
        "pending": [5], "done": [], "total": 1,
    }

    page = roadmap.render_page("nix", ["nix"], model, render, quest_state)

    assert "quest q7" in page
    assert "0/1 done" in page
    assert "Stop quest" in page
    assert "name=id value='q7'" in page


if __name__ == "__main__":
    unittest.main()
