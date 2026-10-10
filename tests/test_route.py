import copy
import itertools
import json
import time
import unittest
from importlib import resources

from lupin import route
from lupin.quota import QuotaDuration

HOUR_MS = 60 * 60 * 1000
MIN_MS = 60 * 1000

_PROVIDERS = ("claude", "opencode-go", "openai")
_SIZES = ("size-xs", "size-s", "size-m", "size-l", "size-xl", "size-?")
_STATES = ("normal", "blocked", "surplus")


def _grid_rows(states):
    """quota_rows for one grid point: `states` gives each provider's state."""
    now_ms = time.time() * 1000
    rows = []
    for provider, state in zip(_PROVIDERS, states):
        if state == "blocked":
            rows.append({"provider": provider, "duration": QuotaDuration.FIVE_HOURS,
                         "used_pct": 100, "resets_at": now_ms + 10 * HOUR_MS})
        elif state == "surplus":
            rows.append({"provider": provider, "duration": QuotaDuration.FIVE_HOURS,
                         "used_pct": 10, "resets_at": now_ms + HOUR_MS})
    return rows


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
    "prose": {
        "tiers": {
            # tier2 lists opus and fable. Both run on the claude account.
            "tier1": [{"model": "sonnet", "effort": "high"}],
            "tier2": [
                {"model": "opus", "effort": "high"},
                {"model": "fable", "effort": "high"},
            ],
        }
    },
}

# prose with a second account in tier2. The fallback tests inject this row.
_PROSE_OTHER_ACCOUNT = {
    "prose": {
        "tiers": {
            "tier1": [{"model": "sonnet", "effort": "high"}],
            "tier2": [
                {"model": "opus", "effort": "high"},
                {"model": "opencode-go/glm-5.3", "effort": "high"},
            ],
        }
    },
}


class RouteTests(unittest.TestCase):
    def test_small_coding_issue_gets_tier0_bmo(self):
        result = route.route("coding", "size-xs", tiers=_TIERS, quota_rows=[])

        self.assertEqual(result, {"model": "bmo:qwen3.8-flash-next", "effort": "low"})

    def test_medium_coding_issue_gets_tier1_sonnet(self):
        result = route.route("coding", "size-m", tiers=_TIERS, quota_rows=[])

        self.assertEqual(result, {"model": "sonnet", "effort": "medium"})

    def test_large_coding_issue_gets_tier2_opus(self):
        result = route.route("coding", "size-l", tiers=_TIERS, quota_rows=[])

        self.assertEqual(result, {"model": "opus", "effort": "high"})

    def test_xl_coding_issue_gets_tier2_opus(self):
        result = route.route("coding", "size-xl", tiers=_TIERS, quota_rows=[])

        self.assertEqual(result, {"model": "opus", "effort": "high"})

    def test_translation_lookup_uses_its_own_row(self):
        result = route.route("translation", "size-xs", tiers=_TIERS, quota_rows=[])

        self.assertEqual(
            result, {"model": "local:deepseek-v4-flash-0731", "effort": "medium"}
        )

    def test_no_tier0_row_escalates_small_issue_to_tier1(self):
        # frontend-ui has no tier0 -- a small UI issue still lands on tier1,
        # not a KeyError and not a silent drop to a cheaper, nonexistent tier.
        result = route.route("frontend-ui", "size-xs", tiers=_TIERS, quota_rows=[])

        self.assertEqual(result, {"model": "sonnet", "effort": "high"})

    def test_bmo_timeout_falls_back_to_tier1_not_local(self):
        # Small/mechanical coding issue would normally get bmo (tier0). A
        # timed-out bmo lock skips tier0 entirely and lands on tier1 Sonnet,
        # per #179 -- not on tier0's own local fallback entry.
        result = route.route(
            "coding", "size-xs", bmo_available=False, tiers=_TIERS, quota_rows=[]
        )

        self.assertEqual(result, {"model": "sonnet", "effort": "medium"})

    def test_bmo_available_keeps_tier0_pick(self):
        result = route.route(
            "coding", "size-xs", bmo_available=True, tiers=_TIERS, quota_rows=[]
        )

        self.assertEqual(result, {"model": "bmo:qwen3.8-flash-next", "effort": "low"})

    def test_non_bmo_tier0_is_unaffected_by_bmo_availability(self):
        # translation's tier0 is local-only; a bmo timeout has nothing to do
        # with it, so it should not get bumped to tier1.
        result = route.route(
            "translation", "size-xs", bmo_available=False, tiers=_TIERS, quota_rows=[]
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
            "cad-spatial", "size-xs", bmo_available=False, tiers=_TIERS, quota_rows=[]
        )

        self.assertEqual(result, {"model": "sonnet", "effort": "high"})

    def test_high_effort_primary_does_not_escalate_the_review(self):
        # The primary pass already ran at "high". The review still gets
        # tier1's base entry (medium), not the high-effort entry -- it
        # doesn't need to match, by design (#179 §4).
        result = route.route(
            "coding", "size-m", primary_effort="high", tiers=_TIERS, quota_rows=[]
        )

        self.assertEqual(result, {"model": "sonnet", "effort": "medium"})

    def test_xhigh_effort_primary_does_not_escalate_the_review(self):
        result = route.route(
            "coding", "size-l", primary_effort="xhigh", tiers=_TIERS, quota_rows=[]
        )

        self.assertEqual(result, {"model": "opus", "effort": "high"})

    def test_unknown_size_defaults_to_tier2(self):
        # Unknown is the least certain case -- it gets the most capable
        # model, not the cheapest. This was a bug (silently landed on tier1)
        # fixed independently of the tier0-widening question below.
        result = route.route("coding", "size-?", tiers=_TIERS, quota_rows=[])

        self.assertEqual(result, {"model": "opus", "effort": "high"})

    def test_loads_the_real_model_tiers_json(self):
        # No injected tiers dict -- confirms the default path actually
        # resolves to the packaged model-tiers.json.
        result = route.route("coding", "size-m", quota_rows=[])

        self.assertEqual(result, {"model": "opencode-go/glm-5.3", "effort": "high"})


