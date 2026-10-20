"""Tests for `lupin dispatch` (issue #181) and the markdown prefetch."""

from __future__ import annotations

import contextlib
import io
import json
import os

from unittest import mock

from lupin import claims
from lupin import cli
from lupin import dispatch as dispatch_mod
from lupin import loop_runtime
from lupin import review_dispatch


ISSUE = {
    "kind": "issue",
    "body": "Fix the thing.\nSecond line.",
    "comments": [
        {
            "author": {"login": "alice"},
            "createdAt": "2026-01-02T03:04:05Z",
            "body": "First correction\nwith \"quotes\".",
        },
        {
            "author": {"login": "bob"},
            "createdAt": "2026-01-03T04:05:06Z",
            "body": "Second correction.",
        },
    ],
}

PR = {
    "kind": "pr",
    "body": "Add the widget.",
    "comments": [
        {
            "author": {"login": "carol"},
            "createdAt": "2026-02-01T00:00:00Z",
            "body": "A PR comment.",
        }
    ],
    "reviews": [
        {
            "author": {"login": "dave"},
            "state": "CHANGES_REQUESTED",
            "submittedAt": "2026-02-02T00:00:00Z",
            "body": "Please rename it.",
        }
    ],
    "diff_stat": [{"path": "src/a.py", "additions": 3, "deletions": 1}],
}


def _prefetch(data):
    return lambda items, *, repo=None: {items[0]: data}


def test_issue_brief_has_every_comment_with_author_and_time():
    brief = review_dispatch.brief_markdown("o/r#1", ISSUE)
    assert "Fix the thing.\nSecond line." in brief
    assert "### @alice - 2026-01-02T03:04:05Z" in brief
    assert "First correction\nwith \"quotes\"." in brief
    assert "### @bob - 2026-01-03T04:05:06Z" in brief
    assert "Second correction." in brief
    assert "## Comments (2)" in brief
    # Markdown, not JSON: no escaped newline or quote reaches the worker.
    assert "\\n" not in brief
    assert '\\"' not in brief


def test_pr_brief_has_reviews_changed_files_and_comments():
    brief = review_dispatch.brief_markdown("o/r#2", PR)
    assert "## Reviews (1)" in brief
    assert "### @dave - CHANGES_REQUESTED - 2026-02-02T00:00:00Z" in brief
    assert "Please rename it." in brief
    assert "## Changed files" in brief
    assert "- src/a.py (+3 -1)" in brief
    assert "## Comments (1)" in brief
    assert "### @carol - 2026-02-01T00:00:00Z" in brief


def test_dispatch_writes_the_brief_and_starts_the_loop(tmp_path):
    start = mock.Mock(return_value=(True, "started widgets"))
    with mock.patch.object(dispatch_mod.claims, "claim"), mock.patch.object(
        dispatch_mod.review_dispatch, "prefetch", side_effect=_prefetch(ISSUE)
    ), mock.patch.object(dispatch_mod.loop_runtime, "start_loop", start):
        result = dispatch_mod.dispatch("o/r#1", "holder", state_dir=tmp_path)

    assert result["item"] == "o/r#1"
    assert result["kind"] == "issue"
    assert result["claim"] == "holder"
    assert result["started"] is True
    path = result["brief_path"]
    assert path.startswith(str(tmp_path / "notes" / "dispatch-o-r-1-"))
    assert os.stat(path).st_mode & 0o777 == 0o640
    with open(path, encoding="utf-8") as handle:
        brief = handle.read()
    assert "### @alice - 2026-01-02T03:04:05Z" in brief
    assert start.call_args == mock.call("r", note=brief)


def test_malformed_item_exits_1_without_claiming_or_starting():
    claim = mock.Mock()
    start = mock.Mock()
    err = io.StringIO()
    with mock.patch.object(dispatch_mod.claims, "claim", claim), mock.patch.object(
        dispatch_mod.loop_runtime, "start_loop", start
    ), contextlib.redirect_stderr(err):
        rc = cli.main(["dispatch", "not-a-target", "--holder", "holder"])

    assert rc == 1
    assert len(err.getvalue().splitlines()) == 1
    claim.assert_not_called()
    start.assert_not_called()


