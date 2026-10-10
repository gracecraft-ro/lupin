"""Quota pacing facts for `route()` (issue #36).

Every provider (Claude, opencode-go, Codex) is one account shared by the
whole fleet -- quota is never a per-machine signal, so this module takes
quota rows (`quota.quota_usage()`'s shape) and a timestamp, and answers two
questions for one provider at a time: is it blocked, and should a task
lean in (route up one tier to use a surplus before it resets)?

Pure. No stored state, no Redis, no file access -- every number comes from
the rows a caller passes in. `route.py` is the only caller; it decides what
to do with these facts (which tier, which fallback). This module just
reports them.

The literal rules come from issue #36 (comment of 2026-10-07 20:31 UTC).
The surplus test on the 30-minute lean-in comes from issue #99.
The abstract pacing-ratio design from issue #36's earlier comment is not
implemented here.

Boundary convention, used consistently below: every "less than" check is
strict (`<`), every "at or above" check is `>=`. An exact boundary value
(used_pct == 95, remaining == 3h, remaining == 30m, remaining == 24h, used_pct
== 50) always falls on the *conservative* side -- still reserved, still
blocked, no lean-in -- never on the side that spends more quota.
"""

from __future__ import annotations

from .quota import QuotaDuration

RESERVE_PCT = 95
RESERVE_GRACE_MS = 3 * 60 * 60 * 1000  # 3 hours
LEAN_IN_SOON_MS = 30 * 60 * 1000  # 30 minutes
LEAN_IN_LOW_USE_PCT = 50
LEAN_IN_LOW_USE_WINDOW_MS = 24 * 60 * 60 * 1000  # 24 hours


def _usable_rows(rows: list[dict], provider: str) -> list[dict]:
    """Rows for `provider` with a real `used_pct` and `resets_at`.

    Skips a row with no data (an "error"/"note" entry, or `used_pct`/
    `resets_at` of `None`) -- missing data is unknown, not 0% used.
    """
    return [
        row for row in rows
        if row.get("provider") == provider
        and isinstance(row.get("used_pct"), (int, float))
        and isinstance(row.get("resets_at"), (int, float))
    ]


def _window_blocked(used_pct: float, resets_at: float, now_ms: float) -> bool:
    """Reserve rule for one window (Grace's decision 1).

    `used_pct >= 100` is always blocked. `used_pct >= 95` is blocked too,
    unless under 3 hours remain until reset -- inside that grace period
    there is no reserve left to protect, so fleet routing can use up to
    (and including) 100%.
    """
    if used_pct >= 100:
        return True
    if used_pct >= RESERVE_PCT:
        return (resets_at - now_ms) >= RESERVE_GRACE_MS
    return False


def blocked(rows: list[dict], provider: str, now_ms: float) -> bool:
    """True if any of `provider`'s windows is blocked (reserve rule)."""
    return any(
        _window_blocked(row["used_pct"], row["resets_at"], now_ms)
        for row in _usable_rows(rows, provider)
    )


def _longest_window(rows: list[dict], provider: str) -> dict | None:
    """The provider's longest window with real data and a known length.

    `QuotaDuration.OTHER` has no `.milliseconds` -- a window of unknown
    length can't be compared to another, so it's excluded here (it still
    counts for `blocked()`, which doesn't need a length).
    """
    candidates = [
        row for row in _usable_rows(rows, provider)
        if isinstance(row.get("duration"), QuotaDuration) and row["duration"].milliseconds
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda row: row["duration"].milliseconds)


def lean_in(rows: list[dict], provider: str, now_ms: float) -> bool:
    """True if `provider` has earned a one-tier-up lean-in (decision 2).

    Looks only at the provider's longest window with real data. Blocked
    always wins: a blocked provider never leans in, even if that same
    window would otherwise qualify.

    A surplus means used_pct is below the elapsed percent of the longest
    window. The 30-minute clause needs a surplus. The 24-hour clause
    needs used_pct under 50 and does not check for a surplus.
    """
    if blocked(rows, provider, now_ms):
        return False
    window = _longest_window(rows, provider)
    if window is None:
        return False
    remaining = window["resets_at"] - now_ms
    duration_ms = window["duration"].milliseconds
    # Surplus: used_pct is below the percent of the window already elapsed.
    elapsed_pct = 100 * (duration_ms - remaining) / duration_ms
    has_surplus = window["used_pct"] < elapsed_pct
    if remaining < LEAN_IN_SOON_MS and has_surplus:
        return True
    return window["used_pct"] < LEAN_IN_LOW_USE_PCT and remaining < LEAN_IN_LOW_USE_WINDOW_MS


def earliest_reset(rows: list[dict], providers: list[str], now_ms: float) -> float | None:
    """Earliest `resets_at` among `providers`' blocked windows.

    `route()` calls this once every candidate it tried is blocked, to
    report how long the caller must wait. Ignores a provider with no
    blocked window (nothing to wait on there) and a blocked window with no
    usable `resets_at` (unknown, not zero) -- `None` means not one of
    `providers` has a usable wait estimate at all.
    """
    waits = [
        row["resets_at"]
        for provider in providers
        for row in _usable_rows(rows, provider)
        if _window_blocked(row["used_pct"], row["resets_at"], now_ms)
    ]
    return min(waits) if waits else None
