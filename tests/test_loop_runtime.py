from __future__ import annotations

import json
from pathlib import Path

import pytest

from lupin import loop_runtime


def test_systemd_run_uses_nixos_sudo_wrapper_when_not_on_path(monkeypatch, tmp_path):
    wrapper = tmp_path / "sudo"
    wrapper.touch()
    wrapper.chmod(0o755)
    monkeypatch.setattr(loop_runtime, "NIXOS_SUDO_WRAPPER", wrapper)
    monkeypatch.setenv("PATH", "")
    launched = []
    monkeypatch.setattr(
        loop_runtime,
        "_run",
        lambda argv, **kwargs: launched.append(argv) or (0, ""),
    )

    result = loop_runtime._systemd_run("widgets", "loop", ["lupin", "loop", "worker"])
    assert result == (0, "")

    assert launched[0][:3] == [str(wrapper), "-n", "systemd-run"]


def test_sudo_argv_uses_path_when_nixos_wrapper_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(loop_runtime, "NIXOS_SUDO_WRAPPER", tmp_path / "sudo")

    assert loop_runtime._sudo_argv("systemd-run") == ["sudo", "-n", "systemd-run"]


def test_loop_state_uses_herdr_agent_state_and_ids(monkeypatch):
    metadata = {
        "repo": "widgets",
        "platform": "claude",
        "session": "lupin-widgets-abc123",
        "started_at": "2025-01-02T03:04:05Z",
        "workspace_id": "workspace-1",
    }
    workspace = {
        "id": "workspace-1",
        "root_pane": {"pane_id": "pane-1", "workspace_id": "workspace-1"},
    }
    monkeypatch.setattr(loop_runtime, "_read_metadata", lambda repo: metadata)
    monkeypatch.setattr(loop_runtime, "_server_running", lambda session: True)
    monkeypatch.setattr(loop_runtime, "_find_workspace", lambda session, data: workspace)
    monkeypatch.setattr(
        loop_runtime,
        "_agent_for_workspace",
        lambda session, workspace_id: {"name": "lupin-loop", "agent_status": "blocked"},
    )

    state = loop_runtime.loop_state("widgets")

    assert state == {
        "repo": "widgets",
        "platform": "claude",
        "backend": "herdr",
        "state": "blocked",
        "session": "lupin-widgets-abc123",
        "workspace_id": "workspace-1",
        "pane_id": "pane-1",
        "since": "2025-01-02T03:04:05Z",
        "agent": "lupin-loop",
    }

