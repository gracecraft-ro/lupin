"""Tests for quest reporting (issue #11).

Redis-backed tests use the real `redis-server` fixtures in conftest.py
(`redis_port`/`flush_redis`/`closed_port`), same as test_slots_redis.py --
not a mock, per this project's existing test convention for the `redis`
backend.
"""

from __future__ import annotations

import json
from unittest import mock

import pytest
import redis as redis_lib

from lupin import cli, claims, machines, place, quest, roadmap


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
# quest start / stop (issue #13)
# --------------------------------------------------------------------------


_AMBIENT_ENV_VARS = (
    "LUPIN_REDIS_HOST", "LUPIN_REDIS_PORT", "LUPIN_REDIS_USERNAME",
    "LUPIN_REDIS_PASSWORD", "LUPIN_FLEET_CONFIG", "LUPIN_BACKEND",
)


@pytest.fixture
def clean_fleet_env(monkeypatch):
    """This sandbox's shell sets `$LUPIN_REDIS_*` for the real shared fleet
    Redis -- clear them so the CLI's env-var defaults don't leak into these
    tests' throwaway `redis-server` fixture.
    """
    for name in _AMBIENT_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _kw(redis_port):
    return {"redis_host": "127.0.0.1", "redis_port": redis_port}


def _issue_json(number, state="OPEN"):
    return {"number": number, "state": state}


def _fake_locate(table):
    """Stand-in for `quest._locate_issue`: `table` maps issue number ->
    `(repo, "owner/repo", issue_json)`, or omits a number to mean "not
    found in any enabled repo".
    """

    def _locate(number, repos, code_dir):
        return table.get(number)

    return _locate


def _write_machine_record(redis_port, name, state="online"):
    record = {
        "version": "0.0.0+dev", "heartbeat": machines._now_iso(), "state": state,
        "slots": {}, "providers": [], "quota": {},
    }
    client = redis_lib.Redis(host="127.0.0.1", port=redis_port, decode_responses=True)
    client.set(f"{machines.PREFIX}machine:{name}", json.dumps(record))


# --- _resolve_issues: existence + closed ---


def test_resolve_issues_raises_not_found_when_no_repo_has_it(monkeypatch):
    monkeypatch.setattr(quest, "_locate_issue", _fake_locate({}))
    with pytest.raises(quest.IssueNotFound, match=r"#31 does not exist in any enabled repo\."):
        quest._resolve_issues([31], ["repo-a"], "/code")


def test_resolve_issues_raises_closed(monkeypatch):
    monkeypatch.setattr(
        quest, "_locate_issue",
        _fake_locate({25: ("repo-a", "acme/repo-a", _issue_json(25, "CLOSED"))}),
    )
    with pytest.raises(quest.IssueClosed, match=r"#25 is closed\. Remove --issue 25\."):
        quest._resolve_issues([25], ["repo-a"], "/code")


def test_resolve_issues_returns_targets_for_open_issues(monkeypatch):
    monkeypatch.setattr(
        quest, "_locate_issue",
        _fake_locate({
            23: ("repo-a", "acme/repo-a", _issue_json(23)),
            24: ("repo-b", "acme/repo-b", _issue_json(24)),
        }),
    )
    resolved = quest._resolve_issues([23, 24], ["repo-a", "repo-b"], "/code")
    assert resolved == {
        23: ("repo-a", "acme/repo-a#23"),
        24: ("repo-b", "acme/repo-b#24"),
    }


def test_locate_issue_tries_each_repo_in_turn():
    responses = [
        ({"owner": {"login": "acme"}, "name": "repo-a"}, None),
        (None, "gh: no such issue"),
        ({"owner": {"login": "acme"}, "name": "repo-b"}, None),
        (_issue_json(24), None),
    ]
    with mock.patch.object(roadmap, "_run_json", side_effect=responses):
        found = quest._locate_issue(24, ["repo-a", "repo-b"], "/code")
    assert found == ("repo-b", "acme/repo-b", _issue_json(24))


