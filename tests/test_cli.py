"""Tests for the 6 new `lupin` CLI verbs from issue #2 phase A: `stop`,
`peek`, `attach`, `schedule`, `pause`, `resume`. All mock
`loops.dispatch_loop_action`/`resolve_machine_for_repo`/`ssh_target_for`
directly, so none of these need a real Redis or a real fleet machine --
that's `test_loops.py`'s job for the dispatch function itself, and
`test_agent.py`'s for the queue actions it talks to.
"""

from __future__ import annotations

import json
from unittest import mock

import pytest

from lupin import cli, loops, machines, slots


@pytest.fixture(autouse=True)
def local_host(monkeypatch):
    monkeypatch.setattr(machines, "hostname", lambda: "h")
    yield "h"


# --------------------------------------------------------------------------
# stop
# --------------------------------------------------------------------------


def test_stop_local_success_exits_zero(capsys):
    with mock.patch.object(loops, "dispatch_loop_action", return_value={"mode": "local", "returncode": 0, "output": ""}):
        code = cli.main(["stop", "widgets", "--machine", "h"])
    assert code == 0


def test_stop_local_failure_exits_one(capsys):
    with mock.patch.object(
        loops, "dispatch_loop_action", return_value={"mode": "local", "returncode": 1, "output": "boom"}
    ):
        code = cli.main(["stop", "widgets", "--machine", "h"])
    assert code == 1


def test_stop_json_shape(capsys):
    with mock.patch.object(loops, "dispatch_loop_action", return_value={"mode": "local", "returncode": 0, "output": "ok"}):
        code = cli.main(["stop", "widgets", "--machine", "h", "--json"])
    captured = capsys.readouterr()
    assert code == 0
    assert json.loads(captured.out) == {"mode": "local", "returncode": 0, "output": "ok"}


def test_stop_ambiguous_machine_exits_five(capsys):
    with mock.patch.object(loops, "resolve_machine_for_repo", side_effect=loops.AmbiguousMachine("widgets", [])):
        code = cli.main(["stop", "widgets"])
    captured = capsys.readouterr()
    assert code == 5
    assert "widgets" in captured.err


def test_stop_resolves_machine_from_repo_when_not_given():
    with (
        mock.patch.object(loops, "resolve_machine_for_repo", return_value="jesus") as resolve,
        mock.patch.object(
            loops, "dispatch_loop_action", return_value={"mode": "local", "returncode": 0, "output": ""}
        ) as dispatch,
    ):
        code = cli.main(["stop", "widgets"])
    assert code == 0
    resolve.assert_called_once()
    assert resolve.call_args.args[0] == "widgets"
    assert dispatch.call_args.kwargs["machine"] == "jesus"


def test_stop_coordinator_unreachable_exits_three(capsys):
    with mock.patch.object(loops, "dispatch_loop_action", side_effect=slots.CoordinatorUnreachable("x")):
        code = cli.main(["stop", "widgets", "--machine", "jesus"])
    captured = capsys.readouterr()
    assert code == 3
    assert "cannot reach" in captured.err


def test_stop_missing_signing_key_exits_one(capsys):
    with mock.patch.object(loops, "dispatch_loop_action", side_effect=loops.MissingSigningKey("jesus")):
        code = cli.main(["stop", "widgets", "--machine", "jesus"])
    captured = capsys.readouterr()
    assert code == 1
    assert "signing-key" in captured.err


def test_stop_remote_ok_exits_zero():
    with mock.patch.object(
        loops, "dispatch_loop_action",
        return_value={"mode": "queued", "id": "abc123", "result": {"id": "abc123", "state": "ok"}},
    ):
        code = cli.main(["stop", "widgets", "--machine", "jesus", "--signing-key", "s"])
    assert code == 0


def test_stop_remote_still_running_exits_four():
    with mock.patch.object(
        loops, "dispatch_loop_action",
        return_value={"mode": "queued", "id": "abc123", "result": {"id": "abc123", "state": "running"}},
    ):
        code = cli.main(["stop", "widgets", "--machine", "jesus", "--signing-key", "s"])
    assert code == 4


