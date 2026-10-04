import unittest

from lupin import route

_TIERS = {
    "coding": {
        "tiers": {
            "tier0": [
                {"model": "bmo:qwen3.8-flash-next", "effort": "low"},
                {"model": "local:deepseek-v4-flash-0731", "effort": "low"},
            ],
            "tier1": [
                {"model": "sonnet", "effort": "medium"},
                {"model": "sonnet", "effort": "high"},
            ],
            "tier2": [
                {"model": "opus", "effort": "high"},
                {"model": "opus", "effort": "xhigh"},
            ],
        }
    },
    "frontend-ui": {
        "tiers": {
            "tier1": [{"model": "sonnet", "effort": "high"}],
            "tier2": [
                {"model": "opus", "effort": "high"},
                {"model": "opus", "effort": "xhigh"},
            ],
        }
    },
    "translation": {
        "tiers": {
            "tier0": [{"model": "local:deepseek-v4-flash-0731", "effort": "medium"}],
            "tier1": [{"model": "sonnet", "effort": "medium"}],
            "tier2": [{"model": "opus", "effort": "high"}],
        }
    },
    "cad-spatial": {
        "tiers": {
            # Mirrors cad-spatial's real shape in model-tiers.json: local is
            # listed first, bmo second -- the opposite order from "coding"/
            # "general". A bmo-dependency check that only looks at index 0
            # would miss this row entirely.
            "tier0": [
                {"model": "local:deepseek-v4-flash-0731", "effort": "medium"},
                {"model": "bmo:qwen3.8-flash-next", "effort": "medium"},
            ],
            "tier1": [{"model": "sonnet", "effort": "high"}],
            "tier2": [{"model": "opus", "effort": "high"}],
        }
    },
}


class RouteTests(unittest.TestCase):
    def test_small_coding_issue_gets_tier0_bmo(self):
        result = route.route("coding", "size-xs", tiers=_TIERS)

        self.assertEqual(result, {"model": "bmo:qwen3.8-flash-next", "effort": "low"})

    def test_medium_coding_issue_gets_tier1_sonnet(self):
        result = route.route("coding", "size-m", tiers=_TIERS)

        self.assertEqual(result, {"model": "sonnet", "effort": "medium"})

    def test_large_coding_issue_gets_tier2_opus(self):
        result = route.route("coding", "size-l", tiers=_TIERS)

        self.assertEqual(result, {"model": "opus", "effort": "high"})

    def test_xl_coding_issue_gets_tier2_opus(self):
        result = route.route("coding", "size-xl", tiers=_TIERS)

        self.assertEqual(result, {"model": "opus", "effort": "high"})

    def test_translation_lookup_uses_its_own_row(self):
        result = route.route("translation", "size-xs", tiers=_TIERS)

        self.assertEqual(
            result, {"model": "local:deepseek-v4-flash-0731", "effort": "medium"}
        )

    def test_no_tier0_row_escalates_small_issue_to_tier1(self):
        # frontend-ui has no tier0 -- a small UI issue still lands on tier1,
        # not a KeyError and not a silent drop to a cheaper, nonexistent tier.
        result = route.route("frontend-ui", "size-xs", tiers=_TIERS)

        self.assertEqual(result, {"model": "sonnet", "effort": "high"})

    def test_bmo_timeout_falls_back_to_tier1_not_local(self):
        # Small/mechanical coding issue would normally get bmo (tier0). A
        # timed-out bmo lock skips tier0 entirely and lands on tier1 Sonnet,
        # per #179 -- not on tier0's own local fallback entry.
        result = route.route("coding", "size-xs", bmo_available=False, tiers=_TIERS)

        self.assertEqual(result, {"model": "sonnet", "effort": "medium"})

    def test_bmo_available_keeps_tier0_pick(self):
        result = route.route("coding", "size-xs", bmo_available=True, tiers=_TIERS)

        self.assertEqual(result, {"model": "bmo:qwen3.8-flash-next", "effort": "low"})

    def test_non_bmo_tier0_is_unaffected_by_bmo_availability(self):
        # translation's tier0 is local-only; a bmo timeout has nothing to do
        # with it, so it should not get bumped to tier1.
        result = route.route(
            "translation", "size-xs", bmo_available=False, tiers=_TIERS
        )

        self.assertEqual(
            result, {"model": "local:deepseek-v4-flash-0731", "effort": "medium"}
        )

    def test_bmo_timeout_falls_back_even_when_bmo_is_not_tier0s_first_entry(self):
        # Regression for the index-0 bug: cad-spatial's tier0 lists local
        # first and bmo second. A check that only looked at row["tier0"][0]
        # would see "local:..." and wrongly conclude this row has no bmo
        # dependency, skipping the fallback and returning a bmo model
        # anyway. It must still fall back to tier1 here.
        result = route.route(
            "cad-spatial", "size-xs", bmo_available=False, tiers=_TIERS
        )

        self.assertEqual(result, {"model": "sonnet", "effort": "high"})

    def test_high_effort_primary_does_not_escalate_the_review(self):
        # The primary pass already ran at "high". The review still gets
        # tier1's base entry (medium), not the high-effort entry -- it
        # doesn't need to match, by design (#179 §4).
        result = route.route("coding", "size-m", primary_effort="high", tiers=_TIERS)

        self.assertEqual(result, {"model": "sonnet", "effort": "medium"})

    def test_xhigh_effort_primary_does_not_escalate_the_review(self):
        result = route.route("coding", "size-l", primary_effort="xhigh", tiers=_TIERS)

        self.assertEqual(result, {"model": "opus", "effort": "high"})

    def test_unknown_size_defaults_to_tier2(self):
        # Unknown is the least certain case -- it gets the most capable
        # model, not the cheapest. This was a bug (silently landed on tier1)
        # fixed independently of the tier0-widening question below.
        result = route.route("coding", "size-?", tiers=_TIERS)

        self.assertEqual(result, {"model": "opus", "effort": "high"})

    def test_loads_the_real_model_tiers_json(self):
        # No injected tiers dict -- confirms the default path actually
        # resolves to the packaged model-tiers.json.
        result = route.route("coding", "size-m")

        self.assertEqual(result, {"model": "sonnet", "effort": "medium"})


if __name__ == "__main__":
    unittest.main()
