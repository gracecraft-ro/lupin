from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
OMP_OUTPUT = '{"scores": [{"id": "gpt-6-luna", "score": 50.0}]}'


@pytest.mark.parametrize(
    ("call", "expected"),
    [
        (
            "from lupin import digest; digest.LLM_TIMEOUT_S = 0.25; "
            "print(digest._run_digest('openai-codex/gpt-6-luna', 'medium', 'prompt'))",
            OMP_OUTPUT,
        ),
        (
            "from lupin import benchmark_fetch as b; b.SHARD_TIMEOUT = 0.25; "
            "scores, error = b._run_shard(['gpt-6-luna'], "
            "candidates=['openai-codex/gpt-6-luna']); print(scores, error)",
            "[{'id': 'gpt-6-luna', 'score': 50.0}] None",
        ),
    ],
    ids=("digest", "benchmark"),
)
def test_omp_prompt_finishes_while_caller_stdin_stays_open(tmp_path, call, expected):
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    omp = binary_dir / "omp"
    omp.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "sys.stdin.read()\n"
        f"print({OMP_OUTPUT!r})\n",
        encoding="utf-8",
    )
    omp.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = os.pathsep.join((str(binary_dir), env.get("PATH", "")))
    env["PYTHONPATH"] = os.pathsep.join(
        (str(REPO / "src"), env.get("PYTHONPATH", ""))
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", call],
        cwd=REPO,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = proc.communicate()
        pytest.fail(f"OMP prompt did not finish; stdout={stdout!r}, stderr={stderr!r}")

    stdout, stderr = proc.communicate()
    assert proc.returncode == 0, stderr
    assert expected in stdout