def test_locate_issue_returns_none_when_no_repo_has_it():
    responses = [
        ({"owner": {"login": "acme"}, "name": "repo-a"}, None),
        (None, "gh: no such issue"),
    ]
    with mock.patch.object(roadmap, "_run_json", side_effect=responses):
        found = quest._locate_issue(31, ["repo-a"], "/code")
    assert found is None


# --- _check_claims: already-claimed detection (real redis) ---


def test_check_claims_raises_issue_claimed(redis_port, flush_redis):
    claims.claim("acme/billing-core#1", "billing-core#1", **_kw(redis_port))
    resolved = {24: ("billing-core", "acme/billing-core#24")}
    # The held claim is on a *different* issue in the same repo than the
    # one we're checking -- claims_for still needs to match this issue's
    # own target, not just the repo.
    claims.claim("acme/billing-core#24", "billing-core#1", **_kw(redis_port))

    with pytest.raises(
        quest.IssueClaimed,
        match=r"#24 is claimed by billing-core#1\. Wait for it or stop that loop\.",
    ):
        quest._check_claims(resolved, [24], _kw(redis_port))


def test_check_claims_passes_when_nothing_claimed(redis_port, flush_redis):
    resolved = {23: ("repo-a", "acme/repo-a#23")}
    quest._check_claims(resolved, [23], _kw(redis_port))  # no raise


# --- _check_blocked_by / _dependency_order ---


def test_check_blocked_by_raises_when_blocker_outside_quest(monkeypatch):
    dag = {
        "repos": {
            "repo-a": [
                {"number": 23, "blockedBy": [{"repo": "repo-a", "number": 19}], "blocking": []},
            ]
        }
    }
    monkeypatch.setattr(quest.roadmap, "cached_dependency_dag", lambda repos, code_dir: dag)
    resolved = {23: ("repo-a", "acme/repo-a#23")}

    with pytest.raises(
        quest.IssueBlocked,
        match=r"#23 is blocked by #19, which is not in this quest\. Add --issue 19 or wait for it\.",
    ):
        quest._check_blocked_by(resolved, [23], ["repo-a"], "/code")


def test_check_blocked_by_passes_when_blocker_is_in_the_quest(monkeypatch):
    dag = {
        "repos": {
            "repo-a": [
                {"number": 23, "blockedBy": [{"repo": "repo-a", "number": 19}], "blocking": []},
                {"number": 19, "blockedBy": [], "blocking": [{"repo": "repo-a", "number": 23}]},
            ]
        }
    }
    monkeypatch.setattr(quest.roadmap, "cached_dependency_dag", lambda repos, code_dir: dag)
    resolved = {23: ("repo-a", "acme/repo-a#23"), 19: ("repo-a", "acme/repo-a#19")}

    returned_dag = quest._check_blocked_by(resolved, [19, 23], ["repo-a"], "/code")
    assert returned_dag is dag


def test_dependency_order_across_repos():
    resolved = {
        23: ("repo-a", "acme/repo-a#23"),
        24: ("repo-b", "acme/repo-b#24"),
        25: ("repo-a", "acme/repo-a#25"),
    }
    dag = {
        "repos": {
            "repo-a": [
                {"number": 23, "blockedBy": [], "blocking": [{"repo": "repo-b", "number": 24}]},
                {"number": 25, "blockedBy": [{"repo": "repo-b", "number": 24}], "blocking": []},
            ],
            "repo-b": [
                {
                    "number": 24,
                    "blockedBy": [{"repo": "repo-a", "number": 23}],
                    "blocking": [{"repo": "repo-a", "number": 25}],
                },
            ],
        }
    }
    order, waits_on = quest._dependency_order([23, 24, 25], resolved, dag)
    assert order == [23, 24, 25]
    assert waits_on == [
        {"number": 24, "blocker": 23},
        {"number": 25, "blocker": 24},
    ]


