from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime, timedelta, timezone

import pytest
import redis as redis_lib

from lupin import debrief, digest

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
RAW = "# Debrief\n\n## Shipped\n\n- None\n\n![shot](20261010-120000-6h-screenshots/a.png)\n"


def _kw(redis_port):
    return {"redis_host": "127.0.0.1", "redis_port": redis_port}


def _raw_client(redis_port):
    return redis_lib.Redis(host="127.0.0.1", port=redis_port, decode_responses=True)


def _write(tmp_path, repo, name, text=RAW):
    folder = tmp_path / "debriefs" / repo
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(text, encoding="utf-8")


def _fake_make(calls):
    def make(repo, name, raw):
        calls.append((repo, name))
        return f"## Summary\n\nDigest of {name}.", "sonnet"

    return make


def test_strip_images_names_the_machine_that_keeps_the_file():
    out = digest.strip_images(RAW, "jesus")

    assert "![" not in out
    assert "(shot: kept on jesus)" in out


def test_argv_uses_omp_for_a_provider_model_and_claude_for_a_bare_name():
    omp = digest._argv("opencode-go/step-5-preview-free", "high", "P")
    claude = digest._argv("sonnet", "high", "P")

    assert omp[:2] == ["omp", "-p"] and omp[omp.index("--model") + 1] == "opencode-go/step-5-preview-free"
    assert claude[:2] == ["claude", "-p"] and claude[claude.index("--model") + 1] == "sonnet"
    assert omp[-1] == claude[-1] == "P"
    # No tools: the model only reads text and writes text. PR titles in the prompt are not trusted.
    assert claude[claude.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in claude and "--disable-slash-commands" in claude
    assert "--no-tools" in omp and "--no-extensions" in omp
    assert "--auto-approve" not in omp
    # `claude --tools` takes a list, so a flag must come right after it.
    assert claude[claude.index("--tools") + 2].startswith("--")


def test_prompt_tells_the_model_that_missing_data_is_not_an_empty_result():
    assert "Missing data is not an empty result" in " ".join(digest.PROMPT.split())


def _run_returning(monkeypatch, *, stdout="", returncode=0, stderr="", raises=None):
    monkeypatch.setattr(digest, "pick_model", lambda: ("sonnet", "high"))
    seen = {}

    def run(argv, **kwargs):
        seen["argv"] = argv
        if raises:
            raise raises
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    monkeypatch.setattr(digest.subprocess, "run", run)
    return seen


def test_make_digest_returns_text_with_a_code_fence_removed(monkeypatch):
    seen = _run_returning(monkeypatch, stdout="```markdown\n## Summary\n\nOk.\n```\n")

    text, model = digest.make_digest("widgets", "20261010-120000.md", RAW)

    assert (text, model) == ("## Summary\n\nOk.", "sonnet")
    assert "widgets" in seen["argv"][-1] and "# Debrief" in seen["argv"][-1]


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"raises": FileNotFoundError()}, "claude not found"),
        ({"raises": subprocess.TimeoutExpired("claude", 1)}, "timed out"),
        ({"returncode": 3, "stderr": "boom"}, "exited 3: boom"),
        ({"stdout": "  \n"}, "gave no text"),
    ],
)
def test_make_digest_raises_digest_error_for_each_model_failure(monkeypatch, kwargs, message):
    _run_returning(monkeypatch, **kwargs)

    with pytest.raises(digest.DigestError, match=message):
        digest.make_digest("widgets", "20261010-120000.md", RAW)


def test_pick_model_refuses_a_blocked_route_and_a_local_model(monkeypatch):
    monkeypatch.setattr(digest.route, "route", lambda *a, **k: {"model": "sonnet", "effort": "high", "wait_seconds": 60})
    with pytest.raises(digest.DigestBlocked):
        digest.pick_model()

    monkeypatch.setattr(digest.route, "route", lambda *a, **k: {"model": "bmo:x", "effort": "high"})
    with pytest.raises(digest.DigestError, match="cannot run"):
        digest.pick_model()

    monkeypatch.setattr(digest.route, "route", lambda *a, **k: {"model": "sonnet", "effort": "high"})
    assert digest.pick_model() == ("sonnet", "high")


