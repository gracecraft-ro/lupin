"""Shared pytest fixtures for the `redis` backend tests (issue #210).

A real `redis-server` subprocess, not a mock -- per #210's test plan. One
server for the whole session (starting it has a real cost); each test gets
a clean keyspace via `flush_redis` instead of a fresh server.
"""

from __future__ import annotations

import socket
import subprocess
import time

import pytest
import redis as redis_lib


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="session")
def redis_port():
    port = _free_port()
    proc = subprocess.Popen(
        [
            "redis-server",
            "--port", str(port),
            "--bind", "127.0.0.1",
            "--save", "",
            "--appendonly", "no",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    client = redis_lib.Redis(host="127.0.0.1", port=port)
    deadline = time.monotonic() + 10
    up = False
    while time.monotonic() < deadline:
        try:
            up = client.ping()
            break
        except redis_lib.exceptions.ConnectionError:
            time.sleep(0.05)
    if not up:
        proc.terminate()
        proc.wait(timeout=5)
        raise RuntimeError("redis-server did not come up in time")
    try:
        yield port
    finally:
        proc.terminate()
        proc.wait(timeout=5)


@pytest.fixture
def flush_redis(redis_port):
    client = redis_lib.Redis(host="127.0.0.1", port=redis_port)
    client.flushall()
    yield
    client.flushall()


@pytest.fixture
def closed_port() -> int:
    """A TCP port nothing listens on -- exercises the unreachable-redis path."""
    return _free_port()


@pytest.fixture(scope="session")
def auth_redis_port():
    """A separate server from `redis_port`, with `requirepass` set -- tests
    the username/password plumbing (jesus's real Redis needs ACL auth, see
    docs/redis-schema.md), without adding auth to the shared no-auth server
    every other test in this file relies on.
    """
    port = _free_port()
    proc = subprocess.Popen(
        [
            "redis-server",
            "--port", str(port),
            "--bind", "127.0.0.1",
            "--save", "",
            "--appendonly", "no",
            "--requirepass", "test-pass",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    client = redis_lib.Redis(host="127.0.0.1", port=port, password="test-pass")
    deadline = time.monotonic() + 10
    up = False
    while time.monotonic() < deadline:
        try:
            up = client.ping()
            break
        except redis_lib.exceptions.ConnectionError:
            time.sleep(0.05)
    if not up:
        proc.terminate()
        proc.wait(timeout=5)
        raise RuntimeError("redis-server did not come up in time")
    try:
        yield port
    finally:
        proc.terminate()
        proc.wait(timeout=5)


@pytest.fixture
def make_checkout(tmp_path):
    """Return a function that makes a real git checkout with an `origin` remote.

    The checkout has one commit on `main`, and `origin/HEAD` points to
    `origin/main`. The bare `origin` repo is under `tmp_path/origins`.
    """

    def git(cwd, *args):
        subprocess.run(
            [
                "git", "-c", "user.name=test", "-c", "user.email=test@example.com",
                "-c", "commit.gpgsign=false", *args,
            ],
            cwd=cwd, check=True, capture_output=True,
        )

    def make(path):
        origin = tmp_path / "origins" / f"{path.name}.git"
        origin.parent.mkdir(parents=True, exist_ok=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
        git(tmp_path, "clone", "-q", str(origin), str(path))
        (path / "README.md").write_text("hello\n", encoding="utf-8")
        git(path, "add", "README.md")
        git(path, "commit", "-q", "-m", "first commit")
        git(path, "push", "-q", "origin", "main")
        git(path, "remote", "set-head", "origin", "--auto")
        return path

    return make