class RoutePacingTests(unittest.TestCase):
    """Issue #36: quota is fleet-wide, so route() paces on it directly.
    Grace's three literal rules (see pace.py), exercised through route()'s
    own tier/bmo logic -- test_pace.py covers the rules themselves.
    """

    def _row(self, provider, used_pct, resets_in_ms, duration=QuotaDuration.FIVE_HOURS):
        # route() uses the real clock internally (it has no `now` override),
        # so fixtures here anchor `resets_at` to the real current time too.
        return {
            "provider": provider,
            "duration": duration,
            "used_pct": used_pct,
            "resets_at": time.time() * 1000 + resets_in_ms,
        }

    def test_blocked_provider_falls_back_to_next_tiers_other_provider(self):
        # The injected tier2 lists opus (claude) then opencode-go/glm-5.3.
        # Claude blocked -> falls to the opencode-go model, not a wait and not
        # a local/bmo model (prose's tier1 has no local/bmo entry anyway, but
        # this exercises the "other entry in the same tier" branch either
        # way).
        rows = [self._row("claude", 100, 10 * HOUR_MS)]
        result = route.route("prose", "size-l", tiers=_PROSE_OTHER_ACCOUNT, quota_rows=rows)

        self.assertEqual(result["model"], "opencode-go/glm-5.3")
        self.assertEqual(result["downgraded_from"], {"model": "opus", "effort": "high"})

    def test_fable_on_a_blocked_claude_account_is_not_a_fallback(self):
        # fable is served by the claude account, the same account as opus.
        # Claude blocked -> no other account to fall to. Route must report a
        # wait on opus. It must not return fable.
        rows = [self._row("claude", 100, 10 * HOUR_MS)]
        tiers = {
            "prose": {
                "tiers": {
                    "tier1": [{"model": "sonnet", "effort": "high"}],
                    "tier2": [
                        {"model": "opus", "effort": "high"},
                        {"model": "fable", "effort": "high"},
                    ],
                }
            }
        }
        result = route.route("prose", "size-l", tiers=tiers, quota_rows=rows)

        self.assertEqual(result["model"], "opus")
        self.assertNotIn("downgraded_from", result)
        self.assertIsNotNone(result["wait_seconds"])

    def test_blocked_tier1_does_not_fall_back_to_a_tier2_on_the_same_blocked_account(self):
        # The bug this issue fixes: coding's tier1 (sonnet) and tier2 (opus)
        # are both "claude". A naive one-tier-down drop (the old
        # quota_exhausted behavior) would have landed on tier2's opus --
        # still claude, still blocked. The new fallback must recognize that
        # and keep searching instead of silently returning a blocked model.
        rows = [self._row("claude", 100, 10 * HOUR_MS)]
        result = route.route("coding", "size-m", tiers=_TIERS, quota_rows=rows)

        # coding has no other provider at any tier in this fixture -- every
        # candidate is claude, so route() must report a wait, not a model
        # it knows is still blocked.
        self.assertEqual(result["model"], "sonnet")
        self.assertIn("wait_seconds", result)
        self.assertIsNotNone(result["wait_seconds"])

    def test_wait_reports_the_earliest_reset_among_everything_tried(self):
        rows = [
            self._row("claude", 100, 5 * HOUR_MS),
            self._row("opencode-go", 100, 20 * MIN_MS),
        ]
        result = route.route("prose", "size-l", tiers=_PROSE_OTHER_ACCOUNT, quota_rows=rows)

        # opus (claude) is blocked, falls to opencode-go/glm-5.3 -- also
        # blocked, resetting sooner than claude. The wait must reflect that
        # sooner reset, not claude's (seen first).
        self.assertAlmostEqual(result["wait_seconds"], 20 * 60, delta=2)

    def test_does_not_fall_back_to_a_local_or_bmo_model_to_dodge_a_wait(self):
        # coding's tier0 is bmo/local, tier1 sonnet (claude), both blocked.
        # Decision 3 forbids landing on tier0 to avoid the wait.
        rows = [self._row("claude", 100, 3 * HOUR_MS)]
        result = route.route("coding", "size-m", tiers=_TIERS, quota_rows=rows)

        self.assertNotIn("bmo:", result["model"])
        self.assertNotIn("local:", result["model"])
        self.assertIsNotNone(result.get("wait_seconds"))

    def test_lean_in_bumps_one_tier_on_a_surplus(self):
        rows = [self._row("claude", 10, 1 * HOUR_MS)]  # <50% used, <24h left
        result = route.route("coding", "size-m", tiers=_TIERS, quota_rows=rows)

        self.assertEqual(result, {"model": "opus", "effort": "high"})

    def test_lean_in_under_30_minutes_to_reset_moves_up_on_the_same_provider(self):
        # 10% used with 20 minutes to reset: under the 30-minute rule, so the
        # pick leans in. The higher entry must be on the same provider, so
        # sonnet (claude) is skipped and qwen3.8-max is picked.
        tiers = {
            "coding": {
                "tiers": {
                    "tier1": [{"model": "opencode-go/glm-5.3", "effort": "high"}],
                    "tier2": [
                        {"model": "sonnet", "effort": "high"},
                        {"model": "opencode-go/qwen3.8-max", "effort": "high"},
                    ],
                }
            }
        }
        rows = [self._row("opencode-go", 10, 20 * 60 * 1000)]
        result = route.route("coding", "size-m", tiers=tiers, quota_rows=rows)

        self.assertEqual(result, {"model": "opencode-go/qwen3.8-max", "effort": "high"})

    def test_lean_in_never_goes_past_the_top_tier(self):
        rows = [self._row("claude", 10, 1 * HOUR_MS)]
        result = route.route("coding", "size-l", tiers=_TIERS, quota_rows=rows)

        self.assertEqual(result, {"model": "opus", "effort": "high"})

    def test_lean_in_and_fallback_stay_on_the_account_with_quota(self):
        # Quota is per account. A surplus leans only to a higher rung on the
        # same provider. A blocked provider falls to an entry on another one.
        glm = {"model": "opencode-go/glm-5.3", "effort": "high"}
        cases = {
            "surplus leans to the next rung on the same provider": (
                self._row("opencode-go", 10, 1 * HOUR_MS),
                [glm],
                [
                    {"model": "sonnet", "effort": "high"},
                    {"model": "opencode-go/qwen3.8-max", "effort": "high"},
                ],
                {"model": "opencode-go/qwen3.8-max", "effort": "high"},
            ),
            "surplus never leans onto another provider": (
                self._row("opencode-go", 10, 1 * HOUR_MS),
                [glm],
                [
                    {"model": "sonnet", "effort": "high"},
                    {"model": "opus", "effort": "high"},
                ],
                glm,
            ),
            "blocked provider falls to the first unblocked entry": (
                self._row("opencode-go", 100, 10 * HOUR_MS),
                [glm],
                [
                    {"model": "sonnet", "effort": "high"},
                    {"model": "opus", "effort": "high"},
                ],
                {"model": "sonnet", "effort": "high", "downgraded_from": glm},
            ),
        }
        for name, (row, tier1, tier2, expected) in cases.items():
            with self.subTest(case=name):
                tiers = {"coding": {"tiers": {"tier1": tier1, "tier2": tier2}}}
                result = route.route("coding", "size-m", tiers=tiers, quota_rows=[row])

                self.assertEqual({key: result.get(key) for key in expected}, expected)

    def test_empty_quota_rows_is_a_no_op(self):
        result = route.route("coding", "size-m", tiers=_TIERS, quota_rows=[])

        self.assertEqual(result, {"model": "sonnet", "effort": "medium"})