def test_sync_publishes_raw_text_and_digest_once(tmp_path, redis_port, flush_redis):
    _write(tmp_path, "widgets", "20261010-120000-6h.md")
    calls = []

    first = digest.sync(tmp_path, _kw(redis_port), machine="jesus", make=_fake_make(calls), now=NOW)
    second = digest.sync(tmp_path, _kw(redis_port), machine="jesus", make=_fake_make(calls), now=NOW)

    entry = digest.read_one("jesus", "widgets", "20261010-120000-6h.md", **_kw(redis_port))
    assert (first, second, len(calls)) == (1, 0, 1)
    assert entry["digest"].startswith("## Summary")
    assert entry["model"] == "sonnet"
    assert "![" not in entry["raw"] and "kept on jesus" in entry["raw"]
    assert _raw_client(redis_port).ttl(digest._key("jesus", "widgets", "20261010-120000-6h.md")) > 0


def test_sync_limit_publishes_the_rest_without_a_digest_then_fills_them_in(tmp_path, redis_port, flush_redis):
    for day in ("01", "02", "03"):
        _write(tmp_path, "widgets", f"202610{day}-120000.md")
    calls = []

    digest.sync(tmp_path, _kw(redis_port), machine="jesus", limit=1, make=_fake_make(calls), now=NOW)
    after_first = {e["name"]: e["digest"] for e in digest.read_all(**_kw(redis_port))}
    digest.sync(tmp_path, _kw(redis_port), machine="jesus", limit=5, make=_fake_make(calls), now=NOW)
    after_second = {e["name"]: e["digest"] for e in digest.read_all(**_kw(redis_port))}

    # Newest first: the 03rd got the one model call. The others are still readable.
    assert after_first["20261003-120000.md"] and not after_first["20261001-120000.md"]
    assert len(after_first) == 3
    assert all(after_second.values())
    assert len(calls) == 3


def test_sync_counts_failed_tries_and_stops_at_the_limit(tmp_path, redis_port, flush_redis):
    _write(tmp_path, "widgets", "20261010-120000.md")
    calls = []

    def failing(repo, name, raw):
        calls.append(name)
        raise digest.DigestError("claude exited 1: nope")

    for _ in range(digest.MAX_TRIES + 2):
        digest.sync(tmp_path, _kw(redis_port), machine="jesus", make=failing, now=NOW)

    entry = digest.read_one("jesus", "widgets", "20261010-120000.md", **_kw(redis_port))
    assert len(calls) == digest.MAX_TRIES
    assert entry["digest"] is None and entry["error"] == "claude exited 1: nope"
    assert entry["raw"]  # The debrief is readable even with no digest.


def test_sync_does_not_count_a_blocked_model_as_a_try(tmp_path, redis_port, flush_redis):
    _write(tmp_path, "widgets", "20261010-120000.md")
    _write(tmp_path, "widgets", "20261009-120000.md")
    calls = []

    def blocked(repo, name, raw):
        calls.append(name)
        raise digest.DigestBlocked("out of quota")

    for _ in range(digest.MAX_TRIES + 1):
        digest.sync(tmp_path, _kw(redis_port), machine="jesus", make=blocked, now=NOW)

    entries = digest.read_all(**_kw(redis_port))
    assert [e["tries"] for e in entries] == [0, 0]
    # One blocked call ends the sync. The older debrief is still published as text.
    assert len(calls) == digest.MAX_TRIES + 1
    assert all(e["raw"] for e in entries)


def test_sync_survives_an_unreachable_redis(tmp_path):
    _write(tmp_path, "widgets", "20261010-120000.md")

    assert digest.sync(tmp_path, {"redis_host": "127.0.0.1", "redis_port": 1}, machine="jesus", make=_fake_make([]), now=NOW) == 0


