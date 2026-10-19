"""Tests for `lupin supervise` (issue #192). All Herdr, GitHub and Redis
access is faked: no test sends a real nudge."""

from __future__ import annotations

import ast
import functools
import inspect

import pytest
import redis

from lupin import supervise


def _loop(repo="widgets", state="idle", **extra):
    value = {
        "repo": repo, "session": "lupin-loops", "state": state, "unit": "active",
        "pane_id": "w1:p1", "started_at": "2026-10-19T00:00:00Z",
    }
    value.update(extra)
    return value


class Fakes:
    """One set of fake sources for `run_pass`. Each records its calls."""

    def __init__(self, tmp_path, loops, *, pane="working on #12", work=3, events=None):
        self.loops = loops
        self.pane = pane
        self.handoff = ""
        self.work = work
        self.events = events or []
        self.clock = 1_000_000.0
        self.busy = False
        self.metadata: dict = {}
        self.nudges: list[tuple[str, str]] = []
        self.ledger: list[tuple[str, dict]] = []
        self.state_path = tmp_path / "supervise.json"

    def lock(self, repo):
        return None if self.busy else (lambda: None)

    def kwargs(self, **override):
        value = dict(
            list_loops=lambda: self.loops,
            read_pane=lambda loop: self.pane,
            read_handoff=lambda loop: self.handoff,
            repo_name=lambda repo: f"acme/{supervise.loop_runtime.base_repo(repo)}",
            work_left=lambda full_name, repo: (self.work, []),
            recent_events=lambda full_name: self.events,
            send_nudge=lambda loop, text: self.nudges.append((loop["repo"], text)),
            lock_loop=self.lock,
            read_metadata=lambda repo: self.metadata.get(repo, {"state": "running", "pane_id": "w1:p1"}),
            append_event=lambda full_name, event: self.ledger.append((full_name, event)),
            state_path=self.state_path,
            now=lambda: self.clock,
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
    assert "no research or docs item is left" in supervise.NUDGE_TEXT
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


def test_nudge_goes_to_the_pane_id_not_an_agent_name(monkeypatch):
    calls = []
    monkeypatch.setattr(supervise.loop_runtime, "_herdr", lambda *args, **kw: calls.append(args))

    supervise._send_nudge(_loop(pane_id="w2E:p1"), "Supervisor: x")

    assert calls == [("lupin-loops", "agent", "prompt", "w2E:p1", "Supervisor: x")]


def test_loop_with_no_pane_id_gets_no_nudge(tmp_path):
    fakes = Fakes(tmp_path, [_loop(state="idle", pane_id=None, agent="lupin-widgets-1")])

    report = supervise.run_pass(**fakes.kwargs())

    assert report["loops"][0]["nudge"] == "no-pane"
    assert fakes.nudges == []


def test_nudge_is_stale_when_metadata_changed_under_the_lock(tmp_path):
    fakes = Fakes(tmp_path, [_loop(state="idle"), _loop(repo="gears", state="idle")])
    fakes.metadata = {
        "widgets": {"state": "running", "pane_id": "w9:p1"},  # a new run, other pane
        "gears": {"state": "stopped", "pane_id": "w1:p1"},
    }

    report = supervise.run_pass(**fakes.kwargs())

    assert [row["nudge"] for row in report["loops"]] == ["stale", "stale"]
    assert fakes.nudges == []


def test_send_error_that_is_not_a_loop_error_is_failed_and_time_is_kept(tmp_path):
    fakes = Fakes(tmp_path, [_loop(state="idle")])

    def broken(loop, text):
        raise OSError("no space")

    report = supervise.run_pass(**fakes.kwargs(send_nudge=broken))

    assert report["loops"][0]["nudge"] == "failed"
    assert "widgets" in supervise._load_state(fakes.state_path)["nudged"]


def test_future_nudge_time_is_recent_only_within_the_interval(tmp_path):
    fakes = Fakes(tmp_path, [_loop(state="idle")])
    supervise._save_state(fakes.state_path, {"panes": {}, "nudged": {"widgets": fakes.clock + 10}})
    assert supervise.run_pass(**fakes.kwargs())["loops"][0]["nudge"] == "recent"

    far = fakes.clock + supervise.NUDGE_INTERVAL_S + 1
    supervise._save_state(fakes.state_path, {"panes": {}, "nudged": {"widgets": far}})
    assert supervise.run_pass(**fakes.kwargs())["loops"][0]["nudge"] == "sent"


def test_second_pass_while_one_runs_exits_zero_and_sends_nothing(tmp_path, capsys):
    fakes = Fakes(tmp_path, [_loop(state="idle")])
    release = supervise._lock_pass(fakes.state_path)
    try:
        code = supervise.supervise(**fakes.kwargs())
    finally:
        release()

    assert code == 0
    assert "another supervise pass is running" in capsys.readouterr().out
    assert fakes.nudges == [] and fakes.ledger == []
    assert supervise.supervise(emit=lambda r: None, **fakes.kwargs()) == 0
    assert len(fakes.nudges) == 1


def test_second_pass_within_the_interval_sends_no_nudge(tmp_path):
    fakes = Fakes(tmp_path, [_loop(state="idle")])

    supervise.run_pass(**fakes.kwargs())
    fakes.clock += supervise.NUDGE_INTERVAL_S - 1
    second = supervise.run_pass(**fakes.kwargs())
    fakes.clock += 1
    third = supervise.run_pass(**fakes.kwargs())

    assert second["loops"][0]["nudge"] == "recent"
    assert third["loops"][0]["nudge"] == "sent"
    assert len(fakes.nudges) == 2
    assert "Supervisor" not in fakes.state_path.read_text(encoding="utf-8")


def test_loop_held_by_stop_gets_no_nudge(tmp_path):
    fakes = Fakes(tmp_path, [_loop(state="idle")])
    fakes.busy = True

    report = supervise.run_pass(**fakes.kwargs())

    assert report["loops"][0]["nudge"] == "busy"
    assert fakes.nudges == []
    assert "nudge=busy" in fakes.ledger[0][1]["highlights"][0]


def test_failed_nudge_is_reported_not_raised(tmp_path):
    fakes = Fakes(tmp_path, [_loop(state="done")])

    def refuse(loop, text):
        raise supervise.loop_runtime.HerdrError("agent_not_found")

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


def test_working_loop_with_inactive_unit_is_flagged_not_nudged(tmp_path):
    fakes = Fakes(tmp_path, [_loop(state="working", unit="inactive")], work=3)

    report = supervise.run_pass(**fakes.kwargs())

    assert (report["loops"][0]["verdict"], report["loops"][0]["nudge"]) == ("stopped", "none")
    assert fakes.nudges == []


def test_lupin_needs_attention_wins_over_a_working_pane(monkeypatch):
    monkeypatch.setattr(supervise.loop_runtime, "local_loops", lambda: [{
        "repo": "widgets", "session": "lupin-loops", "state": "running", "pane_id": "w1:p1",
        "panes": [{"pane_id": "w1:p1", "agent_status": "working"}],
    }])
    monkeypatch.setattr(
        supervise.loop_runtime, "_read_metadata",
        lambda repo: {"repo": repo, "state": "needs_attention", "started_at": "2026-10-19T00:00:00Z"},
    )
    monkeypatch.setattr(supervise, "_unit_state", lambda repo: "active")

    [loop] = supervise._list_loops()

    assert loop["state"] == "needs_attention"
    assert supervise.verdict(state=loop["state"], unit="active", work_left=3) == "stopped"
    assert not supervise._may_nudge("stopped", loop["state"])


@pytest.mark.parametrize("state", ["blocked", "needs_attention"])
def test_loop_waiting_for_the_owner_is_flagged_and_never_nudged(tmp_path, state):
    fakes = Fakes(tmp_path, [_loop(state=state)])

    report = supervise.run_pass(**fakes.kwargs())

    row = report["loops"][0]
    assert (row["verdict"], row["nudge"], row["reason"]) == ("stopped", "none", "waiting for the owner")
    assert fakes.nudges == []
    assert fakes.ledger[0][1]["status"] == "flagged"


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
    fakes = Fakes(tmp_path, [_loop(state="working")], events=events)

    report = supervise.run_pass(**fakes.kwargs())

    assert report["loops"][0]["verdict"] == "repeated"
    assert fakes.nudges == []
    assert fakes.ledger[0][1]["status"] == "flagged"


def test_ask_to_continue_in_pane_of_idle_loop_is_an_unjustified_stop(tmp_path):
    fakes = Fakes(
        tmp_path, [_loop(state="idle")],
        pane="Six PRs wait.\nTell me if you want me to review them next.",
    )

    report = supervise.run_pass(**fakes.kwargs())

    assert report["loops"][0]["verdict"] == "stopped"
    assert report["loops"][0]["nudge"] == "sent"


def test_ask_to_continue_in_working_loop_pane_or_handoff_is_ok(tmp_path):
    fakes = Fakes(tmp_path, [_loop(state="working")], pane="rule 8: never ask 'tell me if you want me to continue'")
    fakes.handoff = "If you want me to merge, say so."

    report = supervise.run_pass(**fakes.kwargs())

    assert report["loops"][0]["verdict"] == "ok"
    assert fakes.nudges == []


def test_verdict_rules_table():
    v = supervise.verdict
    assert v(state="idle", unit="active", work_left=2) == "idle"
    assert v(state="done", unit="active", work_left=2) == "idle"
    assert v(state="idle", unit="active", work_left=0) == "ok"
    assert v(state="idle", unit="active", work_left=None) == "ok"
    assert v(state="working", unit="active", work_left=2) == "ok"
    assert v(state="working", unit="active", work_left=2, same_tail=True) == "busywork"
    assert v(state="working", unit="active", work_left=2, repeated_item=True) == "repeated"
    # `idle` wins over `repeated`: other machines write to the same ledger.
    assert v(state="idle", unit="active", work_left=2, repeated_item=True) == "idle"
    assert v(state="working", unit="inactive", work_left=2) == "stopped"
    assert v(state="blocked", unit="active", work_left=2) == "stopped"
    assert v(state="blocked", unit="active", work_left=0) == "stopped"
    assert v(state="needs_attention", unit="active", work_left=None) == "stopped"
    assert v(state="idle", unit="active", work_left=2, asks_to_continue=True) == "stopped"
    assert v(state="working", unit="active", work_left=2, asks_to_continue=True) == "ok"
    assert v(state="working", unit="inactive", work_left=0) == "ok"
    assert not supervise._may_nudge("stopped", "blocked")
    assert not supervise._may_nudge("stopped", "needs_attention")
    assert not supervise._may_nudge("stopped", "working")
    assert not supervise._may_nudge("stopped", "unknown")
    assert supervise._may_nudge("stopped", "idle")
    assert supervise._may_nudge("idle", "done")


# (3) The supervisor stops when no work is left.
def test_every_mode_ends_on_first_pass_with_no_work_left(tmp_path):
    fakes = Fakes(tmp_path, [_loop(state="idle")], work=2)
    sleeps: list[float] = []

    def sleep(seconds):
        sleeps.append(seconds)
        fakes.work = 0

    code = supervise.supervise(every=600, sleep=sleep, emit=lambda report: None, **fakes.kwargs())

    assert code == 0
    assert sleeps == [600]
    assert len(fakes.nudges) == 1  # first pass only
    assert [event["status"] for _, event in fakes.ledger] == ["nudged", "idle"]


def test_no_loops_on_this_machine_exits_zero(tmp_path, capsys):
    fakes = Fakes(tmp_path, [])

    code = supervise.supervise(every=600, sleep=pytest.fail, **fakes.kwargs())

    assert code == 0
    assert "no loops on this machine" in capsys.readouterr().out
    assert fakes.ledger == []


def _real_work_left(monkeypatch, *, prs, ready=0):
    nodes = [{"status": "ready"}] * ready
    monkeypatch.setattr(
        supervise.roadmap_cli, "build_roadmap",
        lambda repos, **kw: {"repos": {repos[0]: nodes}, "warnings": []},
    )
    monkeypatch.setattr(supervise.roadmap, "_repo_identity", lambda path: ("acme", "widgets", None))
    monkeypatch.setattr(supervise.gh_cache, "cached_gh_json", lambda owner, name, key, fetch, **kw: prs)
    return functools.partial(supervise._work_left, {})


def test_open_pull_request_is_work_left(tmp_path, monkeypatch):
    work_left = _real_work_left(
        monkeypatch, prs=([{"number": 5, "isDraft": False}, {"number": 6, "isDraft": True}], None)
    )
    assert work_left("acme/widgets", "widgets") == (1, [])
    fakes = Fakes(tmp_path, [_loop(state="idle")])
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        supervise.supervise(every=600, sleep=sleep, emit=lambda r: None, **fakes.kwargs(work_left=work_left))

    assert len(sleeps) == 2  # it did not stop
    assert fakes.nudges[0][0] == "widgets"


def test_unreadable_pull_requests_make_work_unknown(monkeypatch):
    work_left = _real_work_left(monkeypatch, prs=(None, "no cached GitHub data yet"), ready=4)
    assert work_left("acme/widgets", "widgets") == (None, ["pull requests: unreadable"])


def test_claim_permission_refusal_counts_as_unclaimed_with_a_note(monkeypatch):
    def refuse(targets, **kwargs):
        raise redis.exceptions.NoPermissionError("no permissions to run the 'multi' command")

    monkeypatch.setattr(supervise.claims, "claims_for", refuse)
    monkeypatch.setattr(supervise.roadmap, "_repo_identity", lambda path: ("acme", "widgets", None))
    monkeypatch.setattr(supervise.gh_cache, "cached_gh_json", lambda *a, **kw: ([], None))

    def build(repos, claims_lookup, **kw):
        try:
            claims_lookup(["acme/widgets"])
        except supervise.CoordinatorUnreachable:
            pass  # what build_roadmap does: show issues as unclaimed
        return {"repos": {repos[0]: [{"status": "ready"}]}, "warnings": ["claim data is unavailable"]}

    monkeypatch.setattr(supervise.roadmap_cli, "build_roadmap", build)
    assert supervise._work_left({}, "acme/widgets", "widgets") == (1, ["claims: unreadable"])


def test_refused_redis_login_is_not_hidden(monkeypatch):
    def refused(targets, **kwargs):
        raise supervise.CoordinatorAuthFailed("login refused")

    monkeypatch.setattr(supervise.claims, "claims_for", refused)
    monkeypatch.setattr(
        supervise.roadmap_cli, "build_roadmap",
        lambda repos, claims_lookup, **kw: claims_lookup(["acme/widgets"]),
    )
    with pytest.raises(supervise.CoordinatorAuthFailed):
        supervise._work_left({}, "acme/widgets", "widgets")


def test_other_claim_errors_make_work_unknown(monkeypatch):
    def unreachable(targets, **kwargs):
        raise supervise.CoordinatorUnreachable("down")

    monkeypatch.setattr(supervise.claims, "claims_for", unreachable)
    monkeypatch.setattr(
        supervise.roadmap_cli, "build_roadmap",
        lambda repos, claims_lookup, **kw: claims_lookup(["acme/widgets"]),
    )
    assert supervise._work_left({}, "acme/widgets", "widgets") == (None, [])


# (4) No merge action exists in the code path.
_FORBIDDEN = ("merge", "push", "--force", "commit", "--author", "reset", "rebase")
_ALLOWED_COMMANDS = {
    "_run": {("systemctl", "is-active")},
    "_herdr": {("agent", "prompt")},
    "_run_json": {("gh", "pr", "list")},
}
_ALLOWED_IMPORTS = {
    "__future__", "fcntl", "functools", "hashlib", "json", "os", "time", "uuid",
    "datetime", "pathlib", "typing", "redis",
}
# The only names that the module may use from each Lupin module.
_ALLOWED_ATTRS = {
    "loop_runtime": {
        "local_loops", "peek_loop", "_read_metadata", "_lock_file", "base_repo", "unit_name",
        "_run", "_herdr", "LoopError", "STATE_DIR", "CODE_DIR",
        "HERDR_TIMEOUT", "SYSTEMCTL_CHECK_TIMEOUT",
    },
    "roadmap": {"_repo_identity", "_run_json"},
    "roadmap_cli": {"build_roadmap"},
    "gh_cache": {"cached_gh_json"},
    "claims": {"claims_for"},
    "ledger": {"read_events", "append_event"},
}
# A name that may be used only in one function.
_ONLY_IN = {
    ("loop_runtime", "_herdr"): "_send_nudge",
    ("loop_runtime", "_run"): "_unit_state",
    ("roadmap", "_run_json"): "_open_prs",
    ("ledger", "append_event"): "_append_event",
}
_BANNED_NAMES = {"__import__", "eval", "exec", "compile", "getattr", "importlib", "globals", "vars"}


def _docstrings(tree) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                ids.add(id(first.value))
    return ids


def check_no_merge_text(source: str) -> None:
    tree = ast.parse(source)
    skip = _docstrings(tree)
    strings = [
        node.value.lower() for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip
    ]
    for word in _FORBIDDEN:
        assert not [s for s in strings if word in s], word


def check_only_allowlisted_commands(source: str) -> None:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name in _ALLOWED_IMPORTS, alias.name
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            assert node.module in _ALLOWED_IMPORTS - {"os"}, node.module
        elif isinstance(node, ast.Name):
            assert node.id not in _BANNED_NAMES, node.id
    os_attrs = {
        node.attr for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "os"
    }
    assert os_attrs <= {"environ", "path", "replace"}, os_attrs
    module_attrs = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            module_attrs.add(id(node.value))
            if node.value.id in _ALLOWED_ATTRS:
                assert node.attr in _ALLOWED_ATTRS[node.value.id], f"{node.value.id}.{node.attr}"
    for node in ast.walk(tree):
        # A Lupin module name only as `module.name`, so no alias can hide a call.
        if isinstance(node, ast.Name) and node.id in _ALLOWED_ATTRS:
            assert id(node) in module_attrs, f"{node.id} is used without an attribute"
    for function in ast.walk(tree):
        if not isinstance(function, ast.FunctionDef):
            continue
        for node in ast.walk(function):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                owner = _ONLY_IN.get((node.value.id, node.attr))
                assert owner in (None, function.name), f"{node.value.id}.{node.attr} in {function.name}"
    top_level = [n for n in tree.body if not isinstance(n, (ast.FunctionDef, ast.ClassDef))]
    for statement in top_level:
        for node in ast.walk(statement):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                assert (node.value.id, node.attr) not in _ONLY_IN, f"{node.value.id}.{node.attr}"
    called = {id(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    commands: dict[str, set] = {name: set() for name in _ALLOWED_COMMANDS}
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in _ALLOWED_COMMANDS:
            # Only a direct call, so no alias or `partial` can hide the arguments.
            assert id(node) in called, f"{node.attr} is used without a direct call"
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        name = node.func.attr
        if name == "_run" or name == "_run_json":
            argv = node.args[0]
            assert isinstance(argv, ast.List), name
            width = 3 if name == "_run_json" else 2
            head = argv.elts[:width]
        elif name == "_herdr":
            head = node.args[1:3]
        else:
            continue
        assert all(isinstance(item, ast.Constant) for item in head), name
        commands[name].add(tuple(item.value for item in head))
    assert commands == _ALLOWED_COMMANDS


def test_module_has_no_merge_push_force_or_author_text():
    check_no_merge_text(inspect.getsource(supervise))


def test_module_runs_only_allowlisted_commands():
    check_only_allowlisted_commands(inspect.getsource(supervise))
