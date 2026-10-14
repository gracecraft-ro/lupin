"""Tests for debrief.py and the stop hook that writes a debrief."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
import redis

from lupin import agent, debrief, ledger, loop_runtime
from lupin.slots import CoordinatorUnreachable

FULL = "acme/widgets"
START = datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc)
END = datetime(2026, 10, 2, 17, 0, tzinfo=timezone.utc)
UUID_A = "11111111-2222-3333-4444-555555555555"
UUID_B = "66666666-7777-8888-9999-aaaaaaaaaaaa"
UUID_C = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"
UUID_D = "12121212-3434-5656-7878-909090909090"


def _url(attachment: str) -> str:
    return f"https://github.com/user-attachments/assets/{attachment}"


def _key(args: list[str]) -> str:
    def flag(name: str) -> str:
        return args[args.index(name) + 1] if name in args else ""

    if args[0] == "api":
        return "api"
    return " ".join([*args[:2], flag("--state"), flag("--label")]).strip()


class FakeGh:
    """Stands in for `debrief._gh`. Answers by sub-command, state and label."""

    def __init__(self, responses: dict):
        self.responses = responses
        self.calls: list[list[str]] = []

    def __call__(self, args, cwd=None, timeout=None):
        self.calls.append(list(args))
        key = _key(args)
        if key not in self.responses:
            raise AssertionError(f"unexpected gh call: {key!r}")
        if isinstance(self.responses[key], Exception):
            raise self.responses[key]
        return self.responses[key]


def _debrief_client_on(redis_port):
    """Stand-in for slots_redis.debrief_client, pointed at the test server."""
    return lambda *_args: redis.Redis(host="127.0.0.1", port=redis_port, decode_responses=True)


def _assert_claims_client_is_bounded(claims_for):
    # write_debrief passes the short-timeout client from debrief_client.
    client = claims_for.call_args.kwargs["client"]
    assert client.connection_pool.connection_kwargs["socket_timeout"] == debrief.slots_redis.DEBRIEF_TIMEOUT_S
    assert client.get_retry().get_retries() == 0


RESPONSES = {
    "repo view": {"nameWithOwner": FULL},
    "pr list merged": [
        {"number": 10, "title": "Early merge", "mergedAt": "2026-10-02T08:00:00Z",
         "mergeCommit": {"oid": "aaaaaaa1111"}},
        {"number": 11, "title": "Shipped feature", "mergedAt": "2026-10-02T10:00:00Z",
         "mergeCommit": {"oid": "bbbbbbb2222"}},
    ],
    "issue list closed": [
        {"number": 5, "title": "Closed in window", "closedAt": "2026-10-02T12:00:00Z"},
        {"number": 6, "title": "Closed before", "closedAt": "2026-10-02T07:00:00Z"},
    ],
    "pr list open": [
        {"number": 20, "title": "Red build",
         "statusCheckRollup": [{"name": "test", "conclusion": "FAILURE"}]},
        {"number": 21, "title": "Green build",
         "statusCheckRollup": [{"name": "test", "conclusion": "SUCCESS"}]},
    ],
    "issue list open blocked": [{"number": 30, "title": "Waits on vendor"}],
    "issue list open ready": [
        {"number": 40, "title": "Ready one"},
        {"number": 41, "title": "Ready two claimed"},
    ],
    "issue list all": [
        {"number": 5, "title": "Closed in window",
         "body": f"Screens: ![shot]({_url(UUID_A)})",
         "comments": [{"body": "![no](https://example.com/x.png)", "createdAt": "2026-10-02T11:00:00Z"}],
         "updatedAt": "2026-10-02T12:00:00Z"},
        {"number": 7, "title": "Stale",
         "body": f"![old]({_url(UUID_B)})", "comments": [],
         "updatedAt": "2026-10-01T23:00:00Z"},
        {"number": 8, "title": "Bad ids",
         "body": ("![x](https://github.com/user-attachments/assets/not-a-uuid) "
                  "![y](https://private-user-images.githubusercontent.com/1/2.png?jwt=abc)"),
         "comments": [], "updatedAt": "2026-10-02T13:00:00Z"},
    ],
    "pr list all": [
        {"number": 11, "title": "Shipped feature", "body": "",
         "comments": [
             {"body": f"![c]({_url(UUID_C)})", "createdAt": "2026-10-02T10:30:00Z"},
             {"body": f"![d]({_url(UUID_D)})", "createdAt": "2026-10-01T10:00:00Z"},
         ],
         "updatedAt": "2026-10-02T10:30:00Z"},
    ],
}

EVENTS = [
    {"timestamp": "2026-10-02T11:00:00Z", "issue": 5, "decisions": ["Use the cache"],
     "next": ["Write docs"]},
    {"timestamp": "2026-10-01T11:00:00Z", "decisions": ["Old decision"], "next": ["Old task"]},
]


def _markdown(responses=None, **kwargs) -> str:
    fake = FakeGh({**RESPONSES, **(responses or {})})
    with mock.patch.object(debrief, "_gh", fake):
        return debrief.build_markdown(FULL, START, END, **kwargs)


def _section(markdown: str, heading: str) -> str:
    """Return the text under `## heading` up to the next heading."""
    after = markdown.split(f"## {heading}\n", 1)[1]
    return after.split("\n## ", 1)[0]


def test_shipped_lists_only_items_in_the_window():
    fake = FakeGh(dict(RESPONSES))
    with mock.patch.object(debrief, "_gh", fake):
        md = debrief.build_markdown(FULL, START, END)

    shipped = _section(md, "Shipped")
    assert "- PR #11: Shipped feature (merged 2026-10-02T10:00:00Z, commit bbbbbbb)" in shipped
    assert "- Issue #5: Closed in window (closed 2026-10-02T12:00:00Z)" in shipped
    assert "Early merge" not in shipped
    assert "Closed before" not in shipped
    assert ["pr", "list", "--repo", FULL, "--state", "merged", "--search",
            "merged:>=2026-10-02", "--json", "number,title,mergedAt,mergeCommit",
            "--limit", "200"] in fake.calls


def test_risk_lists_failing_prs_blocked_issues_and_window_decisions():
    md = _markdown(events=EVENTS)

    risk = _section(md, "Risk")
    assert "- PR #20: Red build (failing: test)" in risk
    assert "Green build" not in risk
    assert "- Issue #30: Waits on vendor (labelled blocked)" in risk
    assert "- Decision: Use the cache" in risk
    assert "Old decision" not in risk
    assert "Derived from GitHub facts and ledger decisions. Not checked against real ledger rows." in risk


def test_opportunities_skip_issues_claimed_in_redis():
    md = _markdown(claimed={41})

    opportunities = _section(md, "Opportunities")
    assert "- Issue #40: Ready one" in opportunities
    assert "Ready two claimed" not in opportunities


def test_opportunities_say_when_claims_were_not_read():
    md = _markdown(claimed=None)

    opportunities = _section(md, "Opportunities")
    assert "Claims not read." in opportunities
    assert "Ready two claimed" in opportunities


def test_follow_up_uses_only_window_events():
    md = _markdown(events=EVENTS)

    follow_up = _section(md, "Follow-up tasks")
    assert "- #5: Write docs" in follow_up
    assert "Old task" not in follow_up


def test_follow_up_names_why_the_ledger_was_not_read():
    assert "No ledger read." in _section(_markdown(), "Follow-up tasks")
    note = _markdown(ledger_note="Ledger unavailable: down")
    assert "- Ledger unavailable: down" in _section(note, "Follow-up tasks")


def test_forced_stop_uses_github_facts_only():
    md = _markdown(forced=True, events=EVENTS, claimed={41})

    assert "- Stop: forced, no handoff" in md
    assert "Forced stop. Ledger not read." in _section(md, "Follow-up tasks")
    assert "Forced stop. Claims not read." in _section(md, "Opportunities")
    assert "Write docs" not in md
    assert "Use the cache" not in md


NO_RISK = {"pr list open": [], "issue list open blocked": []}


def test_forced_risk_says_decisions_were_not_collected():
    md = _markdown(NO_RISK, forced=True)

    risk = _section(md, "Risk")
    assert "- None." not in risk
    assert "- Decisions not collected: forced stop." in risk


def test_evidence_keeps_only_github_attachment_uuids():
    md = _markdown()

    evidence = _section(md, "Evidence")
    assert f"- Issue #5: ![Issue 5 image]({_url(UUID_A)})" in evidence
    assert f"- PR #11: ![PR 11 image]({_url(UUID_C)})" in evidence
    for refused in (UUID_B, UUID_D, "not-a-uuid", "example.com", "private-user-images"):
        assert refused not in evidence


def test_write_debrief_keeps_other_sections_when_ledger_is_down(tmp_path: Path, monkeypatch):
    checkout = tmp_path / "widgets"
    checkout.mkdir()
    monkeypatch.setattr(debrief, "_gh", FakeGh(dict(RESPONSES)))
    monkeypatch.setattr(debrief.ledger, "read_events", mock.Mock(side_effect=CoordinatorUnreachable(FULL)))
    monkeypatch.setattr(debrief.claims, "claims_for", lambda repos, **kw: {})

    path = debrief.write_debrief(tmp_path, "widgets", checkout, "2026-10-02T09:00:00Z")

    text = path.read_text(encoding="utf-8")
    assert "Ledger unavailable" in _section(text, "Follow-up tasks")
    assert "- PR #11: Shipped feature" in text
    assert path.parent == tmp_path / "debriefs" / "widgets"
    assert path.name.endswith(".md") and debrief.FILE_RE.fullmatch(path.name)
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_two_stop_debriefs_in_the_same_second_do_not_replace_each_other(tmp_path: Path, monkeypatch):
    # Two loops of one repo can stop in the same second.
    checkout = tmp_path / "widgets"
    checkout.mkdir()
    monkeypatch.setattr(debrief, "_gh", FakeGh(dict(RESPONSES)))
    end = datetime(2026, 10, 13, 12, 0, 5, tzinfo=timezone.utc)

    first = debrief._write(
        tmp_path, "widgets", checkout, datetime(2026, 10, 13, 11, 0, tzinfo=timezone.utc), end, forced=True,
    )
    second = debrief._write(
        tmp_path, "widgets", checkout, datetime(2026, 10, 13, 11, 30, tzinfo=timezone.utc), end, forced=True,
    )

    assert (first.name, second.name) == ("20261013-120005.md", "20261013-120006.md")
    assert debrief.FILE_RE.fullmatch(second.name)
    assert "2026-10-13T11:00:00" in first.read_text(encoding="utf-8")
    assert "2026-10-13T11:30:00" in second.read_text(encoding="utf-8")
    assert sorted(path.name for path in first.parent.iterdir()) == [first.name, second.name]


def test_a_failed_stop_debrief_leaves_no_empty_file(tmp_path: Path, monkeypatch):
    checkout = tmp_path / "widgets"
    checkout.mkdir()
    monkeypatch.setattr(debrief, "_gh", FakeGh(dict(RESPONSES)))
    monkeypatch.setattr(debrief, "_store", mock.Mock(side_effect=OSError("disk full")))
    end = datetime(2026, 10, 13, 12, 0, 5, tzinfo=timezone.utc)

    with pytest.raises(OSError):
        debrief._write(
            tmp_path, "widgets", checkout, datetime(2026, 10, 13, 11, 0, tzinfo=timezone.utc), end, forced=True,
        )

    assert list((tmp_path / "debriefs" / "widgets").iterdir()) == []


def test_failed_list_call_cuts_only_its_section(tmp_path: Path, monkeypatch):
    checkout = tmp_path / "widgets"
    checkout.mkdir()
    responses = {**RESPONSES, "pr list merged": debrief.DebriefError("gh pr list exited 1: boom")}
    monkeypatch.setattr(debrief, "_gh", FakeGh(responses))
    monkeypatch.setattr(debrief.ledger, "read_events", lambda repo, **kw: [])
    monkeypatch.setattr(debrief.claims, "claims_for", lambda repos, **kw: {})

    path = debrief.write_debrief(tmp_path, "widgets", checkout, "2026-10-02T09:00:00Z")

    text = path.read_text(encoding="utf-8")
    shipped = _section(text, "Shipped")
    assert "- Not collected: gh error. Shipped: merged PR list." in shipped
    assert "- Issue #5: Closed in window" in shipped
    assert "- PR #20: Red build" in _section(text, "Risk")


def test_malformed_ledger_row_is_skipped_without_losing_other_rows(
    tmp_path: Path, monkeypatch, redis_port, flush_redis, capsys
):
    checkout = tmp_path / "widgets"
    checkout.mkdir()
    client = redis.Redis(host="127.0.0.1", port=redis_port)
    # The first row is malformed. The next row must still reach the debrief.
    client.xadd(ledger._stream_key(FULL), {"host": "h", "event": "shipped"})
    client.xadd(ledger._stream_key(FULL), {
        "ts": "2026-10-02T10:00:00Z", "host": "h", "event": "shipped",
        "issue": "12", "next": json.dumps(["Keep going."]),
    })
    monkeypatch.setattr(debrief, "_gh", FakeGh(dict(RESPONSES)))
    monkeypatch.setattr(debrief.slots_redis, "debrief_client", _debrief_client_on(redis_port))

    path = debrief.write_debrief(tmp_path, "widgets", checkout, "2026-10-02T09:00:00Z")

    text = path.read_text(encoding="utf-8")
    assert "- #12: Keep going." in _section(text, "Follow-up tasks")
    assert "Ledger unavailable" not in text
    assert "skipped bad entry" in capsys.readouterr().err
    assert "- PR #11: Shipped feature" in text
    assert "## Evidence" in text


def test_wrong_type_ledger_row_is_skipped_and_other_rows_kept(
    tmp_path: Path, monkeypatch, redis_port, flush_redis
):
    checkout = tmp_path / "widgets"
    checkout.mkdir()
    # The first row has "next" as the number 5, not a list. Real Redis, real ledger.read_events.
    client = redis.Redis(host="127.0.0.1", port=redis_port)
    client.xadd(ledger._stream_key(FULL), {
        "ts": "2026-10-02T10:00:00Z", "host": "h", "event": "shipped", "next": "5",
    })
    client.xadd(ledger._stream_key(FULL), {
        "ts": "2026-10-02T11:00:00Z", "host": "h", "event": "shipped", "issue": "5",
        "next": json.dumps(["Write docs"]),
    })
    monkeypatch.setattr(debrief, "_gh", FakeGh(dict(RESPONSES)))
    monkeypatch.setattr(debrief.slots_redis, "debrief_client", _debrief_client_on(redis_port))

    path = debrief.write_debrief(tmp_path, "widgets", checkout, "2026-10-02T09:00:00Z")

    text = path.read_text(encoding="utf-8")
    assert "- #5: Write docs" in _section(text, "Follow-up tasks")
    assert "- PR #11: Shipped feature" in text
    assert "## Risk" in text
    assert "## Evidence" in text


def test_ledger_times_that_overflow_utc_are_skipped(
    tmp_path: Path, monkeypatch, redis_port, flush_redis
):
    checkout = tmp_path / "widgets"
    checkout.mkdir()
    # Two times that overflow when shifted to UTC, then one normal row. Real Redis.
    client = redis.Redis(host="127.0.0.1", port=redis_port)
    for ts, text in (
        ("9999-12-31T23:59:59-01:00", "Too late"),
        ("0001-01-01T00:00:00+01:00", "Too early"),
        ("2026-10-02T10:00:00Z", "Write docs"),
    ):
        client.xadd(ledger._stream_key(FULL), {
            "ts": ts, "host": "h", "event": "shipped", "issue": "5", "next": json.dumps([text]),
        })
    monkeypatch.setattr(debrief, "_gh", FakeGh(dict(RESPONSES)))
    monkeypatch.setattr(debrief.slots_redis, "debrief_client", _debrief_client_on(redis_port))

    path = debrief.write_debrief(tmp_path, "widgets", checkout, "2026-10-02T09:00:00Z")

    text = path.read_text(encoding="utf-8")
    assert "- #5: Write docs" in _section(text, "Follow-up tasks")
    assert "Too late" not in text
    assert "Too early" not in text
    assert "- PR #11: Shipped feature" in text


def test_ledger_note_gives_the_redis_reason(tmp_path: Path, monkeypatch):
    checkout = tmp_path / "widgets"
    checkout.mkdir()

    def read_events(repo, **kwargs):
        raise CoordinatorUnreachable(repo) from redis.exceptions.ConnectionError("Connection refused")

    monkeypatch.setattr(debrief, "_gh", FakeGh(dict(RESPONSES)))
    monkeypatch.setattr(debrief.ledger, "read_events", read_events)
    monkeypatch.setattr(debrief.claims, "claims_for", lambda repos, **kw: {})

    path = debrief.write_debrief(tmp_path, "widgets", checkout, "2026-10-02T09:00:00Z")

    assert "Ledger unavailable: Connection refused" in _section(path.read_text(encoding="utf-8"), "Follow-up tasks")


def test_ledger_failure_risk_says_decisions_were_not_collected(tmp_path: Path, monkeypatch):
    checkout = tmp_path / "widgets"
    checkout.mkdir()

    def read_events(repo, **kwargs):
        raise CoordinatorUnreachable(repo) from redis.exceptions.ConnectionError("Connection refused")

    monkeypatch.setattr(debrief, "_gh", FakeGh({**RESPONSES, **NO_RISK}))
    monkeypatch.setattr(debrief.ledger, "read_events", read_events)
    monkeypatch.setattr(debrief.claims, "claims_for", lambda repos, **kw: {})

    path = debrief.write_debrief(tmp_path, "widgets", checkout, "2026-10-02T09:00:00Z")

    risk = _section(path.read_text(encoding="utf-8"), "Risk")
    assert "- None." not in risk
    assert "- Decisions not collected: Ledger unavailable: Connection refused." in risk


def test_claims_error_still_writes_the_debrief(tmp_path: Path, monkeypatch):
    checkout = tmp_path / "widgets"
    checkout.mkdir()
    claims_for = mock.Mock(side_effect=CoordinatorUnreachable("claims_for"))
    monkeypatch.setattr(debrief, "_gh", FakeGh(dict(RESPONSES)))
    monkeypatch.setattr(debrief.ledger, "read_events", lambda repo, **kw: [])
    monkeypatch.setattr(debrief.claims, "claims_for", claims_for)

    path = debrief.write_debrief(tmp_path, "widgets", checkout, "2026-10-02T09:00:00Z")

    text = path.read_text(encoding="utf-8")
    claims_for.assert_called_once()
    assert claims_for.call_args.args == ([FULL],)
    _assert_claims_client_is_bounded(claims_for)
    assert "Claims not read. Listed issues may already be claimed." in text
    assert "- Issue #41: Ready two claimed" in text


def test_write_debrief_skips_issues_claimed_in_redis(tmp_path: Path, monkeypatch):
    checkout = tmp_path / "widgets"
    checkout.mkdir()
    monkeypatch.setattr(debrief, "_gh", FakeGh(dict(RESPONSES)))
    monkeypatch.setattr(debrief.ledger, "read_events", lambda repo, **kw: [])
    monkeypatch.setattr(debrief.claims, "claims_for", mock.Mock(return_value={
        f"{FULL}#41": {"host": "jesus"}, "other/repo#40": {"host": "ralpha"},
    }))

    path = debrief.write_debrief(tmp_path, "widgets", checkout, "2026-10-02T09:00:00Z")

    text = path.read_text(encoding="utf-8")
    assert "Ready two claimed" not in text
    assert "- Issue #40: Ready one" in text
    debrief.claims.claims_for.assert_called_once()
    assert debrief.claims.claims_for.call_args.args == ([FULL],)
    _assert_claims_client_is_bounded(debrief.claims.claims_for)


def test_redis_calls_do_not_grow_with_the_claim_count(
    tmp_path: Path, monkeypatch, redis_port, flush_redis, counting_redis
):
    checkout = tmp_path / "widgets"
    checkout.mkdir()
    client = redis.Redis(host="127.0.0.1", port=redis_port, decode_responses=True)
    for number in range(1, 6):
        client.set(f"lupin:v1:claim:{FULL}#{number}", json.dumps({"host": "jesus", "session": "s", "since": 0}))
    counting = counting_redis(client)
    monkeypatch.setattr(debrief, "_gh", FakeGh(dict(RESPONSES)))
    monkeypatch.setattr(debrief.slots_redis, "debrief_client", lambda *_args: counting)

    debrief.write_debrief(tmp_path, "widgets", checkout, "2026-10-02T09:00:00Z")

    # The stop budget lists three debrief Redis calls: ledger read, claims scan, claims read.
    # The claims read is one pipeline, so it shows as pipeline plus execute.
    assert counting.calls == ["xrange", "scan_iter", "pipeline", "execute"]


def test_write_debrief_forced_reads_no_redis(tmp_path: Path, monkeypatch):
    checkout = tmp_path / "widgets"
    checkout.mkdir()
    read_events = mock.Mock()
    claims_for = mock.Mock()
    monkeypatch.setattr(debrief, "_gh", FakeGh(dict(RESPONSES)))
    monkeypatch.setattr(debrief.ledger, "read_events", read_events)
    monkeypatch.setattr(debrief.claims, "claims_for", claims_for)

    path = debrief.write_debrief(tmp_path, "widgets", checkout, "2026-10-02T09:00:00Z", forced=True)

    read_events.assert_not_called()
    claims_for.assert_not_called()
    assert "Forced stop. Ledger not read." in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("repo, started_at, make_checkout", [
    ("../widgets", "2026-10-02T09:00:00Z", True),
    ("widgets", None, True),
    ("widgets", "not a time", True),
    ("widgets", "2026-10-02T09:00:00Z", False),
])
def test_write_debrief_refuses_and_writes_nothing(tmp_path: Path, repo, started_at, make_checkout):
    checkout = tmp_path / "widgets"
    if make_checkout:
        checkout.mkdir()

    with pytest.raises(debrief.DebriefError):
        debrief.write_debrief(tmp_path, repo, checkout, started_at)

    assert not (tmp_path / "debriefs").exists()


def test_render_html_escapes_text_and_keeps_only_attachment_images():
    markdown = "\n".join([
        "# Debrief: acme/widgets",
        "- Issue #5: <script>alert(1)</script> title",
        f"- Issue #5: ![shot]({_url(UUID_A)})",
        "- Issue #8: ![bad](https://example.com/x.png)",
    ])

    html = debrief.render_html(markdown, "widgets")

    assert html.startswith("<h1>Debrief: acme/widgets</h1>")
    assert "<script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert f"<img src='/image?id={UUID_A}'" in html
    assert "![bad](https://example.com/x.png)" in html
    assert html.count("<img") == 1


def test_list_and_read_debriefs(tmp_path: Path):
    for repo, name in (
        ("widgets", "20261001-090000.md"),
        ("widgets", "20261002-090000.md"),
        ("other", "20261001-120000.md"),
    ):
        folder = tmp_path / "debriefs" / repo
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text(f"# Debrief: {repo}\n", encoding="utf-8")
    (tmp_path / "debriefs" / "widgets" / "notes.md").write_text("x", encoding="utf-8")

    assert debrief.list_debriefs(tmp_path) == [
        ("widgets", "20261002-090000.md"),
        ("other", "20261001-120000.md"),
        ("widgets", "20261001-090000.md"),
    ]
    assert debrief.read_debrief(tmp_path, "other", "20261001-120000.md") == "# Debrief: other\n"
    for repo, name in (("../widgets", "20261001-090000.md"), ("widgets", "../x.md"),
                       ("widgets", "missing.md"), ("nope", "20261001-090000.md")):
        with pytest.raises(debrief.DebriefError):
            debrief.read_debrief(tmp_path, repo, name)


NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)


def _stub_io(monkeypatch, gh=None):
    monkeypatch.setattr(debrief, "_gh", gh or FakeGh(dict(RESPONSES)))
    monkeypatch.setattr(debrief.ledger, "read_events", lambda repo, **kw: [])
    monkeypatch.setattr(debrief.claims, "claims_for", lambda repos, **kw: {})


def _debrief_file(root: Path, repo: str, name: str, text: str = "# old\n") -> None:
    folder = root / "debriefs" / repo
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(text, encoding="utf-8")


def test_build_markdown_period_replaces_the_stop_line():
    md = _markdown(period="6h")

    assert "- Period: 6h" in md
    assert "- Stop:" not in md
    assert "- Stop: normal" in _markdown()


def test_write_period_without_earlier_file_starts_one_period_back(tmp_path: Path, monkeypatch):
    checkout = tmp_path / "widgets"
    checkout.mkdir()
    _stub_io(monkeypatch)

    path = debrief.write_period(tmp_path, "widgets", checkout, "6h", now=NOW)

    text = path.read_text(encoding="utf-8")
    assert path.name == "20261010-120000-6h.md"
    assert "- From: 2026-10-10T06:00:00Z" in text
    assert "- To: 2026-10-10T12:00:00Z" in text
    assert "- Period: 6h" in text


def test_stop_debriefs_do_not_set_the_period_start(tmp_path: Path, monkeypatch):
    checkout = tmp_path / "widgets"
    checkout.mkdir()
    _stub_io(monkeypatch)
    _debrief_file(tmp_path, "widgets", "20261010-110000.md")

    path = debrief.write_period(tmp_path, "widgets", checkout, "6h", now=NOW)

    assert "- From: 2026-10-10T06:00:00Z" in path.read_text(encoding="utf-8")


def test_write_period_starts_at_the_newest_period_file(tmp_path: Path, monkeypatch):
    checkout = tmp_path / "widgets"
    checkout.mkdir()
    _stub_io(monkeypatch)
    _debrief_file(tmp_path, "widgets", "20261010-110000-6h.md")
    _debrief_file(tmp_path, "widgets", "20261010-090000-6h.md")

    path = debrief.write_period(tmp_path, "widgets", checkout, "6h", now=NOW)

    assert "- From: 2026-10-10T11:00:00Z" in path.read_text(encoding="utf-8")


def test_write_period_refuses_an_unknown_period(tmp_path: Path):
    with pytest.raises(debrief.DebriefError):
        debrief.write_period(tmp_path, "widgets", tmp_path, "3h", now=NOW)
    assert not (tmp_path / "debriefs").exists()


@pytest.mark.parametrize("period", list(debrief.PERIODS))
def test_period_due_changes_at_exactly_one_period(tmp_path: Path, period: str):
    assert debrief.period_due(tmp_path, "widgets", period, NOW) is True  # No file yet.
    last = datetime(2026, 10, 10, 6, 0, 0, tzinfo=timezone.utc)
    _debrief_file(tmp_path, "widgets", f"{last:%Y%m%d-%H%M%S}-{period}.md")
    span = debrief.PERIODS[period]

    assert debrief.period_due(tmp_path, "widgets", period, last + span - timedelta(seconds=1)) is False
    assert debrief.period_due(tmp_path, "widgets", period, last + span) is True


def test_file_name_time_within_one_period_blocks_the_write(tmp_path: Path):
    _debrief_file(tmp_path, "widgets", "20261010-180000-6h.md")

    assert debrief.period_due(tmp_path, "widgets", "6h", NOW) is False


def test_period_due_when_the_last_file_name_time_is_in_2030(tmp_path: Path):
    _debrief_file(tmp_path, "widgets", "20301010-120000-6h.md")

    assert debrief.period_due(tmp_path, "widgets", "6h", NOW) is True


def test_stamp_at_now_counts_as_the_last_write(tmp_path: Path):
    _debrief_file(tmp_path, "widgets", "20261010-120000-6h.md")

    # At NOW the result is False with or without a block. One period later it is True.
    assert debrief.period_due(tmp_path, "widgets", "6h", NOW) is False
    assert debrief.period_due(tmp_path, "widgets", "6h", NOW + timedelta(hours=6)) is True


def test_clock_stepped_back_does_not_write_an_overlapping_window(tmp_path: Path, monkeypatch):
    code = tmp_path / "code"
    (code / "widgets").mkdir(parents=True)
    state = tmp_path / "state"
    # The clock was at 12:00, then stepped back to 06:00. The 12:00 file is still on disk.
    _debrief_file(state, "widgets", "20261010-000000-6h.md")
    _debrief_file(state, "widgets", "20261010-120000-6h.md")
    at_six = datetime(2026, 10, 10, 6, 0, tzinfo=timezone.utc)
    _stub_io(monkeypatch)
    monkeypatch.setattr(loop_runtime, "STATE_DIR", state)
    monkeypatch.setattr(loop_runtime, "CODE_DIR", code)
    monkeypatch.setattr(loop_runtime, "enabled_repos", lambda: {"widgets": "claude"})

    assert debrief.period_due(state, "widgets", "6h", at_six) is False
    # write_period does not check if a write is due. The periodic loop does.
    loop_runtime.write_due_periodic_debriefs(now=at_six)

    names = sorted(path.name for path in (state / "debriefs" / "widgets").glob("*-6h.md"))
    assert names == ["20261010-000000-6h.md", "20261010-120000-6h.md"]


def test_eight_hour_clock_step_back_blocks_the_write(tmp_path: Path):
    _debrief_file(tmp_path, "widgets", "20261010-060000-6h.md")
    _debrief_file(tmp_path, "widgets", "20261010-200000-6h.md")

    assert debrief.period_due(tmp_path, "widgets", "6h", NOW) is False


def test_period_file_ending_exactly_seven_days_out_blocks_the_write(tmp_path: Path):
    # NOW + 7 days is the horizon edge. The block includes it.
    _debrief_file(tmp_path, "widgets", "20261017-120000-6h.md")

    assert debrief.period_due(tmp_path, "widgets", "6h", NOW) is False


def test_future_file_name_time_writes_one_window_and_then_waits(tmp_path: Path, monkeypatch):
    checkout = tmp_path / "widgets"
    checkout.mkdir()
    _stub_io(monkeypatch)
    _debrief_file(tmp_path, "widgets", "20301010-120000-6h.md")

    path = debrief.write_period(tmp_path, "widgets", checkout, "6h", now=NOW)

    assert "- From: 2026-10-10T06:00:00Z" in path.read_text(encoding="utf-8")
    # The new file ends at NOW, so the next check must wait a full period.
    assert debrief.period_due(tmp_path, "widgets", "6h", NOW) is False


def test_last_period_end_skips_an_impossible_date(tmp_path: Path):
    _debrief_file(tmp_path, "widgets", "20261340-250000-7d.md")
    _debrief_file(tmp_path, "widgets", "20261010-120000-7d.md")

    last = debrief.last_period_end(tmp_path, "widgets", "7d")

    assert last == datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc)


def test_list_and_read_periodic_debriefs(tmp_path: Path):
    _debrief_file(tmp_path, "widgets", "20261010-120000.md", "# stop\n")
    _debrief_file(tmp_path, "widgets", "20261010-120000-6h.md", "# six\n")
    _debrief_file(tmp_path, "widgets", "20261010-120000-3h.md", "# three\n")

    listed = debrief.list_debriefs(tmp_path)

    assert ("widgets", "20261010-120000-6h.md") in listed
    assert ("widgets", "20261010-120000.md") in listed
    assert ("widgets", "20261010-120000-3h.md") not in listed
    assert debrief.read_debrief(tmp_path, "widgets", "20261010-120000-6h.md") == "# six\n"
    with pytest.raises(debrief.DebriefError):
        debrief.read_debrief(tmp_path, "widgets", "20261010-120000-3h.md")


def test_list_sorts_one_time_stamp_by_period_length_then_stop_file(tmp_path: Path):
    for name in ("20261010-120000-6h.md", "20261010-120000-12h.md", "20261010-120000-24h.md",
                 "20261010-120000-7d.md", "20261010-120000.md", "20261010-110000-7d.md"):
        _debrief_file(tmp_path, "widgets", name)

    assert [name for _repo, name in debrief.list_debriefs(tmp_path)] == [
        "20261010-120000-7d.md",
        "20261010-120000-24h.md",
        "20261010-120000-12h.md",
        "20261010-120000-6h.md",
        "20261010-120000.md",
        "20261010-110000-7d.md",
    ]


def test_write_due_periodic_debriefs_writes_only_what_is_due(tmp_path: Path, monkeypatch, capsys):
    code = tmp_path / "code"
    (code / "widgets").mkdir(parents=True)
    (code / "broken").mkdir()
    state = tmp_path / "state"
    # The 6h debrief ended at 11:00, so 6h is not due at 12:00. 12h, 24h and 7d have no file.
    _debrief_file(state, "widgets", "20261010-110000-6h.md")

    class FailingForBroken(FakeGh):
        def __call__(self, args, cwd=None, timeout=None):
            if cwd and cwd.endswith("broken"):
                raise debrief.DebriefError("gh repo view exited 1")
            return super().__call__(args, cwd=cwd, timeout=timeout)

    _stub_io(monkeypatch, gh=FailingForBroken(dict(RESPONSES)))
    monkeypatch.setattr(loop_runtime, "STATE_DIR", state)
    monkeypatch.setattr(loop_runtime, "CODE_DIR", code)
    monkeypatch.setattr(loop_runtime, "enabled_repos", lambda: {
        "broken": "claude", "gone": "claude", "widgets": "claude",
    })

    loop_runtime.write_due_periodic_debriefs(now=NOW)

    names = sorted(path.name for path in (state / "debriefs" / "widgets").iterdir())
    assert names == [
        "20261010-110000-6h.md", "20261010-120000-12h.md",
        "20261010-120000-24h.md", "20261010-120000-7d.md",
    ]
    err = capsys.readouterr().err
    # The broken repo fails each period. The widgets writes still happen after it.
    for period in debrief.PERIODS:
        assert f"lupin agent: no {period} debrief for broken: gh repo view exited 1" in err
    # The gone repo has no checkout, so it is skipped without a warning.
    assert "gone" not in err
    assert not (state / "debriefs" / "gone").exists()


def test_periodic_check_survives_a_bad_repos_file(monkeypatch, capsys):
    def bad_repos():
        raise loop_runtime.LoopError("could not read repos")

    monkeypatch.setattr(loop_runtime, "enabled_repos", bad_repos)

    loop_runtime.write_due_periodic_debriefs(now=NOW)

    assert "lupin agent: no periodic debriefs: could not read repos" in capsys.readouterr().err


def test_periodic_check_survives_a_non_utf8_repos_file(monkeypatch, tmp_path: Path, capsys):
    repos = tmp_path / "repos"
    repos.write_bytes(b"\xff\n")
    monkeypatch.setattr(loop_runtime, "REPOS_FILE", repos)

    loop_runtime.write_due_periodic_debriefs(now=NOW)

    assert "lupin agent: no periodic debriefs:" in capsys.readouterr().err


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "evidence").mkdir()
    (repo / "docs" / "shot.png").write_bytes(b"png-bytes")
    (repo / "evidence" / "diagram.JPG").write_bytes(b"jpg-bytes")
    (repo / "src").mkdir()
    (repo / "src" / "shot.png").write_bytes(b"outside-docs")
    return repo


def test_read_evidence_serves_images_under_docs_and_evidence(checkout: Path):
    assert debrief.read_evidence(checkout, "docs/shot.png") == (b"png-bytes", "image/png")
    assert debrief.read_evidence(checkout, "evidence/diagram.JPG") == (b"jpg-bytes", "image/jpeg")


@pytest.mark.parametrize("rel", [
    "/etc/passwd.png",
    "docs/../secret.png",
    "docs/a://b.png",
    "docs\\shot.png",
    "docs/shot.svg",
    "docs/shot",
    "src/shot.png",
    "",
])
def test_read_evidence_refuses(checkout: Path, rel: str):
    assert debrief.read_evidence(checkout, rel) is None


def test_read_evidence_refuses_symlinks_out_of_the_repo(checkout: Path, tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.png").write_bytes(b"secret")
    (checkout / "docs" / "link.png").symlink_to(outside / "secret.png")
    (checkout / "evidence" / "linked-dir").symlink_to(outside, target_is_directory=True)

    assert debrief.read_evidence(checkout, "docs/link.png") is None
    assert debrief.read_evidence(checkout, "evidence/linked-dir/secret.png") is None


def test_read_evidence_refuses_oversized_files(checkout: Path, monkeypatch):
    monkeypatch.setattr(debrief, "MAX_EVIDENCE_BYTES", 3)

    assert debrief.read_evidence(checkout, "docs/shot.png") is None


def _stop_env(monkeypatch, tmp_path: Path):
    metadata = {
        "repo": "widgets", "platform": "claude", "session": "lupin-widgets-abc123",
        "state": "running", "workspace_id": "workspace-1", "pane_id": "pane-1",
        "started_at": "2026-10-02T09:00:00Z",
    }
    actions: list[tuple] = []
    written: dict = {}
    monkeypatch.setattr(loop_runtime, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(loop_runtime, "CODE_DIR", tmp_path / "code")
    monkeypatch.setattr(loop_runtime, "_read_metadata", lambda repo: dict(metadata))
    monkeypatch.setattr(loop_runtime, "_start_server_for_read", lambda repo, data: data["session"])
    monkeypatch.setattr(loop_runtime, "_find_workspace", lambda session, data: {"id": "workspace-1"})
    monkeypatch.setattr(loop_runtime, "_save_report", lambda repo, session, workspace: tmp_path / "report.log")
    monkeypatch.setattr(loop_runtime, "_workspace_id", lambda workspace: workspace["id"])
    monkeypatch.setattr(loop_runtime, "_agent_for_workspace", lambda session, workspace_id: None)
    monkeypatch.setattr(loop_runtime, "_herdr_json", lambda session, *args: actions.append(args) or {})
    monkeypatch.setattr(loop_runtime, "_herdr", lambda session, *args, timeout=0: actions.append(args) or "")
    monkeypatch.setattr(loop_runtime, "_workspaces", lambda session: [])
    monkeypatch.setattr(loop_runtime, "_write_metadata", lambda repo, value: written.update(value))
    monkeypatch.setattr(loop_runtime, "_run", lambda argv, **kwargs: (0, "inactive"))
    return actions, written


def test_failing_debrief_does_not_block_the_stop(monkeypatch, tmp_path: Path, capsys):
    # No checkout under CODE_DIR, so the real write_debrief fails.
    actions, written = _stop_env(monkeypatch, tmp_path)

    result = loop_runtime.stop_loop("widgets")

    assert result == "stopped widgets; report saved to " + str(tmp_path / "report.log")
    assert ("workspace", "close", "workspace-1") in actions
    assert written["state"] == "stopped"
    assert "lupin loop: no debrief for widgets: no checkout at" in capsys.readouterr().err


def test_unexpected_debrief_error_does_not_block_the_stop(monkeypatch, tmp_path: Path, capsys):
    actions, written = _stop_env(monkeypatch, tmp_path)
    monkeypatch.setattr(debrief, "write_debrief", mock.Mock(side_effect=RuntimeError("boom")))

    result = loop_runtime.stop_loop("widgets", force=True)

    assert result.startswith("stopped widgets")
    assert ("workspace", "close", "workspace-1") in actions
    assert written["state"] == "stopped"
    assert "no debrief for widgets: boom" in capsys.readouterr().err


def test_stop_writes_a_debrief_after_the_stop(monkeypatch, tmp_path: Path):
    _stop_env(monkeypatch, tmp_path)
    write = mock.Mock()
    monkeypatch.setattr(debrief, "write_debrief", write)

    loop_runtime.stop_loop("widgets", force=True)

    write.assert_called_once_with(
        tmp_path / "state", "widgets", tmp_path / "code" / "widgets",
        "2026-10-02T09:00:00Z", forced=True,
    )


def test_stop_time_limit_fits_loop_stop_timeout(monkeypatch, tmp_path: Path):
    """HANDOFF_GRACE_S plus the debrief's gh time limit must be less than loop.stop's limit.

    All gh calls in one debrief share DEBRIEF_TIME_LIMIT_S. Redis reads are not
    in this sum. test_agent.py counts them.
    """
    clock = SimpleNamespace(now=0.0)
    timeouts = _timed_gh(monkeypatch, clock, per_call=0.0)

    checkout = tmp_path / "widgets"
    checkout.mkdir()
    monkeypatch.setattr(debrief.ledger, "read_events", lambda repo, **kw: [])
    monkeypatch.setattr(debrief.claims, "claims_for", lambda repos, **kw: {})

    path = debrief.write_debrief(tmp_path, "widgets", checkout, "2026-10-02T09:00:00Z")

    assert path.is_file()
    assert timeouts, "the debrief made no gh calls"
    assert all(seconds <= debrief.DEBRIEF_TIME_LIMIT_S for seconds in timeouts)
    time_limit = loop_runtime.HANDOFF_GRACE_S + debrief.DEBRIEF_TIME_LIMIT_S
    assert time_limit < agent.ACTION_TIMEOUT_S["loop.stop"], (
        f"gh time limit {debrief.DEBRIEF_TIME_LIMIT_S}s + "
        f"{loop_runtime.HANDOFF_GRACE_S}s grace = {time_limit}s"
    )


def _timed_gh(monkeypatch, clock, per_call: float) -> list[float]:
    """Stub gh calls. Each call takes `per_call` seconds on `clock`. Returns the timeouts asked for."""
    timeouts: list[float] = []

    def fake_run(argv, *, timeout, **kwargs):
        timeouts.append(timeout)
        if per_call > timeout:
            clock.now += timeout
            raise subprocess.TimeoutExpired(argv, timeout)
        clock.now += per_call
        body = {"repo": {"nameWithOwner": FULL}}.get(argv[1], [])
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(body), stderr="")

    monkeypatch.setattr(debrief.subprocess, "run", fake_run)
    monkeypatch.setattr(debrief, "time", SimpleNamespace(monotonic=lambda: clock.now))
    return timeouts


def test_slow_gh_stops_at_the_time_limit_and_names_each_cut_section(tmp_path: Path, monkeypatch):
    clock = SimpleNamespace(now=0.0)
    timeouts = _timed_gh(monkeypatch, clock, per_call=3.0)
    checkout = tmp_path / "widgets"
    checkout.mkdir()
    monkeypatch.setattr(debrief.ledger, "read_events", lambda repo, **kw: [])
    monkeypatch.setattr(debrief.claims, "claims_for", lambda repos, **kw: {})

    path = debrief.write_debrief(tmp_path, "widgets", checkout, "2026-10-02T09:00:00Z")

    text = path.read_text(encoding="utf-8")
    assert timeouts == [10.0, 7.0, 4.0, 1.0]
    assert clock.now == debrief.DEBRIEF_TIME_LIMIT_S
    assert text.count("Not collected: time limit reached.") == 5
    for section in (
        "Risk: open PR list.", "Risk: blocked issue list.", "Opportunities: ready issue list.",
        "Evidence: Issue list.", "Evidence: PR list.",
    ):
        assert f"- Not collected: time limit reached. {section}" in text
    assert "Not collected: time limit reached. Shipped" not in text


def test_fast_gh_gives_no_note_and_every_section(tmp_path: Path, monkeypatch):
    clock = SimpleNamespace(now=0.0)
    timeouts = _timed_gh(monkeypatch, clock, per_call=0.1)
    checkout = tmp_path / "widgets"
    checkout.mkdir()
    monkeypatch.setattr(debrief.ledger, "read_events", lambda repo, **kw: [])
    monkeypatch.setattr(debrief.claims, "claims_for", lambda repos, **kw: {})

    path = debrief.write_debrief(tmp_path, "widgets", checkout, "2026-10-02T09:00:00Z")

    text = path.read_text(encoding="utf-8")
    assert len(timeouts) == 8
    assert clock.now == pytest.approx(0.8)
    assert "Not collected" not in text
    for heading in ("Shipped", "Follow-up tasks", "Risk", "Opportunities", "Evidence"):
        assert f"## {heading}\n" in text


def _every_list_returns(count: int):
    items = [
        {"number": n, "title": f"item {n}", "body": None, "comments": [],
         "mergedAt": None, "closedAt": None, "updatedAt": None,
         "mergeCommit": None, "statusCheckRollup": []}
        for n in range(1, count + 1)
    ]
    return mock.patch.object(debrief, "_gh", lambda args, cwd=None, timeout=None: list(items))


def test_cut_list_is_named_in_its_section():
    with _every_list_returns(200):
        md = debrief.build_markdown(FULL, START, END, events=[], claimed=set())

    notes = {line.split(":", 1)[0][2:] for line in md.splitlines() if "list cut at" in line}
    assert notes == {"Shipped", "Risk", "Opportunities", "Evidence"}
    assert "- Opportunities: ready issue list cut at 200 items. Some items may be missing." in md


def test_list_under_the_limit_gives_no_note():
    with _every_list_returns(199):
        md = debrief.build_markdown(FULL, START, END, events=[], claimed=set())

    assert "cut at" not in md


# Screenshot tests. The browser is off unless a test turns it on.
GH_STUB = """#!/bin/sh
# Stub gh for the screenshot tests. Prints fixed JSON.
case "$*" in
  "repo view"*) echo '{"nameWithOwner": "acme/widgets"}' ;;
  *"--state merged"*) echo '[{"number": 11, "title": "Shipped feature", "mergedAt": "2026-10-10T09:00:00Z", "mergeCommit": {"oid": "bbbbbbb2222"}}]' ;;
  *"--state closed"*) echo '[{"number": 5, "title": "Closed in window", "closedAt": "2026-10-10T10:00:00Z"}]' ;;
  *) echo '[]' ;;
