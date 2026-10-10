"""Checks for the clean_lupin_env fixture in conftest.py."""

import os

import pytest


@pytest.fixture
def leaked_env(monkeypatch):
    monkeypatch.setenv("LUPIN_REDIS_HOST", "127.0.0.1")
    monkeypatch.setenv("LUPIN_BACKEND", "redis")


def test_clean_lupin_env_removes_leaked_redis_host_and_forces_local(leaked_env, clean_lupin_env):
    assert "LUPIN_REDIS_HOST" not in os.environ
    assert os.environ["LUPIN_BACKEND"] == "local"
