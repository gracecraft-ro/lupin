"""Tests for `lupin roadmap` (issue #10) -- the text/JSON rendering on top
of `roadmap.py`'s dependency DAG (issue #4) and `claims.py`'s claims
(issue #6).

Every test mocks `roadmap._repo_identity`, `roadmap.cached_github`, and
`roadmap.cached_dependency_dag` directly (same objects issue #4's own
tests patch) so nothing here touches the network or changes how a
dependency is found -- only how it's rendered.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from lupin import roadmap, roadmap_cli
from lupin.slots import CoordinatorUnreachable


def _issue(number, title, priority="P1", labels=None):
    labels = list(labels) if labels is not None else [{"name": priority}]
    return {"number": number, "title": title, "labels": labels, "body": ""}


def _identity(owner="acme"):
    return lambda path: (owner, path.rsplit("/", 1)[-1], None)


class BuildRoadmapTests(unittest.TestCase):
    def _patch(self, open_issues, dag, identity=None, issue_state=None):
        patches = [
            mock.patch.object(roadmap, "_repo_identity", side_effect=identity or _identity()),
            mock.patch.object(
                roadmap, "cached_github",
                side_effect=lambda repo, path, state="open", **kw: (
                    (open_issues.get(repo, []), {}, []) if state == "open" else ([], {}, [])
                ),
            ),
            mock.patch.object(
                roadmap, "cached_dependency_dag", side_effect=lambda repos, code_dir, **kw: dag
            ),
        ]
        if issue_state is not None:
            # `_issue_state` now takes (path, number, owner, name, connection=...)
            # (issue #35) -- callers here only care about (path, number).
            patches.append(
                mock.patch.object(
                    roadmap_cli, "_issue_state",
                    side_effect=lambda path, number, owner, name, **kw: issue_state(path, number),
                )
            )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_plain_list_marks_ready_blocked_and_claimed(self):
        open_issues = {
            "api-gateway": [
                _issue(418, "retry backoff", "P1"),
                _issue(431, "rate-limit headers", "P2"),
                _issue(422, "split session store", "P2"),
            ]
        }
        dag = {
            "repos": {
                "api-gateway": [
                    {"number": 418, "blockedBy": [], "blocking": []},
                    {"number": 431, "blockedBy": [{"repo": "api-gateway", "number": 418}], "blocking": []},
                    {"number": 422, "blockedBy": [], "blocking": []},
                ]
            },
            "cycles": [],
            "warnings": {},
        }
        self._patch(open_issues, dag)
        claims_lookup = mock.Mock(return_value={"acme/api-gateway#422": {"session": "api-gateway#2"}})

        text, code = roadmap_cli.run(
            "api-gateway", 10, "ready", False, False, False, claims_lookup=claims_lookup
        )

        self.assertEqual(code, 0)
        # #422 is claimed, so it's excluded from the "ready" tally even
        # though it (like #418) still shows as a row in the default view.
        self.assertIn("api-gateway · 1 ready · 1 blocked", text)
        self.assertIn("#418  P1  retry backoff", text)
        self.assertIn("#422  P2  split session store", text)
        self.assertIn("claimed by api-gateway #2", text)
        # #431 is blocked, so it's hidden from the default (ready) stage.
        self.assertNotIn("#431", text)
        claims_lookup.assert_called_once_with(["acme/api-gateway"])

    def test_stage_blocked_shows_only_blocked_issues(self):
        open_issues = {
            "api-gateway": [
                _issue(418, "retry backoff", "P1"),
                _issue(431, "rate-limit headers", "P2"),
            ]
        }
        dag = {
            "repos": {
                "api-gateway": [
                    {"number": 418, "blockedBy": [], "blocking": []},
                    {"number": 431, "blockedBy": [{"repo": "api-gateway", "number": 418}], "blocking": []},
                ]
            },
            "cycles": [],
            "warnings": {},
        }
        self._patch(open_issues, dag)

        text, code = roadmap_cli.run(
            "api-gateway", 10, "blocked", False, False, False, claims_lookup=lambda repos: {}
        )

        self.assertEqual(code, 0)
        self.assertIn("#431", text)
        self.assertIn("blocked", text)
        self.assertNotIn("#418", text)

    def test_no_priority_label_sorts_last_and_warns(self):
        open_issues = {
            "api-gateway": [
                _issue(418, "retry backoff", "P1"),
                _issue(440, "docs", labels=[]),
            ]
        }
        dag = {
            "repos": {
                "api-gateway": [
                    {"number": 418, "blockedBy": [], "blocking": []},
                    {"number": 440, "blockedBy": [], "blocking": []},
                ]
            },
            "cycles": [],
            "warnings": {},
        }
        self._patch(open_issues, dag)

        text, code = roadmap_cli.run(
            "api-gateway", 10, "ready", False, False, False, claims_lookup=lambda repos: {}
        )

        self.assertEqual(code, 0)
        self.assertIn("warning: #440 has no priority label. Sorted last.", text)
        # #418 (P1) must rank above #440 (no label, falls back to last).
        self.assertLess(text.index("#418"), text.index("#440"))

    def test_broken_dependency_link_is_treated_as_unblocked_with_warning(self):
        open_issues = {"api-gateway": [_issue(431, "rate-limit headers", "P2")]}
        dag = {
            "repos": {
                "api-gateway": [
                    {"number": 431, "blockedBy": [{"repo": "api-gateway", "number": 999}], "blocking": []},
                ]
            },
            "cycles": [],
            "warnings": {},
        }
        self._patch(open_issues, dag, issue_state=lambda path, number: None)

        text, code = roadmap_cli.run(
            "api-gateway", 10, "ready", False, False, False, claims_lookup=lambda repos: {}
        )

        self.assertEqual(code, 0)
        self.assertIn(
            "warning: #431 depends on #999, which does not exist. Treated as unblocked.", text
        )
        # Not blocked -- the broken link doesn't count, so it shows as ready.
        row = next(line for line in text.splitlines() if "#431" in line and line.strip()[0].isdigit())
        self.assertTrue(row.rstrip().endswith("ready"))

    def test_closed_blocker_resolves_silently_no_warning(self):
        open_issues = {"api-gateway": [_issue(431, "rate-limit headers", "P2")]}
        dag = {
            "repos": {
                "api-gateway": [
                    {"number": 431, "blockedBy": [{"repo": "api-gateway", "number": 400}], "blocking": []},
                ]
            },
            "cycles": [],
            "warnings": {},
        }
        self._patch(open_issues, dag, issue_state=lambda path, number: "CLOSED")

        text, code = roadmap_cli.run(
            "api-gateway", 10, "ready", False, False, False, claims_lookup=lambda repos: {}
        )

        self.assertEqual(code, 0)
        self.assertNotIn("warning:", text)

    def test_claim_lookup_failure_is_reported_not_guessed(self):
        open_issues = {"api-gateway": [_issue(418, "retry backoff", "P1")]}
        dag = {
            "repos": {"api-gateway": [{"number": 418, "blockedBy": [], "blocking": []}]},
            "cycles": [],
            "warnings": {},
        }
        self._patch(open_issues, dag)

        def failing_claims(repos):
            raise CoordinatorUnreachable("claims_for")

        text, code = roadmap_cli.run(
            "api-gateway", 10, "ready", False, False, False, claims_lookup=failing_claims
        )

        self.assertEqual(code, 0)
        self.assertIn("warning: claim data is unavailable", text)
        self.assertIn("#418  P1  retry backoff", text)


class EmptyStateTests(unittest.TestCase):
    def test_per_repo_empty_message_names_claimed_and_blocked_counts(self):
        with mock.patch.object(roadmap, "_repo_identity", side_effect=_identity()), \
             mock.patch.object(
                 roadmap, "cached_github",
                 side_effect=lambda repo, path, state="open", **kw: (
                     [_issue(422, "split store", "P2"), _issue(440, "docs", "P3")], {}, []
                 ) if state == "open" else ([], {}, []),
             ), \
             mock.patch.object(
                 roadmap, "cached_dependency_dag",
                 side_effect=lambda repos, code_dir: {
                     "repos": {
                         "api-gateway": [
                             {"number": 422, "blockedBy": [], "blocking": []},
                             {"number": 440, "blockedBy": [{"repo": "api-gateway", "number": 422}], "blocking": []},
                         ]
                     },
                     "cycles": [],
                     "warnings": {},
                 },
             ):
            text, code = roadmap_cli.run(
                "api-gateway", 10, "ready", False, False, False,
                claims_lookup=lambda repos: {"acme/api-gateway#422": {"session": "api-gateway#2"}},
            )

        self.assertEqual(code, 0)
        self.assertEqual(
            text, "No ready tasks in api-gateway. 1 is claimed by other loops, 1 is blocked."
        )

    def test_empty_everywhere_across_enabled_repos(self):
        with mock.patch.object(roadmap, "_repo_identity", side_effect=_identity()), \
             mock.patch.object(
                 roadmap, "cached_github",
                 side_effect=lambda repo, path, state="open", **kw: ([], {}, []),
             ), \
             mock.patch.object(
                 roadmap, "cached_dependency_dag",
                 return_value={"repos": {}, "cycles": [], "warnings": {}},
             ):
            text, code = roadmap_cli.run(
                None, 10, "ready", False, False, False,
                enabled_repos=lambda: ["api-gateway", "billing-core"],
                claims_lookup=lambda repos: {},
            )

        self.assertEqual(code, 0)
        self.assertEqual(text, "No ready tasks in any repo. Nothing to run.")

    def test_no_enabled_repos_at_all(self):
        text, code = roadmap_cli.run(
            None, 10, "ready", False, False, False,
            enabled_repos=lambda: [], claims_lookup=lambda repos: {},
        )

        self.assertEqual(code, 0)
        self.assertEqual(text, "No ready tasks in any repo. Nothing to run.")


class DagViewTests(unittest.TestCase):
    def _dag_model(self):
        # #410 unblocks #418 and #422; both unblock #431; #431 unblocks #440.
        open_issues = {
            "api-gateway": [
                _issue(418, "retry backoff", "P1"),
                _issue(422, "split store", "P2"),
                _issue(431, "rate-limit headers", "P2"),
                _issue(440, "docs", "P3"),
                _issue(451, "trace ids", "P3"),
            ]
        }
        dag = {
            "repos": {
                "api-gateway": [
                    {"number": 418, "blockedBy": [], "blocking": [{"repo": "api-gateway", "number": 431}]},
                    {"number": 422, "blockedBy": [], "blocking": [{"repo": "api-gateway", "number": 431}]},
                    {
                        "number": 431,
                        "blockedBy": [
                            {"repo": "api-gateway", "number": 418},
                            {"repo": "api-gateway", "number": 422},
                        ],
                        "blocking": [{"repo": "api-gateway", "number": 440}],
                    },
                    {"number": 440, "blockedBy": [{"repo": "api-gateway", "number": 431}], "blocking": []},
                    {"number": 451, "blockedBy": [], "blocking": []},
                ]
            },
            "cycles": [],
            "warnings": {},
        }
        return open_issues, dag

    def test_dag_renders_edges_glyphs_legend_and_blocking_summary(self):
        open_issues, dag = self._dag_model()
        with mock.patch.object(roadmap, "_repo_identity", side_effect=_identity()), \
             mock.patch.object(
                 roadmap, "cached_github",
                 side_effect=lambda repo, path, state="open", **kw: (
                     (open_issues.get(repo, []), {}, []) if state == "open" else ([], {}, [])
                 ),
             ), \
             mock.patch.object(roadmap, "cached_dependency_dag", side_effect=lambda repos, code_dir: dag):
            text, code = roadmap_cli.run(
                "api-gateway", 10, "ready", True, False, False, claims_lookup=lambda repos: {}
            )

        self.assertEqual(code, 0)
        self.assertIn("#418 ● retry backoff", text)
        self.assertIn("└─> #431", text)
        self.assertIn("(no dependencies)", text)  # #451 has no edges at all
        self.assertIn("● ready  ◐ claimed  ✕ blocked", text)
        self.assertIn("Blocking the most:", text)
        self.assertIn("Next ready on the critical path: #418", text)

    def test_dag_multi_repo_labels_include_repo_name(self):
        open_issues = {
            "api-gateway": [_issue(10, "api task", "P1")],
            "billing-core": [_issue(20, "billing task", "P1")],
        }
        dag = {
            "repos": {
                "api-gateway": [
                    {"number": 10, "blockedBy": [], "blocking": [{"repo": "billing-core", "number": 20}]}
                ],
                "billing-core": [
                    {"number": 20, "blockedBy": [{"repo": "api-gateway", "number": 10}], "blocking": []}
                ],
            },
            "cycles": [],
            "warnings": {},
        }
        with mock.patch.object(roadmap, "_repo_identity", side_effect=_identity()), \
             mock.patch.object(
                 roadmap, "cached_github",
                 side_effect=lambda repo, path, state="open", **kw: (
                     (open_issues.get(repo, []), {}, []) if state == "open" else ([], {}, [])
                 ),
             ), \
             mock.patch.object(roadmap, "cached_dependency_dag", side_effect=lambda repos, code_dir: dag):
            text, code = roadmap_cli.run(
                None, 10, "ready", True, False, False,
                enabled_repos=lambda: ["api-gateway", "billing-core"],
                claims_lookup=lambda repos: {},
            )

        self.assertEqual(code, 0)
        self.assertIn("api-gateway#10", text)
        self.assertIn("billing-core#20", text)

    def test_cycle_is_reported_as_error_and_blocks_rendering(self):
        open_issues = {
            "api-gateway": [_issue(431, "rate-limit headers", "P2"), _issue(440, "docs", "P3")]
        }
        dag = {
            "repos": {
                "api-gateway": [
                    {"number": 431, "blockedBy": [{"repo": "api-gateway", "number": 440}], "blocking": []},
                    {"number": 440, "blockedBy": [{"repo": "api-gateway", "number": 431}], "blocking": []},
                ]
            },
            "cycles": [
                [
                    {"repo": "api-gateway", "number": 431},
                    {"repo": "api-gateway", "number": 440},
                    {"repo": "api-gateway", "number": 431},
                ]
            ],
            "warnings": {},
        }
        with mock.patch.object(roadmap, "_repo_identity", side_effect=_identity()), \
             mock.patch.object(
                 roadmap, "cached_github",
                 side_effect=lambda repo, path, state="open", **kw: (
                     (open_issues.get(repo, []), {}, []) if state == "open" else ([], {}, [])
                 ),
             ), \
             mock.patch.object(roadmap, "cached_dependency_dag", side_effect=lambda repos, code_dir: dag):
            text, code = roadmap_cli.run(
                "api-gateway", 10, "ready", True, False, False, claims_lookup=lambda repos: {}
            )

        self.assertEqual(code, 1)
        self.assertEqual(
            text, "error: dependency cycle #431 -> #440 -> #431. Fix the links in the tracker."
        )


class JsonOutputTests(unittest.TestCase):
    def test_json_mirrors_filtered_list(self):
        open_issues = {"api-gateway": [_issue(418, "retry backoff", "P1")]}
        dag = {
            "repos": {"api-gateway": [{"number": 418, "blockedBy": [], "blocking": []}]},
            "cycles": [],
            "warnings": {},
        }
        with mock.patch.object(roadmap, "_repo_identity", side_effect=_identity()), \
             mock.patch.object(
                 roadmap, "cached_github",
                 side_effect=lambda repo, path, state="open", **kw: (
                     (open_issues.get(repo, []), {}, []) if state == "open" else ([], {}, [])
                 ),
             ), \
             mock.patch.object(roadmap, "cached_dependency_dag", side_effect=lambda repos, code_dir: dag):
            text, code = roadmap_cli.run(
                "api-gateway", 10, "ready", False, True, False, claims_lookup=lambda repos: {}
            )

        import json

        payload = json.loads(text)
        self.assertEqual(code, 0)
        self.assertEqual(payload["repos"]["api-gateway"]["ready"], 1)
        self.assertEqual(payload["repos"]["api-gateway"]["issues"][0]["number"], 418)
        self.assertEqual(payload["warnings"], [])
        self.assertEqual(payload["cycles"], [])


def _empty_model(repos, **_kwargs):
    return {"repos": {repo: [] for repo in repos}, "cycles": [], "warnings": []}


class RepoArgTests(unittest.TestCase):
    def test_owner_repo_uses_the_short_checkout_name(self):
        with tempfile.TemporaryDirectory() as code_dir:
            os.mkdir(os.path.join(code_dir, "bodysmith"))
            with mock.patch.object(
                roadmap_cli, "build_roadmap", side_effect=_empty_model
            ) as build:
                text, code = roadmap_cli.run(
                    "acme/bodysmith", 10, "ready", False, False, False,
                    code_dir=code_dir, claims_lookup=lambda repos: {},
                )

        self.assertEqual(code, 0)
        self.assertEqual(build.call_args.args[0], ["bodysmith"])

    def test_owner_repo_without_checkout_exits_with_error(self):
        with tempfile.TemporaryDirectory() as code_dir:
            with mock.patch.object(roadmap_cli, "build_roadmap") as build:
                text, code = roadmap_cli.run(
                    "acme/bodysmith", 10, "ready", False, False, False,
                    code_dir=code_dir, claims_lookup=lambda repos: {},
                )

        self.assertEqual(code, 2)
        self.assertIn("short name", text)
        build.assert_not_called()


if __name__ == "__main__":
    unittest.main()