def test_gh_error_exits_1_and_releases_the_claim(tmp_path):
    release = mock.Mock(return_value=True)
    start = mock.Mock()
    with mock.patch.object(dispatch_mod.claims, "claim"), mock.patch.object(
        dispatch_mod.claims, "release_claim", release
    ), mock.patch.object(
        dispatch_mod.review_dispatch,
        "prefetch",
        side_effect=_prefetch({"error": "not a fetchable issue or PR '5'"}),
    ), mock.patch.object(
        dispatch_mod.loop_runtime, "start_loop", start
    ), mock.patch.dict(
        "os.environ", {"LUPIN_LOOP_STATE_DIR": str(tmp_path)}
    ), contextlib.redirect_stderr(io.StringIO()):
        rc = cli.main(["dispatch", "o/r#5", "--holder", "holder"])

    assert rc == 1
    release.assert_called_once()
    assert release.call_args.args == ("o/r#5", "holder")
    start.assert_not_called()
    assert not list((tmp_path / "notes").glob("dispatch-*"))


def test_busy_claim_exits_2_without_starting(tmp_path):
    start = mock.Mock()
    err = io.StringIO()
    with mock.patch.object(
        dispatch_mod.claims,
        "claim",
        side_effect=claims.ClaimHeld("o/r#1", '{"session": "other"}'),
    ), mock.patch.object(
        dispatch_mod.loop_runtime, "start_loop", start
    ), mock.patch.dict(
        "os.environ", {"LUPIN_LOOP_STATE_DIR": str(tmp_path)}
    ), contextlib.redirect_stderr(err):
        rc = cli.main(["dispatch", "o/r#1", "--holder", "holder"])

    assert rc == 2
    assert "other" in err.getvalue()
    start.assert_not_called()


def test_start_failure_exits_1_and_keeps_the_claim(tmp_path):
    release = mock.Mock()
    with mock.patch.object(dispatch_mod.claims, "claim"), mock.patch.object(
        dispatch_mod.claims, "release_claim", release
    ), mock.patch.object(
        dispatch_mod.review_dispatch, "prefetch", side_effect=_prefetch(ISSUE)
    ), mock.patch.object(
        dispatch_mod.loop_runtime,
        "start_loop",
        return_value=(False, "skip r: Lupin loop is already active"),
    ):
        try:
            dispatch_mod.dispatch("o/r#1", "holder", state_dir=tmp_path)
        except dispatch_mod.DispatchError as exc:
            assert exc.code == 1
            assert "already active" in str(exc)
        else:
            raise AssertionError("dispatch should fail when start_loop returns false")

    release.assert_not_called()


def test_missing_holder_exits_1_with_one_line(monkeypatch):
    monkeypatch.delenv("LUPIN_CLAIM_HOLDER", raising=False)
    err = io.StringIO()
    with mock.patch.object(dispatch_mod.claims, "claim") as claim, contextlib.redirect_stderr(err):
        rc = cli.main(["dispatch", "o/r#1"])

    assert rc == 1
    assert len(err.getvalue().splitlines()) == 1
    claim.assert_not_called()


def test_no_start_writes_the_brief_but_starts_nothing(tmp_path):
    start = mock.Mock()
    with mock.patch.object(dispatch_mod.claims, "claim"), mock.patch.object(
        dispatch_mod.review_dispatch, "prefetch", side_effect=_prefetch(ISSUE)
    ), mock.patch.object(dispatch_mod.loop_runtime, "start_loop", start):
        result = dispatch_mod.dispatch(
            "o/r#1", "holder", no_start=True, state_dir=tmp_path
        )

    assert result["started"] is False
    assert (tmp_path / "notes" / result["brief_path"].split("/")[-1]).is_file()
    start.assert_not_called()


