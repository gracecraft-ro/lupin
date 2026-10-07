import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from datetime import datetime, timedelta, timezone

from lupin import roadmap


class RoadmapTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        cache_file = str(Path(directory.name) / "cache.json")
        cache_patch = mock.patch.object(roadmap, "CACHE_FILE", cache_file)
        cache_patch.start()
        self.addCleanup(cache_patch.stop)
        roadmap._GITHUB_CACHE.clear()
        roadmap._LEDGER_CACHE.clear()

    def test_normalizes_priority_and_size_labels(self):
        issues = [
            {
                "number": 8,
                "title": "Label aliases",
                "body": "",
                "labels": [{"name": "priority/p1"}, {"name": "size/M"}],
                "url": "https://github.com/gracecraft/nix/issues/8",
            }
        ]

        node = roadmap.build_model(issues, {}, [], repo="nix")["nodes"][0]

        self.assertEqual(node["priority"], "P1")
        self.assertEqual(node["size"], "size-m")

    def test_parent_and_dependency_links_remain_distinct(self):
        issues = [
            {"number": 1, "title": "Parent", "body": "", "labels": ["epic"]},
            {
                "number": 2,
                "title": "Child",
                "body": "Part of #1. Depends on #3.",
                "labels": [],
            },
            {"number": 3, "title": "Prerequisite", "body": "", "labels": []},
        ]

        edges = roadmap.build_model(issues, {}, [], repo="nix")["edges"]

        self.assertIn({"from": 1, "to": 2, "kind": "parent"}, edges)
        self.assertIn({"from": 3, "to": 2, "kind": "depends"}, edges)

    def test_negated_dependency_phrase_creates_no_edge(self):
        issues = [
            {"number": 77, "title": "A", "body": "", "labels": []},
            {
                "number": 79,
                "title": "B",
                "body": "Step 8 is not blocked by #77. Step 9 is blocked by #77.",
                "labels": [],
            },
        ]

        edges = roadmap.build_model(issues, {}, [], repo="nix")["edges"]

        # The negated mention must not create an edge; the real one still does.
        matches = [e for e in edges if e == {"from": 77, "to": 79, "kind": "depends"}]
        self.assertEqual(len(matches), 1)


    def test_quoted_dependency_phrase_creates_no_edge(self):
        issues = [
            {"number": 77, "title": "Original", "body": "", "labels": []},
            {"number": 78, "title": "Blocked issue", "body": "", "labels": []},
            {"number": 79, "title": "Parent issue", "body": "", "labels": []},
            {
                "number": 158,
                "title": "Quoted report",
                "body": 'The old comment said, "priority P1 since it blocks #78 and part of #79".',
                "labels": [],
            },
        ]

        edges = roadmap.build_model(issues, {}, [], repo="nix")["edges"]

        self.assertFalse(
            any(edge["from"] == 158 or edge["to"] == 158 for edge in edges)
        )
    def test_malformed_ledger_does_not_hide_github_issues(self):
        issue = {
            "number": 4,
            "title": "Visible issue",
            "body": "",
            "labels": [],
            "url": "https://github.com/gracecraft/bodysmith/issues/4",
        }
        responses = [
            ({"owner": {"login": "gracecraft"}, "name": "bodysmith"}, None),
            ([issue], None),
        ]
        with tempfile.TemporaryDirectory() as directory:
            ledger_path = Path(directory) / ".loop" / "loop-state.json"
            ledger_path.parent.mkdir()
            ledger_path.write_text("{bad json", encoding="utf-8")
            with (
                mock.patch.object(roadmap, "_run_json", side_effect=responses),
                mock.patch.object(roadmap, "_read_comments", return_value=({}, None)),
            ):
                model = roadmap.load_model("bodysmith", directory)

        self.assertEqual([node["number"] for node in model["nodes"]], [4])
        self.assertTrue(
            any(
                "Could not parse .loop/loop-state.json" in warning
                for warning in model["warnings"]
            )
        )

    def test_dispatch_ledger_event_marks_issue_active(self):
        issue = {"number": 12, "title": "Running", "body": "", "labels": []}

        node = roadmap.build_model(
            [issue], {}, [{"issue": 12, "event": "dispatch"}], repo="nix"
        )["nodes"][0]

        self.assertTrue(node["dispatched"])
        self.assertEqual(node["loop"], "dispatch")

    def test_running_ledger_status_marks_issue_in_flight(self):
        issues = [
            {"number": 12, "title": "Running", "body": "", "labels": []},
            {"number": 13, "title": "Triaged", "body": "", "labels": []},
        ]

        model = roadmap.build_model(
            issues,
            {},
            [
                {"issue": 12, "status": "running"},
                {"issue": 13, "status": "triaged"},
            ],
            repo="nix",
        )

        stages = {stage["name"]: stage["numbers"] for stage in model["stages"]}
        self.assertIn(12, stages["Marked in flight"])
        self.assertNotIn(13, stages["Marked in flight"])

    def test_closed_github_fetch_and_cache_are_separate_from_open(self):
        roadmap._GITHUB_CACHE.clear()
        open_issue = {"number": 1, "title": "Active", "body": "", "labels": []}
        closed_issue = {
            "number": 2,
            "title": "Completed",
            "body": "",
            "labels": [],
            "url": "https://github.com/acme/repo/issues/2",
        }
        with mock.patch.object(
            roadmap,
            "load_github",
            side_effect=[([open_issue], {}, []), ([closed_issue], {}, [])],
        ) as load:
            active = roadmap.cached_github("repo", "/repo")
            completed = roadmap.cached_github("repo", "/repo", "closed")
            self.assertIs(roadmap.cached_github("repo", "/repo"), active)
            self.assertIs(roadmap.cached_github("repo", "/repo", "closed"), completed)

        self.assertEqual([issue["title"] for issue in active[0]], ["Active"])
        self.assertEqual([issue["title"] for issue in completed[0]], ["Completed"])
        self.assertEqual(load.call_args_list[0].args, ("/repo", "open"))
        self.assertEqual(load.call_args_list[1].args, ("/repo", "closed"))

    def test_closed_fetch_uses_closed_state_for_cli_and_comments(self):
        issue = {"number": 9, "title": "Done", "body": "", "labels": []}
        response = [
            ({"owner": {"login": "acme"}, "name": "repo"}, None),
            ([issue], None),
        ]
        with (
            mock.patch.object(roadmap, "_run_json", side_effect=response) as run_json,
            mock.patch.object(roadmap, "_read_comments", return_value=({}, None)) as comments,
        ):
            roadmap.load_github("/repo", "closed")

        self.assertEqual(run_json.call_args_list[1].args[0][3:5], ["--state", "closed"])
        issue_args = run_json.call_args_list[1].args[0]
        self.assertEqual(issue_args[issue_args.index("--limit") + 1], "100")
        search = issue_args[issue_args.index("--search") + 1]
        self.assertTrue(search.startswith("closed:>="))
        comments.assert_called_once_with("/repo", "acme", "repo", "closed", 100)

    def test_closed_graphql_query_filters_closed_issues(self):
        response = {
            "data": {"repository": {"issues": {
                "nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}
            }}}
        }
        with mock.patch.object(roadmap, "_run_json", return_value=(response, None)) as run_json:
            roadmap._read_comments("/repo", "acme", "repo", "closed")

        query = run_json.call_args.args[0][4]
        self.assertIn("states:CLOSED", query)
        self.assertNotIn("states:OPEN", query)

    def test_completed_page_is_separate_from_active_board_and_has_round_trip_links(self):
        active_issue = {
            "number": 1, "title": "Active issue", "body": "", "labels": [],
            "url": "https://github.com/acme/repo/issues/1",
        }
        closed_issue = {
            "number": 2, "title": "Finished issue", "body": "", "labels": [],
            "url": "https://github.com/acme/repo/issues/2",
        }
        model = roadmap.build_model([active_issue], {}, [], repo="repo")
        render = lambda title, body, css, js: body

        active_page = roadmap.render_page("repo", ["repo"], model, render)
        closed_page = roadmap.render_completed_page(
            "repo", ["repo"], {"repo": ([closed_issue], {}, [])}, render
        )
        combined_closed = roadmap.render_completed_page(
            None, ["repo"], {"repo": ([closed_issue], {}, [])}, render
        )

        self.assertIn("#1 Active issue", active_page)
        self.assertNotIn("#2 Finished issue", active_page)
        self.assertIn("state=closed", active_page)
        self.assertIn("#2 Finished issue", closed_page)
        self.assertNotIn("#1 Active issue", closed_page)
        self.assertIn("href='/roadmap?repo=repo'", closed_page)
        self.assertIn("state=closed", active_page)
        self.assertIn("Back to active queue", combined_closed)
        self.assertIn("#2 Finished issue", combined_closed)

    def test_completed_page_renders_raw_github_label_dicts(self):
        # gh issue list --json labels returns [{"name": ..., "color": ...}],
        # not plain strings -- render_completed_page must normalize these
        # the same way build_model does for the active queue.
        closed_issue = {
            "number": 3, "title": "Labeled issue", "body": "", "url": "",
            "labels": [{"name": "priority/P1", "color": "ff0000"}],
        }
        render = lambda title, body, css, js: body
        page = roadmap.render_completed_page(
            "repo", ["repo"], {"repo": ([closed_issue], {}, [])}, render
        )
        self.assertIn("priority/P1", page)

    def test_github_and_ledger_caches_refresh_independently(self):
        roadmap._GITHUB_CACHE.clear()
        roadmap._LEDGER_CACHE.clear()
        clock = [0.0]
        issue = {"number": 1, "title": "Issue", "body": "", "labels": []}
        updated_issue = {"number": 1, "title": "Updated issue", "body": "", "labels": []}
        dispatch = [{"issue": 1, "event": "dispatch"}]
        with (
            mock.patch.object(roadmap.time, "monotonic", side_effect=lambda: clock[0]),
            mock.patch.object(
                roadmap,
                "load_github",
                side_effect=[([issue], {}, []), ([updated_issue], {}, [])],
            ) as github,
            mock.patch.object(
                roadmap, "_read_ledger", side_effect=[([], None), (dispatch, None), ([], None)]
            ) as ledger,
        ):
            first = roadmap.cached_model("repo", "/repo")
            clock[0] = roadmap.LEDGER_CACHE_SECONDS - 1
            cached = roadmap.cached_model("repo", "/repo")
            clock[0] = roadmap.LEDGER_CACHE_SECONDS
            fresh_ledger = roadmap.cached_model("repo", "/repo")
            self.assertEqual(github.call_count, 1)
            self.assertEqual(ledger.call_count, 2)
            clock[0] = roadmap.GITHUB_CACHE_SECONDS
            fresh_github = roadmap.cached_model("repo", "/repo")

        self.assertFalse(first["nodes"][0]["dispatched"])
        self.assertIs(cached["nodes"][0]["dispatched"], False)
        self.assertTrue(fresh_ledger["nodes"][0]["dispatched"])
        self.assertEqual(github.call_count, 2)
        self.assertEqual(ledger.call_count, 3)
        self.assertFalse(fresh_github["nodes"][0]["dispatched"])
        self.assertEqual(fresh_github["nodes"][0]["title"], "Updated issue")

    def test_caches_reload_from_disk_after_restart(self):
        issue = {"number": 7, "title": "Saved", "body": "", "labels": []}
        github_data = ([issue], {}, [])
        ledger_data = ([{"issue": 7, "event": "dispatch"}], None)
        with tempfile.TemporaryDirectory() as directory:
            with (
                mock.patch.object(roadmap, "CACHE_FILE", str(Path(directory) / "cache.json")),
                mock.patch.object(roadmap.time, "monotonic", return_value=100.0),
                mock.patch.object(roadmap.time, "time", return_value=1_000_000.0),
                mock.patch.object(roadmap, "load_github", return_value=github_data) as github,
                mock.patch.object(roadmap, "_read_ledger", return_value=ledger_data) as ledger,
            ):
                roadmap._GITHUB_CACHE.clear()
                roadmap._LEDGER_CACHE.clear()
                self.assertEqual(roadmap.cached_github("repo", "/repo"), github_data)
                self.assertEqual(roadmap.cached_ledger("repo", "/repo"), ledger_data)

                roadmap._GITHUB_CACHE.clear()
                roadmap._LEDGER_CACHE.clear()
                roadmap._load_cache()
                self.assertEqual(roadmap.cached_github("repo", "/repo"), github_data)
                self.assertEqual(roadmap.cached_ledger("repo", "/repo"), ledger_data)
                self.assertEqual(github.call_count, 1)
                self.assertEqual(ledger.call_count, 1)

    def test_expired_disk_caches_are_refreshed(self):
        old_issue = {"number": 7, "title": "Old", "body": "", "labels": []}
        fresh_issue = {"number": 7, "title": "Fresh", "body": "", "labels": []}
        now = 1_000_000.0
        saved = {
            "github": [["repo", "open", now - roadmap.GITHUB_CACHE_SECONDS - 1, ([old_issue], {}, [])]],
            "ledger": [["repo", now - roadmap.LEDGER_CACHE_SECONDS - 1, ([{"issue": 7}], None)]],
        }
        with tempfile.TemporaryDirectory() as directory:
            cache_file = Path(directory) / "cache.json"
            cache_file.write_text(json.dumps(saved), encoding="utf-8")
            with (
                mock.patch.object(roadmap, "CACHE_FILE", str(cache_file)),
                mock.patch.object(roadmap.time, "monotonic", return_value=100.0),
                mock.patch.object(roadmap.time, "time", return_value=now),
                mock.patch.object(
                    roadmap, "load_github", return_value=([fresh_issue], {}, [])
                ) as github,
                mock.patch.object(
                    roadmap, "_read_ledger", return_value=([{"issue": 8}], None)
                ) as ledger,
            ):
                roadmap._GITHUB_CACHE.clear()
                roadmap._LEDGER_CACHE.clear()
                roadmap._load_cache()
                self.assertEqual(
                    roadmap.cached_github("repo", "/repo")[0][0]["title"], "Fresh"
                )
                self.assertEqual(roadmap.cached_ledger("repo", "/repo")[0], [{"issue": 8}])

        self.assertEqual(github.call_count, 1)
        self.assertEqual(ledger.call_count, 1)

    def test_cache_file_is_derived_from_home_not_hardcoded(self):
        # roadmap.py used to hardcode /home/ghosta here. CACHE_FILE must be
        # computed from HOME at import time, so it works for any user.
        with mock.patch.dict(os.environ, {"HOME": "/tmp/fake-home-for-test"}):
            importlib.reload(roadmap)
            self.addCleanup(importlib.reload, roadmap)
            self.assertEqual(
                roadmap.CACHE_FILE,
                "/tmp/fake-home-for-test/.local/state/lupin/cache.json",
            )

    def test_selected_roadmap_shows_handoff_and_five_minute_reload(self):
        model = roadmap.build_model(
            [],
            {},
            [{"event": "handoff", "note": "HANDOFF, not a finish."}],
            repo="nix",
        )
        render = lambda title, body, css, js: json.dumps([title, body, css, js])

        page = roadmap.render_page("nix", ["nix"], model, render)

        self.assertIn("setTimeout(function(){location.reload()},305000)", page)
        self.assertIn("Latest handoff: HANDOFF, not a finish.", page)

    def test_repo_roadmap_orders_issue_details_by_recency(self):
        issues = [
            {
                "number": 1,
                "title": "Oldest issue",
                "body": "",
                "labels": ["priority/P0"],
                "updatedAt": "2026-09-20T12:00:00Z",
            },
            {
                "number": 2,
                "title": "No timestamp issue",
                "body": "",
                "labels": ["priority/P1"],
            },
            {
                "number": 3,
                "title": "Newest issue",
                "body": "",
                "labels": ["priority/P2"],
                "updatedAt": "2026-09-25T12:00:00Z",
            },
        ]
        model = roadmap.build_model(issues, {}, [], repo="nix")
        render = lambda title, body, css, js: body

        page = roadmap.render_page("nix", ["nix"], model, render)
        details = page.split("<h2>Issue details and recent activity</h2>", 1)[1]

        self.assertEqual(
            [node["number"] for node in model["nodes"]], [1, 2, 3]
        )
        self.assertLess(page.index(">#1 ·"), page.index(">#3 ·"))
        # Each summary starts with a quest-pick checkbox (issue #19) before
        # the issue's own title span, so match on the span rather than an
        # exact "<summary>...". span" prefix.
        self.assertLess(
            details.index("<span title='Newest issue'>#3 Newest issue"),
            details.index("<span title='Oldest issue'>#1 Oldest issue"),
        )
        self.assertLess(
            details.index("<span title='Oldest issue'>#1 Oldest issue"),
            details.index("<span title='No timestamp issue'>#2 No timestamp issue"),
        )

    def test_invalid_graphql_data_returns_warning(self):
        with mock.patch.object(roadmap, "_run_json", return_value=([], None)):
            comments, error = roadmap._read_comments("/repo", "owner", "repo")

        self.assertEqual(comments, {})
        self.assertEqual(error, "GitHub returned invalid comment data")

    def test_comments_include_author_time_and_link(self):
        response = {
            "data": {
                "repository": {
                    "issues": {
                        "nodes": [
                            {
                                "number": 5,
                                "comments": {
                                    "nodes": [
                                        {
                                            "body": "Update",
                                            "createdAt": "2026-09-26T12:00:00Z",
                                            "url": "https://github.com/acme/repo/issues/5#issuecomment-9",
                                            "author": {"login": "maintainer"},
                                        }
                                    ]
                                },
                            }
                        ],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            }
        }
        with mock.patch.object(roadmap, "_run_json", return_value=(response, None)):
            comments, error = roadmap._read_comments("/repo", "owner", "repo")

        self.assertIsNone(error)
        self.assertEqual(comments[5][0]["body"], "Update")
        self.assertEqual(comments[5][0]["author"], "maintainer")
        self.assertEqual(comments[5][0]["createdAt"], "2026-09-26T12:00:00Z")

    def test_github_attachment_id_requires_a_canonical_uuid_url(self):
        attachment_id = "12345678-1234-5678-1234-123456789abc"
        base = "https://github.com/user-attachments/assets/"
        self.assertEqual(roadmap.github_attachment_id(base + attachment_id), attachment_id)
        self.assertIsNone(roadmap.github_attachment_id(base + "example.png"))
        self.assertIsNone(
            roadmap.github_attachment_id(
                "https://evil.example/user-attachments/assets/" + attachment_id
            )
        )
        self.assertIsNone(
            roadmap.github_attachment_id(base + attachment_id + "?redirect=evil")
        )

    def test_activity_windows_and_los_angeles_time(self):
        now = datetime(2026, 9, 26, 20, tzinfo=timezone.utc)
        cases = [
            (timedelta(minutes=30), "Past hour"),
            (timedelta(hours=1), "Past hour"),
            (timedelta(hours=6), "Past 6 hours"),
            (timedelta(hours=24), "Past 24 hours"),
            (timedelta(days=7), "Past 7 days"),
            (timedelta(days=7, seconds=1), "Older than 7 days"),
        ]
        for age, expected in cases:
            with self.subTest(age=age):
                value = (now - age).isoformat()
                self.assertEqual(roadmap._activity_group(value, now), expected)
        self.assertEqual(
            roadmap._compact_time("2026-09-26T20:00:00Z"),
            "Sep 26, 1:00 PM PDT",
        )

    def test_issue_details_render_github_attachments_through_local_proxy(self):
        issue = {
            "number": 9,
            "title": "New issue",
            "body": (
                "Description <script>alert(1)</script>\n"
                "![safe](https://github.com/user-attachments/assets/12345678-1234-5678-1234-123456789abc)\n"
                "![unsafe](https://evil.example/track.png)\n"
                "![malformed](https://[invalid)"
            ),
            "labels": [],
        }
        comments = {
            9: [
                {
                    "body": "Comment <script>alert(2)</script>",
                    "createdAt": "2026-09-26T12:00:00Z",
                    "url": "https://github.com/acme/repo/issues/9#issuecomment-1",
                    "author": "<img src=x>",
                }
            ]
        }
        node = roadmap.build_model([issue], comments, [], repo="repo")["nodes"][0]

        details = roadmap.render_issue_details(node)
        self.assertIn(
            "<summary><span title='New issue'>#9 New issue</span> · "
            "Details and activity · 1 comment</summary>",
            details,
        )
        node["url"] = "https://github.com/acme/repo/issues/9"
        linked = roadmap.render_issue_details(node)
        self.assertIn(
            "<summary><a href='https://github.com/acme/repo/issues/9' "
            "title='New issue' target='_blank' rel='noopener'>#9 New issue</a> · "
            "Details and activity · 1 comment</summary>",
            linked,
        )

        self.assertIn("Details and activity · 1 comment", details)
        self.assertIn("Description &lt;script&gt;alert(1)&lt;/script&gt;", details)
        self.assertIn(
            "src='/image?id=12345678-1234-5678-1234-123456789abc'", details
        )
        self.assertNotIn("src='https://github.com/user-attachments/assets/", details)
        self.assertNotIn("href='https://evil.example/track.png'", details)
        self.assertNotIn("<img src=x>", details)
        self.assertIn("&lt;img src=x&gt;", details)
        self.assertNotIn("<script>", details)


    def test_issue_details_truncate_title_and_keep_full_title_in_tooltip(self):
        full_title = "A" * 80 + " & <details>"
        node = {
            "number": 10,
            "title": full_title,
            "body": "",
            "labels": [],
            "url": "https://github.com/acme/repo/issues/10",
            "comments": [],
        }

        details = roadmap.render_issue_details(node)

        self.assertIn(
            f"title='{roadmap._escape_attr(full_title)}' "
            f"target='_blank' rel='noopener'>#10 {'A' * 80}…</a>",
            details,
        )
        self.assertNotIn(f">{'A' * 80} &amp; &lt;details&gt;</a>", details)



    def test_recent_comments_are_visible_as_escaped_text(self):
        issue = {"number": 1, "title": "Comment test", "body": "", "labels": []}
        model = roadmap.build_model(
            [issue], {1: ["Latest <script>alert(1)</script>"]}, [], repo="nix"
        )
        render = lambda title, body, css, js: body + css + js

        page = roadmap.render_page("nix", ["nix"], model, render)

        self.assertIn("Details and activity · 1 comment", page)
        self.assertIn("Latest &lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.assertNotIn("Latest <script>", page)


    def test_combined_update_previews_newest_comment(self):
        issue = {
            "number": 19,
            "title": "Commented issue",
            "body": "Original description",
            "labels": [],
            "createdAt": "2026-09-20T10:00:00Z",
            "updatedAt": "2026-09-25T10:00:00Z",
            "url": "https://github.com/acme/repo/issues/19",
        }
        model = roadmap.build_model(
            [issue],
            {
                19: [
                    {
                        "body": "Newest comment & <script>\nsecond line",
                        "createdAt": "2026-09-26T10:00:00Z",
                    }
                ]
            },
            [],
            repo="alpha",
        )

        page = roadmap.render_combined_page(
            ["alpha"], {"alpha": model}, lambda title, body, css, js: body
        )
        updates = page.split("<h2>Latest updates</h2>", 1)[1].split(
            "<h2>Work board</h2>", 1
        )[0]

        self.assertIn("<span class='pill'>Commented</span>", updates)
        self.assertIn("Newest comment &amp; &lt;script&gt; second line", updates)
        self.assertNotIn("Newest comment & <script>", updates)

    def test_combined_update_previews_description_for_new_issue(self):
        issue = {
            "number": 20,
            "title": "Fresh issue",
            "body": "Description & details\nsecond line",
            "labels": [],
            "createdAt": "2026-09-26T10:00:00Z",
            "updatedAt": "2026-09-26T10:00:00Z",
            "url": "https://github.com/acme/repo/issues/20",
        }
        model = roadmap.build_model([issue], {}, [], repo="alpha")

        page = roadmap.render_combined_page(
            ["alpha"], {"alpha": model}, lambda title, body, css, js: body
        )
        updates = page.split("<h2>Latest updates</h2>", 1)[1].split(
            "<h2>Work board</h2>", 1
        )[0]

        self.assertIn("<span class='pill'>Opened</span>", updates)
        self.assertIn("Description &amp; details second line", updates)

    def test_combined_updates_include_closed_issue(self):
        issue = {
            "number": 21,
            "title": "Recently closed",
            "body": "Closed issue description",
            "labels": [],
            "createdAt": "2026-09-20T10:00:00Z",
            "updatedAt": "2026-09-26T10:00:00Z",
            "closedAt": "2026-09-26T10:00:00Z",
            "url": "https://github.com/acme/repo/issues/21",
        }
        model = roadmap.build_model([], {}, [], repo="alpha")
        model["closedNodes"] = roadmap.build_model(
            [issue], {}, [], repo="alpha"
        )["nodes"]

        page = roadmap.render_combined_page(
            ["alpha"], {"alpha": model}, lambda title, body, css, js: body
        )
        updates = page.split("<h2>Latest updates</h2>", 1)[1].split(
            "<h2>Work board</h2>", 1
        )[0]

        self.assertIn("#21 Recently closed", updates)
        self.assertIn("<span class='pill'>Closed</span>", updates)


    def test_combined_board_groups_recent_updates_and_keeps_issue_details(self):
        now = datetime.now(timezone.utc)
        older_time = now - timedelta(days=2)
        newer_time = now - timedelta(hours=2)
        older = roadmap.build_model(
            [
                {
                    "number": 7,
                    "title": "Older issue",
                    "body": "",
                    "labels": [],
                    "url": "https://github.com/acme/old/issues/7",
                    "updatedAt": older_time.isoformat(),
                }
            ],
            {},
            [],
            repo="alpha",
        )
        newer = roadmap.build_model(
            [
                {
                    "number": 7,
                    "title": "Newer issue",
                    "body": "New issue details ![image](https://github.com/user-attachments/assets/12345678-1234-5678-1234-123456789abc)",
                    "labels": ["owner-decision"],
                    "url": "https://github.com/acme/new/issues/7",
                    "createdAt": (newer_time - timedelta(days=1)).isoformat(),
                    "updatedAt": newer_time.isoformat(),
                }
            ],
            {},
            [],
            repo="beta",
        )
        render = lambda title, body, css, js: body + css + js

        page = roadmap.render_combined_page(
            ["alpha", "beta"], {"alpha": older, "beta": newer}, render
        )
        updates = page.split("<h2>Latest updates</h2>", 1)[1].split(
            "<h2>Work board</h2>", 1
        )[0]

        self.assertIn("alpha", page)
        self.assertIn("beta", page)
        self.assertIn("All repositories", page)
        self.assertIn("Work board", page)
        self.assertIn("GitHub data refreshes hourly", page)
        self.assertIn("ledger JSON and this page refresh every five minutes", page)
        self.assertIn("setTimeout(function(){location.reload()},305000)", page)
        self.assertLess(page.index("Latest updates"), page.index("Work board"))
        self.assertLess(updates.index("#7 Newer issue"), updates.index("#7 Older issue"))
        self.assertIn("Past 6 hours", updates)
        self.assertIn("Past 7 days", updates)
        self.assertIn(roadmap._compact_time(newer_time.isoformat()), updates)
        self.assertIn("Details and activity · 0 comments", updates)
        self.assertIn(
            "src='/image?id=12345678-1234-5678-1234-123456789abc'", updates
        )
        self.assertIn("Created " + roadmap._compact_time((newer_time - timedelta(days=1)).isoformat()), updates)
        self.assertIn("Labels: owner-decision", updates)

    def test_recent_update_item_carries_labels_for_client_side_filter(self):
        # The label filter reads data-labels off the direct child of
        # data-browse-group (updateBrowse's `:scope > [data-browse-item]`).
        # render_issue_details() puts data-labels on a nested <details>, one
        # level too deep for that query, so the wrapping <li> needs its own
        # copy or the label filter treats every recent update as unlabeled.
        now = datetime.now(timezone.utc)
        model = roadmap.build_model(
            [
                {
                    "number": 7,
                    "title": "Labeled issue",
                    "body": "",
                    "labels": ["priority/P0"],
                    "url": "https://github.com/acme/repo/issues/7",
                    "updatedAt": now.isoformat(),
                }
            ],
            {},
            [],
            repo="alpha",
        )
        render = lambda title, body, css, js: body + css + js
        page = roadmap.render_combined_page(["alpha"], {"alpha": model}, render)
        updates = page.split("<h2>Latest updates</h2>", 1)[1].split(
            "<h2>Work board</h2>", 1
        )[0]
        self.assertIn(
            "<li data-browse-item data-labels='[&quot;priority/P0&quot;]'>", updates
        )

    def test_browse_filter_does_not_hide_matching_items(self):
        # updateBrowse() must show every item that matches an active label
        # or search filter, not just the first `chunk` of them — the chunk
        # limit only applies with no filter active.
        js = roadmap._browse_script()
        self.assertIn("item.hidden=filtering?false:shown>=limit;", js)
        self.assertNotIn("item.hidden=Boolean(query||selected.size)||shown>=limit;", js)

    def test_combined_board_lists_body_and_comment_images_by_recency(self):
        body_image = "12345678-1234-5678-1234-123456789abc"
        comment_image = "abcdefab-cdef-abcd-efab-cdefabcdefab"
        image_url = "https://github.com/user-attachments/assets/"
        model = roadmap.build_model(
            [
                {
                    "number": 1,
                    "title": "Body image",
                    "body": f"Older body context ![body]({image_url}{body_image})",
                    "labels": [],
                    "url": "https://github.com/acme/repo/issues/1",
                    "createdAt": "2026-09-20T00:00:00Z",
                    "updatedAt": "2026-09-21T00:00:00Z",
                },
                {
                    "number": 2,
                    "title": "Comment image",
                    "body": "No image in this issue",
                    "labels": [],
                    "url": "https://github.com/acme/repo/issues/2",
                    "updatedAt": "2026-09-22T00:00:00Z",
                },
                {
                    "number": 3,
                    "title": "No image",
                    "body": "This issue has no image",
                    "labels": [],
                    "url": "https://github.com/acme/repo/issues/3",
                    "updatedAt": "2026-09-23T00:00:00Z",
                },
            ],
            {
                2: [
                    {
                        "body": f"Newer comment context ![comment]({image_url}{comment_image})",
                        "createdAt": "2026-09-25T00:00:00Z",
                        "url": "https://github.com/acme/repo/issues/2#issuecomment-2",
                    }
                ]
            },
            [],
            repo="repo",
        )

        page = roadmap.render_combined_page(
            ["repo"], {"repo": model}, lambda title, body, css, js: body
        )
        section = page.split("<details class='recent-image-details'>", 1)[1].split(
            "</details>", 1
        )[0]

        self.assertIn("Recently added images", section)
        self.assertIn(f"src='/image?id={body_image}'", section)
        self.assertIn(f"src='/image?id={comment_image}'", section)
        self.assertLess(
            section.index("Newer comment context"),
            section.index("Older body context"),
        )
        self.assertIn("issues/2#issuecomment-2", section)
        self.assertNotIn("No image", section)

        empty_model = roadmap.build_model(
            [{"number": 4, "title": "No image", "body": "", "labels": []}],
            {},
            [],
            repo="repo",
        )
        empty_page = roadmap.render_combined_page(
            ["repo"], {"repo": empty_model}, lambda title, body, css, js: body
        )
        self.assertIn("<p class='dim'>No images found</p>", empty_page)

    def test_queue_legend_and_stage_notes_render_on_both_pages(self):

        model = roadmap.build_model(
            [{"number": 1, "title": "Issue", "body": "", "labels": []}],
            {},
            [],
            repo="alpha",
        )
        render = lambda title, body, css, js: body

        repo_page = roadmap.render_page("alpha", ["alpha"], model, render)
        combined_page = roadmap.render_combined_page(
            ["alpha"], {"alpha": model}, render
        )

        self.assertIn("Line key: solid lines show dependencies", repo_page)
        self.assertIn("dashed lines show parent or split links", repo_page)
        for page in (repo_page, combined_page):
            self.assertIn("Next batch starts with the first eligible issue", page)
            self.assertIn("Blocked or held means a decision", page)
            self.assertIn(
                "In-flight status comes from the last ledger action. "
                "It does not confirm that a process is running.",
                page,
            )

    def test_saved_filters_and_manual_queue_order_render_for_open_eligible_issues(self):
        issues = [
            {
                "number": 31,
                "title": "Labelled eligible issue",
                "body": "",
                "labels": ["priority/P0", "team-a"],
            },
            {
                "number": 32,
                "title": "Held issue",
                "body": "",
                "labels": ["priority/P0", "owner-todo"],
            },
        ]
        model = roadmap.build_model(issues, {}, [], repo="alpha")
        rendered = {}

        def render(title, body, css, js):
            rendered["body"] = body
            rendered["js"] = js
            return body + js

        page = roadmap.render_page("alpha", ["alpha"], model, render)

        self.assertIn("<select id='roadmap-labels' multiple>", page)
        self.assertIn("value='team-a'", page)
        self.assertIn("data-labels='[&quot;priority/P0&quot;, &quot;team-a&quot;]'", page)
        self.assertIn("matchesLabels=[...selected].every", rendered["js"])
        self.assertIn("lupin-roadmap-filter", rendered["js"])
        self.assertIn("lupin-queue-order", rendered["js"])
        self.assertIn("queue=queue.filter(key=>eligible.has(String(key)))", rendered["js"])
        self.assertIn("data-next-batch='alpha'", page)
        self.assertIn("data-queue-key='31'", page)
        self.assertNotIn("<li data-queue-item data-queue-key='32'", page)
        self.assertIn("data-computed-index='0' draggable='true'", page)
        self.assertEqual(model["batch"], [31])
        combined_page = roadmap.render_combined_page(
            ["alpha"], {"alpha": model}, render
        )
        self.assertIn("<ol data-next-batch='all'>", combined_page)
        self.assertIn("data-queue-key='alpha#31'", combined_page)

    def test_latest_update_title_appears_once(self):
        model = roadmap.build_model(
            [
                {
                    "number": 12,
                    "title": "One visible title",
                    "body": "",
                    "labels": [],
                    "url": "https://github.com/acme/repo/issues/12",
                    "updatedAt": "2026-09-26T12:00:00Z",
                }
            ],
            {},
            [],
            repo="alpha",
        )
        page = roadmap.render_combined_page(
            ["alpha"], {"alpha": model}, lambda title, body, css, js: body
        )
        updates = page.split("<h2>Latest updates</h2>", 1)[1].split(
            "<h2>Work board</h2>", 1
        )[0]

        self.assertEqual(updates.count("#12 One visible title"), 1)
        self.assertIn("#12 One visible title", updates)

    def test_needs_expert_decision_label_stays_eligible_and_batched(self):
        issues = [
            {
                "number": 41,
                "title": "Needs expert decision",
                "body": "",
                "labels": ["priority/P0", "needs-expert-decision"],
            },
        ]
        model = roadmap.build_model(issues, {}, [], repo="alpha")
        held_stage = next(
            stage for stage in model["stages"] if stage["name"] == "Blocked or held"
        )

        self.assertNotIn(41, held_stage["numbers"])
        self.assertIn(41, model["batch"])

    def test_completely_blocked_on_human_label_is_owner_blocked_not_held(self):
        issues = [
            {
                "number": 42,
                "title": "Blocked on human",
                "body": "",
                "labels": ["priority/P0", "completely-blocked-on-human"],
            },
        ]
        model = roadmap.build_model(issues, {}, [], repo="alpha")
        held_stage = next(
            stage for stage in model["stages"] if stage["name"] == "Blocked or held"
        )

        self.assertNotIn(42, held_stage["numbers"])
        self.assertNotIn(42, model["batch"])
        self.assertEqual(model["ownerBlocked"], [42])

    def test_completely_blocked_on_human_issue_renders_in_owner_blocked_section(self):
        issues = [
            {
                "number": 42,
                "title": "Blocked on human",
                "body": "",
                "labels": ["priority/P0", "completely-blocked-on-human"],
                "url": "https://github.com/gracecraft/alpha/issues/42",
            },
        ]
        model = roadmap.build_model(issues, {}, [], repo="alpha")
        render = lambda title, body, css, js: body

        page = roadmap.render_page("alpha", ["alpha"], model, render)
        combined = roadmap.render_combined_page(["alpha"], {"alpha": model}, render)

        for page_html in (page, combined):
            self.assertIn("<h2>Owner blocked</h2>", page_html)
            owner_blocked_html = page_html.split("<h2>Owner blocked</h2>", 1)[1]
            self.assertIn("#42", owner_blocked_html)
            self.assertIn("Blocked on human", owner_blocked_html)

    def test_no_owner_blocked_issues_hides_owner_blocked_section(self):
        issues = [
            {
                "number": 43,
                "title": "Regular issue",
                "body": "",
                "labels": ["priority/P0"],
            },
        ]
        model = roadmap.build_model(issues, {}, [], repo="alpha")
        render = lambda title, body, css, js: body

        page = roadmap.render_page("alpha", ["alpha"], model, render)
        combined = roadmap.render_combined_page(["alpha"], {"alpha": model}, render)

        self.assertEqual(model["ownerBlocked"], [])
        self.assertNotIn("Owner blocked", page)
        self.assertNotIn("Owner blocked", combined)

    def test_legacy_owner_todo_and_needs_decision_labels_remain_held(self):
        issues = [
            {
                "number": 43,
                "title": "Owner todo",
                "body": "",
                "labels": ["priority/P0", "owner-todo"],
            },
            {
                "number": 44,
                "title": "Needs decision",
                "body": "",
                "labels": ["priority/P0", "needs-decision"],
            },
        ]
        model = roadmap.build_model(issues, {}, [], repo="alpha")
        held_stage = next(
            stage for stage in model["stages"] if stage["name"] == "Blocked or held"
        )

        self.assertIn(43, held_stage["numbers"])
        self.assertIn(44, held_stage["numbers"])
        self.assertNotIn(43, model["batch"])
        self.assertNotIn(44, model["batch"])

    def test_needs_expert_decision_badge_appears_on_dependency_graph(self):
        issues = [
            {
                "number": 45,
                "title": "Needs expert decision badge",
                "body": "",
                "labels": ["priority/P0", "needs-expert-decision"],
            },
        ]
        model = roadmap.build_model(issues, {}, [], repo="alpha")
        page = roadmap.render_page(
            "alpha", ["alpha"], model, lambda title, body, css, js: body
        )

        self.assertIn("needs expert decision", page)

    def test_combined_page_shows_dependency_graph_for_repo_with_edges(self):
        issues = [
            {"number": 1, "title": "Parent", "body": "", "labels": ["epic"]},
            {
                "number": 2,
                "title": "Child",
                "body": "Part of #1. Depends on #3.",
                "labels": [],
            },
            {"number": 3, "title": "Prerequisite", "body": "", "labels": []},
        ]
        model = roadmap.build_model(issues, {}, [], repo="alpha")

        page = roadmap.render_combined_page(
            ["alpha"], {"alpha": model}, lambda title, body, css, js: body
        )

        self.assertIn("<h2>Dependency graphs</h2>", page)
        self.assertIn("queue-graph", page)
        self.assertIn("dependency", page)

    def test_combined_page_states_cross_repo_edges_are_out_of_scope(self):
        model = roadmap.build_model(
            [{"number": 1, "title": "Issue", "body": "", "labels": []}],
            {},
            [],
            repo="alpha",
        )

        page = roadmap.render_combined_page(
            ["alpha"], {"alpha": model}, lambda title, body, css, js: body
        )

        self.assertIn(roadmap.CROSS_REPO_EDGE_NOTE, page)

    def test_structured_digest_becomes_labelled_bullet_lists(self):
        issue = {"number": 31, "title": "Redis backend", "body": "", "labels": []}
        ledger = [
            {
                "issue": 31,
                "summary": "Redis slot leases, staged in the prep repo.",
                "highlights": ["Added src/lupin/slots_redis.py."],
                "evidence": ["nix build .#checks.aarch64-linux.default — 39/39 passed."],
                "decisions": ["Only the bmo slot falls back to local; others exit 3."],
                "next": ["Owner: create gracecraft/lupin (#201)."],
            }
        ]

        node = roadmap.build_model([issue], {}, ledger, repo="nix")["nodes"][0]
        digest = node["digest"]

        self.assertEqual(
            [field["label"] for field in digest["fields"]],
            ["Highlights", "Evidence", "Decisions", "Next"],
        )
        self.assertEqual(digest["fields"][0]["bullets"], ["Added src/lupin/slots_redis.py."])
        self.assertEqual(
            digest["headline"], "Redis slot leases, staged in the prep repo."
        )

    def test_prose_entry_without_lists_still_renders_a_headline(self):
        issue = {"number": 32, "title": "Tunnel", "body": "", "labels": []}
        ledger = [{"issue": 32, "summary": "Mac launchd tunnel added.", "followups": "Owner: set the tailnet IP."}]

        node = roadmap.build_model([issue], {}, ledger, repo="nix")["nodes"][0]

        self.assertEqual(node["digest"]["headline"], "Mac launchd tunnel added.")
        self.assertEqual(
            node["digest"]["fields"],
            [{"key": "next", "label": "Next", "bullets": ["Owner: set the tailnet IP."]}],
        )

    def test_entry_without_any_summary_produces_no_digest(self):
        issue = {"number": 33, "title": "Untouched", "body": "", "labels": []}

        node = roadmap.build_model(
            [issue], {}, [{"issue": 33, "action": "dispatch"}], repo="nix"
        )["nodes"][0]

        self.assertIsNone(node["digest"])
        self.assertNotIn("class='digest'", roadmap.render_issue_details(node))

    def test_digest_bullets_drop_markdown_markers_and_blank_lines(self):
        digest = roadmap._digest(
            {
                "highlights": ["- first item\n\n* second item", 7],
                "evidence": "single string",
            }
        )

        self.assertEqual(
            digest["fields"][0]["bullets"], ["first item", "second item"]
        )
        self.assertEqual(digest["fields"][1]["bullets"], ["single string"])

    def test_digest_bullets_are_escaped_in_the_page(self):
        issue = {"number": 34, "title": "Escaped", "body": "", "labels": []}
        ledger = [{"issue": 34, "highlights": ["<script>alert(1)</script>"]}]

        node = roadmap.build_model([issue], {}, ledger, repo="nix")["nodes"][0]
        page = roadmap.render_issue_details(node)

        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;", page)

    def test_digest_ships_on_both_roadmap_pages(self):
        issue = {"number": 35, "title": "Digest page", "body": "", "labels": []}
        ledger = [{"issue": 35, "highlights": ["One bullet."]}]
        model = roadmap.build_model([issue], {}, ledger, repo="alpha")
        render = lambda title, body, css, js: body

        per_repo = roadmap.render_page("alpha", ["alpha"], model, render)
        combined = roadmap.render_combined_page(["alpha"], {"alpha": model}, render)

        for page in (per_repo, combined):
            self.assertIn("data-digest-field='highlights'", page)
            self.assertIn("One bullet.", page)

class DependencyDagTests(unittest.TestCase):
    """build_dependency_dag combines blockedBy/blocking links -- GitHub's
    real issue-dependency feature, not a text search -- into one DAG that
    can span repos.
    """

    def setUp(self):
        roadmap._DEPENDENCY_CACHE.clear()

    def test_cross_repo_edge_from_blocked_by_and_blocking(self):
        # api#10 is blocked by core#3; core#3 reports the same link as
        # "blocking" api#10. Both directions should produce one DAG.
        repo_links = {
            "api": {10: {"blockedBy": [{"repo": "core", "number": 3}], "blocking": []}},
            "core": {3: {"blockedBy": [], "blocking": [{"repo": "api", "number": 10}]}},
        }

        dag = roadmap.build_dependency_dag(repo_links)

        self.assertEqual(dag["cycles"], [])
        self.assertEqual(
            dag["repos"]["api"],
            [{"number": 10, "blockedBy": [{"repo": "core", "number": 3}], "blocking": []}],
        )
        self.assertEqual(
            dag["repos"]["core"],
            [{"number": 3, "blockedBy": [], "blocking": [{"repo": "api", "number": 10}]}],
        )

    def test_cycle_across_repos_is_detected_and_reported_once(self):
        # api#1 blocks core#2, core#2 blocks web#3, web#3 blocks api#1.
        repo_links = {
            "api": {1: {"blockedBy": [], "blocking": [{"repo": "core", "number": 2}]}},
            "core": {2: {"blockedBy": [], "blocking": [{"repo": "web", "number": 3}]}},
            "web": {3: {"blockedBy": [], "blocking": [{"repo": "api", "number": 1}]}},
        }

        dag = roadmap.build_dependency_dag(repo_links)

        self.assertEqual(len(dag["cycles"]), 1)
        cycle_keys = {(node["repo"], node["number"]) for node in dag["cycles"][0]}
        self.assertEqual(cycle_keys, {("api", 1), ("core", 2), ("web", 3)})

    def test_self_blocking_issue_is_reported_as_a_cycle(self):
        # Malformed data: an issue listed as blocking itself. This must
        # still show up in "cycles" -- not get silently dropped.
        repo_links = {
            "lupin": {5: {"blockedBy": [], "blocking": [{"repo": "lupin", "number": 5}]}},
        }

        dag = roadmap.build_dependency_dag(repo_links)

        self.assertEqual(len(dag["cycles"]), 1)
        cycle_keys = [(node["repo"], node["number"]) for node in dag["cycles"][0]]
        self.assertEqual(cycle_keys, [("lupin", 5), ("lupin", 5)])

    def test_no_dependencies_is_an_empty_but_well_formed_dag(self):
        repo_links = {"solo": {5: {"blockedBy": [], "blocking": []}}}

        dag = roadmap.build_dependency_dag(repo_links)

        self.assertEqual(dag["cycles"], [])
        self.assertEqual(
            dag["repos"]["solo"],
            [{"number": 5, "blockedBy": [], "blocking": []}],
        )

    def test_read_dependencies_keeps_the_target_repo_name(self):
        response = {
            "data": {
                "repository": {
                    "issues": {
                        "nodes": [
                            {
                                "number": 10,
                                "blockedBy": {
                                    "nodes": [{"number": 3, "repository": {"name": "core"}}]
                                },
                                "blocking": {"nodes": []},
                            }
                        ],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            }
        }

        with mock.patch.object(roadmap, "_run_json", return_value=(response, None)):
            links, error = roadmap._read_dependencies("/repo", "acme", "api")

        self.assertIsNone(error)
        self.assertEqual(
            links, {10: {"blockedBy": [{"repo": "core", "number": 3}], "blocking": []}}
        )

    def test_cached_dependency_dag_combines_repos_and_surfaces_warnings(self):
        with mock.patch.object(
            roadmap,
            "load_dependencies",
            side_effect=[
                ({1: {"blockedBy": [], "blocking": [{"repo": "core", "number": 2}]}}, []),
                ({}, ["GitHub dependency links are unavailable: boom"]),
            ],
        ) as load:
            dag = roadmap.cached_dependency_dag(["api", "core"], code_dir="/code")

        self.assertEqual(dag["repos"]["api"][0]["number"], 1)
        self.assertEqual(
            dag["warnings"], {"core": ["GitHub dependency links are unavailable: boom"]}
        )
        self.assertEqual(load.call_count, 2)


if __name__ == "__main__":
    unittest.main()
