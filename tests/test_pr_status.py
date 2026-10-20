import contextlib
import io
import json
import subprocess
import unittest
from unittest import mock

from lupin import cli
from lupin import pr_status
from lupin.slots import CoordinatorUnreachable


def _pr(**overrides):
    pr = {
        "number": 226,
        "title": "Add the queue view",
        "body": "A short description.",
        "isDraft": False,
        "headRefName": "feat/issue-175-pr-status",
        "headRefOid": "head-sha",
        "mergeable": "MERGEABLE",
        "reviewDecision": "APPROVED",
        "reviews": [],
        "comments": [],
        "statusCheckRollup": [],
        "reviewRequests": [],
    }
    pr.update(overrides)
    return pr


def _review(login, state, when, commit="head-sha"):
    return {
        "author": {"login": login},
        "state": state,
        "submittedAt": when,
        "commit_id": commit,
    }


class MergeTests(unittest.TestCase):
    def test_mergeable_is_clean(self):
        self.assertEqual(pr_status.analyze(_pr(), "o/r")["merge"], "clean")

    def test_conflicting_is_conflict(self):
        self.assertEqual(
            pr_status.analyze(_pr(mergeable="CONFLICTING"), "o/r")["merge"], "conflict"
        )

    def test_other_value_is_unknown(self):
        self.assertEqual(
            pr_status.analyze(_pr(mergeable="UNKNOWN"), "o/r")["merge"], "unknown"
        )


class ChecksTests(unittest.TestCase):
    def _checks(self, rollup):
        return pr_status.analyze(_pr(statusCheckRollup=rollup), "o/r")["checks"]

    def test_no_checks_is_none(self):
        self.assertEqual(self._checks([]), "none")

    def test_success_is_pass(self):
        self.assertEqual(self._checks([{"conclusion": "SUCCESS"}]), "pass")

    def test_failure_is_fail(self):
        self.assertEqual(self._checks([{"conclusion": "FAILURE"}]), "fail")

    def test_any_failing_state_wins_over_pending(self):
        self.assertEqual(
            self._checks(
                [
                    {"conclusion": "SUCCESS"},
                    {"conclusion": "ACTION_REQUIRED"},
                    {"state": "IN_PROGRESS"},
                ]
            ),
            "fail",
        )

    def test_pending_is_pending(self):
        self.assertEqual(self._checks([{"conclusion": "PENDING"}]), "pending")

    def test_state_pending_is_pending(self):
        self.assertEqual(self._checks([{"state": "QUEUED"}]), "pending")

    def test_check_without_state_is_pending(self):
        self.assertEqual(self._checks([{"name": "no result yet"}]), "pending")

    def test_unknown_conclusion_still_passes(self):
        # A neutral or skipped check is not a failure and not pending.
        self.assertEqual(self._checks([{"conclusion": "NEUTRAL"}]), "pass")


class ApprovalTests(unittest.TestCase):
    def test_approval_on_head(self):
        row = pr_status.analyze(
            _pr(reviews=[_review("alice", "APPROVED", "2026-01-01T00:00:00Z")]),
            "o/r",
        )
        self.assertTrue(row["approval_at_head"])
        self.assertEqual(row["approval"], "head")

    def test_approval_before_head_is_stale(self):
        row = pr_status.analyze(
            _pr(
                reviews=[
                    _review("alice", "APPROVED", "2026-01-01T00:00:00Z", "old-sha")
                ]
            ),
            "o/r",
        )
        self.assertFalse(row["approval_at_head"])
        self.assertEqual(row["approval"], "stale")

    def test_no_approval_is_none(self):
        row = pr_status.analyze(
            _pr(reviews=[_review("alice", "CHANGES_REQUESTED", "2026-01-01T00:00:00Z")]),
            "o/r",
        )
        self.assertFalse(row["approval_at_head"])
        self.assertEqual(row["approval"], "none")

    def test_newest_approval_decides(self):
        row = pr_status.analyze(
            _pr(
                reviews=[
                    _review("alice", "APPROVED", "2026-01-02T00:00:00Z", "old-sha"),
                    _review("bob", "APPROVED", "2026-01-03T00:00:00Z", "head-sha"),
                ]
            ),
            "o/r",
        )
        self.assertTrue(row["approval_at_head"])

    def test_nested_commit_object_is_read(self):
        row = pr_status.analyze(
            _pr(
                reviews=[
                    {
                        "author": {"login": "alice"},
                        "state": "APPROVED",
                        "submittedAt": "2026-01-01T00:00:00Z",
                        "commit": {"oid": "head-sha"},
                    }
                ]
            ),
            "o/r",
        )
        self.assertTrue(row["approval_at_head"])