def test_read_all_sorts_newest_first_and_drops_bad_entries(redis_port, flush_redis):
    client = _raw_client(redis_port)
    for machine, repo, name in [
        ("jesus", "widgets", "20261001-090000.md"),
        ("ralpha", "widgets", "20261002-090000.md"),
    ]:
        client.set(digest._key(machine, repo, name), json.dumps({"machine": machine, "repo": repo, "name": name, "raw": "# x"}))
    client.set(digest._key("jesus", "..", "20261003-090000.md"), json.dumps({"machine": "jesus", "repo": "..", "name": "20261003-090000.md", "raw": "x"}))
    client.set(digest._key("jesus", "widgets", "evil.md"), json.dumps({"machine": "jesus", "repo": "widgets", "name": "evil.md", "raw": "x"}))
    client.set(digest._key("jesus", "widgets", "20261004-090000.md"), "not json")

    entries = digest.read_all(**_kw(redis_port))

    assert [(e["machine"], e["name"]) for e in entries] == [("ralpha", "20261002-090000.md"), ("jesus", "20261001-090000.md")]


def test_read_one_refuses_a_bad_machine_repo_or_name(redis_port, flush_redis):
    assert digest.read_one("a:b", "widgets", "20261001-090000.md", **_kw(redis_port)) is None
    assert digest.read_one("jesus", "../x", "20261001-090000.md", **_kw(redis_port)) is None
    assert digest.read_one("jesus", "widgets", "x.md", **_kw(redis_port)) is None


def test_make_digest_cuts_the_text_by_bytes_so_one_argument_stays_under_the_linux_limit(monkeypatch):
    seen = _run_returning(monkeypatch, stdout="## Summary\n\nOk.")

    digest.make_digest("widgets", "20261010-120000.md", "\U0001F600" * 100_000)  # 4 bytes each

    assert len(seen["argv"][-1].encode("utf-8")) < 100_000


@pytest.mark.parametrize("exc", [PermissionError("denied"), OSError(7, "Argument list too long"), ValueError("embedded null byte")])
def test_make_digest_turns_start_errors_into_digest_error(monkeypatch, exc):
    _run_returning(monkeypatch, raises=exc)

    with pytest.raises(digest.DigestError, match="could not start"):
        digest.make_digest("widgets", "20261010-120000.md", RAW)


def test_an_expired_redis_entry_does_not_start_a_new_model_call_for_an_old_debrief(tmp_path, redis_port, flush_redis):
    _write(tmp_path, "widgets", "20261010-120000.md")
    calls = []
    digest.sync(tmp_path, _kw(redis_port), machine="jesus", make=_fake_make(calls), now=NOW)

    # The entry expires. It is 40 days later, and the file is still on disk.
    _raw_client(redis_port).delete(digest._key("jesus", "widgets", "20261010-120000.md"))
    later = NOW + timedelta(days=40)
    written = digest.sync(tmp_path, _kw(redis_port), machine="jesus", make=_fake_make(calls), now=later)

    assert (written, len(calls)) == (0, 1)
    assert digest.read_all(**_kw(redis_port)) == []


def test_one_bad_debrief_does_not_stop_the_older_ones(tmp_path, redis_port, flush_redis, capsys):
    _write(tmp_path, "widgets", "20261012-120000.md")
    _write(tmp_path, "widgets", "20261011-120000.md")
    calls = []

    def make(repo, name, raw):
        calls.append(name)
        if name.startswith("20261012"):
            raise RuntimeError("unexpected")
        return "## Summary\n\nOk.", "sonnet"

    digest.sync(tmp_path, _kw(redis_port), machine="jesus", make=make, now=NOW)

    assert calls == ["20261012-120000.md", "20261011-120000.md"]
    assert [e["name"] for e in digest.read_all(**_kw(redis_port))] == ["20261011-120000.md"]
    assert "skipped widgets/20261012-120000.md: unexpected" in capsys.readouterr().err


