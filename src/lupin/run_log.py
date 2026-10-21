"""Store and read one JSONL event file per loop run."""

from __future__ import annotations

import fcntl
import json
import os
import re
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}(~([2-9]|[1-9][0-9]))?$")
_RUN_ID = re.compile(r"^\d{8}-\d{6}-\d{6}-[0-9a-f]{32}$")
MAX_RUNS = 20
MAX_AGE = timedelta(days=30)


class RunLogError(Exception):
    """A run log cannot be read."""


def new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    return f"{stamp}-{uuid.uuid4().hex}"


def valid_run_id(run_id: object) -> bool:
    return isinstance(run_id, str) and _RUN_ID.fullmatch(run_id) is not None


def _repo_dir(state_dir: Path | str, repo: str) -> Path:
    if not isinstance(repo, str) or not _REPO_ID.fullmatch(repo):
        raise RunLogError(f"invalid repo name {repo!r}")
    return Path(state_dir) / "logs" / repo


def _log_path(state_dir: Path | str, repo: str, run_id: str) -> Path:
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise RunLogError(f"invalid run ID {run_id!r}")
    return _repo_dir(state_dir, repo) / f"{run_id}.jsonl"


def append_event(
    state_dir: Path | str, repo: str, run_id: str, event: str, **fields: object
) -> bool:
    """Append one event. A log error does not stop a loop."""
    try:
        path = _log_path(state_dir, repo, run_id)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        record = {
            **fields,
            "event": event,
            "run_id": run_id,
            "timestamp": datetime.now(timezone.utc).isoformat(
                timespec="microseconds"
            ).replace("+00:00", "Z"),
        }
        data = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o640)
    except Exception:
        return False

    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            start = os.lseek(fd, 0, os.SEEK_END)
            remaining = memoryview(data)
            try:
                while remaining:
                    written = os.write(fd, remaining)
                    if written <= 0:
                        raise OSError("short write to run log")
                    remaining = remaining[written:]
            except Exception:
                try:
                    os.ftruncate(fd, start)
                except OSError:
                    pass
                return False
            try:
                os.fsync(fd)
            except OSError:
                pass
            return True
        except Exception:
            return False
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _started_at(run_id: str) -> datetime:
    try:
        return datetime.strptime(run_id.rsplit("-", 1)[0], "%Y%m%d-%H%M%S-%f").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def prune_runs(state_dir: Path | str, repo: str, *, now: datetime | None = None) -> None:
    """Delete run logs older than 30 days, then keep at most 20."""
    try:
        root = _repo_dir(state_dir, repo)
        files = [path for path in root.glob("*.jsonl") if _RUN_ID.fullmatch(path.stem)]
    except (OSError, RunLogError):
        return
    if not files:
        return

    current = now or datetime.now(timezone.utc)
    current = (
        current.replace(tzinfo=timezone.utc)
        if current.tzinfo is None
        else current.astimezone(timezone.utc)
    )
    cutoff = current - MAX_AGE
    files.sort(key=lambda path: (_started_at(path.stem), path.name))
    remaining: list[Path] = []
    for path in files:
        if _started_at(path.stem) < cutoff:
            try:
                path.unlink()
                continue
            except OSError:
                pass
        remaining.append(path)

    for path in remaining[: max(0, len(remaining) - MAX_RUNS)]:
        try:
            path.unlink()
        except OSError:
            pass


def latest_run_id(state_dir: Path | str, repo: str) -> str | None:
    try:
        root = _repo_dir(state_dir, repo)
        ids = [path.stem for path in root.glob("*.jsonl") if _RUN_ID.fullmatch(path.stem)]
    except (OSError, RunLogError):
        return None
    return max(ids, default=None)


def read_events(
    state_dir: Path | str, repo: str, run_id: str, *, tail: int = 50
) -> list[dict]:
    if not isinstance(tail, int) or tail < 1:
        raise RunLogError("tail must be a positive integer")
    path = _log_path(state_dir, repo, run_id)
    if not path.is_file():
        raise RunLogError(f"no run log for {repo} ({run_id})")

    events: deque[dict] = deque(maxlen=tail)
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, RecursionError):
                    continue
                if isinstance(record, dict) and record.get("run_id") == run_id:
                    events.append(record)
    except OSError as exc:
        raise RunLogError(f"could not read run log for {repo} ({run_id}): {exc}") from exc
    return list(events)