def test_stop_remote_unknown_result_exits_four():
    with mock.patch.object(
        loops, "dispatch_loop_action", return_value={"mode": "queued", "id": "abc123", "result": None}
    ):
        code = cli.main(["stop", "widgets", "--machine", "jesus", "--signing-key", "s"])
    assert code == 4


def test_stop_remote_failed_exits_one():
    with mock.patch.object(
        loops, "dispatch_loop_action",
        return_value={"mode": "queued", "id": "abc123", "result": {"id": "abc123", "state": "failed"}},
    ):
        code = cli.main(["stop", "widgets", "--machine", "jesus", "--signing-key", "s"])
    assert code == 1


# --------------------------------------------------------------------------
# peek
# --------------------------------------------------------------------------


def test_peek_default_lines_is_sixty():
    with mock.patch.object(
        loops, "dispatch_loop_action", return_value={"mode": "local", "returncode": 0, "output": "pane"}
    ) as dispatch:
        code = cli.main(["peek", "widgets", "--machine", "h"])
    assert code == 0
    assert dispatch.call_args.kwargs["local_argv"] == ["loopctl", "peek", "widgets", "60"]
    assert dispatch.call_args.kwargs["queue_params"] == {"repo": "widgets", "lines": 60}


def test_peek_custom_lines():
    with mock.patch.object(
        loops, "dispatch_loop_action", return_value={"mode": "local", "returncode": 0, "output": ""}
    ) as dispatch:
        cli.main(["peek", "widgets", "20", "--machine", "h"])
    assert dispatch.call_args.kwargs["local_argv"] == ["loopctl", "peek", "widgets", "20"]


def test_peek_prints_pane_output(capsys):
    with mock.patch.object(
        loops, "dispatch_loop_action", return_value={"mode": "local", "returncode": 0, "output": "pane text\n"}
    ):
        cli.main(["peek", "widgets", "--machine", "h"])
    assert "pane text" in capsys.readouterr().out


# --------------------------------------------------------------------------
# attach
# --------------------------------------------------------------------------


def test_attach_local_print_shows_loopctl_argv(capsys):
    code = cli.main(["attach", "widgets", "--machine", "h", "--print"])
    assert code == 0
    assert capsys.readouterr().out.strip() == "loopctl attach widgets"


def test_attach_remote_print_shows_ssh_argv(capsys):
    with mock.patch.object(loops, "ssh_target_for", return_value="ghosta@jesus.local"):
        code = cli.main(["attach", "widgets", "--machine", "jesus", "--print"])
    assert code == 0
    assert capsys.readouterr().out.strip() == "ssh -t ghosta@jesus.local loopctl attach widgets"


def test_attach_remote_without_ssh_target_exits_one(capsys):
    with mock.patch.object(loops, "ssh_target_for", return_value=None):
        code = cli.main(["attach", "widgets", "--machine", "jesus", "--print"])
    captured = capsys.readouterr()
    assert code == 1
    assert "ssh-targets" in captured.err or "ssh target" in captured.err


def test_attach_ambiguous_machine_exits_five(capsys):
    with mock.patch.object(loops, "resolve_machine_for_repo", side_effect=loops.AmbiguousMachine("widgets", [])):
        code = cli.main(["attach", "widgets", "--print"])
    assert code == 5


def test_attach_execs_when_not_print(monkeypatch):
    # Real `os.execvp` replaces this process and never returns -- `_cmd_attach`
    # has no `return` after that call (see its `# pragma: no cover` line), so
    # `sys.exit(None)` is what actually runs the process down, with exit code
    # 0. The mock below lets the call return instead of exec'ing, which is
    # why `cli.main` gives back `None` here rather than `0`.
    calls = []
    monkeypatch.setattr(cli.os, "execvp", lambda prog, argv: calls.append((prog, argv)))
    code = cli.main(["attach", "widgets", "--machine", "h"])
    assert code is None
    assert calls == [("loopctl", ["loopctl", "attach", "widgets"])]


# --------------------------------------------------------------------------
# schedule
# --------------------------------------------------------------------------


def test_schedule_bare_means_show():
    with mock.patch.object(
        loops, "dispatch_loop_action", return_value={"mode": "local", "returncode": 0, "output": ""}
    ) as dispatch:
        code = cli.main(["schedule", "--machine", "h"])
    assert code == 0
    assert dispatch.call_args.kwargs["local_argv"] == ["loopctl", "schedule"]
    assert dispatch.call_args.kwargs["queue_action"] == "schedule.show"