esac
"""
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
SHOT_FILES = ("debrief-list.png", "debrief.png")


@pytest.fixture(autouse=True)
def no_browser(monkeypatch):
    monkeypatch.setattr(debrief, "SHOT_BROWSER", "lupin-no-such-browser")


def _periodic_env(tmp_path: Path, monkeypatch, redis_port) -> tuple[Path, Path]:
    """Put a stub gh on PATH and one checkout in place. Return (state, code)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(GH_STUB, encoding="utf-8")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    # The dashboard runs as a subprocess. It must find lupin on its import path.
    monkeypatch.setenv("PYTHONPATH", str(Path(debrief.__file__).resolve().parents[1]))
    monkeypatch.setattr(debrief.slots_redis, "debrief_client", _debrief_client_on(redis_port))
    code = tmp_path / "code"
    (code / "widgets").mkdir(parents=True)
    state = tmp_path / "state"
    monkeypatch.setattr(loop_runtime, "STATE_DIR", state)
    monkeypatch.setattr(loop_runtime, "CODE_DIR", code)
    monkeypatch.setattr(loop_runtime, "enabled_repos", lambda: {"widgets": "claude"})
    return state, code


@pytest.mark.skipif(shutil.which("chromium") is None, reason="needs chromium on PATH")
def test_periodic_debriefs_save_screenshots_and_link_them(tmp_path: Path, monkeypatch, redis_port):
    state, _code = _periodic_env(tmp_path, monkeypatch, redis_port)
    monkeypatch.setattr(debrief, "SHOT_BROWSER", "chromium")

    loop_runtime.write_due_periodic_debriefs(now=NOW)

    for period in debrief.PERIODS:
        stem = f"20261010-120000-{period}"
        md = state / "debriefs" / "widgets" / f"{stem}.md"
        text = md.read_text(encoding="utf-8")
        assert "## Screenshots" in text
        for file in SHOT_FILES:
            link = f"{stem}-screenshots/{file}"
            assert f"]({link})" in text
            png = md.parent / link
            assert png.is_file() and png.stat().st_size > 0
            assert png.read_bytes().startswith(PNG_SIGNATURE)
        for link in re.findall(r"\]\(([^)]+)\)", text):
            assert (md.parent / link).is_file()


