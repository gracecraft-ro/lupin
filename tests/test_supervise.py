"""Tests for `lupin supervise` (issue #192). All Herdr, GitHub and Redis
access is faked: no test sends a real nudge."""

from __future__ import annotations

import ast
import inspect

import pytest

from lupin import supervise


def _loop(repo="widgets", state="idle", **extra):
    value = {
        "repo": repo, "session": "lupin-loops", "state": state, "unit": "active",
        "agent": f"lupin-{repo}-1", "pane_id": "w1:p1",
        "started_at": "2026-10-19T00:00:00Z",
    }
    value.update(extra)
    return value


class Fakes:
    """One set of fake sources for `run_pass`. Each records its calls."""

    def __init__(self, tmp_path, loops, *, pane="working on #12", work=3, events=None):
        self.loops = loops
        self.pane = pane
        self.work = work
        self.events = events or []
        self.nudges: list[tuple[str, str]] = []
        self.ledger: list[tuple[str, dict]] = []
        self.state_path = tmp_path / "supervise.json"

    def kwargs(self, **override):
        value = dict(
            list_loops=lambda: self.loops,
            read_pane=lambda loop: self.pane,
            read_handoff=lambda loop: "",
            repo_name=lambda repo: f"acme/{supervise.loop_runtime.base_repo(repo)}",
            work_left=lambda full_name, repo: self.work,
            recent_events=lambda full_name: self.events,
            send_nudge=lambda loop, text: self.nudges.append((loop["repo"], text)),
            append_event=lambda full_name, event: self.ledger.append((full_name, event)),
            state_path=self.state_path,
        )
        value.update(override)
        return value


# (1) An idle loop is nudged.
def test_idle_loop_with_work_is_nudged_and_one_event_is_written(tmp_path):
    fakes = Fakes(tmp_path, [_loop(state="idle")])

    report = supervise.run_pass(**fakes.kwargs())

    assert fakes.nudges == [("widgets", supervise.NUDGE_TEXT)]
    assert supervise.NUDGE_TEXT.startswith("Supervisor:")
    assert "until nothing is left" in supervise.NUDGE_TEXT
    assert "Loop:" in supervise.NUDGE_TEXT
    assert report["loops"][0]["verdict"] == "idle"
    assert report["loops"][0]["nudge"] == "sent"
    assert len(fakes.ledger) == 1
    full_name, event = fakes.ledger[0]
    assert full_name == "acme/widgets"
    assert event["event"] == "supervise"
    assert event["status"] == "nudged"
    assert event["highlights"] == ["widgets: state=idle, unit=active, verdict=idle, nudge=sent"]
    # No pane text reaches the ledger.
    assert "working on #12" not in repr(event)


def test_failed_nudge_is_reported_not_raised(tmp_path):
    fakes = Fakes(tmp_path, [_loop(state="done")])

    def refuse(loop, text):
        raise supervise.loop_runtime.HerdrError("agent_blocked")

    report = supervise.run_pass(**fakes.kwargs(send_nudge=refuse))

    assert report["loops"][0]["nudge"] == "failed"
    assert fakes.ledger[0][1]["status"] == "flagged"


def test_dry_run_sends_nothing_and_writes_nothing(tmp_path):
    fakes = Fakes(tmp_path, [_loop(state="idle")])

    report = supervise.run_pass(**fakes.kwargs(), dry_run=True)

    assert report["loops"][0]["verdict"] == "idle"
    assert report["loops"][0]["nudge"] == "dry-run"
    assert fakes.nudges == []
    assert fakes.ledger == []
    assert not fakes.state_path.exists()


# (2) Busywork is flagged, and no nudge is sent.
def test_same_pane_tail_twice_while_working_is_busywork_without_nudge(tmp_path):
    fakes = Fakes(tmp_path, [_loop(state="working")], pane="Running tests...\n  pass 3 \n")

    first = supervise.run_pass(**fakes.kwargs())
    fakes.pane = "Running tests...\npass 3"  # same text, other whitespace
    second = supervise.run_pass(**fakes.kwargs())

    assert first["loops"][0]["verdict"] == "ok"
    assert second["loops"][0]["verdict"] == "busywork"
    assert second["loops"][0]["nudge"] == "none"
    assert fakes.nudges == []
    assert fakes.ledger[-1][1]["status"] == "flagged"
    # Only a hash of the pane is kept.
    assert "Running tests" not in fakes.state_path.read_text(encoding="utf-8")


def test_three_ledger_events_on_one_issue_with_one_status_is_repeated(tmp_path):
    events = [
        {"event": "dispatch", "issue": 7, "status": "in-progress"},
        {"event": "supervise", "status": "ok"},
        {"event": "dispatch", "issue": 7, "status": "in-progress"},
        {"event": "review", "issue": 7, "status": "in-progress"},
    ]
    fakes = Fakes(tmp_path, [_loop(state="idle")], events=events)

    report = supervise.run_pass(**fakes.kwargs())

    assert report["loops"][0]["verdict"] == "repeated"
    assert fakes.nudges == []
    assert fakes.ledger[0][1]["status"] == "flagged"