class PackagedTiersTests(unittest.TestCase):
    def test_every_packaged_tier_entry_can_be_picked(self):
        # An entry route() never returns is dead config. Give each entry a
        # unique effort tag, run the whole quota grid, and require every tag
        # to show up as a pick or as a downgraded_from.
        packaged = json.loads(
            resources.files("lupin").joinpath("model-tiers.json").read_text(encoding="utf-8")
        )
        states = itertools.product(_STATES, repeat=len(_PROVIDERS))
        grid = list(itertools.product(_SIZES, (True, False), states))
        for category, row in packaged.items():
            if category.startswith("_"):
                continue
            with self.subTest(category=category):
                probed = copy.deepcopy(row)
                probes = set()
                for tier_name, entries in probed["tiers"].items():
                    for index, entry in enumerate(entries):
                        entry["effort"] = f"__probe_{tier_name}_{index}__"
                        probes.add(entry["effort"])
                seen = set()
                for size, bmo, states in grid:
                    result = route.route(
                        category,
                        size,
                        bmo_available=bmo,
                        tiers={category: probed},
                        quota_rows=_grid_rows(states),
                    )
                    seen.add(result.get("effort"))
                    if "downgraded_from" in result:
                        seen.add(result["downgraded_from"]["effort"])

                self.assertEqual(probes - seen, set())


if __name__ == "__main__":
    unittest.main()
