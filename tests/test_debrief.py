"""Tests for debrief.py and the stop hook that writes a debrief."""

from __future__ import annotations

import json
import stat
import subprocess
import functools
from datetime import datetime, timezone
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
        return self.responses[key]


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
    assert "Not checked against real ledger rows." in risk


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


def test_malformed_ledger_row_keeps_other_sections(
    tmp_path: Path, monkeypatch, redis_port, flush_redis
):
    checkout = tmp_path / "widgets"
    checkout.mkdir()
    # A row with no "ts" field. Real Redis, real ledger.read_events.
    redis.Redis(host="127.0.0.1", port=redis_port).xadd(
        ledger._stream_key(FULL), {"host": "h", "event": "shipped"}
    )
    real_read_events = debrief.ledger.read_events
    real_claims_for = debrief.claims.claims_for
    monkeypatch.setattr(debrief, "_gh", FakeGh(dict(RESPONSES)))
    monkeypatch.setattr(
        debrief.ledger, "read_events",
        functools.partial(real_read_events, redis_host="127.0.0.1", redis_port=redis_port),
    )
    monkeypatch.setattr(
        debrief.claims, "claims_for",
        functools.partial(real_claims_for, redis_host="127.0.0.1", redis_port=redis_port),
    )

    path = debrief.write_debrief(tmp_path, "widgets", checkout, "2026-10-02T09:00:00Z")

    text = path.read_text(encoding="utf-8")
    assert "Ledger unavailable" in _section(text, "Follow-up tasks")
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
    real_read_events = debrief.ledger.read_events
    real_claims_for = debrief.claims.claims_for
    monkeypatch.setattr(debrief, "_gh", FakeGh(dict(RESPONSES)))
    monkeypatch.setattr(
        debrief.ledger, "read_events",
        functools.partial(real_read_events, redis_host="127.0.0.1", redis_port=redis_port),
    )
    monkeypatch.setattr(
        debrief.claims, "claims_for",
        functools.partial(real_claims_for, redis_host="127.0.0.1", redis_port=redis_port),
    )

    path = debrief.write_debrief(tmp_path, "widgets", checkout, "2026-10-02T09:00:00Z")

    text = path.read_text(encoding="utf-8")
    assert "- #5: Write docs" in _section(text, "Follow-up tasks")
    assert "- PR #11: Shipped feature" in text
    assert "## Risk" in text
    assert "## Evidence" in text


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


def test_claims_error_still_writes_the_debrief(tmp_path: Path, monkeypatch):
    checkout = tmp_path / "widgets"
    checkout.mkdir()
    claims_for = mock.Mock(side_effect=CoordinatorUnreachable("claims_for"))
    monkeypatch.setattr(debrief, "_gh", FakeGh(dict(RESPONSES)))
    monkeypatch.setattr(debrief.ledger, "read_events", lambda repo, **kw: [])
    monkeypatch.setattr(debrief.claims, "claims_for", claims_for)

    path = debrief.write_debrief(tmp_path, "widgets", checkout, "2026-10-02T09:00:00Z")

    text = path.read_text(encoding="utf-8")
    claims_for.assert_called_once_with([FULL])
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
    debrief.claims.claims_for.assert_called_once_with([FULL])


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

    html = debrief.render_html(markdown)

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
