import json
import unittest
from unittest import mock

from lupin import review_dispatch


class DispatchPlanTests(unittest.TestCase):
    def test_bmo_model_takes_the_omp_lock(self):
        self.assertEqual(review_dispatch.dispatch_plan("bmo:qwen3.8-flash-next"), "omp_lock")

    def test_local_model_takes_the_omp_lock(self):
        self.assertEqual(
            review_dispatch.dispatch_plan("local:deepseek-v4-flash-0731"), "omp_lock"
        )

    def test_claude_model_defaults_to_same_unit(self):
        self.assertEqual(review_dispatch.dispatch_plan("sonnet"), "same_unit")

    def test_claude_model_in_separate_mode_takes_the_claude_lock(self):
        self.assertEqual(
            review_dispatch.dispatch_plan("opus", mode="separate"), "claude_lock"
        )

    def test_bmo_model_ignores_mode(self):
        # The lock a bmo/local model needs is about the shared LM Studio
        # server, not about where the caller is running -- "separate" must
        # not override it to claude_lock.
        self.assertEqual(
            review_dispatch.dispatch_plan("bmo:qwen3.8-flash-next", mode="separate"),
            "omp_lock",
        )


class DecideTests(unittest.TestCase):
    def test_small_coding_issue_has_model_effort_and_plan(self):
        # No injected tiers dict -- decide() goes through route.route(),
        # which falls back to the real model-tiers.json, same as
        # test_route.py's test_loads_the_real_model_tiers_json.
        result = review_dispatch.decide("coding", "size-xs")
        self.assertIn("model", result)
        self.assertIn("effort", result)
        self.assertIn("plan", result)

    def test_bmo_unavailable_falls_back_to_claude_same_unit(self):
        result = review_dispatch.decide(
            "coding", "size-xs", bmo_available=False, mode="same-unit"
        )
        self.assertEqual(result["plan"], "same_unit")
        self.assertNotIn(":", result["model"])


class MainCliTests(unittest.TestCase):
    def test_category_and_size_print_json_with_plan(self):
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = review_dispatch.main(["--category", "coding", "--size", "size-m"])
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["category"], "coding")
        self.assertEqual(payload["size"], "size-m")
        self.assertIn("model", payload)
        self.assertIn("plan", payload)

    def test_issue_json_is_classified_first(self):
        import contextlib
        import io
        import tempfile
        import os

        issue = {
            "title": "Translate the onboarding copy",
            "body": "We need localization for the settings page.",
            "labels": [{"name": "size-s"}],
        }
        fd, path = tempfile.mkstemp(suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(issue, handle)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = review_dispatch.main(["--issue-json", path])
            self.assertEqual(rc, 0)
            payload = json.loads(buf.getvalue())
            self.assertEqual(payload["category"], "translation")
        finally:
            os.remove(path)


def _fake_run_gh_json(args, *, repo=None, timeout=60):
    # args looks like ["issue", "view", ITEM, "--json", "body,comments"] or
    # ["pr", "view", ITEM, "--json", "body,comments,reviews,files"].
    kind, item = args[0], args[2]
    if kind == "issue":
        if item == "1":
            return {"body": "issue body", "comments": [{"body": "a comment"}]}, None
        return None, "GraphQL: Could not resolve to an Issue"
    if kind == "pr":
        if item == "2":
            return (
                {
                    "body": "pr body",
                    "comments": [],
                    "reviews": [],
                    "files": [{"path": "a.py", "additions": 3, "deletions": 1}],
                },
                None,
            )
        return None, "no pull requests found"
    raise AssertionError(f"unexpected gh args: {args}")


class PrefetchTests(unittest.TestCase):
    def test_issue_number_returns_body_and_comments(self):
        with mock.patch.object(review_dispatch, "_run_gh_json", side_effect=_fake_run_gh_json):
            result = review_dispatch.prefetch(["1"])
        self.assertEqual(result["1"]["kind"], "issue")
        self.assertEqual(result["1"]["body"], "issue body")
        self.assertEqual(len(result["1"]["comments"]), 1)

    def test_pr_number_returns_body_comments_reviews_and_diff_stat_only(self):
        with mock.patch.object(review_dispatch, "_run_gh_json", side_effect=_fake_run_gh_json):
            result = review_dispatch.prefetch(["2"])
        self.assertEqual(result["2"]["kind"], "pr")
        self.assertEqual(result["2"]["body"], "pr body")
        self.assertIn("reviews", result["2"])
        self.assertEqual(
            result["2"]["diff_stat"], [{"path": "a.py", "additions": 3, "deletions": 1}]
        )
        # "files" is the raw gh field name; it's renamed to diff_stat, not
        # duplicated, and never carries full diff text either way.
        self.assertNotIn("files", result["2"])

    def test_mixed_issue_and_pr_numbers(self):
        with mock.patch.object(review_dispatch, "_run_gh_json", side_effect=_fake_run_gh_json):
            result = review_dispatch.prefetch(["1", "2"])
        self.assertEqual(result["1"]["kind"], "issue")
        self.assertEqual(result["2"]["kind"], "pr")

    def test_number_that_is_neither_gets_an_error_entry_not_a_crash(self):
        with mock.patch.object(review_dispatch, "_run_gh_json", side_effect=_fake_run_gh_json):
            result = review_dispatch.prefetch(["999"])
        self.assertIn("error", result["999"])
        self.assertNotIn("kind", result["999"])

    def test_main_prefetch_prints_one_json_blob_keyed_by_number(self):
        import contextlib
        import io

        buf = io.StringIO()
        with mock.patch.object(review_dispatch, "_run_gh_json", side_effect=_fake_run_gh_json):
            with contextlib.redirect_stdout(buf):
                rc = review_dispatch.main(["--prefetch", "1,2"])
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(set(payload), {"1", "2"})
        self.assertEqual(payload["1"]["kind"], "issue")
        self.assertEqual(payload["2"]["kind"], "pr")

    def test_prefetch_is_exclusive_of_category_requirement(self):
        # --prefetch alone must not trip the "one of --category or
        # --issue-json is required" check.
        import contextlib
        import io

        with mock.patch.object(review_dispatch, "_run_gh_json", side_effect=_fake_run_gh_json):
            with contextlib.redirect_stdout(io.StringIO()):
                rc = review_dispatch.main(["--prefetch", "1"])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