# --- _resolve_machine ---


def test_resolve_machine_explicit_online(redis_port, flush_redis):
    _write_machine_record(redis_port, "mac-studio", state="online")
    assert quest._resolve_machine("mac-studio", [23], _kw(redis_port)) == "mac-studio"


def test_resolve_machine_explicit_draining_raises(redis_port, flush_redis):
    _write_machine_record(redis_port, "mac-studio", state="draining")
    with pytest.raises(
        quest.MachineDraining,
        match=r"mac-studio is draining and takes no new work\. Pick another machine\.",
    ):
        quest._resolve_machine("mac-studio", [23], _kw(redis_port))


def test_resolve_machine_explicit_unknown_machine_is_allowed(redis_port, flush_redis):
    # No record at all (never joined, or heartbeat stale past expiry) isn't
    # one of the five documented failures -- only an explicit "draining"
    # state blocks an explicit --machine.
    assert quest._resolve_machine("brand-new", [23], _kw(redis_port)) == "brand-new"


def test_resolve_machine_default_uses_place_pick(monkeypatch):
    monkeypatch.setattr(
        quest.place_mod, "place", lambda task, connection: {"pick": "mini-2"}
    )
    assert quest._resolve_machine(None, [23], {}) == "mini-2"


def test_resolve_machine_default_raises_when_no_pick(monkeypatch):
    monkeypatch.setattr(quest.place_mod, "place", lambda task, connection: {"pick": None})
    with pytest.raises(quest.QuestError):
        quest._resolve_machine(None, [23], {})


# --- _claim_all: the partial-claim-failure rollback (real, pre-existing claim) ---


def test_claim_all_rolls_back_when_a_later_issue_is_already_claimed(redis_port, flush_redis):
    # #24 is pre-claimed by a real, independent holder before our quest
    # ever starts -- not a mock -- so claiming it for real fails partway
    # through the list.
    claims.claim("acme/repo-b#24", "someone-else", **_kw(redis_port))

    resolved = {
        23: ("repo-a", "acme/repo-a#23"),
        24: ("repo-b", "acme/repo-b#24"),
        25: ("repo-a", "acme/repo-a#25"),
    }
    with pytest.raises(quest.IssueClaimed, match=r"#24 is claimed by someone-else\."):
        quest._claim_all(resolved, [23, 24, 25], "quest:q1", _kw(redis_port))

    # #23 was claimed by this attempt before the failure -- it must be
    # released, not left sitting under "quest:q1".
    remaining = claims.claims_for(["acme/repo-a", "acme/repo-b"], **_kw(redis_port))
    assert "acme/repo-a#23" not in remaining
    # #25 was never reached.
    assert "acme/repo-a#25" not in remaining
    # #24 still belongs to its real, pre-existing holder -- our rollback
    # must not touch a claim it doesn't own.
    assert remaining["acme/repo-b#24"]["session"] == "someone-else"


def test_claim_all_succeeds_claims_every_target(redis_port, flush_redis):
    resolved = {23: ("repo-a", "acme/repo-a#23"), 24: ("repo-b", "acme/repo-b#24")}
    quest._claim_all(resolved, [23, 24], "quest:q1", _kw(redis_port))

    held = claims.claims_for(["acme/repo-a", "acme/repo-b"], **_kw(redis_port))
    assert held["acme/repo-a#23"]["session"] == "quest:q1"
    assert held["acme/repo-b#24"]["session"] == "quest:q1"


# --- quest id minting ---


def test_next_quest_id_increments(redis_port, flush_redis):
    assert quest._next_quest_id(_kw(redis_port)) == "q1"
    assert quest._next_quest_id(_kw(redis_port)) == "q2"


# --- start(): end-to-end, real redis, gh/place/machines boundary mocked ---


