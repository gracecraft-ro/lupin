from __future__ import annotations

from datetime import datetime, timedelta, timezone

from lupin import run_log


def test_append_and_read_events_keep_valid_jsonl_and_apply_tail(tmp_path):
    run_id = run_log.new_run_id()

    assert run_log.append_event(tmp_path, "widgets", run_id, "run_start", platform="omp")
    assert run_log.append_event(tmp_path, "widgets", run_id, "agent_exit", exit_code=0)
    path = tmp_path / "logs" / "widgets" / f"{run_id}.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"torn":\n')

    events = run_log.read_events(tmp_path, "widgets", run_id, tail=2)

    assert [event["event"] for event in events] == ["run_start", "agent_exit"]
    assert all(event["run_id"] == run_id for event in events)
    assert events[0]["timestamp"].endswith("Z")
    datetime.fromisoformat(events[0]["timestamp"].replace("Z", "+00:00"))
    assert run_log.read_events(tmp_path, "widgets", run_id, tail=1) == [events[-1]]


def test_prune_runs_removes_expired_logs_and_keeps_only_twenty_newest(tmp_path):
    root = tmp_path / "logs" / "widgets"
    root.mkdir(parents=True)
    now = datetime(2026, 3, 1, tzinfo=timezone.utc)
    old = datetime(2026, 1, 1, tzinfo=timezone.utc)
    recent = [
        datetime(2026, 2, 20, tzinfo=timezone.utc) + timedelta(minutes=i)
        for i in range(21)
    ]
    run_ids = [
        value.strftime("%Y%m%d-%H%M%S-%f") + "-" + "0" * 32
        for value in [old, *recent]
    ]
    for run_id in run_ids:
        (root / f"{run_id}.jsonl").write_text("{}\n", encoding="utf-8")

    run_log.prune_runs(tmp_path, "widgets", now=now)

    remaining = sorted(path.stem for path in root.glob("*.jsonl"))
    assert len(remaining) == run_log.MAX_RUNS
    assert run_ids[0] not in remaining
    assert run_ids[1] not in remaining
    assert run_ids[-1] in remaining


def test_append_event_does_not_raise_when_storage_fails(monkeypatch, tmp_path):
    def fail(*args, **kwargs):
        raise PermissionError("read-only state directory")

    monkeypatch.setattr(run_log, "_log_path", fail)

    assert run_log.append_event(tmp_path, "widgets", run_log.new_run_id(), "run_start") is False