def test_schedule_cal_builds_expected_argv():
    with mock.patch.object(
        loops, "dispatch_loop_action", return_value={"mode": "local", "returncode": 0, "output": ""}
    ) as dispatch:
        code = cli.main(["schedule", "--machine", "h", "cal", "*-*-* 00/5:00:00"])
    assert code == 0
    assert dispatch.call_args.kwargs["local_argv"] == ["loopctl", "schedule", "cal", "*-*-* 00/5:00:00"]
    assert dispatch.call_args.kwargs["queue_params"] == {"mode": "cal", "expr": "*-*-* 00/5:00:00"}


def test_schedule_cal_with_newline_is_rejected_before_dispatch(capsys):
    with mock.patch.object(loops, "dispatch_loop_action") as dispatch:
        code = cli.main(["schedule", "--machine", "h", "cal", "bad\nexpr"])
    assert code == 1
    dispatch.assert_not_called()
    assert capsys.readouterr().err  # some explanation was printed


def test_schedule_first_builds_expected_argv():
    with mock.patch.object(
        loops, "dispatch_loop_action", return_value={"mode": "local", "returncode": 0, "output": ""}
    ) as dispatch:
        code = cli.main(["schedule", "--machine", "h", "first", "+2h5m", "every", "5h15m"])
    assert code == 0
    assert dispatch.call_args.kwargs["local_argv"] == ["loopctl", "schedule", "first", "+2h5m", "every", "5h15m"]
    assert dispatch.call_args.kwargs["queue_params"] == {"mode": "first", "when": "+2h5m", "interval": "5h15m"}


def test_schedule_defaults_machine_to_local_host():
    with mock.patch.object(
        loops, "dispatch_loop_action", return_value={"mode": "local", "returncode": 0, "output": ""}
    ) as dispatch:
        cli.main(["schedule"])
    assert dispatch.call_args.kwargs["machine"] == "h"


# --------------------------------------------------------------------------
# pause / resume
# --------------------------------------------------------------------------


def test_pause_single_machine_default_local():
    with mock.patch.object(
        loops, "dispatch_loop_action", return_value={"mode": "local", "returncode": 0, "output": ""}
    ) as dispatch:
        code = cli.main(["pause"])
    assert code == 0
    assert dispatch.call_args.kwargs["machine"] == "h"
    assert dispatch.call_args.kwargs["local_argv"] == ["loopctl", "pause"]
    assert dispatch.call_args.kwargs["queue_action"] == "schedule.pause"


def test_resume_single_machine():
    with mock.patch.object(
        loops, "dispatch_loop_action", return_value={"mode": "local", "returncode": 0, "output": ""}
    ) as dispatch:
        code = cli.main(["resume", "--machine", "jesus", "--signing-key", "s"])
    assert code == 0
    assert dispatch.call_args.kwargs["machine"] == "jesus"
    assert dispatch.call_args.kwargs["queue_action"] == "schedule.resume"


def test_pause_all_dispatches_to_every_registered_machine(capsys):
    records = [{"name": "jesus"}, {"name": "mini"}]
    with (
        mock.patch.object(machines, "machines", return_value=records),
        mock.patch.object(
            loops, "dispatch_loop_action", return_value={"mode": "local", "returncode": 0, "output": ""}
        ) as dispatch,
    ):
        code = cli.main(["pause", "--all", "--json"])
    assert code == 0
    assert sorted(call.kwargs["machine"] for call in dispatch.call_args_list) == ["jesus", "mini"]
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {"jesus", "mini"}


def test_pause_all_worst_exit_code_wins(capsys):
    records = [{"name": "jesus"}, {"name": "mini"}]

    def fake_dispatch(*, machine, **kwargs):
        if machine == "jesus":
            return {"mode": "local", "returncode": 0, "output": ""}
        return {"mode": "local", "returncode": 1, "output": "boom"}

    with (
        mock.patch.object(machines, "machines", return_value=records),
        mock.patch.object(loops, "dispatch_loop_action", side_effect=fake_dispatch),
    ):
        code = cli.main(["pause", "--all"])
    assert code == 1
