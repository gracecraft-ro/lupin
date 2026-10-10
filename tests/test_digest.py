"""Tests for the Recent updates feed (issue #105): `lupin.digest` and its routes."""

import contextlib
import socket
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from unittest import mock

import pytest

from lupin import digest, roadmap, serve

NOW = datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc)
START = NOW - timedelta(hours=12)
ATTACHMENT = "0123abcd-0123-4abc-8abc-0123456789ab"
ATTACHMENT_URL = f"https://github.com/user-attachments/assets/{ATTACHMENT}"


def _ago(**delta) -> str:
    return (NOW - timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _issue(number, title="Title", **extra):
    issue = {
        "number": number,
        "title": title,
        "body": "",
        "url": f"https://github.com/acme/widgets/issues/{number}",
        "createdAt": _ago(days=30),
        "closedAt": None,
    }
    issue.update(extra)
    return issue


def _comment(body, minutes=5, author="alice", number=1):
    return {
        "body": body,
        "createdAt": _ago(minutes=minutes),
        "author": author,
        "url": f"https://github.com/acme/widgets/issues/{number}#issuecomment-1",
    }


def _collect(issues, comments=None, repo="widgets"):
    return digest.collect_repo(repo, issues, comments or {}, START, NOW)


# ---- the collector ---------------------------------------------------------


def test_collect_reports_opened_closed_and_comment_kinds():
    issues = [
        _issue(1, "Opened now", createdAt=_ago(minutes=10), body="First line.\nSecond."),
        _issue(2, "Closed now", closedAt=_ago(minutes=20)),
        _issue(3, "Old and quiet"),
    ]
    comments = {3: [_comment("Looks good to me.", number=3)]}

    updates = _collect(issues, comments)

    by_kind = {item["kind"]: item for item in updates}
    assert sorted(by_kind) == ["Closed", "Comment", "Opened"]
    assert by_kind["Opened"]["number"] == 1
    assert by_kind["Opened"]["text"] == "First line."
    assert by_kind["Closed"]["number"] == 2
    assert by_kind["Closed"]["title"] == "Closed now"
    assert by_kind["Comment"]["number"] == 3
    assert by_kind["Comment"]["title"] == "Old and quiet"
    assert by_kind["Comment"]["text"] == "alice: Looks good to me."
    assert by_kind["Comment"]["url"].endswith("#issuecomment-1")
    assert all(item["repo"] == "widgets" for item in updates)


def test_an_issue_opened_and_closed_in_the_window_gives_two_updates():
    updates = _collect([_issue(1, createdAt=_ago(hours=3), closedAt=_ago(hours=1))])

    assert sorted(item["kind"] for item in updates) == ["Closed", "Opened"]


def test_window_start_is_included_and_end_is_not():
    issues = [
        _issue(1, createdAt=START.isoformat()),
        _issue(2, createdAt=NOW.isoformat()),
        _issue(3, createdAt=(START - timedelta(seconds=1)).isoformat()),
    ]

    assert [item["number"] for item in _collect(issues)] == [1]


def test_a_comment_with_only_an_image_is_a_screenshot():
    comments = {1: [_comment(f"![form-error.png]({ATTACHMENT_URL})")]}

    (update,) = _collect([_issue(1)], comments)

    assert update["kind"] == "Screenshot"
    assert update["text"] == "New image on the issue."
    assert update["images"] == [{"id": ATTACHMENT, "label": "form-error.png"}]


def test_a_comment_with_text_and_an_image_stays_a_comment_with_its_image():
    comments = {1: [_comment(f"Failing case.\n![refund]({ATTACHMENT_URL})")]}

    (update,) = _collect([_issue(1)], comments)

    assert update["kind"] == "Comment"
    assert update["text"] == "alice: Failing case."
    assert update["images"] == [{"id": ATTACHMENT, "label": "refund"}]


def test_only_github_attachment_images_are_kept():
    body = (
        f"![a]({ATTACHMENT_URL}) "
        "![b](https://example.com/b.png) "
        "![c](https://github.com/user-attachments/assets/not-an-id)"
    )

    (update,) = _collect([_issue(1, createdAt=_ago(minutes=1), body=body)])

    assert update["images"] == [{"id": ATTACHMENT, "label": "a"}]
    assert "example.com" not in update["text"]


def test_the_text_is_one_short_line():
    long_line = "word " * 100
    body = f"\n\n## Heading line\n{long_line}"

    (update,) = _collect([_issue(1, createdAt=_ago(minutes=1), body=body)])

    assert update["text"] == "Heading line"
    (long_update,) = _collect([_issue(2, createdAt=_ago(minutes=1), body=long_line)])
    assert len(long_update["text"]) == digest.EXCERPT_CHARS + 1
    assert long_update["text"].endswith("…")


def test_a_link_that_is_not_github_is_dropped():
    issues = [_issue(1, createdAt=_ago(minutes=1), url="https://evil.example/x")]
    comments = {
        2: [{**_comment("hi", number=2), "url": "javascript:alert(1)"}],
    }
    issues.append(_issue(2, url="https://github.com/acme/widgets/issues/2"))

    updates = {item["number"]: item for item in _collect(issues, comments)}

    assert updates[1]["url"] == ""
    # A bad comment link falls back to the issue link.
    assert updates[2]["url"] == "https://github.com/acme/widgets/issues/2"


def test_a_comment_on_an_unknown_issue_still_shows():
    (update,) = _collect([], {99: [_comment("Orphan.", number=99)]})

    assert update["number"] == 99
    assert update["title"] == ""


@pytest.mark.parametrize(
    "created",
    [
        1_700_000_000_000,  # a millisecond epoch, not an ISO string
        20261003,  # a number that `fromisoformat` would read as a date
        "0001-01-01T00:00:00+05:00",  # overflows when converted to UTC
        "not a time",
        None,
        "",
    ],
)
def test_a_time_that_is_not_usable_is_skipped_without_an_error(created):
    issues = [_issue(1, createdAt=created), _issue(2, createdAt=_ago(minutes=1))]

    assert [item["number"] for item in _collect(issues)] == [2]


def test_malformed_data_is_skipped_and_good_data_is_kept():
    issues = [
        "not a dict",
        {"title": "no number", "createdAt": _ago(minutes=1)},
        {"number": "7", "createdAt": _ago(minutes=1)},
        _issue(3, "Good", createdAt=_ago(minutes=2), body=None),
    ]
    comments = {
        "x": [_comment("string key")],
        3: "not a list",
        4: ["not a dict", {"createdAt": None}, _comment("kept", number=4)],
    }

    updates = _collect(issues, comments)

    assert sorted((item["number"], item["kind"]) for item in updates) == [
        (3, "Opened"),
        (4, "Comment"),
    ]
    assert _collect("nope", "nope") == []
    assert _collect(None, None) == []


def test_collect_merges_repos_newest_first_and_one_bad_repo_does_not_hide_others():
    sources = {
        "alpha": {
            "issues": [_issue(1, "Older", createdAt=_ago(hours=2))],
            "comments": {},
        },
        "broken": {"issues": "garbage", "comments": 7},
        "beta": {
            "issues": [_issue(2, "Newer", createdAt=_ago(minutes=1))],
            "comments": {},
        },
    }

    updates = digest.collect(sources, NOW)

    assert [(item["repo"], item["title"]) for item in updates] == [
        ("beta", "Newer"),
        ("alpha", "Older"),
    ]


def test_collect_uses_the_window_hours_it_is_given():
    sources = {"w": {"issues": [_issue(1, createdAt=_ago(hours=3))], "comments": {}}}

    assert digest.collect(sources, NOW, hours=2) == []
    assert len(digest.collect(sources, NOW, hours=4)) == 1


def _sample_updates():
    sources = {
        "api": {
            "issues": [
                _issue(1, "Retry", createdAt=_ago(minutes=30)),
                _issue(2, "Rounding", closedAt=_ago(minutes=20)),
            ],
            "comments": {
                1: [_comment("Cap it.", minutes=10, number=1)],
                2: [_comment(f"![r.png]({ATTACHMENT_URL})", minutes=5, number=2)],
            },
        },
        "web": {
            "issues": [_issue(3, "Form", createdAt=_ago(minutes=40))],
            "comments": {3: [_comment(f"See\n![s.png]({ATTACHMENT_URL})", minutes=2, number=3)]},
        },
    }
    return digest.collect(sources, NOW)


def _filtered(**kwargs):
    return [(i["repo"], i["number"], i["kind"]) for i in digest.filter_updates(_sample_updates(), **kwargs)]


def test_filter_chips():
    assert len(_filtered()) == 6
    assert _filtered(kind="Comments") == [("web", 3, "Comment"), ("api", 1, "Comment")]
    assert _filtered(kind="Closed") == [("api", 2, "Closed")]
    assert sorted(_filtered(kind="Opened")) == [("api", 1, "Opened"), ("web", 3, "Opened")]
    # Images keeps a Screenshot and a Comment that has an image.
    assert sorted(_filtered(kind="Images")) == [("api", 2, "Screenshot"), ("web", 3, "Comment")]
    assert {r for r, _n, _k in _filtered(repo="web")} == {"web"}
    assert _filtered(repo="web", kind="Closed") == []


# ---- the HTML fragment -----------------------------------------------------


def test_render_updates_shows_each_update_with_its_parts():
    html_text = digest.render_updates(_sample_updates())

    assert "api #2" in html_text
    assert "<b>Rounding</b>" in html_text
    assert "<span class='update-kind pill on'>Closed</span>" in html_text
    assert "<span class='update-kind pill'>Screenshot</span>" in html_text
    assert f"<img src='/image?id={ATTACHMENT}' alt='r.png' loading='lazy'>" in html_text
    assert "href='https://github.com/acme/widgets/issues/2#issuecomment-1'" in html_text
    assert "Images come from GitHub attachments" in html_text
    # The time cell carries an epoch for the page's live clock and a plain fallback.
    assert f"data-since='{int((NOW - timedelta(minutes=5)).timestamp())}'" in html_text
    assert "2026-10-10 11:55 UTC" in html_text


def test_render_updates_escapes_every_text_field():
    sources = {
        "<repo>": {
            "issues": [
                _issue(
                    1,
                    "<script>alert(1)</script>",
                    createdAt=_ago(minutes=1),
                    body="<img src=x onerror=alert(2)>",
                )
            ],
            "comments": {
                1: [_comment("<b>bold</b>", author="<i>me</i>", number=1)],
            },
        }
    }
    updates = digest.collect(sources, NOW)

    html_text = digest.render_updates(updates, warnings=["<u>warn</u>"])

    for raw in ("<script>", "<img src=x", "<b>bold", "<i>me", "<u>warn", "<repo>"):
        assert raw not in html_text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html_text
    assert "&lt;u&gt;warn&lt;/u&gt;" in html_text


def test_chips_mark_the_active_filter_and_keep_the_other_filter():
    html_text = digest.render_updates(_sample_updates(), repo="api", kind="Closed")

    assert "<a class='updates-chip active' href='/updates?full=1&amp;repo=api&amp;kind=Closed' " in html_text
    assert "data-src='/updates?repo=api&amp;kind=Closed'>Closed</a>" in html_text
    # A kind chip keeps the chosen repo. A repo chip keeps the chosen kind.
    assert "href='/updates?full=1&amp;repo=api&amp;kind=Images'" in html_text
    assert "href='/updates?full=1&amp;repo=web&amp;kind=Closed'" in html_text
    # "All" drops its own parameter.
    assert "href='/updates?full=1&amp;repo=api' data-src='/updates?repo=api'>All</a>" in html_text
    assert "href='/updates?full=1&amp;kind=Closed' data-src='/updates?kind=Closed'>All repos</a>" in html_text


def test_repo_chips_count_the_whole_window_not_the_filter():
    wide = digest.render_updates(_sample_updates())
    narrow = digest.render_updates(_sample_updates(), kind="Closed")

    for text in (wide, narrow):
        assert ">api 4</a>" in text
        assert ">web 2</a>" in text


def test_a_chosen_repo_with_no_updates_still_has_a_chip():
    html_text = digest.render_updates([], repo="quiet")

    assert ">quiet 0</a>" in html_text


def test_empty_states_say_why():
    assert "No updates in the last 12 hours." in digest.render_updates([])
    assert "No updates in the last 3 hours." in digest.render_updates([], hours=3)
    assert "No updates match this filter." in digest.render_updates(
        _sample_updates(), repo="web", kind="Closed"
    )


def test_the_list_is_cut_to_the_maximum():
    issues = [
        _issue(n, createdAt=_ago(minutes=1)) for n in range(1, digest.MAX_UPDATES + 6)
    ]
    updates = digest.collect({"w": {"issues": issues, "comments": {}}}, NOW)

    html_text = digest.render_updates(updates)

    assert html_text.count("class='update'") == digest.MAX_UPDATES
    assert f">w {digest.MAX_UPDATES + 5}</a>" in html_text


# ---- the routes ------------------------------------------------------------


def _github(repo_data):
    """A stand-in for `roadmap.cached_github` that reads `repo_data[repo][state]`."""

    def fake(repo, path, state="open", *, connection=None):
        return repo_data[repo][state]

    return fake


def _repo_data():
    return {
        "widgets": {
            "open": (
                [_issue(1, "Retry <backoff>", createdAt=_ago(minutes=30))],
                {1: [_comment(f"Cap it.\n![a.png]({ATTACHMENT_URL})", number=1)]},
                [],
            ),
            "closed": ([_issue(2, "Rounding", closedAt=_ago(minutes=20))], {}, []),
        },
        "gadgets": {
            "open": ([], {}, ["GitHub issue data is unavailable: gh timed out"]),
            "closed": ([], {}, ["GitHub issue data is unavailable: gh timed out"]),
        },
    }


@pytest.fixture
def feed(monkeypatch):
    """Two repos on disk, GitHub data from `_repo_data`, an empty fragment cache."""
    serve._FRAGMENT_CACHE.clear()
    monkeypatch.setattr(
        serve,
        "code_repos",
        lambda: [{"repo": "widgets", "loopable": True}, {"repo": "gadgets", "loopable": True}],
    )
    fake = mock.Mock(side_effect=_github(_repo_data()))
    monkeypatch.setattr(roadmap, "cached_github", fake)
    yield fake
    serve._FRAGMENT_CACHE.clear()


@pytest.fixture
def live_times(monkeypatch):
    """The route reads the real clock. Make `digest.collect` treat NOW as the time."""
    real_collect = digest.collect

    def collect(sources, now, hours=digest.WINDOW_HOURS):
        return real_collect(sources, NOW, hours)

    monkeypatch.setattr(serve.digest, "collect", collect)


def test_updates_fragment_renders_updates_and_warnings(feed, live_times):
    body, status = serve.updates_fragment({}, {})

    text = body.decode()
    assert status == 200
    assert "<!doctype" not in text
    assert "Retry &lt;backoff&gt;" in text
    assert "Rounding" in text
    assert f"/image?id={ATTACHMENT}" in text
    assert "gadgets: GitHub issue data is unavailable: gh timed out" in text
    # The repeated warning from open and closed shows once.
    assert text.count("gh timed out") == 1


def test_updates_fragment_filters_by_repo_and_kind(feed, live_times):
    closed, _ = serve.updates_fragment({"kind": "Closed"}, {})
    assert "Rounding" in closed.decode()
    assert "Retry" not in closed.decode()

    gadgets, _ = serve.updates_fragment({"repo": "gadgets"}, {})
    assert "No updates match this filter." in gadgets.decode()


def test_updates_fragment_refuses_an_unknown_repo(feed):
    body, status = serve.updates_fragment({"repo": "../etc"}, {})

    assert status == 404
    assert b"unknown repository" in body
    feed.assert_not_called()


def test_updates_fragment_ignores_an_unknown_kind(feed, live_times):
    body, status = serve.updates_fragment({"kind": "<script>"}, {})

    assert status == 200
    assert b"<script>" not in body
    assert b"updates-chip active' href='/updates?full=1' data-src='/updates'>All</a>" in body


def test_updates_fragment_reads_github_once_inside_the_cache_window(feed, live_times):
    serve.updates_fragment({}, {})
    first_calls = feed.call_count
    serve.updates_fragment({}, {})

    assert first_calls == 4  # open and closed, for two repos
    assert feed.call_count == first_calls


def _get(path):
    handler = serve.Handler.__new__(serve.Handler)
    handler.path = path
    handler.fleet_connection = {}
    handler.host_ok = mock.Mock(return_value=True)
    handler.reply = mock.Mock()
    handler.do_GET()
    return handler.reply.call_args


def test_updates_route_replies_with_a_fragment(feed, live_times):
    (body, *_rest), _kwargs = _get("/updates?kind=Closed")

    assert body.startswith(b"<div class='updates-filters'>")
    assert b"Rounding" in body


def test_updates_route_full_page_wraps_the_fragment(feed, live_times):
    (body, *_rest), _kwargs = _get("/updates?full=1&repo=widgets")

    text = body.decode()
    assert text.startswith("<!doctype html>")
    assert "<title>Recent updates</title>" in text
    assert "<a class='navlink active' href='/'>" in text  # Overview stays highlighted
    assert "<div id='updates-page'><div class='updates-filters'>" in text
    assert ".update-text{" in text  # the page carries the feed's CSS
    assert "Retry &lt;backoff&gt;" in text


def test_updates_route_unknown_repo_is_404_with_no_feed(feed):
    for path in ("/updates?repo=nope", "/updates?full=1&repo=nope"):
        (body, status), _kwargs = _get(path)

        assert status == 404
        assert b"unknown repository" in body
        assert b"updates-chip" not in body
    feed.assert_not_called()


def test_overview_has_the_placeholder_and_never_calls_github():
    state = {"loops": [], "enabled": [], "timers": [], "timer_active": False, "repos": []}
    with mock.patch.object(roadmap, "cached_github") as github:
        text = serve.render_dashboard(state).decode()

    github.assert_not_called()
    assert "<h2>Recent updates</h2><span class=dim>last 12 hours</span>" in text
    assert "<div id='updates' data-src='/updates' data-full='/updates?full=1'>" in text
    assert "href='/updates?full=1'" in text  # the no-JavaScript link
    assert ".updates-chip{" in text  # the feed's CSS is on the page
    assert "document.getElementById('updates')" in text  # the loader


# ---- a real server ---------------------------------------------------------


@contextlib.contextmanager
def _live_dashboard():
    """Run the real `serve.Handler` on a real socket."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    saved = (serve.Handler.fleet_connection, serve.Handler.allowed_hosts)
    serve.Handler.fleet_connection = {}
    serve.Handler.allowed_hosts = {f"127.0.0.1:{port}"}

    class Server(ThreadingHTTPServer):
        daemon_threads = True

    httpd = Server(("127.0.0.1", port), serve.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        serve.Handler.fleet_connection, serve.Handler.allowed_hosts = saved


def test_updates_over_a_real_socket(feed, live_times):
    with _live_dashboard() as port:
        base = f"http://127.0.0.1:{port}"
        with urllib.request.urlopen(f"{base}/updates?repo=widgets", timeout=15) as response:
            status = response.status
            csp = response.headers["Content-Security-Policy"]
            body = response.read().decode()
        with urllib.request.urlopen(f"{base}/updates?full=1", timeout=15) as response:
            full = response.read().decode()
        with pytest.raises(urllib.error.HTTPError) as missing:
            urllib.request.urlopen(f"{base}/updates?repo=nope", timeout=15)

    assert status == 200
    assert "img-src 'self'" in csp
    assert "Retry &lt;backoff&gt;" in body
    assert f"src='/image?id={ATTACHMENT}'" in body
    assert full.startswith("<!doctype html>")
    assert missing.value.code == 404