def test_sync_returns_the_number_of_digests_written_not_the_calls_made(tmp_path, redis_port, flush_redis):
    _write(tmp_path, "widgets", "20261010-120000.md")

    def blocked(repo, name, raw):
        raise digest.DigestBlocked("out of quota")

    assert digest.sync(tmp_path, _kw(redis_port), machine="jesus", make=blocked, now=NOW) == 0


def test_a_bad_stored_try_count_is_replaced_instead_of_crashing_sync(tmp_path, redis_port, flush_redis):
    _write(tmp_path, "widgets", "20261010-120000.md")
    key = digest._key("jesus", "widgets", "20261010-120000.md")
    _raw_client(redis_port).set(key, json.dumps({"machine": "jesus", "repo": "widgets", "name": "20261010-120000.md", "raw": "x", "tries": "many"}))

    written = digest.sync(tmp_path, _kw(redis_port), machine="jesus", make=_fake_make([]), now=NOW)

    assert written == 1 and digest.read_one("jesus", "widgets", "20261010-120000.md", **_kw(redis_port))["tries"] == 1


@pytest.mark.parametrize("bad", [{"raw": 5}, {"digest": ["x"]}, {"tries": True}, {"machine": "a:b"}])
def test_read_refuses_a_badly_formed_entry(redis_port, flush_redis, bad):
    entry = {"machine": "jesus", "repo": "widgets", "name": "20261010-120000.md", "raw": "# x", **bad}
    _raw_client(redis_port).set(digest._key("jesus", "widgets", "20261010-120000.md"), json.dumps(entry))

    assert digest.read_one("jesus", "widgets", "20261010-120000.md", **_kw(redis_port)) is None
    assert digest.read_all(**_kw(redis_port)) == []


def test_reads_return_nothing_when_redis_denies_the_command(tmp_path, redis_port, flush_redis):
    _write(tmp_path, "widgets", "20261010-120000.md")
    admin = _raw_client(redis_port)
    admin.acl_setuser("pingonly", enabled=True, passwords=["+pw"], commands=["+ping"], keys=["~*"])
    try:
        conn = {**_kw(redis_port), "redis_username": "pingonly", "redis_password": "pw"}
        assert digest.read_all(**conn) == []
        assert digest.read_one("jesus", "widgets", "20261010-120000.md", **conn) is None
        assert digest.sync(tmp_path, conn, machine="jesus", make=_fake_make([]), now=NOW) == 0
    finally:
        admin.acl_deluser("pingonly")


def test_reads_do_not_wait_long_for_a_host_that_does_not_answer():
    started = time.monotonic()

    result = digest.read_all(redis_host="192.0.2.1", redis_port=6379)  # TEST-NET-1. Nothing answers.

    assert result == [] and time.monotonic() - started < 10


def test_publish_debrief_digests_reads_the_agents_redis_settings_and_state_dir(tmp_path, redis_port, flush_redis, monkeypatch):
    from lupin import loop_runtime

    _write(tmp_path, "widgets", "20261010-120000.md")
    monkeypatch.setattr(loop_runtime, "STATE_DIR", tmp_path)
    monkeypatch.setattr(debrief, "_redis_connection", lambda: ("127.0.0.1", redis_port, None, None))
    monkeypatch.setattr(digest, "make_digest", lambda repo, name, raw: ("## Summary\n\nOk.", "sonnet"))
    monkeypatch.setattr(digest.socket, "gethostname", lambda: "jesus")
    monkeypatch.setattr(digest, "_too_old", lambda name, now: False)  # The file name date is fixed. The clock is not.

    loop_runtime.publish_debrief_digests()

    entry = digest.read_one("jesus", "widgets", "20261010-120000.md", **_kw(redis_port))
    assert entry and entry["digest"].startswith("## Summary")


