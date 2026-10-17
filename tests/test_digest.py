from __future__ import annotations

import json
import subprocess

import pytest
import redis as redis_lib

from lupin import digest

RAW = "# Debrief\n\n## Shipped\n\n- None\n\n![shot](20261010-120000-6h-screenshots/a.png)\n"


def _kw(redis_port):
    return {"redis_host": "127.0.0.1", "redis_port": redis_port}


def _raw_client(redis_port):
    return redis_lib.Redis(host="127.0.0.1", port=redis_port, decode_responses=True)


def _write(tmp_path, repo, name, text=RAW):
    folder = tmp_path / "debriefs" / repo
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(text, encoding="utf-8")


def _fake_make(calls):
    def make(repo, name, raw):
        calls.append((repo, name))
        return f"## Summary\n\nDigest of {name}.", "sonnet"

    return make


def test_strip_images_names_the_machine_that_keeps_the_file():
    out = digest.strip_images(RAW, "jesus")

    assert "![" not in out
    assert "(shot: kept on jesus)" in out


def test_argv_uses_omp_for_a_provider_model_and_claude_for_a_bare_name():
    omp = digest._argv("opencode-go/step-5-preview-free", "high", "P")
    claude = digest._argv("sonnet", "high", "P")

    assert omp[:2] == ["omp", "-p"] and omp[omp.index("--model") + 1] == "opencode-go/step-5-preview-free"
    assert claude[:2] == ["claude", "-p"] and claude[claude.index("--model") + 1] == "sonnet"
    assert omp[-1] == claude[-1] == "P"
    # No tools: the model only reads text and writes text.
    assert omp[omp.index("--tools") + 1] == claude[claude.index("--tools") + 1] == ""


def test_prompt_tells_the_model_that_missing_data_is_not_an_empty_result():
    assert "Missing data is not an empty result" in digest.PROMPT


def _run_returning(monkeypatch, *, stdout="", returncode=0, stderr="", raises=None):
    monkeypatch.setattr(digest, "pick_model", lambda: ("sonnet", "high"))
    seen = {}

    def run(argv, **kwargs):
        seen["argv"] = argv
        if raises:
            raise raises
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    monkeypatch.setattr(digest.subprocess, "run", run)
    return seen


def test_make_digest_returns_text_with_a_code_fence_removed(monkeypatch):
    seen = _run_returning(monkeypatch, stdout="```markdown\n## Summary\n\nOk.\n```\n")

    text, model = digest.make_digest("widgets", "20261010-120000.md", RAW)

    assert (text, model) == ("## Summary\n\nOk.", "sonnet")
    assert "widgets" in seen["argv"][-1] and "# Debrief" in seen["argv"][-1]


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"raises": FileNotFoundError()}, "claude not found"),
        ({"raises": subprocess.TimeoutExpired("claude", 1)}, "timed out"),
        ({"returncode": 3, "stderr": "boom"}, "exited 3: boom"),
        ({"stdout": "  \n"}, "gave no text"),
    ],
)
def test_make_digest_raises_digest_error_for_each_model_failure(monkeypatch, kwargs, message):
    _run_returning(monkeypatch, **kwargs)

    with pytest.raises(digest.DigestError, match=message):
        digest.make_digest("widgets", "20261010-120000.md", RAW)


def test_pick_model_refuses_a_blocked_route_and_a_local_model(monkeypatch):
    monkeypatch.setattr(digest.route, "route", lambda *a, **k: {"model": "sonnet", "effort": "high", "wait_seconds": 60})
    with pytest.raises(digest.DigestBlocked):
        digest.pick_model()

    monkeypatch.setattr(digest.route, "route", lambda *a, **k: {"model": "bmo:x", "effort": "high"})
    with pytest.raises(digest.DigestError, match="cannot run"):
        digest.pick_model()

    monkeypatch.setattr(digest.route, "route", lambda *a, **k: {"model": "sonnet", "effort": "high"})
    assert digest.pick_model() == ("sonnet", "high")


def test_sync_publishes_raw_text_and_digest_once(tmp_path, redis_port, flush_redis):
    _write(tmp_path, "widgets", "20261010-120000-6h.md")
    calls = []

    first = digest.sync(tmp_path, _kw(redis_port), machine="jesus", make=_fake_make(calls))
    second = digest.sync(tmp_path, _kw(redis_port), machine="jesus", make=_fake_make(calls))

    entry = digest.read_one("jesus", "widgets", "20261010-120000-6h.md", **_kw(redis_port))
    assert (first, second, len(calls)) == (1, 0, 1)
    assert entry["digest"].startswith("## Summary")
    assert entry["model"] == "sonnet"
    assert "![" not in entry["raw"] and "kept on jesus" in entry["raw"]
    assert _raw_client(redis_port).ttl(digest._key("jesus", "widgets", "20261010-120000-6h.md")) > 0