def test_missing_browser_still_writes_the_periodic_debrief(tmp_path: Path, monkeypatch, redis_port):
    state, code = _periodic_env(tmp_path, monkeypatch, redis_port)
    monkeypatch.setattr(debrief, "SHOT_BROWSER", "lupin-no-such-browser")

    path = debrief.write_period(state, "widgets", code / "widgets", "6h", now=NOW)

    text = path.read_text(encoding="utf-8")
    assert "Shipped feature" in text
    reason = "browser command 'lupin-no-such-browser' not found on PATH"
    assert f"- Not captured: Debrief list (debrief-list.png). Reason: {reason}." in text
    assert f"- Not captured: Debrief page (debrief.png). Reason: {reason}." in text
    assert "![" not in text
    assert list(path.with_name("20261010-120000-6h-screenshots").glob("*.png")) == []


FAKE_BROWSER = "lupin-fake-browser"
STEM = "20261010-120000-6h"
# Writes the --screenshot file, as chromium does.
FAKE_SHOT_OK = (
    'for arg in "$@"; do case "$arg" in --screenshot=*) '
    'printf x > "${arg#--screenshot=}" ;; esac; done\n'
)


def _running(pid: int) -> bool:
    """True when `pid` is alive. A zombie is not running."""
    state = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False,
    ).stdout.strip()
    return bool(state) and not state.startswith("Z")