QUIET = """# Debrief: o/r

## Window
- From: 2026-10-10T00:00:00Z
- To: 2026-10-10T06:00:00Z
- Period: 6h

## Shipped
- None.

## Follow-up tasks
- None.

## Risk
Derived from GitHub facts and ledger decisions. Not checked against real ledger rows.
- None.

## Opportunities
- Issue #5: a ready issue

## Evidence
- None.
"""


def _real_debrief(monkeypatch, listing):
    """Build a debrief with the real generator. `listing` is what every gh list call returns."""
    from lupin import debrief

    monkeypatch.setattr(debrief, "_list", lambda time_limit, args: listing)
    start = datetime(2026, 10, 10, tzinfo=timezone.utc)
    return debrief.build_markdown("o/r", start, start + timedelta(hours=6), events=[], claimed=set(), period="6h")


def test_is_quiet_is_true_for_a_period_with_nothing_in_it():
    assert digest.is_quiet(QUIET)


def test_is_quiet_is_true_for_what_the_real_generator_writes_for_an_empty_period(monkeypatch):
    assert digest.is_quiet(_real_debrief(monkeypatch, ([], "")))


def test_is_quiet_is_false_when_the_generator_could_not_collect_the_data(monkeypatch):
    # A gh time-out says nothing about what shipped. It must not read as "nothing changed".
    assert not digest.is_quiet(_real_debrief(monkeypatch, (None, "gh time limit reached")))


@pytest.mark.parametrize(
    "old, new",
    [
        ("## Shipped\n- None.", "## Shipped\n- PR #1: a fix (merged 2026-10-10T01:00:00Z, commit abc1234)"),
        ("## Shipped\n- None.", "## Shipped\n- None.\n- Not collected: gh time limit. Shipped: merged PR."),
        ("## Follow-up tasks\n- None.", "## Follow-up tasks\n- #4: write the tests"),
        ("## Follow-up tasks\n- None.", "## Follow-up tasks\n- Error 111 connecting to localhost:6379."),
        ("## Risk\nDerived from GitHub facts and ledger decisions. Not checked against real ledger rows.\n- None.",
         "## Risk\nDerived from GitHub facts and ledger decisions. Not checked against real ledger rows.\n- Decision: ship it"),
        ("## Evidence\n- None.", "## Evidence\n- PR #3: ![PR 3 image](https://github.com/user-attachments/assets/x)"),
    ],
)
def test_is_quiet_is_false_when_anything_changed_or_is_missing(old, new):
    assert old in QUIET
    assert not digest.is_quiet(QUIET.replace(old, new))


def test_is_quiet_is_false_for_text_with_no_sections():
    assert not digest.is_quiet("# Just a note\n\nSome text.\n")


def test_sync_publishes_a_quiet_debrief_without_a_model_call_or_a_digest(tmp_path, redis_port, flush_redis):
    _write(tmp_path, "widgets", "20261010-120000-6h.md", QUIET)
    calls = []

    written = digest.sync(tmp_path, _kw(redis_port), machine="jesus", make=_fake_make(calls), now=NOW)
    again = digest.sync(tmp_path, _kw(redis_port), machine="jesus", make=_fake_make(calls), now=NOW)

    entry = digest.read_one("jesus", "widgets", "20261010-120000-6h.md", **_kw(redis_port))
    assert (written, again, calls) == (0, 0, [])
    assert entry["quiet"] is True and entry["digest"] is None and entry["tries"] == 0
    assert entry["raw"]  # The debrief is still there.


def test_a_quiet_debrief_does_not_use_up_the_model_call_limit(tmp_path, redis_port, flush_redis):
    _write(tmp_path, "widgets", "20261012-120000-6h.md", QUIET)  # newest
    _write(tmp_path, "widgets", "20261011-120000-6h.md")
    calls = []

    digest.sync(tmp_path, _kw(redis_port), machine="jesus", limit=1, make=_fake_make(calls), now=NOW)

    assert calls == [("widgets", "20261011-120000-6h.md")]


