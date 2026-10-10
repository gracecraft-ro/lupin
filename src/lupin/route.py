"""Pick a {model, effort} for a (category, size) pair from model-tiers.json.

Moved from ghostbook.nix's hosts/jesus/loopgui/route.py (issue #203, part of
#198's plan) at source commit 12c007705e63b08c363062487195c8003d5347d3.
Only the tiers-file lookup changed, from a path relative to this file's
location in a Nix store checkout, to a packaged resource (see `_load_tiers`
below) -- the routing logic itself is unchanged.

`category`/`size` come from classify.classify() (issue #183). This looks up
the matching row in model-tiers.json (issue #182), turns size into a tier,
and applies three adjustments:

- bmo can cold-sleep for ~300s. A caller that already tried the bmo lock with
  a short timeout (~20-30s) and gave up passes bmo_available=False here. If
  tier0 has a bmo: entry, tier0 is skipped and tier1 is used -- no block
  (#179 research).
- A review call doesn't need to match the primary pass's effort. If the
  primary ran at high/xhigh, the review still gets the tier's base (lowest)
  effort, to ease pressure on shared quota (#179 §4).
- Quota is fleet-wide, not per-machine (every provider is one account shared
  by the whole fleet, confirmed in issue #36) -- so this is also where quota
  pacing happens, via `pace.py`. See `_apply_pacing` below for the three
  rules (reserve, lean-in, exhaustion fallback). A lean-in moves to the
  nearest higher entry on the *same* provider. A surplus is spent on that
  account, never on another account's quota.

This module only decides; it never touches a lock, a scheduler, or a machine
itself.
"""

from __future__ import annotations

import json
import time
from importlib import resources

from . import pace
from . import quota as quota_mod

# Size -> tier. size-xs and size-s: tier0. size-m: tier1. size-l, size-xl
# and size-?: tier2. size-? is unknown, so it gets tier2, not the cheapest
# tier. See #179 §3.
_SIZE_TO_TIER = {
    "size-xs": "tier0",
    "size-s": "tier0",
    "size-m": "tier1",
    "size-l": "tier2",
    "size-xl": "tier2",
    "size-?": "tier2",
}

_TIER_ORDER = ("tier0", "tier1", "tier2")

# route()'s models, mapped to the quota-tracked provider that serves them.
# Moved here from place.py (issue #36) -- quota pacing is route()'s concern
# now, so it needs this mapping too. place.py still exposes the same name
# (`provider_for_model`) for its own, unrelated use (labeling a machine's
# quota entry), pointed at this copy instead of keeping its own.
_PROVIDER_BY_MODEL = {
    "sonnet": "claude",
    "opus": "claude",
    # fable is served by the claude account. It shares that account's quota
    # with sonnet and opus.
    "fable": "claude",
}


def provider_for_model(model: str) -> str:
    """Map a routed model to the provider `quota.py` tracks it under.

    The prefix is the same one `lupin run`/`lupin enable --orchestrator` use
    for a model selector, so `opencode-go/glm-5.3` is the opencode-go
    subscription's glm-5.3. A bare name (sonnet/opus/fable) is a Claude
    subscription alias.

    "bmo:"/"local:" models run on local/shared-GPU inference, not a cloud
    account with a quota reading -- `pace.py` never has rows for them, so
    they are never blocked. Falls back to the model name itself for
    anything this table doesn't know yet, rather than raising.
    """
    if model.startswith("bmo:"):
        return "bmo"
    if model.startswith("local:"):
        return "local"
    if model.startswith("opencode-go/"):
        return "opencode-go"
    if model.startswith("openai/"):
        return "openai"
    return _PROVIDER_BY_MODEL.get(model, model)


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


def _lean_in_entry(row: dict, tier: str, provider: str) -> dict | None:
    """First entry on `provider` in the nearest tier above `tier`, or None.

    Tiers are checked from the lowest one up. Entries are checked in list
    order. A lean-in must stay on one provider, so it never spends another
    account's quota. None means no tier above `tier` has an entry on
    `provider`, and the pick stays where it is.
    """
    for candidate in _TIER_ORDER[_TIER_ORDER.index(tier) + 1 :]:
        for entry in row.get(candidate, []):
            if provider_for_model(entry["model"]) == provider:
                return entry
    return None


