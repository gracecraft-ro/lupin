"""Tests for quest reporting (issue #11).

Redis-backed tests use the real `redis-server` fixtures in conftest.py
(`redis_port`/`flush_redis`/`closed_port`), same as test_slots_redis.py --
not a mock, per this project's existing test convention for the `redis`
backend.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest
import redis as redis_lib

from lupin import cli, quest, roadmap
from lupin import machines as machines_mod


def _task_node(number, title, state, merged=False):
    prs = {"nodes": [{"state": "MERGED"}]} if merged else {"nodes": []}
    return {
        "number": number,
        "title": title,
        "url": f"https://github.com/acme/repo/issues/{number}",
        "state": state,
        "closedByPullRequestsReferences": prs,
    }


def _quest_node(number, title, state, sub_issues):
    return {
        "number": number,
        "title": title,
        "url": f"https://github.com/acme/repo/issues/{number}",
        "state": state,
        "closedByPullRequestsReferences": {"nodes": []},
        "subIssues": {"nodes": sub_issues},
    }


def _graphql_page(nodes):
    return {
        "data": {
            "repository": {
                "issues": {
                    "nodes": nodes,
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
        }
    }


# --------------------------------------------------------------------------
# load_quests: finding quests and counting done/open tasks
# --------------------------------------------------------------------------


def test_load_quests_counts_done_and_open_tasks():
    tasks = [
        _task_node(1, "Task one", "CLOSED"),
        _task_node(2, "Task two", "OPEN", merged=True),  # merged PR, not yet closed
        _task_node(3, "Task three", "OPEN"),
    ]
    quest_issue = _quest_node(10, "Session rewrite", "OPEN", tasks)
    responses = [
        ({"owner": {"login": "acme"}, "name": "repo"}, None),
        (_graphql_page([quest_issue]), None),
    ]
    with mock.patch.object(roadmap, "_run_json", side_effect=responses):
        quests, warnings = quest.load_quests(["repo"], code_dir="/code")

    assert warnings == []
    assert len(quests) == 1
    one = quests[0]
    assert one["name"] == "session-rewrite"
    assert one["doneCount"] == 2
    assert one["total"] == 3
    assert [task["done"] for task in one["tasks"]] == [True, True, False]


def test_no_quests_anywhere_is_an_empty_list_not_an_error():
    responses = [
        ({"owner": {"login": "acme"}, "name": "repo"}, None),
        (_graphql_page([]), None),
    ]
    with mock.patch.object(roadmap, "_run_json", side_effect=responses):
        quests, warnings = quest.load_quests(["repo"], code_dir="/code")

    assert quests == []
    assert warnings == []


def test_unreadable_repo_adds_a_warning_not_a_failure():
    with mock.patch.object(
        roadmap, "_repo_identity", return_value=(None, None, "boom")
    ):
        quests, warnings = quest.load_quests(["repo"], code_dir="/code")

    assert quests == []
    assert warnings == ["repo: boom"]


# --------------------------------------------------------------------------
# find_quest
# --------------------------------------------------------------------------


def test_find_quest_matches_by_name_or_number_with_or_without_hash():
    quests = [{"name": "session-rewrite", "number": 10}]
    assert quest.find_quest(quests, "session-rewrite") is quests[0]
    assert quest.find_quest(quests, "10") is quests[0]
    assert quest.find_quest(quests, "#10") is quests[0]
    assert quest.find_quest(quests, "nope") is None


# --------------------------------------------------------------------------
# read_focus: real redis-server, per this repo's test convention
# --------------------------------------------------------------------------


def test_read_focus_returns_none_when_key_absent(redis_port, flush_redis):
    result = quest.read_focus(
        "session-rewrite", redis_host="127.0.0.1", redis_port=redis_port
    )
    assert result is None


def test_read_focus_returns_parsed_json_when_present(redis_port, flush_redis):
    client = redis_lib.Redis(host="127.0.0.1", port=redis_port, decode_responses=True)
    client.set(
        "lupin:v1:focus:session-rewrite",
        json.dumps({"machine": "mac-studio", "pinned": False}),
    )

    result = quest.read_focus(
        "session-rewrite", redis_host="127.0.0.1", redis_port=redis_port
    )
    assert result == {"machine": "mac-studio", "pinned": False}


def test_read_focus_unreachable_redis_reports_as_no_focus(closed_port):
    result = quest.read_focus(
        "session-rewrite", redis_host="127.0.0.1", redis_port=closed_port
    )
    assert result is None


# --------------------------------------------------------------------------
# render_list: text format for `lupin quest`
# --------------------------------------------------------------------------


def test_render_list_shows_focus_and_no_focus():
    quests = [
        {"name": "session-rewrite", "doneCount": 2, "total": 5},
        {"name": "billing-v2", "doneCount": 0, "total": 4},
    ]
    focuses = {"session-rewrite": {"machine": "mac-studio"}, "billing-v2": None}

    text = quest.render_list(quests, focuses)

    lines = text.splitlines()
    assert lines[0] == "session-rewrite   2/5 done   5 tasks, 3 open   focus: mac-studio"
    assert lines[1] == "billing-v2        0/4 done   4 tasks, 4 open   no focus"


def test_render_list_with_no_quests_is_a_plain_message():
    assert quest.render_list([], {}) == "No quests found."


# --------------------------------------------------------------------------
# status_to_json: dependency order within one quest
# --------------------------------------------------------------------------


def test_status_orders_tasks_by_dependency_even_when_input_order_does_not():
    quest_data = {
        "name": "session-rewrite",
        "repo": "repo",
        "number": 10,
        "doneCount": 0,
        "total": 3,
        "tasks": [
            {"number": 431, "title": "rate limits", "done": False},
            {"number": 422, "title": "split store", "done": False},
            {"number": 418, "title": "retry backoff", "done": False},
        ],
    }
    dag = {
        "repos": {
            "repo": [
                {
                    "number": 431,
                    "blockedBy": [
                        {"repo": "repo", "number": 422},
                        {"repo": "repo", "number": 418},
                    ],
                    "blocking": [],
                },
                {"number": 422, "blockedBy": [], "blocking": [{"repo": "repo", "number": 431}]},
                {"number": 418, "blockedBy": [], "blocking": [{"repo": "repo", "number": 431}]},
            ]
        }
    }

    data = quest.status_to_json(quest_data, dag, None)

    assert [task["number"] for task in data["tasks"]] == [422, 418, 431]
    assert data["tasks"][2]["status"] == "waits on #418"


def test_status_marks_done_and_ready_tasks():
    quest_data = {
        "name": "session-rewrite",
        "repo": "repo",
        "number": 10,
        "doneCount": 1,
        "total": 2,
        "tasks": [
            {"number": 418, "title": "retry backoff", "done": True},
            {"number": 440, "title": "docs", "done": False},
        ],
    }
    dag = {"repos": {"repo": []}}

    data = quest.status_to_json(quest_data, dag, {"machine": "mac-studio"})

    statuses = {task["number"]: task["status"] for task in data["tasks"]}
    assert statuses == {418: "done", 440: "ready"}
    text = quest.render_status(data)
    assert text.splitlines()[0] == "session-rewrite · focus: mac-studio · 1 of 2 done"
    assert "#418 ✓ done" in text
    assert "#440 ● ready" in text


# --------------------------------------------------------------------------
# CLI wiring
# --------------------------------------------------------------------------


def test_cli_quest_lists_quests_with_real_redis_focus(redis_port, flush_redis, capsys, monkeypatch):
    # This sandbox sets LUPIN_REDIS_USERNAME (and _HOST/_PORT) to point at a
    # real Redis with ACL auth -- clear them so the CLI's env-var defaults
    # don't try to AUTH against this test's plain local redis-server.
    for name in ("LUPIN_REDIS_HOST", "LUPIN_REDIS_PORT", "LUPIN_REDIS_USERNAME", "LUPIN_REDIS_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    client = redis_lib.Redis(host="127.0.0.1", port=redis_port, decode_responses=True)
    client.set("lupin:v1:focus:session-rewrite", json.dumps({"machine": "mac-studio"}))
    fake_quests = [
        {
            "name": "session-rewrite", "repo": "repo", "number": 10, "title": "Session rewrite",
            "url": "u", "done": False, "doneCount": 2, "total": 5,
            "tasks": [{"number": n, "title": "t", "done": n < 3} for n in range(1, 6)],
        },
    ]
    monkeypatch.setattr(cli.serve, "enabled_repos", lambda: ["repo"])
    monkeypatch.setattr(cli.quest_mod, "load_quests", lambda repos, **kw: (fake_quests, []))

    code = cli.main(
        ["quest", "--redis-host", "127.0.0.1", "--redis-port", str(redis_port)]
    )
    captured = capsys.readouterr()

    assert code == 0
    assert "session-rewrite   2/5 done   5 tasks, 3 open   focus: mac-studio" in captured.out


def test_cli_quest_with_no_quests_anywhere_prints_empty_message(monkeypatch, capsys):
    monkeypatch.setattr(cli.serve, "enabled_repos", lambda: [])
    monkeypatch.setattr(cli.quest_mod, "load_quests", lambda repos, **kw: ([], []))

    code = cli.main(["quest"])
    captured = capsys.readouterr()

    assert code == 0
    assert captured.out.strip() == "No quests found."


def test_cli_quest_status_for_one_id_shows_dependency_order(monkeypatch, capsys):
    fake_quests = [
        {
            "name": "session-rewrite", "repo": "repo", "number": 10, "title": "Session rewrite",
            "url": "u", "done": False, "doneCount": 1, "total": 2,
            "tasks": [
                {"number": 431, "title": "rate limits", "done": False},
                {"number": 418, "title": "retry backoff", "done": True},
            ],
        },
    ]
    dag = {
        "repos": {
            "repo": [
                {"number": 431, "blockedBy": [{"repo": "repo", "number": 418}], "blocking": []},
                {"number": 418, "blockedBy": [], "blocking": [{"repo": "repo", "number": 431}]},
            ]
        }
    }
    monkeypatch.setattr(cli.serve, "enabled_repos", lambda: ["repo"])
    monkeypatch.setattr(cli.quest_mod, "load_quests", lambda repos, **kw: (fake_quests, []))
    monkeypatch.setattr(cli.roadmap, "cached_dependency_dag", lambda repos: dag)
    monkeypatch.setattr(cli.quest_mod, "read_focus", lambda name, **kw: None)

    code = cli.main(["quest", "status", "session-rewrite"])
    captured = capsys.readouterr()

    assert code == 0
    assert "session-rewrite · no focus · 1 of 2 done" in captured.out
    assert "#418 ✓ done   #431 ● ready" in captured.out


def test_cli_quest_status_unknown_id_errors(monkeypatch, capsys):
    monkeypatch.setattr(cli.serve, "enabled_repos", lambda: ["repo"])
    monkeypatch.setattr(cli.quest_mod, "load_quests", lambda repos, **kw: ([], []))

    code = cli.main(["quest", "status", "nope"])
    captured = capsys.readouterr()

    assert code == 1
    assert "no quest matches" in captured.err


# --------------------------------------------------------------------------
# ready_tasks / _first_blocker: quest focus's readiness check (issue #12)
# --------------------------------------------------------------------------


def _quest(name, tasks, repo="repo", number=10):
    return {"name": name, "repo": repo, "number": number, "tasks": tasks}


def test_ready_tasks_excludes_done_and_blocked_keeps_order():
    quest_data = _quest(
        "session-rewrite",
        [
            {"number": 431, "title": "rate limits", "done": False},
            {"number": 422, "title": "split store", "done": False},
            {"number": 418, "title": "retry backoff", "done": True},
        ],
    )
    dag = {
        "repos": {
            "repo": [
                {"number": 431, "blockedBy": [{"repo": "repo", "number": 422}], "blocking": []},
                {"number": 422, "blockedBy": [], "blocking": [{"repo": "repo", "number": 431}]},
                {"number": 418, "blockedBy": [], "blocking": []},
            ]
        }
    }

    ready = quest.ready_tasks(quest_data, dag)

    assert [task["number"] for task in ready] == [422]


def test_ready_tasks_empty_when_everything_blocked_or_done():
    quest_data = _quest(
        "billing-v2",
        [
            {"number": 470, "title": "migrate billing", "done": False},
            {"number": 480, "title": "ship billing", "done": False},
        ],
    )
    dag = {
        "repos": {
            "repo": [
                {"number": 470, "blockedBy": [{"repo": "repo", "number": 480}], "blocking": []},
                {"number": 480, "blockedBy": [{"repo": "repo", "number": 470}], "blocking": []},
            ]
        }
    }

    assert quest.ready_tasks(quest_data, dag) == []


def test_ready_tasks_excludes_a_task_blocked_from_outside_the_quest():
    # #470 is not one of billing-v2's own tasks -- `_blockers_within_quest`
    # (quest status's per-task label) would drop this link and call #480
    # "ready". `ready_tasks` must not: it has no state for #470, so it
    # treats #480 as still blocked, same as `lupin-ctl-copy.md`'s own
    # "#470 is blocking" example.
    quest_data = _quest("billing-v2", [{"number": 480, "title": "ship billing", "done": False}])
    dag = {"repos": {"repo": [{"number": 480, "blockedBy": [{"repo": "repo", "number": 470}], "blocking": []}]}}

    assert quest.ready_tasks(quest_data, dag) == []
    assert quest._first_blocker(quest_data, dag) == 470


def test_first_blocker_none_when_every_task_done():
    quest_data = _quest(
        "session-rewrite",
        [{"number": 418, "title": "retry backoff", "done": True}],
    )
    dag = {"repos": {"repo": [{"number": 418, "blockedBy": [], "blocking": []}]}}

    assert quest._first_blocker(quest_data, dag) is None


# --------------------------------------------------------------------------
# pick_focus_machine: auto-pick by free slots (issue #12)
# --------------------------------------------------------------------------


def _machine(name, *, state="online", used=0, max_=2, heartbeat=None):
    return {
        "name": name,
        "state": state,
        "heartbeat": heartbeat or machines_mod._now_iso(),
        "slots": {"bmo": {"used": used, "max": max_}},
    }


def test_pick_focus_machine_prefers_most_free_slots():
    records = [
        _machine("mac-studio", used=2, max_=4),  # 2 free
        _machine("mini-2", used=0, max_=4),  # 4 free
    ]
    assert quest.pick_focus_machine(records) == "mini-2"


def test_pick_focus_machine_ignores_draining_and_offline():
    records = [
        _machine("jesus", state="draining", used=0, max_=4),
        _machine("mac-studio", used=2, max_=4),
    ]
    assert quest.pick_focus_machine(records) == "mac-studio"


def test_pick_focus_machine_breaks_ties_on_heartbeat():
    stale = (datetime.now(timezone.utc) - timedelta(seconds=90)).strftime("%Y-%m-%dT%H:%M:%SZ")
    records = [
        _machine("staler", used=1, max_=2, heartbeat=stale),
        _machine("fresher", used=1, max_=2),
    ]
    assert quest.pick_focus_machine(records) == "fresher"


def test_pick_focus_machine_returns_none_with_no_online_machines():
    records = [_machine("jesus", state="draining")]
    assert quest.pick_focus_machine(records) is None


# --------------------------------------------------------------------------
# write_focus / delete_focus / _read_focus_strict: real redis-server
# --------------------------------------------------------------------------


def _kw(redis_port):
    return {"redis_host": "127.0.0.1", "redis_port": redis_port}


def test_write_focus_then_read_focus_round_trips(redis_port, flush_redis):
    quest.write_focus("session-rewrite", "mac-studio", pinned=True, **_kw(redis_port))

    result = quest.read_focus("session-rewrite", **_kw(redis_port))

    assert result["machine"] == "mac-studio"
    assert result["pinned"] is True
    assert result["release_when"] is None
    assert "since" in result


def test_delete_focus_returns_false_when_absent(redis_port, flush_redis):
    assert quest.delete_focus("nope", **_kw(redis_port)) is False


def test_delete_focus_returns_true_and_removes_key(redis_port, flush_redis):
    quest.write_focus("session-rewrite", "mac-studio", pinned=False, **_kw(redis_port))

    assert quest.delete_focus("session-rewrite", **_kw(redis_port)) is True
    assert quest.read_focus("session-rewrite", **_kw(redis_port)) is None


def test_read_focus_strict_raises_on_unreachable_redis(closed_port):
    with pytest.raises(quest.CoordinatorUnreachable):
        quest._read_focus_strict("session-rewrite", redis_host="127.0.0.1", redis_port=closed_port)


# --------------------------------------------------------------------------
# focus() / release(): orchestration (issue #12)
# --------------------------------------------------------------------------


_SESSION_REWRITE = _quest(
    "session-rewrite",
    [
        {"number": 418, "title": "retry backoff", "done": False},
        {"number": 431, "title": "rate limits", "done": False},
    ],
)
_SESSION_REWRITE_DAG = {
    "repos": {"repo": [{"number": 418, "blockedBy": [], "blocking": []}, {"number": 431, "blockedBy": [], "blocking": []}]}
}


@pytest.fixture
def quests_fixture(monkeypatch):
    monkeypatch.setattr(quest, "load_quests", lambda repos, **kw: ([dict(_SESSION_REWRITE, tasks=list(_SESSION_REWRITE["tasks"]))], []))
    monkeypatch.setattr(roadmap, "cached_dependency_dag", lambda repos, **kw: _SESSION_REWRITE_DAG)


def test_focus_raises_quest_not_found(quests_fixture, redis_port, flush_redis):
    with pytest.raises(quest.QuestNotFound):
        quest.focus("nope", _kw(redis_port), ["repo"])


def test_focus_raises_no_ready_tasks(monkeypatch, redis_port, flush_redis):
    blocked = _quest("billing-v2", [{"number": 480, "title": "ship billing", "done": False}])
    blocked_dag = {"repos": {"repo": [{"number": 480, "blockedBy": [{"repo": "repo", "number": 470}], "blocking": []}]}}
    monkeypatch.setattr(quest, "load_quests", lambda repos, **kw: ([blocked], []))
    monkeypatch.setattr(roadmap, "cached_dependency_dag", lambda repos, **kw: blocked_dag)

    with pytest.raises(quest.NoReadyTasks) as exc_info:
        quest.focus("billing-v2", _kw(redis_port), ["repo"])
    assert "#470 is blocking" in str(exc_info.value)


def test_focus_raises_machine_not_found(quests_fixture, monkeypatch, redis_port, flush_redis):
    monkeypatch.setattr(machines_mod, "machines", lambda connection: [])
    with pytest.raises(quest.MachineNotFound):
        quest.focus("session-rewrite", _kw(redis_port), ["repo"], machine="ghost")


def test_focus_raises_machine_draining(quests_fixture, monkeypatch, redis_port, flush_redis):
    monkeypatch.setattr(machines_mod, "machines", lambda connection: [_machine("jesus", state="draining")])
    with pytest.raises(quest.MachineDraining):
        quest.focus("session-rewrite", _kw(redis_port), ["repo"], machine="jesus")


def test_focus_raises_no_machine_available(quests_fixture, monkeypatch, redis_port, flush_redis):
    monkeypatch.setattr(machines_mod, "machines", lambda connection: [_machine("jesus", state="draining")])
    with pytest.raises(quest.NoMachineAvailable):
        quest.focus("session-rewrite", _kw(redis_port), ["repo"])


def test_focus_auto_picks_most_free_slots_and_writes_record(quests_fixture, monkeypatch, redis_port, flush_redis):
    monkeypatch.setattr(
        machines_mod,
        "machines",
        lambda connection: [_machine("mac-studio", used=3, max_=4), _machine("mini-2", used=0, max_=4)],
    )

    result = quest.focus("session-rewrite", _kw(redis_port), ["repo"], pin=True)

    assert result == {"quest": "session-rewrite", "machine": "mini-2", "ready_count": 2}
    stored = quest.read_focus("session-rewrite", **_kw(redis_port))
    assert stored["machine"] == "mini-2"
    assert stored["pinned"] is True


def test_focus_explicit_machine_overrides_auto_pick(quests_fixture, monkeypatch, redis_port, flush_redis):
    monkeypatch.setattr(
        machines_mod,
        "machines",
        lambda connection: [_machine("mac-studio", used=3, max_=4), _machine("mini-2", used=0, max_=4)],
    )

    result = quest.focus("session-rewrite", _kw(redis_port), ["repo"], machine="mac-studio")

    assert result["machine"] == "mac-studio"


def test_focus_raises_coordinator_unreachable(quests_fixture, monkeypatch, closed_port):
    monkeypatch.setattr(machines_mod, "machines", lambda connection: (_ for _ in ()).throw(machines_mod.CoordinatorUnreachable("machine registry")))
    with pytest.raises(machines_mod.CoordinatorUnreachable):
        quest.focus("session-rewrite", {"redis_host": "127.0.0.1", "redis_port": closed_port}, ["repo"])


def test_release_raises_quest_not_found(quests_fixture, redis_port, flush_redis):
    with pytest.raises(quest.QuestNotFound):
        quest.release("nope", _kw(redis_port), ["repo"])


def test_release_raises_no_focus(quests_fixture, redis_port, flush_redis):
    with pytest.raises(quest.NoFocus):
        quest.release("session-rewrite", _kw(redis_port), ["repo"])


def test_release_deletes_and_returns_machine(quests_fixture, redis_port, flush_redis):
    quest.write_focus("session-rewrite", "mac-studio", pinned=False, **_kw(redis_port))

    result = quest.release("session-rewrite", _kw(redis_port), ["repo"])

    assert result == {"quest": "session-rewrite", "machine": "mac-studio"}
    assert quest.read_focus("session-rewrite", **_kw(redis_port)) is None


def test_release_raises_coordinator_unreachable(quests_fixture, closed_port):
    with pytest.raises(quest.CoordinatorUnreachable):
        quest.release("session-rewrite", {"redis_host": "127.0.0.1", "redis_port": closed_port}, ["repo"])


# --------------------------------------------------------------------------
# CLI wiring: `lupin quest focus` / `lupin quest release`
# --------------------------------------------------------------------------


def test_cli_quest_focus_prints_exact_copy_text(quests_fixture, monkeypatch, redis_port, flush_redis, capsys):
    monkeypatch.setattr(cli.serve, "enabled_repos", lambda: ["repo"])
    monkeypatch.setattr(
        cli.machines, "machines", lambda connection: [_machine("mac-studio", used=0, max_=4)]
    )

    code = cli.main(
        ["quest", "focus", "session-rewrite", "--redis-host", "127.0.0.1", "--redis-port", str(redis_port)]
    )
    captured = capsys.readouterr()

    assert code == 0
    assert captured.out.strip() == (
        "focused session-rewrite on mac-studio · 2 ready tasks will route there in order"
    )


def test_cli_quest_focus_draining_machine_error(quests_fixture, monkeypatch, redis_port, flush_redis, capsys):
    monkeypatch.setattr(cli.serve, "enabled_repos", lambda: ["repo"])
    monkeypatch.setattr(
        cli.machines, "machines", lambda connection: [_machine("mac-studio", state="draining")]
    )

    code = cli.main(
        [
            "quest", "focus", "session-rewrite", "--machine", "mac-studio",
            "--redis-host", "127.0.0.1", "--redis-port", str(redis_port),
        ]
    )
    captured = capsys.readouterr()

    assert code == 1
    assert captured.err.strip() == (
        "error: mac-studio is draining and cannot take a focus. Pick another machine."
    )


def test_cli_quest_focus_no_ready_tasks_error(monkeypatch, redis_port, flush_redis, capsys):
    blocked = _quest("billing-v2", [{"number": 480, "title": "ship billing", "done": False}])
    blocked_dag = {"repos": {"repo": [{"number": 480, "blockedBy": [{"repo": "repo", "number": 470}], "blocking": []}]}}
    monkeypatch.setattr(cli.serve, "enabled_repos", lambda: ["repo"])
    monkeypatch.setattr(cli.quest_mod, "load_quests", lambda repos, **kw: ([blocked], []))
    monkeypatch.setattr(cli.roadmap, "cached_dependency_dag", lambda repos, **kw: blocked_dag)

    code = cli.main(
        ["quest", "focus", "billing-v2", "--redis-host", "127.0.0.1", "--redis-port", str(redis_port)]
    )
    captured = capsys.readouterr()

    assert code == 1
    assert captured.err.strip() == (
        "error: billing-v2 has no ready tasks to focus on. #470 is blocking; see lupin roadmap --dag"
    )


def test_cli_quest_release_prints_exact_copy_text(quests_fixture, redis_port, flush_redis, capsys):
    quest.write_focus("session-rewrite", "mac-studio", pinned=False, **_kw(redis_port))

    code = cli.main(
        ["quest", "release", "session-rewrite", "--redis-host", "127.0.0.1", "--redis-port", str(redis_port)]
    )
    captured = capsys.readouterr()

    assert code == 0
    assert captured.out.strip() == "released session-rewrite · mac-studio returns to normal routing"


def test_cli_quest_release_no_focus_error(quests_fixture, redis_port, flush_redis, capsys):
    code = cli.main(
        ["quest", "release", "session-rewrite", "--redis-host", "127.0.0.1", "--redis-port", str(redis_port)]
    )
    captured = capsys.readouterr()

    assert code == 1
    assert captured.err.strip() == "error: session-rewrite has no focus to release"


def test_cli_quest_focus_unreachable_redis_exits_3(quests_fixture, monkeypatch, closed_port, capsys):
    monkeypatch.setattr(cli.serve, "enabled_repos", lambda: ["repo"])
    monkeypatch.setattr(
        cli.machines, "machines",
        lambda connection: (_ for _ in ()).throw(cli.machines.CoordinatorUnreachable("machine registry")),
    )

    code = cli.main(
        [
            "quest", "focus", "session-rewrite",
            "--redis-host", "127.0.0.1", "--redis-port", str(closed_port),
        ]
    )
    captured = capsys.readouterr()

    assert code == 3
    assert "cannot reach" in captured.err