@pytest.fixture
def fake_browser(tmp_path: Path, monkeypatch):
    """Put a fake browser first on PATH. Return a function that writes its script."""
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    # The dashboard runs as a subprocess. It must find lupin on its import path.
    monkeypatch.setenv("PYTHONPATH", str(Path(debrief.__file__).resolve().parents[1]))
    monkeypatch.setattr(debrief, "SHOT_BROWSER", FAKE_BROWSER)

    def install(body: str) -> Path:
        script = bin_dir / FAKE_BROWSER
        script.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        script.chmod(0o755)
        return script

    return install


def test_page_that_is_not_200_is_not_captured(tmp_path: Path, fake_browser):
    fake_browser(FAKE_SHOT_OK)
    folder = tmp_path / "shots"

    # No debrief file exists, so /debrief?repo=...&file=... answers 404.
    missing = debrief._capture(tmp_path, "widgets", f"{STEM}.md", folder)

    assert missing == {"debrief.png": "dashboard returned HTTP 404"}
    assert (folder / "debrief-list.png").is_file()
    assert not (folder / "debrief.png").exists()


def test_browser_exit_1_is_recorded_as_not_captured(tmp_path: Path, fake_browser):
    fake_browser("exit 1\n")
    _debrief_file(tmp_path, "widgets", f"{STEM}.md")

    missing = debrief._capture(tmp_path, "widgets", f"{STEM}.md", tmp_path / "shots")

    reason = "browser exited 1 without a screenshot"
    assert missing == {"debrief-list.png": reason, "debrief.png": reason}