def test_sync_limit_publishes_the_rest_without_a_digest_then_fills_them_in(tmp_path, redis_port, flush_redis):
    for day in ("01", "02", "03"):
        _write(tmp_path, "widgets", f"202610{day}-120000.md")
    calls = []

    digest.sync(tmp_path, _kw(redis_port), machine="jesus", limit=1, make=_fake_make(calls))
    after_first = {e["name"]: e["digest"] for e in digest.read_all(**_kw(redis_port))}
    digest.sync(tmp_path, _kw(redis_port), machine="jesus", limit=5, make=_fake_make(calls))
    after_second = {e["name"]: e["digest"] for e in digest.read_all(**_kw(redis_port))}

    # Newest first: the 03rd got the one model call. The others are still readable.
    assert after_first["20261003-120000.md"] and not after_first["20261001-120000.md"]
    assert len(after_first) == 3
    assert all(after_second.values())
    assert len(calls) == 3


def test_sync_counts_failed_tries_and_stops_at_the_limit(tmp_path, redis_port, flush_redis):
    _write(tmp_path, "widgets", "20261010-120000.md")
    calls = []

    def failing(repo, name, raw):
        calls.append(name)
        raise digest.DigestError("claude exited 1: nope")

    for _ in range(digest.MAX_TRIES + 2):
        digest.sync(tmp_path, _kw(redis_port), machine="jesus", make=failing)

    entry = digest.read_one("jesus", "widgets", "20261010-120000.md", **_kw(redis_port))
    assert len(calls) == digest.MAX_TRIES
    assert entry["digest"] is None and entry["error"] == "claude exited 1: nope"
    assert entry["raw"]  # The debrief is readable even with no digest.


def test_sync_does_not_count_a_blocked_model_as_a_try(tmp_path, redis_port, flush_redis):
    _write(tmp_path, "widgets", "20261010-120000.md")
    _write(tmp_path, "widgets", "20261009-120000.md")
    calls = []

    def blocked(repo, name, raw):
        calls.append(name)
        raise digest.DigestBlocked("out of quota")

    for _ in range(digest.MAX_TRIES + 1):
        digest.sync(tmp_path, _kw(redis_port), machine="jesus", make=blocked)

    entries = digest.read_all(**_kw(redis_port))
    assert [e["tries"] for e in entries] == [0, 0]
    # One blocked call ends the sync. The older debrief is still published as text.
    assert len(calls) == digest.MAX_TRIES + 1
    assert all(e["raw"] for e in entries)


def test_sync_survives_an_unreachable_redis(tmp_path):
    _write(tmp_path, "widgets", "20261010-120000.md")

    assert digest.sync(tmp_path, {"redis_host": "127.0.0.1", "redis_port": 1}, machine="jesus", make=_fake_make([])) == 0


def test_read_all_sorts_newest_first_and_drops_bad_entries(redis_port, flush_redis):
    client = _raw_client(redis_port)
    for machine, repo, name in [
        ("jesus", "widgets", "20261001-090000.md"),
        ("ralpha", "widgets", "20261002-090000.md"),
    ]:
        client.set(digest._key(machine, repo, name), json.dumps({"machine": machine, "repo": repo, "name": name, "raw": "# x"}))
    client.set(digest._key("jesus", "..", "20261003-090000.md"), json.dumps({"machine": "jesus", "repo": "..", "name": "20261003-090000.md", "raw": "x"}))
    client.set(digest._key("jesus", "widgets", "evil.md"), json.dumps({"machine": "jesus", "repo": "widgets", "name": "evil.md", "raw": "x"}))
    client.set(digest._key("jesus", "widgets", "20261004-090000.md"), "not json")

    entries = digest.read_all(**_kw(redis_port))

    assert [(e["machine"], e["name"]) for e in entries] == [("ralpha", "20261002-090000.md"), ("jesus", "20261001-090000.md")]


def test_read_one_refuses_a_bad_machine_repo_or_name(redis_port, flush_redis):
    assert digest.read_one("a:b", "widgets", "20261001-090000.md", **_kw(redis_port)) is None
    assert digest.read_one("jesus", "../x", "20261001-090000.md", **_kw(redis_port)) is None
    assert digest.read_one("jesus", "widgets", "x.md", **_kw(redis_port)) is None
