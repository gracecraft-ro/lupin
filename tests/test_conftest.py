"""Test that the autouse fixture isolates the environment and loop state."""

import os

from lupin import loop_runtime


def test_clean_lupin_env_leaves_only_local_backend():
    lupin_names = sorted(name for name in os.environ if name.startswith("LUPIN_"))
    assert lupin_names == ["LUPIN_BACKEND"]
    assert os.environ["LUPIN_BACKEND"] == "local"


def test_clean_lupin_env_uses_test_loop_state(tmp_path):
    assert loop_runtime.STATE_DIR == tmp_path
    assert loop_runtime.REPOS_FILE == tmp_path / "repos"
    assert loop_runtime.ORCHESTRATORS_FILE == tmp_path / "orchestrators.json"
    assert loop_runtime.LOOPS_DIR == tmp_path / "herdr-loops"
    assert loop_runtime.REPORTS_DIR == tmp_path / "reports"
    with loop_runtime._lock_file("widgets"):
        pass
    assert (tmp_path / "locks" / "widgets.lock").is_file()