def test_json_prints_the_contract_fields(tmp_path):
    out = io.StringIO()
    with mock.patch.object(dispatch_mod.claims, "claim"), mock.patch.object(
        dispatch_mod.review_dispatch, "prefetch", side_effect=_prefetch(ISSUE)
    ), mock.patch.object(
        dispatch_mod.loop_runtime, "start_loop", return_value=(True, "started widgets")
    ), mock.patch.dict(
        "os.environ", {"LUPIN_LOOP_STATE_DIR": str(tmp_path)}
    ), contextlib.redirect_stdout(out):
        rc = cli.main(["dispatch", "o/r#1", "--holder", "holder", "--json"])

    assert rc == 0
    payload = json.loads(out.getvalue())
    assert set(payload) == {"item", "kind", "brief_path", "claim", "started"}
    assert payload["item"] == "o/r#1"
    assert payload["kind"] == "issue"
    assert payload["claim"] == "holder"
    assert payload["started"] is True


def _fake_gh(args, *, repo=None, timeout=60):
    kind, item = args[0], args[2]
    if kind == "issue":
        if item == "1":
            return {
                "body": "issue body",
                "comments": [
                    {
                        "author": {"login": "alice"},
                        "createdAt": "2026-01-02T03:04:05Z",
                        "body": "one",
                        "url": "https://example.invalid/comment/1",
                        "reactionGroups": [],
                    }
                ],
            }, None
        return None, "not an issue"
    if item == "2":
        return {
            "body": "pr body",
            "comments": [],
            "reviews": [
                {
                    "author": {"login": "dave"},
                    "state": "APPROVED",
                    "submittedAt": "2026-02-02T00:00:00Z",
                    "body": "looks good",
                    "commit": {"oid": "abc"},
                }
            ],
            "files": [{"path": "a.py", "additions": 3, "deletions": 1}],
        }, None
    return None, "not a pr"


def test_prefetch_defaults_to_markdown():
    out = io.StringIO()
    with mock.patch.object(review_dispatch, "_run_gh_json", side_effect=_fake_gh), contextlib.redirect_stdout(out):
        rc = review_dispatch.main(["--prefetch", "1,2", "--repo", "o/r"])

    assert rc == 0
    text = out.getvalue()
    assert text.startswith("# Issue o/r#1\n")
    assert "# PR o/r#2\n" in text
    assert "### @alice - 2026-01-02T03:04:05Z" in text
    assert "## Reviews (1)" in text
    assert "### @dave - APPROVED - 2026-02-02T00:00:00Z" in text
    assert "- a.py (+3 -1)" in text
    # Sections are separated by a blank line.
    assert "\n\n# PR o/r#2" in text


def test_prefetch_json_trims_comments_and_reviews():
    out = io.StringIO()
    with mock.patch.object(review_dispatch, "_run_gh_json", side_effect=_fake_gh), contextlib.redirect_stdout(out):
        rc = review_dispatch.main(["--prefetch", "1,2", "--repo", "o/r", "--json"])

    assert rc == 0
    payload = json.loads(out.getvalue())
    assert set(payload) == {"1", "2"}
    assert set(payload["1"]) == {"kind", "body", "comments"}
    assert payload["1"]["comments"] == [
        {
            "author": {"login": "alice"},
            "createdAt": "2026-01-02T03:04:05Z",
            "body": "one",
        }
    ]
    assert set(payload["2"]) == {"kind", "body", "comments", "reviews", "diff_stat"}
    assert payload["2"]["reviews"] == [
        {
            "author": {"login": "dave"},
            "state": "APPROVED",
            "submittedAt": "2026-02-02T00:00:00Z",
            "body": "looks good",
        }
    ]
    assert payload["2"]["diff_stat"] == [
        {"path": "a.py", "additions": 3, "deletions": 1}
    ]