def test_browser_timeout_is_recorded_as_not_captured(tmp_path: Path, fake_browser, monkeypatch):
    fake_browser("exec sleep 60\n")
    monkeypatch.setattr(debrief, "SHOT_TIMEOUT_S", 1.0)
    target = tmp_path / "shot.png"

    started = time.monotonic()
    reason = debrief._shot("http://127.0.0.1:1/", target)

    assert reason == "browser timed out"
    assert time.monotonic() - started < 30
    assert not target.exists()


def test_failed_dashboard_start_marks_every_shot_not_captured(tmp_path: Path, fake_browser, monkeypatch):
    fake_browser(FAKE_SHOT_OK)
    _debrief_file(tmp_path, "widgets", f"{STEM}.md")

    def no_server(root, port):
        raise debrief.DebriefError("dashboard server did not start")

    monkeypatch.setattr(debrief, "_serve", no_server)
    folder = tmp_path / "shots"

    missing = debrief._capture(tmp_path, "widgets", f"{STEM}.md", folder)

    reason = "dashboard server did not start"
    assert missing == {"debrief-list.png": reason, "debrief.png": reason}
    assert not folder.exists() or list(folder.glob("*.png")) == []


@pytest.mark.skipif(shutil.which("ps") is None, reason="needs ps on PATH")
def test_browser_timeout_kills_the_whole_process_group(tmp_path: Path, fake_browser, monkeypatch):
    pid_file = tmp_path / "child.pid"
    # The child holds no pipe. Only a group kill stops it.
    fake_browser(
        f'sleep 120 </dev/null >/dev/null 2>&1 &\necho $! > "{pid_file}"\nwait\n'
    )
    monkeypatch.setattr(debrief, "SHOT_TIMEOUT_S", 1.0)

    reason = debrief._shot("http://127.0.0.1:1/", tmp_path / "shot.png")

    child = int(pid_file.read_text(encoding="utf-8"))
    try:
        assert reason == "browser timed out"
        assert not _running(child)
    finally:
        if _running(child):
            os.kill(child, signal.SIGKILL)