def _patch_quest_boundaries(monkeypatch, locate_table, dag, pick="mac-studio"):
    monkeypatch.setattr(quest, "_locate_issue", _fake_locate(locate_table))
    monkeypatch.setattr(quest.roadmap, "cached_dependency_dag", lambda repos, code_dir: dag)
    monkeypatch.setattr(quest.place_mod, "place", lambda task, connection: {"pick": pick})


def test_start_writes_a_quest_record_and_claims_every_issue(redis_port, flush_redis, monkeypatch):
    locate_table = {
        23: ("repo-a", "acme/repo-a", _issue_json(23)),
        24: ("repo-b", "acme/repo-b", _issue_json(24)),
    }
    dag = {
        "repos": {
            "repo-a": [{"number": 23, "blockedBy": [], "blocking": [{"repo": "repo-b", "number": 24}]}],
            "repo-b": [{"number": 24, "blockedBy": [{"repo": "repo-a", "number": 23}], "blocking": []}],
        }
    }
    _patch_quest_boundaries(monkeypatch, locate_table, dag)

    result = quest.start([23, 24], ["repo-a", "repo-b"], connection=_kw(redis_port), note="ship it")

    assert result["id"] == "q1"
    assert result["issues"] == [23, 24]
    assert result["targets"] == ["acme/repo-a#23", "acme/repo-b#24"]
    assert result["order"] == [23, 24]
    assert result["machine"] == "mac-studio"
    assert result["state"] == "running"
    assert result["note"] == "ship it"
    # #24 is blocked by #23 in `dag` above -- the record carries that pair
    # so `render_start` can print the copy doc's "waits on" line.
    assert result["waits_on"] == [{"number": 24, "blocker": 23}]

    stored = quest.read_quest("q1", _kw(redis_port))
    assert stored == {k: v for k, v in result.items() if k != "id"}

    held = claims.claims_for(["acme/repo-a", "acme/repo-b"], **_kw(redis_port))
    assert held["acme/repo-a#23"]["session"] == "quest:q1"
    assert held["acme/repo-b#24"]["session"] == "quest:q1"


def test_render_start_adds_a_waits_on_line_per_in_quest_blocker():
    result = {
        "id": "q1", "issues": [23, 24], "machine": "mac-studio",
        "order": [23, 24], "waits_on": [{"number": 24, "blocker": 23}],
    }
    assert quest.render_start(result) == (
        "quest q1 started on mac-studio · #23 #24 claimed · order: #23, #24\n"
        "  #24 waits on #23 (blocked by)"
    )


def test_render_start_omits_the_waits_on_line_when_nothing_blocks():
    result = {"id": "q1", "issues": [23], "machine": "mac-studio", "order": [23]}
    assert quest.render_start(result) == "quest q1 started on mac-studio · #23 claimed · order: #23"


def test_start_dedupes_a_repeated_issue(redis_port, flush_redis, monkeypatch):
    locate_table = {23: ("repo-a", "acme/repo-a", _issue_json(23))}
    dag = {"repos": {"repo-a": [{"number": 23, "blockedBy": [], "blocking": []}]}}
    _patch_quest_boundaries(monkeypatch, locate_table, dag)

    result = quest.start([23, 23], ["repo-a"], connection=_kw(redis_port))
    assert result["issues"] == [23]


def test_start_raises_not_found_and_claims_nothing(redis_port, flush_redis, monkeypatch):
    _patch_quest_boundaries(monkeypatch, {}, {"repos": {}})
    with pytest.raises(quest.IssueNotFound):
        quest.start([31], ["repo-a"], connection=_kw(redis_port))
    assert claims.claims_for(["repo-a"], **_kw(redis_port)) == {}


def test_start_raises_machine_draining_before_claiming_anything(redis_port, flush_redis, monkeypatch):
    locate_table = {23: ("repo-a", "acme/repo-a", _issue_json(23))}
    dag = {"repos": {"repo-a": [{"number": 23, "blockedBy": [], "blocking": []}]}}
    _patch_quest_boundaries(monkeypatch, locate_table, dag)
    _write_machine_record(redis_port, "mac-studio", state="draining")

    with pytest.raises(quest.MachineDraining):
        quest.start([23], ["repo-a"], connection=_kw(redis_port), machine="mac-studio")

    assert claims.claims_for(["acme/repo-a"], **_kw(redis_port)) == {}


