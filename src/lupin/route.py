"""Pick a {model, effort} for a (category, size) pair from model-tiers.json.

Moved from ghostbook.nix's hosts/jesus/loopgui/route.py (issue #203, part of
#198's plan) at source commit 12c007705e63b08c363062487195c8003d5347d3.
Only the tiers-file lookup changed, from a path relative to this file's
location in a Nix store checkout, to a packaged resource (see `_load_tiers`
below) -- the routing logic itself is unchanged.

`category`/`size` come from classify.classify() (issue #183). This looks up
the matching row in model-tiers.json (issue #182), turns size into a tier,
and applies two fallbacks from the #179 research:

- bmo can cold-sleep for ~300s. A caller that already tried the bmo lock with
  a short timeout (~20-30s) and gave up passes bmo_available=False here, and
  a tier0 pick that depends on bmo is skipped in favor of tier1 -- no block.
- A review call doesn't need to match the primary pass's effort. If the
  primary ran at high/xhigh, the review still gets the tier's base (lowest)
  effort, to ease pressure on shared quota.

This module only decides; it never touches a lock or a scheduler itself.
"""

from __future__ import annotations

import json
from importlib import resources

# size-xs/s: mechanical, low-risk -> free tier. size-m: default -> same-tier
# Sonnet. size-l/xl, size-? (unknown -- the least certain case, so it gets
# the most capable model, not the cheapest): frontier. See #179 §3.
_SIZE_TO_TIER = {
    "size-xs": "tier0",
    "size-s": "tier0",
    "size-m": "tier1",
    "size-l": "tier2",
    "size-xl": "tier2",
    "size-?": "tier2",
}

_TIER_ORDER = ("tier0", "tier1", "tier2")


def _load_tiers() -> dict:
    data = resources.files("lupin").joinpath("model-tiers.json").read_text(encoding="utf-8")
    return json.loads(data)


def _resolve_tier(tiers: dict, tier: str) -> str:
    """Return `tier` if this category has it, else the next higher one.

    Some rows (frontend-ui, prose) skip tier0 on purpose -- see the "note"
    field in model-tiers.json. Escalating up is the safe direction; there is
    no silent downgrade to a cheaper model the matrix didn't ask for.
    """
    for candidate in _TIER_ORDER[_TIER_ORDER.index(tier) :]:
        if candidate in tiers:
            return candidate
    raise KeyError(f"no tier at or above {tier!r} in this row")


def route(
    category: str,
    size: str,
    *,
    bmo_available: bool = True,
    quota_exhausted: bool = False,
    primary_effort: str | None = None,
    tiers: dict | None = None,
) -> dict:
    """Return `{"model": ..., "effort": ...}` for a (category, size) pair.

    `bmo_available=False` models a timed-out bmo lock: skip a bmo-dependent
    tier0 pick and use tier1 instead. `quota_exhausted=True` models a caller
    that already confirmed (elsewhere -- not this function's job) the
    resolved tier's provider is completely out of quota: drop one tier down,
    the same forced-not-speculative way `bmo_available=False` drops tier0 to
    tier1. The two compose: if a bmo-forced drop and a quota-forced drop both
    apply, each drops one tier, never two at once, and the lowest tier is
    never dropped below bounds. `primary_effort` is the primary pass's
    effort level ("low"/"medium"/"high"/"xhigh"); it is accepted so a caller
    can log or assert on it, but it never escalates the result -- a review
    call always gets the tier's base effort, never matching a high/xhigh
    primary. That de-escalation is the point (#179 §4), not an edge case.
    """
    all_tiers = tiers if tiers is not None else _load_tiers()
    row = all_tiers[category]["tiers"]

    tier = _resolve_tier(row, _SIZE_TO_TIER.get(size, "tier1"))
    if (
        tier == "tier0"
        and not bmo_available
        and any(entry["model"].startswith("bmo:") for entry in row["tier0"])
    ):
        tier = _resolve_tier(row, "tier1")

    if quota_exhausted:
        next_index = _TIER_ORDER.index(tier) + 1
        if next_index < len(_TIER_ORDER):
            tier = _resolve_tier(row, _TIER_ORDER[next_index])

    choice = row[tier][0]
    return {"model": choice["model"], "effort": choice["effort"]}