def test_read_screenshot_serves_pngs_inside_the_repo_debrief_folder(tmp_path: Path):
    shots = tmp_path / "debriefs" / "widgets" / f"{STEM}-screenshots"
    shots.mkdir(parents=True)
    (shots / "debrief.png").write_bytes(b"png-bytes")

    found = debrief.read_screenshot(tmp_path, "widgets", f"{STEM}-screenshots/debrief.png")

    assert found == (b"png-bytes", "image/png")


@pytest.mark.parametrize("rel", [
    "../secret.png",
    f"{STEM}-screenshots/../../secret.png",
    "/etc/passwd.png",
    f"{STEM}-screenshots\\debrief.png",
    f"{STEM}-screenshots/debrief.svg",
    f"{STEM}-screenshots/missing.png",
    "",
])
def test_read_screenshot_refuses(tmp_path: Path, rel: str):
    shots = tmp_path / "debriefs" / "widgets" / f"{STEM}-screenshots"
    shots.mkdir(parents=True)
    (shots / "debrief.png").write_bytes(b"png-bytes")
    (tmp_path / "debriefs" / "secret.png").write_bytes(b"secret")

    assert debrief.read_screenshot(tmp_path, "widgets", rel) is None


def test_read_screenshot_refuses_symlinks_out_of_the_debrief_folder(tmp_path: Path):
    shots = tmp_path / "debriefs" / "widgets" / f"{STEM}-screenshots"
    shots.mkdir(parents=True)
    (tmp_path / "secret.png").write_bytes(b"secret")
    (shots / "link.png").symlink_to(tmp_path / "secret.png")

    assert debrief.read_screenshot(tmp_path, "widgets", f"{STEM}-screenshots/link.png") is None


