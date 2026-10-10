"""Checks for the clean_lupin_env fixture in conftest.py."""

import os

import pytest


@pytest.fixture
def leaked_env(monkeypatch):
    monkeypatch.setenv("LUPIN_REDIS_HOST", "127.0.0.1")


def test_clean_lupin_env_removes_leaked_redis_host(leaked_env, clean_lupin_env):
    assert "LUPIN_REDIS_HOST" not in os.environ
