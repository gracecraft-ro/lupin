"""Quota reading: how much of each provider's rate limit is left.

Moved out of `serve.py` (issue #8) so the fleet heartbeat (`machines.py`,
issue #7/#2-C3) can read quota without pulling in the dashboard's HTTP
server. `serve.py` still renders this data on the `/usage` page -- it
imports the functions below instead of defining them.

`snapshot()` is the one entry point other modules should use: one number
per provider, `{provider: {pct_left, resets_at, source}}`. The rest of
this module is the detail `snapshot()` and `serve.py`'s rendering build
on top of.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
from contextlib import closing
from datetime import date, datetime, timedelta
from enum import Enum

CODEX_SESSIONS_DIR = os.path.expanduser("~/.codex/sessions")
CLAUDE_STATS_FILE = os.path.expanduser("~/.claude/stats-cache.json")
CLAUDE_CREDENTIALS_FILE = os.path.expanduser("~/.claude/.credentials.json")
OPENCODE_GO_USAGE_URL = "https://opencode.ai/zen/go/v1/usage"
OPENCODE_GO_AUTH_FILE = os.path.join(
    os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")),
    "opencode",
    "auth.json",
)
OMP_STATS_FILE = os.path.expanduser("~/.omp/stats.db")


class QuotaDuration(str, Enum):
    FIVE_HOURS = "PT5H"
    WEEKLY = "P7D"
    MONTHLY = "P30D"
    OTHER = "OTHER"

    @property
    def label(self) -> str:
        return {
            QuotaDuration.FIVE_HOURS: "5 hours",
            QuotaDuration.WEEKLY: "7 days",
            QuotaDuration.MONTHLY: "Monthly",
            QuotaDuration.OTHER: "Other",
        }[self]

    @property
    def milliseconds(self) -> int | None:
        return {
            QuotaDuration.FIVE_HOURS: 18_000_000,
            QuotaDuration.WEEKLY: 604_800_000,
            QuotaDuration.MONTHLY: 2_592_000_000,
        }.get(self)


def run(argv: list[str], timeout: float = 10.0) -> tuple[int, str]:
    """Run a fixed read-only probe. Never a shell, never browser input.

    A private copy of `serve.run` -- this module must not import `serve`
    (the fleet heartbeat in `machines.py` reads quota without starting a
    web server), so it carries its own small subprocess wrapper rather
    than share one.
    """
    try:
        proc = subprocess.run(
            argv,
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return 127, f"not found: {argv[0]}"
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s: {' '.join(argv)}"
    out = proc.stdout
    if proc.stderr:
        out = out + ("\n" if out and not out.endswith("\n") else "") + proc.stderr
    return proc.returncode, out


def unavailable_usage(provider: str, source: str, error: Exception) -> list[dict]:
    return [{
        "provider": provider,
        "source": source,
        "error": f"unavailable ({type(error).__name__})",
    }]


def claude_usage() -> list[dict]:
    """Read Claude Code's local telemetry cache.

    dailyModelTokens holds one flat token total per model per day -- no
    input/output split, no cost. That breakdown exists only as an all-time
    total under the top-level modelUsage key, which has no date range, so
    it can't be windowed to 7 days. Report what the daily source actually
    has: a combined token total, and cost as untracked rather than 0.
    """
    try:
        with open(CLAUDE_STATS_FILE, encoding="utf-8") as stats_file:
            stats = json.load(stats_file)
        last_update = stats["lastComputedDate"]
        date.fromisoformat(last_update)
        cutoff = (date.today() - timedelta(days=6)).isoformat()
        total_tokens = 0
        for day in stats["dailyModelTokens"]:
            if cutoff <= day["date"] <= date.today().isoformat():
                total_tokens += sum(day["tokensByModel"].values())
        return [{
            "provider": "claude",
            "input_tokens": total_tokens,
            "output_tokens": None,
            "cost": None,
            "period": "last 7 days",
            "source": CLAUDE_STATS_FILE,
            "last_update": last_update,
        }]
    except Exception as error:
        return unavailable_usage("claude", CLAUDE_STATS_FILE, error)


def omp_usage() -> list[dict]:
    try:
        with closing(sqlite3.connect(OMP_STATS_FILE)) as db:
            latest = dict(db.execute(
                "SELECT provider, MAX(timestamp) FROM messages GROUP BY provider"
            ))
            if not latest:
                raise ValueError("messages table is empty")
            now = time.time()
            cutoff = (now - 7 * 24 * 60 * 60) * 1000
            totals = db.execute(
                "SELECT provider, SUM(input_tokens), SUM(output_tokens), "
                "SUM(cost_total) FROM messages WHERE timestamp >= ? AND timestamp <= ? "
                "GROUP BY provider",
                (cutoff, now * 1000),
            )
            period_totals = {
                provider: (input_tokens or 0, output_tokens or 0, cost or 0)
                for provider, input_tokens, output_tokens, cost in totals
            }
        return [
            {
                "provider": provider,
                "input_tokens": period_totals.get(provider, (0, 0, 0))[0],
                "output_tokens": period_totals.get(provider, (0, 0, 0))[1],
                "cost": period_totals.get(provider, (0, 0, 0))[2],
                "period": "last 7 days",
                "source": OMP_STATS_FILE,
                "last_update": time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(timestamp / 1000)
                ),
            }
            for provider, timestamp in sorted(latest.items())
        ]
    except Exception as error:
        return unavailable_usage("stats.db", OMP_STATS_FILE, error)


def epoch_ms_to_local(value) -> str:
    if not isinstance(value, (int, float)):
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value / 1000))


def codex_session_quota() -> list[dict]:
    """Read the newest OpenAI Codex rate-limit snapshot written by Codex.

    Orca reads the live Codex app-server when it is available. The local
    rollout files provide its cached fallback: exact percentages, but only
    refreshed when Codex writes a session event.
    """
    files = []
    for root, _, names in os.walk(CODEX_SESSIONS_DIR):
        files.extend(
            os.path.join(root, name)
            for name in names
            if name.startswith("rollout-") and name.endswith(".jsonl")
        )
    for rollout_path in sorted(files, key=os.path.getmtime, reverse=True):
        try:
            with open(rollout_path, encoding="utf-8") as rollout:
                for line in reversed(rollout.readlines()):
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    payload = event.get("payload") or {}
                    if payload.get("type") != "token_count":
                        continue
                    limits = payload.get("rate_limits") or {}
                    rows = []
                    for key, duration in (
                        ("primary", QuotaDuration.FIVE_HOURS),
                        ("secondary", QuotaDuration.WEEKLY),
                    ):
                        limit = limits.get(key) or {}
                        used = limit.get("used_percent")
                        if not isinstance(used, (int, float)):
                            continue
                        rows.append({
                            "provider": "openai",
                            "duration": duration,
                            "used_pct": min(100, max(0, used)),
                            "resets_at": quota_reset_timestamp(limit.get("resets_at")),
                            "generated_at": time.strftime(
                                "%Y-%m-%d %H:%M:%S",
                                time.localtime(os.path.getmtime(rollout_path)),
                            ),
                        })
                    if rows:
                        return rows
        except OSError:
            continue
    return [{"provider": "openai", "note": "no Codex session quota data available"}]


def quota_reset_timestamp(value) -> int | None:
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 10_000_000_000 else value
        return int(seconds * 1000)
    if isinstance(value, str):
        try:
            return quota_reset_timestamp(float(value))
        except ValueError:
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                return int(parsed.timestamp() * 1000)
            except ValueError:
                return None
    return None


def quota_duration(limit: dict, window: dict) -> QuotaDuration:
    duration_ms = window.get("durationMs")
    if isinstance(duration_ms, (int, float)):
        for duration in (
            QuotaDuration.FIVE_HOURS,
            QuotaDuration.WEEKLY,
            QuotaDuration.MONTHLY,
        ):
            if duration_ms == duration.milliseconds:
                return duration
    identifier = " ".join(
        str(value).lower()
        for value in (
            window.get("id"),
            limit.get("id"),
            window.get("label"),
            limit.get("label"),
        )
        if value is not None
    )
    normalized = re.sub(r"[^a-z0-9]", "", identifier)
    if any(key in normalized for key in ("5h", "fivehour", "5hour", "session", "primary")):
        return QuotaDuration.FIVE_HOURS
    if any(key in normalized for key in ("7d", "sevenday", "7day", "weekly", "secondary")):
        return QuotaDuration.WEEKLY
    if any(key in normalized for key in ("30d", "monthly", "month")):
        return QuotaDuration.MONTHLY
    return QuotaDuration.OTHER

def claude_oauth_quota() -> list[dict]:
    try:
        with open(CLAUDE_CREDENTIALS_FILE, encoding="utf-8") as credentials_file:
            credentials = json.load(credentials_file)
        token = credentials["claudeAiOauth"]["accessToken"]
        if not isinstance(token, str) or not token:
            raise ValueError("missing OAuth access token")
        request = urllib.request.Request(
            "https://api.anthropic.com/api/oauth/usage",
            headers={
                "Authorization": f"Bearer {token}",
                "anthropic-beta": "oauth-2025-04-20",
                "User-Agent": "claude-code/2.1.0",
            },
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            data = json.load(response)
    except (OSError, KeyError, TypeError, ValueError, urllib.error.URLError) as error:
        return [{"provider": "claude", "note": f"quota unavailable ({type(error).__name__})"}]
    if not isinstance(data, dict):
        return [{"provider": "claude", "note": "no quota data reported"}]
    rows = []
    for field, duration in (
        ("five_hour", QuotaDuration.FIVE_HOURS),
        ("seven_day", QuotaDuration.WEEKLY),
    ):
        limit = data.get(field) or {}
        if not isinstance(limit, dict):
            continue
        used = limit.get("utilization", limit.get("used_percentage"))
        if isinstance(used, (int, float)):
            rows.append({
                "provider": "claude",
                "duration": duration,
                "used_pct": min(100, max(0, used)),
                "resets_at": quota_reset_timestamp(limit.get("resets_at")),
                "generated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            })
    return rows or [{"provider": "claude", "note": "no quota data reported"}]


def opencode_go_quota() -> list[dict]:
    key = ""
    try:
        with open(OPENCODE_GO_AUTH_FILE, encoding="utf-8") as auth_file:
            auth = json.load(auth_file)
        entry = auth.get("opencode-go") or {}
        if entry.get("type") == "api":
            key = entry.get("key", "").strip()
    except (OSError, ValueError, AttributeError):
        pass
    if not key:
        key = os.environ.get("OPENCODE_API_KEY", "").strip()
    if not key:
        return [{"provider": "opencode-go", "note": "no OpenCode Go API key available"}]

    request = urllib.request.Request(
        OPENCODE_GO_USAGE_URL,
        headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            data = json.load(response)
    except (OSError, ValueError, urllib.error.URLError) as error:
        return [{"provider": "opencode-go", "note": f"quota unavailable ({type(error).__name__})"}]

    if not isinstance(data, dict) or not isinstance(data.get("usage"), dict):
        return [{"provider": "opencode-go", "note": "no quota data reported"}]
    usage = data["usage"]
    rows = []
    for field, duration in (
        ("rolling", QuotaDuration.FIVE_HOURS),
        ("weekly", QuotaDuration.WEEKLY),
        ("monthly", QuotaDuration.MONTHLY),
    ):
        limit = usage.get(field) or {}
        if not isinstance(limit, dict):
            continue
        used = limit.get("percent")
        if isinstance(used, (int, float)):
            rows.append({
                "provider": "opencode-go",
                "duration": duration,
                "used_pct": min(100, max(0, used)),
                "resets_at": quota_reset_timestamp(limit.get("resetsAt")),
                "generated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            })
    return rows or [{"provider": "opencode-go", "note": "no quota data reported"}]


def add_quota_fallbacks(rows: list[dict]) -> list[dict]:
    if not any(row.get("provider") == "opencode-go" and "used_pct" in row for row in rows):
        rows.extend(opencode_go_quota())
    rows.extend(claude_oauth_quota())
    return rows


def quota_usage() -> list[dict]:
    rc, out = run(["omp", "usage", "--json"])
    if rc != 0:
        rows = unavailable_usage(
            "omp", "omp usage --json", RuntimeError(f"exit code {rc}")
        ) + codex_session_quota()
        return add_quota_fallbacks(rows)
    try:
        data = json.loads(out)
    except json.JSONDecodeError as error:
        rows = unavailable_usage("omp", "omp usage --json", error) + codex_session_quota()
        return add_quota_fallbacks(rows)
    if not isinstance(data, dict):
        rows = unavailable_usage(
            "omp", "omp usage --json", ValueError("expected a JSON object")
        ) + codex_session_quota()
        return add_quota_fallbacks(rows)

    generated_at = epoch_ms_to_local(data.get("generatedAt"))
    rows = []
    for report in data.get("reports", []):
        provider = report.get("provider", "unknown")
        if provider == "ollama":
            continue
        if provider == "openai-codex":
            provider = "openai"
        limits = report.get("limits") or []
        if not limits:
            for note in report.get("notes") or ["no quota data reported"]:
                rows.append({"provider": provider, "note": note, "generated_at": generated_at})
            continue
        for limit in limits:
            amount = limit.get("amount") or {}
            window = limit.get("window") or {}
            used_fraction = amount.get("usedFraction")
            rows.append({
                "provider": provider,
                "duration": quota_duration(limit, window),
                "label": limit.get("label", limit.get("id", "-")),
                "used_pct": None if used_fraction is None else used_fraction * 100,
                "resets_at": quota_reset_timestamp(window.get("resetsAt")),
                "generated_at": generated_at,
            })
    return add_quota_fallbacks(rows)


def quota_source_label(provider: str) -> str:
    return {
        "claude": "Anthropic OAuth usage API",
        "openai": "omp / Codex local snapshot",
        "opencode-go": "omp / Orca usage API",
    }.get(provider, "omp quota data")


def snapshot() -> dict[str, dict]:
    """One number per provider: `{provider: {pct_left, resets_at, source}}`.

    `quota_usage()` can return several rows per provider -- one per
    window (5 hours, 7 days, 30 days). This picks the 5-hour row when
    there is one (the window every reader tags "primary"/"session"), since
    that is the window that actually blocks the next request; otherwise it
    falls back to the first row for that provider. A provider with no
    usable percentage (an error or a "no data" note) still gets an entry,
    with `pct_left`/`resets_at` as `None` -- callers (`machines.py`'s
    heartbeat, eventually `place`) decide what to do with missing data.
    """
    chosen: dict[str, dict] = {}
    for row in quota_usage():
        provider = row.get("provider")
        if not provider:
            continue
        if provider not in chosen or row.get("duration") == QuotaDuration.FIVE_HOURS:
            chosen[provider] = row

    result = {}
    for provider, row in chosen.items():
        used = row.get("used_pct")
        pct_left = None if not isinstance(used, (int, float)) else max(0.0, 100 - used)
        result[provider] = {
            "pct_left": pct_left,
            "resets_at": row.get("resets_at"),
            "source": quota_source_label(provider),
        }
    return result