def test_a_quiet_entry_is_not_rewritten_on_every_cycle(tmp_path, redis_port, flush_redis):
    _write(tmp_path, "widgets", "20261010-120000-6h.md", QUIET)
    digest.sync(tmp_path, _kw(redis_port), machine="jesus", make=_fake_make([]), now=NOW)
    key = digest._key("jesus", "widgets", "20261010-120000-6h.md")
    client = _raw_client(redis_port)
    client.expire(key, 1000)

    digest.sync(tmp_path, _kw(redis_port), machine="jesus", make=_fake_make([]), now=NOW)

    assert client.ttl(key) <= 1000  # A rewrite would reset it to 30 days.


def test_read_refuses_an_entry_whose_quiet_flag_is_not_a_boolean(redis_port, flush_redis):
    entry = {"machine": "jesus", "repo": "widgets", "name": "20261010-120000.md", "raw": "# x", "quiet": "yes"}
    _raw_client(redis_port).set(digest._key("jesus", "widgets", "20261010-120000.md"), json.dumps(entry))

    assert digest.read_one("jesus", "widgets", "20261010-120000.md", **_kw(redis_port)) is None


ATTACH = "https://github.com/user-attachments/assets/eba1e250-ae34-481a-9f8c-98503030f83e"
OTHER = "https://github.com/user-attachments/assets/07d1bea3-3b20-4677-9dd0-ad2dc901f3e8"


def test_strip_images_keeps_a_github_attachment_the_dashboard_can_fetch_and_replaces_the_rest():
    text = f"![a]({ATTACH})\n![b](20261010-120000-6h-screenshots/a.png)\n![c](https://example.com/x.png)\n"

    out = digest.strip_images(text, "jesus")

    assert f"![a]({ATTACH})" in out
    assert "(b: kept on jesus)" in out
    assert "(c: kept on jesus)" in out  # Only GitHub attachments are served by the dashboard.


def test_limit_images_drops_a_link_the_debrief_does_not_have_and_caps_the_count(monkeypatch):
    raw = f"- Issue #1: ![i]({ATTACH})\n- Issue #2: ![j]({OTHER})\n"
    invented = "https://github.com/user-attachments/assets/11111111-1111-1111-1111-111111111111"
    text = f"# T\n\n![a]({ATTACH})\n![x]({invented})\n![b]({OTHER})\n"

    out = digest.limit_images(text, raw)

    assert ATTACH in out and OTHER in out and invented not in out
    monkeypatch.setattr(digest, "MAX_IMAGES", 1)
    assert digest.limit_images(text, raw).count("![") == 1


def test_count_shipped_counts_only_the_shipped_section():
    raw = "## Shipped\n- PR #1: a\n- PR #2: b\n- Issue #3: c\n- Commit abc1234: d\n\n## Evidence\n- PR #9: not counted\n"

    assert digest.count_shipped(raw) == "2 pull requests merged, 1 issues closed, 1 other commits"


def test_make_digest_puts_the_counts_in_the_prompt_and_removes_an_invented_image(monkeypatch):
    seen = _run_returning(monkeypatch, stdout=f"# Story\n\n![x](https://github.com/user-attachments/assets/11111111-1111-1111-1111-111111111111)\n![a]({ATTACH})\n")
    raw = f"## Shipped\n- PR #1: a\n\n## Evidence\n- Issue #1: ![i]({ATTACH})\n"

    text, _model = digest.make_digest("widgets", "20261010-120000.md", raw)

    assert "1 pull requests merged" in seen["argv"][-1]
    assert ATTACH in text and "11111111" not in text


def test_limit_images_cannot_be_beaten_by_text_that_joins_after_a_removal():
    raw = f"- Issue #1: ![i]({ATTACH})\n"
    invented = "https://github.com/user-attachments/assets/11111111-1111-1111-1111-111111111111"
    glued = f"!![x](https://example.com/a.png)[y]({invented})"

    out = digest.strip_images(digest.limit_images(glued, raw), "jesus")

    # The text may stay, but it must not be an image link, which is what the page renders.
    assert debrief.IMAGE_LINK_RE.search(out) is None