class ReviewTests(unittest.TestCase):
    def test_latest_review_per_reviewer(self):
        row = pr_status.analyze(
            _pr(
                reviews=[
                    _review("alice", "CHANGES_REQUESTED", "2026-01-01T00:00:00Z"),
                    _review("alice", "APPROVED", "2026-01-02T00:00:00Z"),
                    _review("bob", "COMMENTED", "2026-01-01T12:00:00Z"),
                ]
            ),
            "o/r",
        )
        latest = {review["login"]: review for review in row["latest_reviews"]}
        self.assertEqual(latest["alice"]["state"], "APPROVED")
        self.assertEqual(latest["alice"]["submitted_at"], "2026-01-02T00:00:00Z")
        self.assertEqual(latest["bob"]["state"], "COMMENTED")

    def test_review_requests_include_users_and_teams(self):
        row = pr_status.analyze(
            _pr(
                reviewRequests=[
                    {"login": "alice"},
                    {"name": "Reviewers"},
                    {"slug": "core-team"},
                ]
            ),
            "o/r",
        )
        self.assertEqual(row["review_requests"], ["alice", "Reviewers", "core-team"])


class InformalApprovalTests(unittest.TestCase):
    def _comments(self, bodies):
        return [
            {
                "author": {"login": f"user{i}"},
                "createdAt": f"2026-01-0{i + 1}T00:00:00Z",
                "body": body,
            }
            for i, body in enumerate(bodies)
        ]

    def test_verdict_approve_matches(self):
        row = pr_status.analyze(
            _pr(comments=self._comments(["Looks good.\nReview verdict: APPROVE"])), "o/r"
        )
        self.assertEqual(len(row["informal_approvals"]), 1)
        approval = row["informal_approvals"][0]
        self.assertEqual(approval["login"], "user0")
        self.assertEqual(approval["line"], "Review verdict: APPROVE")

    def test_case_insensitive_ship_matches(self):
        row = pr_status.analyze(
            _pr(comments=self._comments(["  verdict: Ship it"])), "o/r"
        )
        self.assertEqual(row["informal_approvals"][0]["line"], "verdict: Ship it")

    def test_other_verdict_does_not_match(self):
        row = pr_status.analyze(
            _pr(comments=self._comments(["Review verdict: REJECT", "Verdict: maybe"])),
            "o/r",
        )
        self.assertEqual(row["informal_approvals"], [])


class PreviewTests(unittest.TestCase):
    LONG = "word " * 60

    def test_description_is_truncated_by_default(self):
        row = pr_status.analyze(_pr(body=self.LONG), "o/r")
        self.assertLessEqual(len(row["description"]), 160)
        self.assertTrue(row["description"].endswith("…"))

    def test_full_keeps_the_whole_description(self):
        row = pr_status.analyze(_pr(body=self.LONG), "o/r", full=True)
        self.assertEqual(row["description"], " ".join(self.LONG.split()))
        self.assertNotIn("…", row["description"])

    def test_latest_comment_preview_is_truncated(self):
        pr = _pr(
            comments=[
                {"author": {"login": "alice"}, "createdAt": "2026-01-01T00:00:00Z", "body": self.LONG}
            ]
        )
        row = pr_status.analyze(pr, "o/r")
        self.assertLessEqual(len(row["latest_comment"]["body"]), 160)
        self.assertEqual(row["latest_comment"]["login"], "alice")

    def test_latest_comment_uses_newest_time(self):
        pr = _pr(
            comments=[
                {"author": {"login": "old"}, "createdAt": "2026-01-01T00:00:00Z", "body": "old"},
                {"author": {"login": "new"}, "createdAt": "2026-01-02T00:00:00Z", "body": "new"},
            ]
        )
        row = pr_status.analyze(pr, "o/r")
        self.assertEqual(row["latest_comment"]["login"], "new")