# --- stop() ---


def test_stop_releases_still_held_claims_and_deletes_the_record(redis_port, flush_redis, monkeypatch):
    locate_table = {
        23: ("repo-a", "acme/repo-a", _issue_json(23)),
        24: ("repo-b", "acme/repo-b", _issue_json(24)),
        25: ("repo-a", "acme/repo-a", _issue_json(25)),
    }
    dag = {"repos": {"repo-a": [], "repo-b": []}}
    _patch_quest_boundaries(monkeypatch, locate_table, dag)
    result = quest.start([23, 24, 25], ["repo-a", "repo-b"], connection=_kw(redis_port))
    quest_id = result["id"]

    # #23 and #24 finished already (merged/closed) and released their own
    # claims, same as any other claim -- only #25 is still held by stop time.
    claims.release_claim("acme/repo-a#23", f"quest:{quest_id}", **_kw(redis_port))
    claims.release_claim("acme/repo-b#24", f"quest:{quest_id}", **_kw(redis_port))

    message = quest.stop(quest_id, connection=_kw(redis_port))

    assert message == f"quest {quest_id} stopped · #25 released to the queue, branch kept"
    assert quest.read_quest(quest_id, _kw(redis_port)) is None
    assert claims.claims_for(["acme/repo-a", "acme/repo-b"], **_kw(redis_port)) == {}


def test_stop_with_nothing_left_to_release(redis_port, flush_redis, monkeypatch):
    locate_table = {23: ("repo-a", "acme/repo-a", _issue_json(23))}
    dag = {"repos": {"repo-a": []}}
    _patch_quest_boundaries(monkeypatch, locate_table, dag)
    result = quest.start([23], ["repo-a"], connection=_kw(redis_port))
    claims.release_claim("acme/repo-a#23", f"quest:{result['id']}", **_kw(redis_port))

    message = quest.stop(result["id"], connection=_kw(redis_port))
    assert message == f"quest {result['id']} stopped · nothing left to release, branch kept"


def test_stop_raises_quest_not_found(redis_port, flush_redis):
    with pytest.raises(quest.QuestNotFound, match=r"no quest matches 'q999'"):
        quest.stop("q999", connection=_kw(redis_port))


# --- CLI wiring ---


def test_cli_quest_start_needs_an_issue(capsys):
    code = cli.main(["quest", "start"])
    captured = capsys.readouterr()
    assert code == 1
    assert "needs at least one --issue" in captured.err


def test_cli_quest_start_prints_error_and_exit_2_when_claimed(
    redis_port, flush_redis, clean_fleet_env, capsys, monkeypatch
):
    monkeypatch.setattr(cli.serve, "enabled_repos", lambda: ["repo-a"])
    monkeypatch.setattr(
        cli.quest_mod, "_locate_issue",
        _fake_locate({24: ("repo-a", "acme/repo-a", _issue_json(24))}),
    )
    claims.claim("acme/repo-a#24", "billing-core#1", **_kw(redis_port))

    code = cli.main([
        "quest", "start", "--issue", "24",
        "--redis-host", "127.0.0.1", "--redis-port", str(redis_port),
    ])
    captured = capsys.readouterr()

    assert code == 2
    assert captured.err.strip() == "error: #24 is claimed by billing-core#1. Wait for it or stop that loop."