def test_ask_to_continue_in_pane_is_an_unjustified_stop(tmp_path):
    fakes = Fakes(
        tmp_path, [_loop(state="idle")],
        pane="Six PRs wait.\nTell me if you want me to review them next.",
    )

    report = supervise.run_pass(**fakes.kwargs())

    assert report["loops"][0]["verdict"] == "stopped"
    assert report["loops"][0]["nudge"] == "sent"


def test_verdict_rules_table():
    v = supervise.verdict
    assert v(state="idle", unit="active", work_left=2) == "idle"
    assert v(state="done", unit="active", work_left=2) == "idle"
    assert v(state="idle", unit="active", work_left=0) == "ok"
    assert v(state="idle", unit="active", work_left=None) == "ok"
    assert v(state="working", unit="active", work_left=2) == "ok"
    assert v(state="working", unit="active", work_left=2, same_tail=True) == "busywork"
    assert v(state="idle", unit="active", work_left=2, repeated_item=True) == "repeated"
    assert v(state="working", unit="inactive", work_left=2) == "stopped"
    assert v(state="blocked", unit="active", work_left=2) == "stopped"
    assert v(state="working", unit="active", work_left=2, asks_to_continue=True) == "stopped"
    assert v(state="working", unit="inactive", work_left=0) == "ok"


# (3) The supervisor stops when no work is left.
def test_every_mode_ends_on_first_pass_with_no_work_left(tmp_path):
    fakes = Fakes(tmp_path, [_loop(state="idle")], work=2)
    sleeps: list[float] = []

    def sleep(seconds):
        sleeps.append(seconds)
        fakes.work = 0

    code = supervise.supervise(every=60, sleep=sleep, emit=lambda report: None, **fakes.kwargs())

    assert code == 0
    assert sleeps == [60]
    assert len(fakes.nudges) == 1  # first pass only
    assert [event["status"] for _, event in fakes.ledger] == ["nudged", "idle"]


def test_no_loops_on_this_machine_exits_zero(tmp_path, capsys):
    fakes = Fakes(tmp_path, [])

    code = supervise.supervise(every=60, sleep=pytest.fail, **fakes.kwargs())

    assert code == 0
    assert "no loops on this machine" in capsys.readouterr().out
    assert fakes.ledger == []


# (4) No merge action exists in the code path.
_FORBIDDEN = ("merge", "push", "--force", "commit", "--author", "reset", "rebase")
_ALLOWED_COMMANDS = {("systemctl", "is-active"), ("agent", "prompt")}
_ALLOWED_RUNTIME = {
    "local_loops", "loop_state", "peek_loop", "_read_metadata", "base_repo", "unit_name",
    "_run", "_herdr", "LoopError", "HerdrError", "STATE_DIR", "CODE_DIR",
    "HERDR_TIMEOUT", "SYSTEMCTL_CHECK_TIMEOUT",
}


def _tree():
    return ast.parse(inspect.getsource(supervise))


def _docstrings(tree) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                ids.add(id(first.value))
    return ids


def test_module_has_no_merge_push_force_or_author_text():
    tree = _tree()
    skip = _docstrings(tree)
    strings = [
        node.value.lower() for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip
    ]
    for word in _FORBIDDEN:
        assert not [s for s in strings if word in s], word


def test_module_runs_only_allowlisted_commands():
    tree = _tree()
    imported = {
        alias.name for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom)) for alias in node.names
    }
    assert not imported & {"subprocess", "shutil", "pty"}
    os_calls = {
        node.attr for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "os"
    }
    assert os_calls <= {"environ", "path", "replace"}, os_calls
    used_runtime = {
        node.attr for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name) and node.value.id == "loop_runtime"
    }
    assert used_runtime <= _ALLOWED_RUNTIME, used_runtime - _ALLOWED_RUNTIME
    commands = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr == "_run":
            argv = node.args[0]
            assert isinstance(argv, ast.List)
            commands.add(tuple(item.value for item in argv.elts[:2]))
        elif node.func.attr == "_herdr":
            commands.add(tuple(item.value for item in node.args[1:3]))
    assert commands == _ALLOWED_COMMANDS


def test_claim_refusal_counts_as_unreachable(monkeypatch):
    import redis

    def refuse(targets, **kwargs):
        raise redis.exceptions.NoPermissionError("no permissions to run the 'multi' command")

    monkeypatch.setattr(supervise.claims, "claims_for", refuse)
    with pytest.raises(supervise.CoordinatorUnreachable):
        supervise._claims_or_unreachable({}, ["acme/widgets"])