def _fallback_entries(row: dict, tier: str) -> list[dict]:
    """Other entries route() can try once `tier`'s own pick is blocked.

    Order (Grace's decision 3, issue #36): the rest of `tier`'s own list
    first, then lower tiers (nearest first), then higher tiers (nearest
    first). Skips every bmo:/local: entry outright -- decision 3 is
    explicit that a blocked cloud provider waits for its reset rather than
    quietly falling back to a free local model.
    """
    tier_index = _TIER_ORDER.index(tier)
    tier_walk = (
        (tier,)
        + tuple(reversed(_TIER_ORDER[:tier_index]))
        + _TIER_ORDER[tier_index + 1 :]
    )
    entries = []
    for candidate_tier in tier_walk:
        if candidate_tier not in row:
            continue
        candidate_entries = row[candidate_tier][1:] if candidate_tier == tier else row[candidate_tier]
        for entry in candidate_entries:
            if entry["model"].startswith("bmo:") or entry["model"].startswith("local:"):
                continue
            entries.append(entry)
    return entries


def _apply_pacing(row: dict, tier: str, quota_rows: list[dict]) -> dict:
    """Quota pacing (issue #36's three rules, implemented in `pace.py`).

    A no-op when `quota_rows` is empty -- the caller already decided
    whether to pass live data. Otherwise: lean up to the nearest higher
    entry on a provider's surplus (decision 2, skipped if blocked, and
    always staying on that same provider -- see `_lean_in_entry`), then, if
    the resulting pick is blocked (decision 1), search for another viable
    entry (decision 3).

    Returns `{"model", "effort"}` on an ordinary pick, plus
    `"downgraded_from"` when decision 3 had to move off a blocked pick, or
    plus `"wait_seconds"` (and no change to `"model"`/`"effort"`) when
    every entry decision 3 tried -- the original blocked pick included --
    is itself blocked. Waiting for the reset is Grace's call for that case:
    no silent fallback to a bmo: or local: model to dodge it.
    """
    choice = row[tier][0]
    if not quota_rows:
        return {"model": choice["model"], "effort": choice["effort"]}

    now_ms = time.time() * 1000
    provider = provider_for_model(choice["model"])
    is_blocked = pace.blocked(quota_rows, provider, now_ms)

    if not is_blocked and pace.lean_in(quota_rows, provider, now_ms):
        # `_lean_in_entry` only ever returns an entry on `provider` itself, so
        # the block check above already covers the rung it moves to; nothing
        # to re-check here.
        lean_choice = _lean_in_entry(row, tier, provider)
        if lean_choice is not None:
            choice = lean_choice

    if not is_blocked:
        return {"model": choice["model"], "effort": choice["effort"]}

    blocked_choice = {"model": choice["model"], "effort": choice["effort"]}
    examined = [provider]
    for entry in _fallback_entries(row, tier):
        entry_provider = provider_for_model(entry["model"])
        if pace.blocked(quota_rows, entry_provider, now_ms):
            examined.append(entry_provider)
            continue
        return {"model": entry["model"], "effort": entry["effort"], "downgraded_from": blocked_choice}

    earliest = pace.earliest_reset(quota_rows, examined, now_ms)
    wait_seconds = max(0.0, (earliest - now_ms) / 1000) if earliest is not None else None
    return {**blocked_choice, "wait_seconds": wait_seconds}


def route(
    category: str,
    size: str,
    *,
    bmo_available: bool = True,
    primary_effort: str | None = None,
    tiers: dict | None = None,
    quota_rows: list[dict] | None = None,
) -> dict:
    """Return `{"model": ..., "effort": ...}` for a (category, size) pair.

    `bmo_available=False` models a timed-out bmo lock: if tier0 has a bmo:
    entry, skip tier0 and use tier1. `primary_effort` is the primary
    pass's effort level ("low"/"medium"/"high"/"xhigh"); it is accepted so
    a caller can log or assert on it, but it never escalates the result --
    a review call always gets the tier's base effort, never matching a
    high/xhigh primary. That de-escalation is the point (#179 §4), not an
    edge case.

    `quota_rows` is `quota.quota_usage()`'s row shape. `None` (the default)
    reads live quota for real; pass `[]` for "no data, don't change
    anything" (every pre-#36 test does this, to keep testing this
    function's tier/bmo logic undisturbed by live quota). See
    `_apply_pacing` for what a real `quota_rows` list does to the pick.
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

    live_quota_rows = quota_mod.quota_usage() if quota_rows is None else quota_rows
    return _apply_pacing(row, tier, live_quota_rows)