def test_render_html_maps_local_screenshots_to_the_shot_route():
    markdown = "\n".join([
        "## Screenshots",
        f"![Debrief page]({STEM}-screenshots/debrief.png)",
        "![Escape](../secret.png)",
    ])

    html = debrief.render_html(markdown, "widgets")

    assert (
        f"<img src='/debrief/shot?repo=widgets&amp;path={STEM}-screenshots%2Fdebrief.png' "
        "alt='Debrief page' loading=lazy>"
    ) in html
    assert "![Escape](../secret.png)" in html
    assert html.count("<img") == 1


def test_periods_hold_6h_12h_24h_and_7d():
    assert debrief.PERIODS == {
        "6h": timedelta(hours=6), "12h": timedelta(hours=12),
        "24h": timedelta(hours=24), "7d": timedelta(days=7),
    }
    for period in debrief.PERIODS:
        assert debrief.FILE_RE.fullmatch(f"20261010-120000-{period}.md")


def test_12h_is_due_at_twelve_hours_and_not_before(tmp_path: Path):
    last = datetime(2026, 10, 10, 0, 0, 0, tzinfo=timezone.utc)
    _debrief_file(tmp_path, "widgets", "20261010-000000-12h.md")

    assert debrief.period_due(tmp_path, "widgets", "12h", last + timedelta(hours=12, seconds=-1)) is False
    assert debrief.period_due(tmp_path, "widgets", "12h", last + timedelta(hours=12)) is True
    # A 12h file does not move the 6h or 24h schedule.
    assert debrief.period_due(tmp_path, "widgets", "6h", last + timedelta(hours=1)) is True
    assert debrief.period_due(tmp_path, "widgets", "24h", last + timedelta(hours=13)) is True


def test_write_period_12h_starts_twelve_hours_back(tmp_path: Path, monkeypatch):
    checkout = tmp_path / "widgets"
    checkout.mkdir()
    _stub_io(monkeypatch)

    path = debrief.write_period(tmp_path, "widgets", checkout, "12h", now=NOW)

    text = path.read_text(encoding="utf-8")
    assert path.name == "20261010-120000-12h.md"
    assert "- From: 2026-10-10T00:00:00Z" in text
    assert "- Period: 12h" in text


DAY_START = datetime(2026, 10, 9, tzinfo=timezone.utc)


def test_write_day_window_is_midnight_to_midnight_and_half_open(tmp_path: Path, monkeypatch):
    checkout = tmp_path / "widgets"
    checkout.mkdir()
    gh = FakeGh({
        "repo view": {"nameWithOwner": FULL},
        "pr list merged": [
            {"number": 1, "title": "Day start", "mergedAt": "2026-10-09T00:00:00Z", "mergeCommit": {"oid": "a" * 40}},
            {"number": 2, "title": "Last second", "mergedAt": "2026-10-09T23:59:59Z", "mergeCommit": {"oid": "b" * 40}},
            {"number": 3, "title": "Next day start", "mergedAt": "2026-10-10T00:00:00Z", "mergeCommit": {"oid": "c" * 40}},
            {"number": 4, "title": "Before", "mergedAt": "2026-10-08T23:59:59Z", "mergeCommit": {"oid": "d" * 40}},
        ],
        "issue list closed": [], "issue list all": [], "pr list all": [],
        "api": [],
    })
    monkeypatch.setattr(debrief, "_gh", gh)
    monkeypatch.setattr(debrief.ledger, "read_events", lambda repo, **kw: [])

    path = debrief.write_day(tmp_path, "widgets", checkout, DAY_START)

    text = path.read_text(encoding="utf-8")
    assert path.name == "20261010-000000-day.md"
    assert "- From: 2026-10-09T00:00:00Z" in text
    assert "- To: 2026-10-10T00:00:00Z" in text
    assert "PR #1:" in text and "PR #2:" in text
    assert "PR #3:" not in text and "PR #4:" not in text
    search = [a[a.index("--search") + 1] for a in gh.calls if "--search" in a]
    assert "merged:2026-10-09..2026-10-09" in search
    assert "closed:2026-10-09..2026-10-09" in search


def test_write_day_does_not_show_todays_state_as_history(tmp_path: Path, monkeypatch):
    checkout = tmp_path / "widgets"
    checkout.mkdir()
    gh = FakeGh({"repo view": {"nameWithOwner": FULL}, "pr list merged": [], "issue list closed": [],
                 "issue list all": [], "pr list all": [], "api": []})
    monkeypatch.setattr(debrief, "_gh", gh)
    monkeypatch.setattr(debrief.ledger, "read_events", lambda repo, **kw: [])
    monkeypatch.setattr(debrief.claims, "claims_for", lambda *a, **kw: pytest.fail("claims read"))

    text = debrief.write_day(tmp_path, "widgets", checkout, DAY_START).read_text(encoding="utf-8")

    assert "Claims are for today only" in text
    assert "Not collected: past day" in text
    assert not any("--label" in call or "open" in call for call in gh.calls)


def test_write_day_says_when_redis_is_unreachable(tmp_path: Path, monkeypatch):
    checkout = tmp_path / "widgets"
    checkout.mkdir()
    monkeypatch.setattr(debrief, "_gh", FakeGh({
        "repo view": {"nameWithOwner": FULL}, "pr list merged": [], "issue list closed": [],
        "issue list all": [], "pr list all": [], "api": [],
    }))

    def down(repo, **kw):
        raise CoordinatorUnreachable("no route")

    monkeypatch.setattr(debrief.ledger, "read_events", down)

    text = debrief.write_day(tmp_path, "widgets", checkout, DAY_START).read_text(encoding="utf-8")

    assert "Ledger unavailable: no route" in text


def test_write_day_lists_commits_in_the_window(tmp_path: Path, monkeypatch):
    checkout = tmp_path / "widgets"
    checkout.mkdir()
    commits = [
        {"sha": "1" * 40, "commit": {"message": "fix a\n\nbody", "committer": {"date": "2026-10-09T10:00:00Z"}}},
        {"sha": "2" * 40, "commit": {"message": "at end", "committer": {"date": "2026-10-10T00:00:00Z"}}},
        {"sha": "c" * 40, "commit": {"message": "merge", "committer": {"date": "2026-10-09T11:00:00Z"}}},
    ]
    monkeypatch.setattr(debrief, "_gh", FakeGh({
        "repo view": {"nameWithOwner": FULL},
        "pr list merged": [{"number": 3, "title": "P", "mergedAt": "2026-10-09T11:00:00Z", "mergeCommit": {"oid": "c" * 40}}],
        "issue list closed": [], "issue list all": [], "pr list all": [], "api": commits,
    }))
    monkeypatch.setattr(debrief.ledger, "read_events", lambda repo, **kw: [])

    text = debrief.write_day(tmp_path, "widgets", checkout, DAY_START).read_text(encoding="utf-8")

    assert "- Commit 1111111: fix a (2026-10-09T10:00:00Z)" in text
    assert "2222222" not in text and "Commit ccccccc" not in text


def test_write_day_refuses_a_time_that_is_not_midnight_utc(tmp_path: Path):
    with pytest.raises(debrief.DebriefError):
        debrief.write_day(tmp_path, "widgets", tmp_path, DAY_START + timedelta(hours=1))


def test_day_file_does_not_change_period_due_or_the_period_window(tmp_path: Path, monkeypatch):
    checkout = tmp_path / "widgets"
    checkout.mkdir()
    _stub_io(monkeypatch)
    # Backfill files for the days around NOW, including one that ends after NOW.
    for name in ("20261010-000000-day.md", "20261010-120000-day.md", "20261011-000000-day.md",
                 "20271010-000000-day.md"):
        _debrief_file(tmp_path, "widgets", name)

    for period in debrief.PERIODS:
        assert debrief.last_period_end(tmp_path, "widgets", period, NOW) is None
        assert debrief._period_ends(tmp_path, "widgets", period) == []
        assert debrief.period_due(tmp_path, "widgets", period, NOW) is True
    path = debrief.write_period(tmp_path, "widgets", checkout, "6h", now=NOW)
    assert "- From: 2026-10-10T06:00:00Z" in path.read_text(encoding="utf-8")


def test_day_file_is_listed_on_the_dashboard_and_readable(tmp_path: Path):
    _debrief_file(tmp_path, "widgets", "20261010-000000-day.md", "# day\n")

    assert ("widgets", "20261010-000000-day.md") in debrief.list_debriefs(tmp_path)
    assert debrief.read_debrief(tmp_path, "widgets", "20261010-000000-day.md") == "# day\n"