def test_cli_quest_start_prints_error_and_exit_1_when_closed(
    redis_port, flush_redis, clean_fleet_env, capsys, monkeypatch
):
    monkeypatch.setattr(cli.serve, "enabled_repos", lambda: ["repo-a"])
    monkeypatch.setattr(
        cli.quest_mod, "_locate_issue",
        _fake_locate({25: ("repo-a", "acme/repo-a", _issue_json(25, "CLOSED"))}),
    )

    code = cli.main([
        "quest", "start", "--issue", "25",
        "--redis-host", "127.0.0.1", "--redis-port", str(redis_port),
    ])
    captured = capsys.readouterr()

    assert code == 1
    assert captured.err.strip() == "error: #25 is closed. Remove --issue 25."


def test_cli_quest_start_prints_error_for_missing_issue(
    redis_port, flush_redis, clean_fleet_env, capsys, monkeypatch
):
    monkeypatch.setattr(cli.serve, "enabled_repos", lambda: ["repo-a"])
    monkeypatch.setattr(cli.quest_mod, "_locate_issue", _fake_locate({}))

    code = cli.main([
        "quest", "start", "--issue", "31",
        "--redis-host", "127.0.0.1", "--redis-port", str(redis_port),
    ])
    captured = capsys.readouterr()

    assert code == 1
    assert captured.err.strip() == "error: #31 does not exist in any enabled repo."


def test_cli_quest_start_prints_error_for_blocker_outside_quest(
    redis_port, flush_redis, clean_fleet_env, capsys, monkeypatch
):
    monkeypatch.setattr(cli.serve, "enabled_repos", lambda: ["repo-a"])
    monkeypatch.setattr(
        cli.quest_mod, "_locate_issue",
        _fake_locate({23: ("repo-a", "acme/repo-a", _issue_json(23))}),
    )
    dag = {
        "repos": {
            "repo-a": [{"number": 23, "blockedBy": [{"repo": "repo-a", "number": 19}], "blocking": []}]
        }
    }
    monkeypatch.setattr(cli.quest_mod.roadmap, "cached_dependency_dag", lambda repos, code_dir: dag)

    code = cli.main([
        "quest", "start", "--issue", "23",
        "--redis-host", "127.0.0.1", "--redis-port", str(redis_port),
    ])
    captured = capsys.readouterr()

    assert code == 1
    assert captured.err.strip() == (
        "error: #23 is blocked by #19, which is not in this quest. Add --issue 19 or wait for it."
    )


def test_cli_quest_start_prints_error_for_draining_machine(
    redis_port, flush_redis, clean_fleet_env, capsys, monkeypatch
):
    monkeypatch.setattr(cli.serve, "enabled_repos", lambda: ["repo-a"])
    monkeypatch.setattr(
        cli.quest_mod, "_locate_issue",
        _fake_locate({23: ("repo-a", "acme/repo-a", _issue_json(23))}),
    )
    monkeypatch.setattr(cli.quest_mod.roadmap, "cached_dependency_dag", lambda repos, code_dir: {"repos": {}})
    _write_machine_record(redis_port, "mac-studio", state="draining")

    code = cli.main([
        "quest", "start", "--issue", "23", "--machine", "mac-studio",
        "--redis-host", "127.0.0.1", "--redis-port", str(redis_port),
    ])
    captured = capsys.readouterr()

    assert code == 2
    assert captured.err.strip() == "error: mac-studio is draining and takes no new work. Pick another machine."


def test_cli_quest_start_success_prints_rendered_line(
    redis_port, flush_redis, clean_fleet_env, capsys, monkeypatch
):
    monkeypatch.setattr(cli.serve, "enabled_repos", lambda: ["repo-a"])
    monkeypatch.setattr(
        cli.quest_mod, "_locate_issue",
        _fake_locate({23: ("repo-a", "acme/repo-a", _issue_json(23))}),
    )
    monkeypatch.setattr(cli.quest_mod.roadmap, "cached_dependency_dag", lambda repos, code_dir: {"repos": {}})

    code = cli.main([
        "quest", "start", "--issue", "23", "--machine", "mac-studio",
        "--redis-host", "127.0.0.1", "--redis-port", str(redis_port),
    ])
    captured = capsys.readouterr()

    assert code == 0
    assert captured.out.strip() == "quest q1 started on mac-studio · #23 claimed · order: #23"