class MainTests(unittest.TestCase):
    def _run(self, argv, prs, claims=None, warning=None):
        stdout = io.StringIO()
        with mock.patch.object(pr_status, "_fetch_prs", return_value=(prs, None)), \
             mock.patch.object(pr_status, "_read_claims", return_value=(claims or {}, warning)), \
             contextlib.redirect_stdout(stdout):
            code = pr_status.main(["--repo", "o/r", *argv])
        return code, stdout.getvalue()

    def test_text_line_has_every_column(self):
        pr = _pr(
            reviews=[_review("alice", "APPROVED", "2026-01-01T00:00:00Z")],
            comments=[
                {"author": {"login": "bob"}, "createdAt": "2026-01-01T00:00:00Z", "body": "hi"}
            ],
        )
        code, text = self._run([], [pr], claims={"o/r#226": {"session": "holder-1"}})
        self.assertEqual(code, 0)
        self.assertIn(
            "#226  merge=clean  checks=none  review=APPROVED  approval=head  "
            "claim=holder-1  comments=1  Add the queue view",
            text,
        )

    def test_claim_absent_is_a_dash(self):
        code, text = self._run([], [_pr()])
        self.assertEqual(code, 0)
        self.assertIn("claim=-", text)

    def test_claim_unknown_adds_a_warning(self):
        code, text = self._run([], [_pr()], warning="claim data is unavailable: boom.")
        self.assertEqual(code, 0)
        self.assertIn("claim=unknown", text)
        self.assertIn("warning: claim data is unavailable: boom.", text)

    def test_json_has_the_table_fields_and_warnings(self):
        code, text = self._run(["--json"], [_pr()], warning="redis down")
        self.assertEqual(code, 0)
        payload = json.loads(text)
        self.assertEqual(payload["repo"], "o/r")
        self.assertEqual(payload["warnings"], ["redis down"])
        row = payload["prs"][0]
        for field in (
            "number",
            "title",
            "merge",
            "checks",
            "review",
            "approval",
            "claim",
            "comments_count",
            "description",
            "latest_comment",
        ):
            self.assertIn(field, row)

    def test_json_full_keeps_the_long_text(self):
        code, text = self._run(["--json", "--full"], [_pr(body=PreviewTests.LONG)])
        self.assertEqual(code, 0)
        row = json.loads(text)["prs"][0]
        self.assertEqual(row["description"], " ".join(PreviewTests.LONG.split()))

    def test_informal_approval_is_listed_after_the_table(self):
        pr = _pr(
            comments=[
                {
                    "author": {"login": "alice"},
                    "createdAt": "2026-01-01T00:00:00Z",
                    "body": "Review verdict: APPROVE",
                }
            ]
        )
        code, text = self._run([], [pr])
        self.assertEqual(code, 0)
        self.assertTrue(
            text.rstrip().endswith("#226: alice 2026-01-01T00:00:00Z: Review verdict: APPROVE")
        )

    def test_gh_error_exits_1_with_redacted_token(self):
        stderr = io.StringIO()
        with mock.patch.object(
            pr_status,
            "_fetch_prs",
            return_value=(None, "fatal: token ghp_supersecret rejected"),
        ), contextlib.redirect_stderr(stderr):
            code = pr_status.main(["--repo", "o/r"])
        self.assertEqual(code, 1)
        self.assertIn("[redacted]", stderr.getvalue())
        self.assertNotIn("ghp_supersecret", stderr.getvalue())

    def test_default_repo_uses_gh_resolution(self):
        with mock.patch.object(pr_status, "_resolve_repo", return_value=("o/r", None)), \
             mock.patch.object(pr_status, "_fetch_prs", return_value=([], None)) as fetch, \
             mock.patch.object(pr_status, "_read_claims", return_value=({}, None)), \
             contextlib.redirect_stdout(io.StringIO()):
            code = pr_status.main([])
        self.assertEqual(code, 0)
        fetch.assert_called_once_with("o/r")

    def test_unreachable_redis_keeps_the_command_alive(self):
        stdout = io.StringIO()
        with mock.patch.object(pr_status, "_fetch_prs", return_value=([_pr()], None)), \
             mock.patch.object(pr_status.machines, "resolve_connection", return_value={}), \
             mock.patch.object(
                 pr_status.claims, "claims_for", side_effect=CoordinatorUnreachable("down")
             ), \
             contextlib.redirect_stdout(stdout):
            code = pr_status.main(["--repo", "o/r"])
        self.assertEqual(code, 0)
        self.assertIn("claim=unknown", stdout.getvalue())
        self.assertIn("warning: claim data is unavailable: down.", stdout.getvalue())


class RunGhTests(unittest.TestCase):
    def test_error_redacts_a_github_token(self):
        completed = subprocess.CompletedProcess(
            ["gh"], 1, stdout="", stderr="fatal: https://ghp_abc123@github.com failed"
        )
        with mock.patch.object(subprocess, "run", return_value=completed):
            output, error = pr_status._run_gh(["pr", "list"])
        self.assertIsNone(output)
        self.assertIn("[redacted]", error)
        self.assertNotIn("ghp_abc123", error)


class CliRegistrationTests(unittest.TestCase):
    def test_parser_accepts_pr_status_flags(self):
        args = cli._build_parser().parse_args(
            ["pr-status", "--repo", "o/r", "--json", "--full"]
        )
        self.assertEqual(args.cmd, "pr-status")
        self.assertEqual(args.repo, "o/r")
        self.assertTrue(args.json)
        self.assertTrue(args.full)


if __name__ == "__main__":
    unittest.main()