def test_active_days_counts_prs_issues_and_commits(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(debrief, "_gh", FakeGh({
        "repo view": {"nameWithOwner": FULL},
        "pr list merged": [{"mergedAt": "2026-10-08T05:00:00Z"}],
        "issue list closed": [{"closedAt": "2026-10-09T05:00:00Z"}],
        "api": [{"sha": "1" * 40, "commit": {"message": "m", "committer": {"date": "2026-10-11T05:00:00Z"}}}],
    }))
    first = datetime(2026, 10, 7, tzinfo=timezone.utc)
    last = datetime(2026, 10, 13, tzinfo=timezone.utc)

    days = debrief.active_days(tmp_path, first, last)

    assert days == [datetime(2026, 10, d, tzinfo=timezone.utc) for d in (8, 9, 11)]


def test_redis_client_uses_the_agent_configuration(tmp_path: Path, monkeypatch):
    for name in ("LUPIN_REDIS_HOST", "LUPIN_REDIS_PORT", "LUPIN_REDIS_USERNAME", "LUPIN_REDIS_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LUPIN_REDIS_HOST", "100.64.0.11")
    monkeypatch.setenv("LUPIN_REDIS_PORT", "6380")
    monkeypatch.setenv("LUPIN_REDIS_USERNAME", "agent-user")
    creds = tmp_path / "creds"
    creds.mkdir()
    (creds / "redis-password").write_text("not-a-real-secret\n")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(creds))
    monkeypatch.setenv("LUPIN_FLEET_CONFIG", str(tmp_path / "none.json"))
    seen = []

    def fake_client(*args):
        seen.append(args)
        return redis.Redis()

    monkeypatch.setattr(debrief.slots_redis, "debrief_client", fake_client)
    monkeypatch.setattr(debrief, "_gh", FakeGh({"repo view": {"nameWithOwner": FULL}}))
    monkeypatch.setattr(debrief.ledger, "read_events", lambda repo, **kw: [])
    monkeypatch.setattr(debrief.claims, "claims_for", lambda repos, **kw: {})
    monkeypatch.setattr(debrief, "build_markdown", lambda *a, **kw: "# x\n")

    debrief._write(tmp_path, "widgets", tmp_path, START, END, forced=False, past_day=True)

    assert seen == [("100.64.0.11", 6380, "agent-user", "not-a-real-secret")]


def test_serve_env_lets_a_bare_interpreter_import_lupin(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("PYTHONPATH", raising=False)
    probe = [sys.executable, "-S", "-c", "import lupin, redis"]  # -S: no site-packages, like a bare interpreter.

    bare = subprocess.run(probe, cwd=tmp_path, capture_output=True, env={**os.environ, "PYTHONPATH": ""})
    fixed = subprocess.run(probe, cwd=tmp_path, capture_output=True, env=debrief._serve_env(tmp_path))

    assert bare.returncode != 0
    assert fixed.returncode == 0, fixed.stderr.decode()
    assert debrief._serve_env(tmp_path)["LUPIN_LOOP_STATE_DIR"] == str(tmp_path)


def test_serve_starts_and_answers_healthz_from_another_directory(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    port = debrief._free_port()

    server = debrief._serve(tmp_path, port)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as resp:
            assert resp.status == 200
    finally:
        server.kill()
        server.wait()


def _commit(day: int, n: int = 0) -> dict:
    return {"sha": f"{day:02d}{n:038d}", "commit": {"message": "m", "committer": {"date": f"2026-10-{day:02d}T05:00:00Z"}}}


def _paged_gh(pages: list, calls: list):
    """Stand-in for `_gh`: the "page" query value picks an entry of `pages`."""
    def fake(args, cwd=None, timeout=None):
        if args[0] != "api":
            return {"nameWithOwner": FULL} if args[:2] == ["repo", "view"] else []
        calls.append(args[1])
        page = int(args[1].rsplit("&page=", 1)[1])
        if isinstance(pages[page - 1], Exception):
            raise pages[page - 1]
        return pages[page - 1]
    return fake


FIRST = datetime(2026, 10, 1, tzinfo=timezone.utc)
LAST = datetime(2026, 10, 12, tzinfo=timezone.utc)


def test_active_days_reads_every_commit_page(tmp_path: Path, monkeypatch):
    calls: list = []
    full = [_commit(12, n) for n in range(debrief.COMMIT_PAGE)]
    monkeypatch.setattr(debrief, "_gh", _paged_gh([full, [_commit(3)]], calls))

    days = debrief.active_days(tmp_path, FIRST, LAST)

    assert days == [datetime(2026, 10, 3, tzinfo=timezone.utc), datetime(2026, 10, 12, tzinfo=timezone.utc)]
    assert len(calls) == 2


def test_active_days_fails_when_a_later_commit_page_fails(tmp_path: Path, monkeypatch):
    full = [_commit(12, n) for n in range(debrief.COMMIT_PAGE)]
    monkeypatch.setattr(debrief, "_gh", _paged_gh([full, debrief.DebriefError("boom")], []))

    with pytest.raises(debrief.DebriefError, match="commit list not collected: gh error"):
        debrief.active_days(tmp_path, FIRST, LAST)


def test_commit_list_at_the_page_cap_is_complete(tmp_path: Path, monkeypatch):
    calls: list = []
    full = [_commit(12, n) for n in range(debrief.COMMIT_PAGE)]
    pages = [full] * debrief.COMMIT_PAGES_MAX + [[]]
    monkeypatch.setattr(debrief, "_gh", _paged_gh(pages, calls))

    debrief.active_days(tmp_path, FIRST, LAST)

    assert len(calls) == debrief.COMMIT_PAGES_MAX + 1


def test_commit_list_over_the_page_cap_fails(tmp_path: Path, monkeypatch):
    calls: list = []
    full = [_commit(12, n) for n in range(debrief.COMMIT_PAGE)]
    pages = [full] * (debrief.COMMIT_PAGES_MAX + 1)
    monkeypatch.setattr(debrief, "_gh", _paged_gh(pages, calls))

    with pytest.raises(debrief.DebriefError, match="more than"):
        debrief.active_days(tmp_path, FIRST, LAST)
    assert len(calls) == debrief.COMMIT_PAGES_MAX + 1


@pytest.mark.parametrize("kind,field", [("pr", "mergedAt"), ("issue", "closedAt")])
def test_active_days_accepts_exact_pr_issue_cap(tmp_path: Path, monkeypatch, kind, field):
    rows = [{field: "2026-10-08T05:00:00Z"}] * debrief.PR_ISSUE_LIMIT
    gh = FakeGh({
        "repo view": {"nameWithOwner": FULL}, f"{kind} list {'merged' if kind == 'pr' else 'closed'}": rows,
        f"{'issue' if kind == 'pr' else 'pr'} list {'closed' if kind == 'pr' else 'merged'}": [],
        "api": [],
    })
    monkeypatch.setattr(debrief, "_gh", gh)

    assert debrief.active_days(tmp_path, FIRST, LAST) == [datetime(2026, 10, 8, tzinfo=timezone.utc)]
    list_call = next(call for call in gh.calls if call[:2] == [kind, "list"])
    assert list_call[list_call.index("--limit") + 1] == str(debrief.PR_ISSUE_LIMIT + 1)


@pytest.mark.parametrize("kind,field", [("pr", "mergedAt"), ("issue", "closedAt")])
def test_active_days_rejects_pr_issue_overflow(tmp_path: Path, monkeypatch, kind, field):
    rows = [{field: "2026-10-08T05:00:00Z"}] * (debrief.PR_ISSUE_LIMIT + 1)
    gh = FakeGh({
        "repo view": {"nameWithOwner": FULL}, f"{kind} list {'merged' if kind == 'pr' else 'closed'}": rows,
        f"{'issue' if kind == 'pr' else 'pr'} list {'closed' if kind == 'pr' else 'merged'}": [],
        "api": [],
    })
    monkeypatch.setattr(debrief, "_gh", gh)

    with pytest.raises(debrief.DebriefError, match=f"{kind} list cut at"):
        debrief.active_days(tmp_path, FIRST, LAST)
    list_call = next(call for call in gh.calls if call[:2] == [kind, "list"])
    assert list_call[list_call.index("--limit") + 1] == str(debrief.PR_ISSUE_LIMIT + 1)

def test_write_day_refuses_a_day_that_is_not_over(tmp_path: Path):
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    with pytest.raises(debrief.DebriefError, match="not over yet"):
        debrief.write_day(tmp_path, "widgets", tmp_path, today)


def test_redis_connection_reads_the_fleet_config_named_by_the_environment(tmp_path: Path, monkeypatch):
    for name in ("LUPIN_REDIS_HOST", "LUPIN_REDIS_PORT", "LUPIN_REDIS_USERNAME", "LUPIN_REDIS_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    config = tmp_path / "fleet.json"
    config.write_text(json.dumps({"redis_host": "100.64.0.9", "redis_port": 6390}), encoding="utf-8")
    monkeypatch.setenv("LUPIN_FLEET_CONFIG", str(config))

    host, port, _user, _password = debrief._redis_connection()

    assert (host, port) == ("100.64.0.9", 6390)


def test_write_debrief_notes_a_bad_fleet_config_and_still_writes(tmp_path: Path, monkeypatch):
    checkout = tmp_path / "widgets"
    checkout.mkdir()
    _stub_io(monkeypatch)
    bad = tmp_path / "fleet.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("LUPIN_FLEET_CONFIG", str(bad))

    path = debrief.write_period(tmp_path, "widgets", checkout, "6h", now=NOW)

    assert "Ledger unavailable" in _section(path.read_text(encoding="utf-8"), "Follow-up tasks")