def test_limit_images_cap_cannot_be_beaten_by_joined_text(monkeypatch):
    raw = f"- Issue #1: ![i]({ATTACH})\n- Issue #2: ![j]({OTHER})\n"
    monkeypatch.setattr(digest, "MAX_IMAGES", 2)
    text = f"![a]({ATTACH})![b]({OTHER})!![z](https://example.com/z.png)[c]({ATTACH})![d]({OTHER})"

    assert digest.limit_images(text, raw).count("![") <= 2


@pytest.mark.parametrize(
    "line",
    ["- Not collected: gh error. Shipped: merged PR list.", "- Shipped: merged PR list cut at 200 items. Some items may be missing."],
)
def test_count_shipped_says_unknown_when_the_data_is_missing_or_cut(line):
    raw = f"## Shipped\n- PR #1: a\n{line}\n"

    counts = digest.count_shipped(raw)

    assert counts.startswith("unknown") and "0 pull requests" not in counts


def test_prompt_gets_the_unknown_counts_for_a_gap(monkeypatch):
    seen = _run_returning(monkeypatch, stdout="# T\n\nText.")

    digest.make_digest("widgets", "20261010-120000.md", "## Shipped\n- Not collected: gh error. Shipped: merged PR list.\n")

    assert "unknown" in seen["argv"][-1] and "0 pull requests" not in seen["argv"][-1]


def test_is_quiet_is_false_when_a_section_name_appears_twice():
    faked = QUIET + "\n## Shipped\n- None.\n"
    real_change = faked.replace("## Shipped\n- None.\n\n## Follow-up", "## Shipped\n- PR #1: real\n\n## Follow-up", 1)

    assert digest.is_quiet(QUIET) and not digest.is_quiet(real_change)


PNG = b"\x89PNG\r\n\x1a\n-test-image"
WITH_IMAGE = f"## Evidence\n- Issue #1: ![i]({ATTACH})\n" + RAW
ATTACH_ID = "eba1e250-ae34-481a-9f8c-98503030f83e"
STORY = f"# Story\n\nText.\n![i]({ATTACH})\n"


def _fake_story(calls):
    def make(repo, name, raw):
        calls.append(name)
        return STORY, "sonnet"

    return make


def _fetcher(monkeypatch, result):
    fetched = []

    def fetch(attachment_id):
        fetched.append(attachment_id)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(digest, "_fetch_image", fetch)
    return fetched


def test_image_types_match_the_dashboards():
    from lupin import serve

    assert digest.IMAGE_TYPES == serve.IMAGE_TYPES


def test_sync_copies_the_pictures_of_a_new_digest_to_redis(tmp_path, redis_port, flush_redis, monkeypatch):
    _write(tmp_path, "widgets", "20261010-120000.md", WITH_IMAGE)
    fetched = _fetcher(monkeypatch, (PNG, "image/png"))

    digest.sync(tmp_path, _kw(redis_port), machine="jesus", make=_fake_story([]), now=NOW)

    assert fetched == [ATTACH_ID]
    assert digest.read_image(ATTACH_ID, **_kw(redis_port)) == (PNG, "image/png")
    assert digest.read_image(ATTACH_ID.upper(), **_kw(redis_port)) == (PNG, "image/png")
    assert _raw_client(redis_port).ttl(digest.IMAGE_KEY_PREFIX + ATTACH_ID) > 0


def test_a_copied_picture_is_not_fetched_again(tmp_path, redis_port, flush_redis, monkeypatch):
    _write(tmp_path, "widgets", "20261010-120000.md", WITH_IMAGE)
    fetched = _fetcher(monkeypatch, (PNG, "image/png"))

    for _ in range(3):
        digest.sync(tmp_path, _kw(redis_port), machine="jesus", make=_fake_story([]), now=NOW)

    assert fetched == [ATTACH_ID]