def test_cli_quest_start_prints_waits_on_line_for_in_quest_blocker(
    redis_port, flush_redis, clean_fleet_env, capsys, monkeypatch
):
    # Same shape as the copy doc's `quest start` example: #25 is blocked by
    # #23, both in the quest, so the success output gets a second line.
    monkeypatch.setattr(cli.serve, "enabled_repos", lambda: ["repo-a"])
    monkeypatch.setattr(
        cli.quest_mod, "_locate_issue",
        _fake_locate({
            23: ("repo-a", "acme/repo-a", _issue_json(23)),
            25: ("repo-a", "acme/repo-a", _issue_json(25)),
        }),
    )
    dag = {
        "repos": {
            "repo-a": [
                {"number": 23, "blockedBy": [], "blocking": [{"repo": "repo-a", "number": 25}]},
                {"number": 25, "blockedBy": [{"repo": "repo-a", "number": 23}], "blocking": []},
            ]
        }
    }
    monkeypatch.setattr(cli.quest_mod.roadmap, "cached_dependency_dag", lambda repos, code_dir: dag)

    code = cli.main([
        "quest", "start", "--issue", "23", "--issue", "25", "--machine", "mac-studio",
        "--redis-host", "127.0.0.1", "--redis-port", str(redis_port),
    ])
    captured = capsys.readouterr()

    assert code == 0
    assert captured.out.strip() == (
        "quest q1 started on mac-studio · #23 #25 claimed · order: #23, #25\n"
        "  #25 waits on #23 (blocked by)"
    )


def test_cli_quest_start_coordinator_unreachable_exits_3(closed_port, clean_fleet_env, capsys, monkeypatch):
    monkeypatch.setattr(cli.serve, "enabled_repos", lambda: ["repo-a"])
    monkeypatch.setattr(
        cli.quest_mod, "_locate_issue",
        _fake_locate({23: ("repo-a", "acme/repo-a", _issue_json(23))}),
    )

    code = cli.main([
        "quest", "start", "--issue", "23",
        "--redis-host", "127.0.0.1", "--redis-port", str(closed_port),
    ])
    captured = capsys.readouterr()

    assert code == 3
    assert "cannot reach the redis coordinator" in captured.err


def test_cli_quest_stop_success(redis_port, flush_redis, clean_fleet_env, capsys, monkeypatch):
    monkeypatch.setattr(cli.serve, "enabled_repos", lambda: ["repo-a"])
    monkeypatch.setattr(
        cli.quest_mod, "_locate_issue",
        _fake_locate({23: ("repo-a", "acme/repo-a", _issue_json(23))}),
    )
    monkeypatch.setattr(cli.quest_mod.roadmap, "cached_dependency_dag", lambda repos, code_dir: {"repos": {}})
    common = ["--redis-host", "127.0.0.1", "--redis-port", str(redis_port)]
    cli.main(["quest", "start", "--issue", "23", "--machine", "mac-studio", *common])
    capsys.readouterr()

    code = cli.main(["quest", "stop", "q1", *common])
    captured = capsys.readouterr()

    assert code == 0
    assert captured.out.strip() == "quest q1 stopped · #23 released to the queue, branch kept"


def test_cli_quest_stop_unknown_id_errors(redis_port, flush_redis, clean_fleet_env, capsys):
    code = cli.main([
        "quest", "stop", "nope",
        "--redis-host", "127.0.0.1", "--redis-port", str(redis_port),
    ])
    captured = capsys.readouterr()

    assert code == 1
    assert captured.err.strip() == "error: no quest matches 'nope'"


def test_cli_quest_stop_needs_an_id(capsys):
    code = cli.main(["quest", "stop"])
    captured = capsys.readouterr()
    assert code == 1
    assert "needs an id" in captured.err
