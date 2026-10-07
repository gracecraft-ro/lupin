"""Tests for `lupin place` (issue #9).

Redis-backed tests use the same real `redis-server` fixtures as
`test_machines.py` (`redis_port`/`flush_redis`/`closed_port` from
`conftest.py`), not a mock -- same rule as the rest of this backend's
tests. Machine records are written directly (bypassing `join`/`heartbeat`,
which always register *this* process's own hostname), the same approach
`test_machines.py`'s `_write_raw_record` uses, extended with the
`slots`/`quota` fields `place` actually reads.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import pytest
import redis as redis_lib

from lupin import cli, machines, place


_AMBIENT_ENV_VARS = (
    "LUPIN_REDIS_HOST", "LUPIN_REDIS_PORT", "LUPIN_REDIS_USERNAME",
    "LUPIN_REDIS_PASSWORD", "LUPIN_FLEET_CONFIG", "LUPIN_BACKEND",
)


@pytest.fixture
def clean_fleet_env(monkeypatch):
    """Same reason `test_machines.py` needs this: a real fleet host sets
    `$LUPIN_REDIS_*` for its own Redis, which would otherwise leak into
    these tests' throwaway `redis-server` fixture and break auth.
    """
    for name in _AMBIENT_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _kw(redis_port):
    return {"redis_host": "127.0.0.1", "redis_port": redis_port}


def _old_stamp(seconds_ago):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_machine(
    redis_port,
    name,
    *,
    state="online",
    version="0.9.2",
    heartbeat=None,
    slots=None,
    quota=None,
):
    record = {
        "version": version,
        "heartbeat": heartbeat or machines._now_iso(),
        "state": state,
        "slots": slots or {},
        "providers": [],
        "quota": quota or {},
    }
    client = redis_lib.Redis(host="127.0.0.1", port=redis_port, decode_responses=True)
    client.set(f"{machines.PREFIX}machine:{name}", json.dumps(record))


_CLAUDE_QUOTA = {"claude": {"pct_left": 62.0, "resets_at": 1_800_000_000_000, "source": "test"}}


# --- pure helpers, no redis ---


def test_provider_for_model_maps_claude_models():
    assert place.provider_for_model("sonnet") == "claude"
    assert place.provider_for_model("opus") == "claude"


def test_provider_for_model_maps_fable_to_opencode_go():
    assert place.provider_for_model("fable") == "opencode-go"


def test_provider_for_model_maps_bmo_and_local_prefixes():
    assert place.provider_for_model("bmo:qwen3.8-flash-next") == "bmo"
    assert place.provider_for_model("local:deepseek-v4-flash-0731") == "local"


def test_provider_for_model_falls_back_to_the_model_name():
    assert place.provider_for_model("some-future-model") == "some-future-model"


def test_format_duration():
    assert place.format_duration(11400) == "3h 10m"
    assert place.format_duration(2700) == "45m"
    assert place.format_duration(7200) == "2h"
    assert place.format_duration(0) == "now"
    assert place.format_duration(-5) == "now"


def test_resolve_task_free_text_is_classified_as_is():
    issue, label = place._resolve_task("retry backoff")
    assert issue == {"title": "retry backoff"}
    assert label == "retry backoff"


def test_resolve_task_issue_number_uses_gh_title(monkeypatch):
    monkeypatch.setattr(
        place, "_fetch_issue", lambda number: ({"title": "retry backoff"}, None)
    )
    issue, label = place._resolve_task("#418")
    assert issue == {"title": "retry backoff"}
    assert label == "#418 retry backoff"


def test_resolve_task_issue_number_falls_back_when_gh_fails(monkeypatch):
    monkeypatch.setattr(place, "_fetch_issue", lambda number: ({}, "gh: not found"))
    issue, label = place._resolve_task("418")
    assert issue == {}
    assert label == "#418"


# --- quest_focus_for: the hook `place()` fills in for issue #12 ---


def test_quest_focus_for_returns_none_for_free_text():
    assert place.quest_focus_for(None, {}) is None


def test_quest_focus_for_returns_none_when_task_is_in_no_quest(monkeypatch):
    monkeypatch.setattr(place.quest_mod, "load_quests", lambda repos: ([], []))
    assert place.quest_focus_for("418", {}) is None


def test_quest_focus_for_returns_none_when_quest_has_no_focus(monkeypatch):
    quests = [{"name": "session-rewrite", "tasks": [{"number": 418, "title": "t", "done": False}]}]
    monkeypatch.setattr(place.quest_mod, "load_quests", lambda repos: (quests, []))
    monkeypatch.setattr(place.quest_mod, "read_focus", lambda name, **kw: None)
    assert place.quest_focus_for("418", {}) is None


def test_quest_focus_for_returns_the_quests_focus_machine(monkeypatch):
    quests = [{"name": "session-rewrite", "tasks": [{"number": 418, "title": "t", "done": False}]}]
    monkeypatch.setattr(place.quest_mod, "load_quests", lambda repos: (quests, []))
    monkeypatch.setattr(
        place.quest_mod, "read_focus", lambda name, **kw: {"machine": "mac-studio"} if name == "session-rewrite" else None
    )
    assert place.quest_focus_for("418", {}) == "mac-studio"


# --- place(), against a real (throwaway) redis-server ---


def test_place_picks_the_machine_with_more_free_slots(redis_port, flush_redis):
    _write_machine(redis_port, "mac-studio", slots={"bmo": {"used": 2, "max": 4}}, quota=_CLAUDE_QUOTA)
    _write_machine(redis_port, "mini-2", slots={"bmo": {"used": 1, "max": 2}}, quota=_CLAUDE_QUOTA)

    result = place.place("retry backoff", _kw(redis_port))

    # No labels on this synthetic issue -> classify() falls back to
    # size-? -> tier2 -> "opus" (model-tiers.json's "coding" row).
    assert result["model"] == "opus"
    assert result["provider"] == "claude"
    assert result["pick"] == "mac-studio"
    by_name = {c["name"]: c for c in result["candidates"]}
    assert by_name["mac-studio"]["result"] == "pick"
    assert by_name["mini-2"]["result"] == "fewer slots free"


def test_place_ranks_draining_below_online_and_labels_it_draining(redis_port, flush_redis):
    _write_machine(redis_port, "mac-studio", state="online", slots={"bmo": {"used": 2, "max": 4}}, quota=_CLAUDE_QUOTA)
    _write_machine(redis_port, "jesus", state="draining", slots={"bmo": {"used": 0, "max": 2}}, quota=_CLAUDE_QUOTA)

    result = place.place("retry backoff", _kw(redis_port))

    assert result["pick"] == "mac-studio"
    by_name = {c["name"]: c for c in result["candidates"]}
    assert by_name["jesus"]["result"] == "draining"


def test_place_has_no_pick_when_every_matching_machine_is_draining(redis_port, flush_redis):
    _write_machine(redis_port, "jesus", state="draining", slots={"bmo": {"used": 0, "max": 2}}, quota=_CLAUDE_QUOTA)

    result = place.place("retry backoff", _kw(redis_port))

    assert result["pick"] is None
    assert result["run_command"] is None
    assert result["candidates"][0]["result"] == "draining"


def test_place_skips_machines_running_a_different_provider(redis_port, flush_redis):
    _write_machine(redis_port, "mac-studio", quota=_CLAUDE_QUOTA)
    _write_machine(redis_port, "codex-box", quota={"openai": {"pct_left": 80.0, "resets_at": None, "source": "test"}})

    result = place.place("retry backoff", _kw(redis_port))

    assert result["pick"] == "mac-studio"
    assert len(result["candidates"]) == 1
    assert result["skipped"]["other_provider"] == 1


def test_place_skips_offline_machines(redis_port, flush_redis):
    _write_machine(redis_port, "mac-studio", quota=_CLAUDE_QUOTA)
    _write_machine(
        redis_port, "ghost", quota=_CLAUDE_QUOTA,
        heartbeat="2000-01-01T00:00:00Z",
    )

    result = place.place("retry backoff", _kw(redis_port))

    assert result["pick"] == "mac-studio"
    assert len(result["candidates"]) == 1
    assert result["skipped"]["offline"] == 1


def test_place_breaks_ties_on_heartbeat_freshness(redis_port, flush_redis):
    fresh = machines._now_iso()
    stale = _old_stamp(90)  # older, but still under OFFLINE_AFTER (120s)
    _write_machine(redis_port, "fresher", slots={"bmo": {"used": 1, "max": 2}}, quota=_CLAUDE_QUOTA, heartbeat=fresh)
    _write_machine(redis_port, "staler", slots={"bmo": {"used": 1, "max": 2}}, quota=_CLAUDE_QUOTA, heartbeat=stale)

    result = place.place("retry backoff", _kw(redis_port))

    assert result["pick"] == "fresher"
    by_name = {c["name"]: c for c in result["candidates"]}
    assert by_name["staler"]["result"] == "staler heartbeat"


def test_place_ranks_by_burn_margin_prefers_more_headroom_per_hour(redis_port, flush_redis):
    """Issue #30's own example: both machines report 5% left, but one
    resets in 2 minutes (high %/hour margin, about to refill) and the
    other in 4 hours (low margin, has to last longer on the same 5%). The
    2-minute machine should win even though raw pct_left ties.
    """
    near_reset = int((time.time() + 120) * 1000)
    far_reset = int((time.time() + 4 * 3600) * 1000)
    _write_machine(
        redis_port, "fast-reset", slots={"bmo": {"used": 1, "max": 2}},
        quota={"claude": {"pct_left": 5.0, "resets_at": near_reset, "source": "test"}},
    )
    _write_machine(
        redis_port, "slow-reset", slots={"bmo": {"used": 1, "max": 2}},
        quota={"claude": {"pct_left": 5.0, "resets_at": far_reset, "source": "test"}},
    )

    result = place.place("retry backoff", _kw(redis_port))

    assert result["pick"] == "fast-reset"
    by_name = {c["name"]: c for c in result["candidates"]}
    assert by_name["slow-reset"]["result"] == "worse burn margin"


def test_place_stale_resets_at_does_not_blow_up_the_ranking(redis_port, flush_redis):
    """A `resets_at` already in the past (stale heartbeat) must not turn
    into a tiny or negative divisor. It's treated as safe/full instead, so
    it doesn't lose to a machine with a much smaller real margin.
    """
    stale_reset = int((time.time() - 3600) * 1000)
    good_reset = int((time.time() + 3600) * 1000)
    _write_machine(
        redis_port, "stale-reset", slots={"bmo": {"used": 1, "max": 2}},
        quota={"claude": {"pct_left": 5.0, "resets_at": stale_reset, "source": "test"}},
    )
    _write_machine(
        redis_port, "normal", slots={"bmo": {"used": 1, "max": 2}},
        quota={"claude": {"pct_left": 90.0, "resets_at": good_reset, "source": "test"}},
    )

    result = place.place("retry backoff", _kw(redis_port))

    assert result["pick"] == "stale-reset"


def test_place_missing_resets_at_sorts_after_a_real_margin(redis_port, flush_redis):
    """No `resets_at` means no margin can be computed, so this machine
    falls back to `pct_left` alone and ranks after any machine with a real,
    computable margin -- even one with far less raw `pct_left`.
    """
    good_reset = int((time.time() + 3600) * 1000)
    _write_machine(
        redis_port, "has-margin", slots={"bmo": {"used": 1, "max": 2}},
        quota={"claude": {"pct_left": 1.0, "resets_at": good_reset, "source": "test"}},
    )
    _write_machine(
        redis_port, "no-resets-at", slots={"bmo": {"used": 1, "max": 2}},
        quota={"claude": {"pct_left": 99.0, "resets_at": None, "source": "test"}},
    )

    result = place.place("retry backoff", _kw(redis_port))

    assert result["pick"] == "has-margin"
    by_name = {c["name"]: c for c in result["candidates"]}
    assert by_name["no-resets-at"]["result"] == "worse burn margin"


def test_place_prefers_the_quest_focus_machine_over_more_free_slots(redis_port, flush_redis, monkeypatch):
    _write_machine(redis_port, "mac-studio", slots={"bmo": {"used": 1, "max": 2}}, quota=_CLAUDE_QUOTA)
    _write_machine(redis_port, "mini-2", slots={"bmo": {"used": 0, "max": 4}}, quota=_CLAUDE_QUOTA)
    monkeypatch.setattr(place, "_fetch_issue", lambda number: ({"title": "retry backoff"}, None))
    quests = [{"name": "session-rewrite", "tasks": [{"number": 418, "title": "t", "done": False}]}]
    monkeypatch.setattr(place.quest_mod, "load_quests", lambda repos: (quests, []))
    monkeypatch.setattr(place.quest_mod, "read_focus", lambda name, **kw: {"machine": "mac-studio"})

    result = place.place("#418", _kw(redis_port))

    assert result["pick"] == "mac-studio"
    by_name = {c["name"]: c for c in result["candidates"]}
    assert by_name["mac-studio"]["quest_focus"] == "mac-studio"
    assert by_name["mini-2"]["result"] == "not quest focus"


def test_place_quota_header_prefers_a_real_reading_over_unavailable(redis_port, flush_redis):
    _write_machine(
        redis_port, "no-data", quota={"claude": {"pct_left": None, "resets_at": None, "source": "test"}}
    )
    _write_machine(redis_port, "mac-studio", quota=_CLAUDE_QUOTA)

    result = place.place("retry backoff", _kw(redis_port))

    assert result["quota"] == _CLAUDE_QUOTA["claude"]


def test_place_raises_coordinator_unreachable(closed_port):
    with pytest.raises(place.CoordinatorUnreachable):
        place.place("retry backoff", {"redis_host": "127.0.0.1", "redis_port": closed_port})


# --- cli wiring ---


def test_cli_place_prints_run_command(redis_port, flush_redis, tmp_path, capsys, clean_fleet_env):
    _write_machine(redis_port, "mac-studio", quota=_CLAUDE_QUOTA)
    common = [
        "--redis-host", "127.0.0.1", "--redis-port", str(redis_port),
        "--config-path", str(tmp_path / "fleet.json"),
    ]

    code = cli.main(["place", "retry backoff", *common])
    captured = capsys.readouterr()

    assert code == 0
    assert captured.out.strip() == "lupin run --machine mac-studio retry backoff"


def test_cli_place_no_pick_exits_2(redis_port, flush_redis, tmp_path, capsys, clean_fleet_env):
    common = [
        "--redis-host", "127.0.0.1", "--redis-port", str(redis_port),
        "--config-path", str(tmp_path / "fleet.json"),
    ]

    code = cli.main(["place", "retry backoff", *common])
    captured = capsys.readouterr()

    assert code == 2
    assert "no online machine" in captured.err


def test_cli_place_explain_lists_candidates_and_skip_footer(
    redis_port, flush_redis, tmp_path, capsys, clean_fleet_env
):
    _write_machine(redis_port, "mac-studio", slots={"bmo": {"used": 2, "max": 4}}, quota=_CLAUDE_QUOTA)
    _write_machine(redis_port, "codex-box", quota={"openai": {"pct_left": 80.0, "resets_at": None, "source": "test"}})
    common = [
        "--redis-host", "127.0.0.1", "--redis-port", str(redis_port),
        "--config-path", str(tmp_path / "fleet.json"),
    ]

    code = cli.main(["place", "retry backoff", "--explain", *common])
    captured = capsys.readouterr()

    assert code == 0
    assert "mac-studio" in captured.out
    assert "pick" in captured.out
    assert "1 machine(s) skipped" in captured.out
    assert "different provider" in captured.out


def test_cli_place_json_output(redis_port, flush_redis, tmp_path, capsys, clean_fleet_env):
    _write_machine(redis_port, "mac-studio", quota=_CLAUDE_QUOTA)
    common = [
        "--redis-host", "127.0.0.1", "--redis-port", str(redis_port),
        "--config-path", str(tmp_path / "fleet.json"),
    ]

    code = cli.main(["place", "retry backoff", "--json", *common])
    captured = capsys.readouterr()

    assert code == 0
    payload = json.loads(captured.out)
    assert payload["pick"] == "mac-studio"
    assert payload["provider"] == "claude"


def test_cli_place_json_no_pick_exits_2(redis_port, flush_redis, tmp_path, capsys, clean_fleet_env):
    """Same "try again later" contract as plain/--explain output (see
    cli.py's exit-code docstring) -- --json must not silently report 0
    just because it always has a body to print.
    """
    common = [
        "--redis-host", "127.0.0.1", "--redis-port", str(redis_port),
        "--config-path", str(tmp_path / "fleet.json"),
    ]

    code = cli.main(["place", "retry backoff", "--json", *common])
    captured = capsys.readouterr()

    assert code == 2
    assert json.loads(captured.out)["pick"] is None


def test_cli_place_unreachable_redis_exits_3(closed_port, tmp_path, capsys, clean_fleet_env):
    common = [
        "--redis-host", "127.0.0.1", "--redis-port", str(closed_port),
        "--config-path", str(tmp_path / "fleet.json"),
    ]

    code = cli.main(["place", "retry backoff", *common])
    captured = capsys.readouterr()

    assert code == 3
    assert "cannot reach" in captured.err