def test_a_failed_copy_leaves_a_marker_and_is_not_retried_at_once(tmp_path, redis_port, flush_redis, monkeypatch, capsys):
    _write(tmp_path, "widgets", "20261010-120000.md", WITH_IMAGE)
    fetched = _fetcher(monkeypatch, RuntimeError("gh broke"))

    digest.sync(tmp_path, _kw(redis_port), machine="jesus", make=_fake_story([]), now=NOW)
    digest.sync(tmp_path, _kw(redis_port), machine="jesus", make=_fake_story([]), now=NOW)

    assert fetched == [ATTACH_ID]  # The marker stops a second try.
    assert digest.read_image(ATTACH_ID, **_kw(redis_port)) is None
    assert 0 < _raw_client(redis_port).ttl(digest.IMAGE_KEY_PREFIX + ATTACH_ID) <= digest.IMAGE_RETRY_S
    assert "gh broke" in capsys.readouterr().err
    assert digest.read_one("jesus", "widgets", "20261010-120000.md", **_kw(redis_port))["digest"]  # The digest is kept.


def test_a_picture_that_is_too_big_is_not_copied(tmp_path, redis_port, flush_redis, monkeypatch):
    _write(tmp_path, "widgets", "20261010-120000.md", WITH_IMAGE)
    _fetcher(monkeypatch, (b"x" * (digest.IMAGE_MAX_BYTES + 1), "image/png"))

    digest.sync(tmp_path, _kw(redis_port), machine="jesus", make=_fake_story([]), now=NOW)

    assert digest.read_image(ATTACH_ID, **_kw(redis_port)) is None


def test_sync_fills_in_the_pictures_of_a_digest_made_before_pictures_were_copied(tmp_path, redis_port, flush_redis, monkeypatch):
    _write(tmp_path, "widgets", "20261010-120000.md", WITH_IMAGE)
    _fetcher(monkeypatch, RuntimeError("down"))
    digest.sync(tmp_path, _kw(redis_port), machine="jesus", make=_fake_story([]), now=NOW)
    _raw_client(redis_port).delete(digest.IMAGE_KEY_PREFIX + ATTACH_ID)  # As if the copy never happened.
    fetched = _fetcher(monkeypatch, (PNG, "image/png"))

    calls = []
    digest.sync(tmp_path, _kw(redis_port), machine="jesus", make=_fake_story(calls), now=NOW)

    assert calls == []  # No new model call. The digest exists.
    assert fetched == [ATTACH_ID]
    assert digest.read_image(ATTACH_ID, **_kw(redis_port)) == (PNG, "image/png")


@pytest.mark.parametrize(
    "stored",
    [
        "not json",
        json.dumps({"missing": True}),
        json.dumps({"type": "image/svg+xml", "data": "AAAA"}),
        json.dumps({"type": "image/png", "data": "***not base64***"}),
        json.dumps({"type": "image/png", "data": ""}),
        json.dumps({"type": "image/png"}),
        json.dumps(["image/png", "AAAA"]),
    ],
)
def test_read_image_refuses_anything_that_is_not_a_well_formed_picture(redis_port, flush_redis, stored):
    _raw_client(redis_port).set(digest.IMAGE_KEY_PREFIX + ATTACH_ID, stored)

    assert digest.read_image(ATTACH_ID, **_kw(redis_port)) is None


@pytest.mark.parametrize("bad", ["", "../etc/passwd", "not-an-id", ATTACH_ID + "x"])
def test_read_image_refuses_a_bad_id(redis_port, flush_redis, bad):
    assert digest.read_image(bad, **_kw(redis_port)) is None


def test_read_image_returns_nothing_when_redis_fails():
    assert digest.read_image(ATTACH_ID, redis_host="127.0.0.1", redis_port=1) is None
