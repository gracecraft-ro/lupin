"""Shared, per-machine snapshots of 7-day token and cost totals."""

from __future__ import annotations

import json
import socket
from datetime import datetime, timezone

import redis

from . import quota, slots_redis

REDIS_KEY_PREFIX = f"{slots_redis.PREFIX}usage-snapshot:"
CACHE_TTL = 300
REDIS_KEY_TTL = 24 * 60 * 60

_REDIS_ERRORS = (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError)


def _client(connection: dict):
    return slots_redis._client(**connection)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_fresh(entry: dict | None, now: float | None = None) -> bool:
    if not entry:
        return False
    fetched_at = entry.get("fetched_at")
    if not fetched_at:
        return False
    try:
        epoch = datetime.fromisoformat(fetched_at).timestamp()
    except (TypeError, ValueError):
        return False
    now = datetime.now(timezone.utc).timestamp() if now is None else now
    return now - epoch < CACHE_TTL


def read_snapshot(**connection) -> dict[str, dict]:
    """Read the latest per-machine snapshots. Return empty on Redis failure."""
    try:
        client = _client(connection)
        keys = slots_redis._call_with_retry(
            lambda: list(client.scan_iter(match=f"{REDIS_KEY_PREFIX}*"))
        )
        snapshots = {}
        for key in keys:
            machine = key.removeprefix(REDIS_KEY_PREFIX)
            raw = slots_redis._call_with_retry(lambda: client.get(key))
            if not machine or raw is None:
                continue
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            if isinstance(entry, dict) and isinstance(entry.get("rows"), list):
                snapshots[machine] = entry
        return snapshots
    except _REDIS_ERRORS:
        return {}


def refresh_snapshot(**connection) -> dict[str, dict]:
    """Publish this machine's local 7-day totals and return the shared view."""
    machine = socket.gethostname()
    entry = {
        "rows": quota.claude_usage() + quota.omp_usage(),
        "fetched_at": _now_iso(),
        "fetched_by": machine,
    }
    try:
        client = _client(connection)
        slots_redis._call_with_retry(
            lambda: client.set(
                f"{REDIS_KEY_PREFIX}{machine}",
                json.dumps(entry),
                ex=REDIS_KEY_TTL,
            )
        )
    except _REDIS_ERRORS:
        return {machine: entry}

    snapshots = read_snapshot(**connection)
    snapshots[machine] = entry
    return snapshots


def aggregate_rows(snapshots: dict[str, dict]) -> list[dict]:
    totals: dict[str, dict] = {}
    errors: dict[str, list[tuple[str, dict]]] = {}
    metrics = ("input_tokens", "output_tokens", "cost")

    for machine, entry in sorted(snapshots.items()):
        rows = entry.get("rows") if isinstance(entry, dict) else None
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("provider"), str):
                continue
            provider = row["provider"]
            if "error" in row or "note" in row:
                message = row.get("error", row.get("note"))
                errors.setdefault(provider, []).append((machine, {**row, "error": message}))
                continue

            total = totals.setdefault(provider, {
                "provider": provider,
                "period": row.get("period", "last 7 days"),
                "last_update": "",
                "sources": set(),
                **{metric: 0 for metric in metrics},
                **{f"_{metric}_known": True for metric in metrics},
            })
            for metric in metrics:
                value = row.get(metric)
                if isinstance(value, (int, float)):
                    total[metric] += value
                else:
                    total[f"_{metric}_known"] = False
            last_update = row.get("last_update")
            if isinstance(last_update, str) and last_update > total["last_update"]:
                total["last_update"] = last_update
            source = row.get("source", "-")
            total["sources"].add(f"{source} on {machine}")

    result = []
    for provider, total in totals.items():
        row = {key: value for key, value in total.items() if not key.startswith("_") and key != "sources"}
        for metric in metrics:
            if not total[f"_{metric}_known"]:
                row[metric] = None
        row["source"] = ", ".join(sorted(total["sources"]))
        result.append(row)

    for provider, entries in errors.items():
        if provider in totals:
            continue
        result.append({
            "provider": provider,
            "error": "; ".join(sorted(
                row["error"] for _, row in entries
            )),
            "period": "last 7 days",
            "source": ", ".join(sorted(
                f"{row.get('source', '-')} on {machine}" for machine, row in entries
            )),
            "last_update": "-",
        })

    return sorted(result, key=lambda row: row["provider"])