def test_local_loops_marks_a_missing_running_workspace_for_attention(monkeypatch, tmp_path: Path):
    loops_dir = tmp_path / "herdr-loops"
    loops_dir.mkdir()
    path = loops_dir / "widgets.json"
    path.write_text(
        json.dumps({
            "repo": "widgets",
            "platform": "claude",
            "session": "lupin-widgets-abc123",
            "state": "running",
            "started_at": "2025-01-02T03:04:05Z",
            "workspace_id": "workspace-1",
            "pane_id": "pane-1",
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(loop_runtime, "LOOPS_DIR", loops_dir)
    monkeypatch.setattr(loop_runtime, "_server_running", lambda session: True)
    monkeypatch.setattr(loop_runtime, "_find_workspace", lambda session, metadata: None)

    loops = loop_runtime.local_loops()

    assert len(loops) == 1
    assert loops[0]["state"] == "needs_attention"
    assert loops[0]["workspace_id"] == "workspace-1"
    assert loops[0]["pane_id"] == "pane-1"
    assert json.loads(path.read_text(encoding="utf-8"))["state"] == "needs_attention"




@pytest.mark.parametrize("other_workspaces, stop_session", [([{"id": "other"}], False), ([], True)])
def test_stop_closes_only_loop_workspace_and_stops_empty_session(
    monkeypatch, tmp_path: Path, other_workspaces: list[dict], stop_session: bool
):
    metadata = {
        "repo": "widgets",
        "platform": "claude",
        "session": "lupin-widgets-abc123",
        "state": "running",
        "workspace_id": "workspace-1",
        "pane_id": "pane-1",
    }
    monkeypatch.setattr(loop_runtime, "STATE_DIR", tmp_path)
    actions = []
    writes = []
    monkeypatch.setattr(loop_runtime, "_read_metadata", lambda repo: dict(metadata))
    monkeypatch.setattr(loop_runtime, "_start_server_for_read", lambda repo, data: data["session"])
    monkeypatch.setattr(loop_runtime, "_find_workspace", lambda session, data: {"id": "workspace-1"})
    monkeypatch.setattr(loop_runtime, "_save_report", lambda repo, session, workspace: tmp_path / "report.log")
    monkeypatch.setattr(loop_runtime, "_workspace_id", lambda workspace: workspace["id"])
    monkeypatch.setattr(loop_runtime, "_herdr_json", lambda session, *args: actions.append((session, args)) or {})
    monkeypatch.setattr(loop_runtime, "_workspaces", lambda session: other_workspaces)
    monkeypatch.setattr(loop_runtime, "_write_metadata", lambda repo, value: writes.append(dict(value)))

    def run(argv, **kwargs):
        if argv[:3] == ["systemctl", "is-active", loop_runtime.unit_name("widgets") + ".service"]:
            return 0, "active"
        if argv[-4:] == [
            "-n",
            "systemctl",
            "stop",
            f"{loop_runtime.unit_name('widgets')}.service",
        ]:
            actions.append(("systemd", tuple(argv[-3:])))
            return 0, ""
        raise AssertionError(argv)

    monkeypatch.setattr(loop_runtime, "_run", run)

    result = loop_runtime.stop_loop("widgets")

    assert result == "stopped widgets; report saved to " + str(tmp_path / "report.log")
    assert ("lupin-widgets-abc123", ("workspace", "close", "workspace-1")) in actions
    assert any(item[0] == "systemd" for item in actions)
    assert any(item == (None, ("session", "stop", "lupin-widgets-abc123", "--json")) for item in actions) is stop_session
    assert writes[-1]["state"] == "stopped"
    assert writes[-1]["workspace_id"] is None


def test_local_loops_keeps_unknown_state_when_herdr_is_down(monkeypatch, tmp_path: Path):
    loops_dir = tmp_path / "herdr-loops"
    loops_dir.mkdir()
    (loops_dir / "widgets.json").write_text(
        '{"repo":"widgets","platform":"omp","session":"lupin-widgets-abc123",'
        '"state":"running","started_at":"2025-01-02T03:04:05Z"}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(loop_runtime, "LOOPS_DIR", loops_dir)
    monkeypatch.setattr(
        loop_runtime,
        "loop_state",
        lambda repo: {"repo": repo, "backend": "herdr", "state": "unknown", "session": "lupin-widgets-abc123"},
    )

    assert loop_runtime.local_loops() == [
        {
            "repo": "widgets",
            "platform": "omp",
            "state": "unknown",
            "since": "2025-01-02T03:04:05Z",
            "backend": "herdr",
            "session": "lupin-widgets-abc123",
            "workspace_id": None,
            "pane_id": None,
        }
    ]


def test_attach_uses_verified_herdr_remote_session():
    session = loop_runtime.session_name("widgets")
    assert loop_runtime.attach_argv("widgets", "mini", "jesus", "ghosta@mini.local") == [
        loop_runtime.HERDR, "--remote", "ghosta@mini.local", "--session", session
    ]


@pytest.mark.parametrize("expression", ["daily\nOnBootSec=0", "daily\x7f"])
def test_schedule_rejects_systemd_control_characters(
    monkeypatch, tmp_path: Path, expression: str
):
    monkeypatch.setattr(loop_runtime, "STATE_DIR", tmp_path)
    monkeypatch.setattr(loop_runtime, "_run", lambda *args, **kwargs: pytest.fail("systemd was called"))

    with pytest.raises(loop_runtime.LoopError, match="invalid calendar expression"):
        loop_runtime.schedule_local(["cal", expression])

    assert not (tmp_path / "timer-dropin.d" / "override.conf").exists()

@pytest.mark.parametrize(
    ("args", "settings"),
    [
        (
            ["first", "+2h", "every", "1h"],
            "OnActiveSec=2h\nOnUnitActiveSec=1h\nAccuracySec=1s\n",
        ),
        (
            ["first", "tomorrow 09:00", "every", "1h"],
            "OnCalendar=tomorrow 09:00\nOnUnitActiveSec=1h\nAccuracySec=1s\n",
        ),
        (["cal", "daily"], "OnCalendar=daily\nAccuracySec=1s\n"),
    ],
)
def test_schedule_replaces_previous_timer_triggers(
    monkeypatch, tmp_path: Path, args: list[str], settings: str
):
    monkeypatch.setattr(loop_runtime, "STATE_DIR", tmp_path)
    monkeypatch.setattr(loop_runtime, "_run", lambda *args, **kwargs: (0, ""))

    assert loop_runtime.schedule_local(args) == "updated delegation-loop.timer"

    override = (tmp_path / "timer-dropin.d" / "override.conf").read_text(encoding="utf-8")
    reset = (
        "[Timer]\n"
        "OnCalendar=\n"
        "OnActiveSec=\n"
        "OnBootSec=\n"
        "OnStartupSec=\n"
        "OnUnitActiveSec=\n"
        "OnUnitInactiveSec=\n"
    )
    assert override == reset + settings


def test_schedule_once_rejects_empty_repo_list_before_systemd(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(loop_runtime, "STATE_DIR", tmp_path)
    monkeypatch.setattr(loop_runtime, "_run", lambda *args, **kwargs: pytest.fail("systemd ran"))

    with pytest.raises(loop_runtime.LoopError, match="at least one repo"):
        loop_runtime.schedule_once("+2h", [])

    assert not (tmp_path / "once").exists()



def test_repo_catalog_includes_repo_without_delegation_doc(monkeypatch, tmp_path: Path):
    code_dir = tmp_path / "code"
    (code_dir / "widgets").mkdir(parents=True)
    monkeypatch.setattr(loop_runtime, "CODE_DIR", code_dir)
    monkeypatch.setattr(loop_runtime, "enabled_repos", lambda: {})

    assert loop_runtime.repo_catalog() == [
        {
            "repo": "widgets",
            "loopable": True,
            "has_doc": False,
            "state": "disabled",
            "platform": "claude",
        }
    ]


def test_start_loop_skips_missing_checkout(monkeypatch, tmp_path: Path):
    code_dir = tmp_path / "code"
    monkeypatch.setattr(loop_runtime, "CODE_DIR", code_dir)
    monkeypatch.setattr(loop_runtime, "enabled_repos", lambda: {})
    monkeypatch.setattr(
        loop_runtime, "_run",
        lambda *args, **kwargs: pytest.fail("systemd must not run without a checkout"),
    )

    started, message = loop_runtime.start_loop("widgets")

    assert not started
    assert message == f"skip widgets: no checkout at {code_dir / 'widgets'}"


def test_start_loop_starts_without_delegation_doc_and_skips_duplicate(
    monkeypatch, tmp_path: Path
):
    state_dir = tmp_path / "state"
    code_dir = tmp_path / "code"
    (code_dir / "widgets").mkdir(parents=True)
    monkeypatch.setattr(loop_runtime, "STATE_DIR", state_dir)
    monkeypatch.setattr(loop_runtime, "LOOPS_DIR", state_dir / "herdr-loops")
    monkeypatch.setattr(loop_runtime, "REPOS_FILE", state_dir / "repos")
    monkeypatch.setattr(loop_runtime, "CODE_DIR", code_dir)
    monkeypatch.setattr(loop_runtime, "enabled_repos", lambda: {})
    monkeypatch.setattr(loop_runtime, "_session_info", lambda session: None)

    def run(argv, **kwargs):
        if argv[0] == "systemctl":
            return 3, "inactive"
        if argv[0] == "tmux":
            return 1, "no session"
        raise AssertionError(argv)

    monkeypatch.setattr(loop_runtime, "_run", run)
    launches = []

    def systemd_run(repo, kind, command, **kwargs):
        launches.append((repo, kind, command, kwargs))
        return 0, ""

    monkeypatch.setattr(loop_runtime, "_systemd_run", systemd_run)

    started, message = loop_runtime.start_loop(
        "widgets", platform="omp", note="review", resume=True
    )

    assert started
    assert "Herdr session" in message
    assert len(launches) == 1
    repo, kind, command, options = launches[0]
    assert repo == "widgets"
    assert kind == "loop"
    metadata = loop_runtime._read_metadata("widgets")
    assert metadata["state"] == "starting"
    assert metadata["platform"] == "omp"
    prompt = Path(metadata["prompt_file"]).read_text(encoding="utf-8")
    assert "If docs/delegation-loop.md exists, read it" in prompt
    assert prompt.endswith("review\n")
    assert command[1:] == [
        "loop",
        "worker",
        "--repo",
        "widgets",
        "--platform",
        "omp",
        "--session",
        loop_runtime.session_name("widgets"),
        "--prompt-file",
        metadata["prompt_file"],
        "--resume",
    ]
    assert options["claude_limits"] is False

    started, message = loop_runtime.start_loop("widgets", platform="omp")

    assert not started
    assert message.startswith("skip widgets:")
    assert len(launches) == 1


def test_launch_agent_finds_the_pane_when_workspace_list_has_no_root_pane(monkeypatch, tmp_path: Path):
    # Herdr 0.9.3 `workspace list` rows carry `workspace_id` but no `root_pane`.
    prompt_file = tmp_path / "prompt"
    prompt_file.write_text("finish the handoff\n", encoding="utf-8")
    session = "lupin-widgets-abc123"
    calls = []
    answers = {
        ("workspace", "list"): {"workspaces": [
            {"workspace_id": "w1", "label": "widgets", "pane_count": 1},
        ]},
        ("pane", "list"): {"panes": [
            {"pane_id": "w9:p1", "workspace_id": "w9"},
            {"pane_id": "w1:p1", "workspace_id": "w1"},
        ]},
    }
    monkeypatch.setattr(loop_runtime, "_herdr_json", lambda got_session, *args: answers.get(args, {}))
    monkeypatch.setattr(loop_runtime, "_run", lambda argv, **kwargs: calls.append(argv) or (0, ""))
    monkeypatch.setattr(loop_runtime, "_wait_agent", lambda got_session, workspace_id: 0)

    assert loop_runtime.launch_agent(
        "widgets", session, "w1", "claude", str(prompt_file), resume=False
    ) == 0

    assert calls[0][calls[0].index("--pane") + 1] == "w1:p1"


def test_launch_agent_sends_the_prompt_without_newlines(monkeypatch, tmp_path: Path):
    # Herdr 0.9.3 rejects any newline in an agent argument.
    prompt_file = tmp_path / "prompt"
    prompt_file.write_text("first line\n\nsecond  line\n", encoding="utf-8")
    workspace = {"id": "w1", "root_pane": {"pane_id": "w1:p1", "workspace_id": "w1"}}
    calls = []
    monkeypatch.setattr(loop_runtime, "_run", lambda argv, **kwargs: calls.append(argv) or (0, ""))
    monkeypatch.setattr(loop_runtime, "_wait_agent", lambda got_session, workspace_id: 0)

    loop_runtime._launch_agent("lupin-widgets-abc123", "claude", workspace, str(prompt_file), resume=False)

    assert calls[0][-1] == "first line second line"


def test_launch_agent_uses_herdr_start_api(monkeypatch, tmp_path: Path):
    prompt_file = tmp_path / "prompt"
    prompt_file.write_text("finish the handoff\n", encoding="utf-8")
    session = "lupin-widgets-abc123"
    workspace = {
        "id": "workspace-1",
        "root_pane": {"pane_id": "pane-1", "workspace_id": "workspace-1"},
    }
    calls = []
    waited = []
    monkeypatch.setattr(
        loop_runtime,
        "_run",
        lambda argv, **kwargs: calls.append((argv, kwargs)) or (0, ""),
    )

    def wait_agent(got_session, workspace_id):
        waited.append((got_session, workspace_id))
        return 0

    monkeypatch.setattr(loop_runtime, "_wait_agent", wait_agent)

    assert loop_runtime._launch_agent(
        session, "claude", workspace, str(prompt_file), resume=True
    ) == 0

    argv, options = calls[0]
    assert argv == [
        loop_runtime.HERDR,
        "--session",
        session,
        "agent",
        "start",
        loop_runtime.AGENT_NAME,
        "--kind",
        "claude",
        "--pane",
        "pane-1",
        "--timeout",
        "300000",
        "--",
        "--continue",
        "finish the handoff",
    ]
    assert options["timeout"] == 310.0
    assert options["env"]["HERDR_ENV"] == "1"
    assert waited == [(session, "workspace-1")]


def test_start_loop_refuses_an_open_legacy_session(monkeypatch, tmp_path: Path):
    state_dir = tmp_path / "state"
    code_dir = tmp_path / "code"
    repo_dir = code_dir / "widgets"
    (repo_dir / "docs").mkdir(parents=True)
    (repo_dir / "docs" / "delegation-loop.md").touch()
    monkeypatch.setattr(loop_runtime, "STATE_DIR", state_dir)
    monkeypatch.setattr(loop_runtime, "LOOPS_DIR", state_dir / "herdr-loops")
    monkeypatch.setattr(loop_runtime, "REPOS_FILE", state_dir / "repos")
    monkeypatch.setattr(loop_runtime, "CODE_DIR", code_dir)
    monkeypatch.setattr(loop_runtime, "enabled_repos", lambda: {})
    monkeypatch.setattr(loop_runtime, "_session_info", lambda session: None)

    def run(argv, **kwargs):
        if argv[0] == "systemctl":
            return 3, "inactive"
        if argv[0] == "tmux":
            return 0, ""
        raise AssertionError(argv)

    monkeypatch.setattr(loop_runtime, "_run", run)
    monkeypatch.setattr(
        loop_runtime,
        "_systemd_run",
        lambda *args, **kwargs: pytest.fail("a legacy session must block Lupin"),
    )

    started, message = loop_runtime.start_loop("widgets")

    assert not started
    assert "legacy tmux loop-widgets is open" in message


def test_send_loop_types_the_text_then_presses_enter(monkeypatch):
    calls = []
    monkeypatch.setattr(loop_runtime, "_read_metadata", lambda repo: {"session": "lupin-widgets-abc123"})
    monkeypatch.setattr(loop_runtime, "_server_running", lambda session: True)
    monkeypatch.setattr(
        loop_runtime, "_find_workspace",
        lambda session, metadata: {"id": "w1", "root_pane": {"pane_id": "w1:p1", "workspace_id": "w1"}},
    )
    monkeypatch.setattr(loop_runtime, "_herdr", lambda session, *args, **kw: calls.append((session, args)) or "")

    loop_runtime.send_loop("widgets", "yes, keep going")

    assert calls == [
        ("lupin-widgets-abc123", ("pane", "send-text", "w1:p1", "yes, keep going")),
        ("lupin-widgets-abc123", ("pane", "send-keys", "w1:p1", "Enter")),
    ]


def test_send_loop_refuses_a_loop_that_is_not_running(monkeypatch):
    monkeypatch.setattr(loop_runtime, "_read_metadata", lambda repo: {"session": "lupin-widgets-abc123"})
    monkeypatch.setattr(loop_runtime, "_server_running", lambda session: False)

    with pytest.raises(loop_runtime.LoopError):
        loop_runtime.send_loop("widgets", "hello")
