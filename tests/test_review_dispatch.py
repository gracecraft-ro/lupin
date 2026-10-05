import json
import unittest

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


if __name__ == "__main__":
    unittest.main()
