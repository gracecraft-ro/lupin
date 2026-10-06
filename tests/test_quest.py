"""Tests for quest reporting (issue #11).

Redis-backed tests use the real `redis-server` fixtures in conftest.py
(`redis_port`/`flush_redis`/`closed_port`), same as test_slots_redis.py --
not a mock, per this project's existing test convention for the `redis`
backend.
"""

from __future__ import annotations

import json
from unittest import mock

import redis as redis_lib

from lupin import cli, quest, roadmap


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
