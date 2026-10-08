"""Source-backed benchmark scores for the Models page."""

from __future__ import annotations

import json
from importlib import resources

_DATA = json.loads(resources.files("lupin").joinpath("benchmark-data.json").read_text(encoding="utf-8"))


def matrix(models: list[dict], snapshot: dict | None) -> tuple[list[dict], dict, dict]:
    """Return category metadata, per-model scores, and normalized performance."""
    categories = []
    for definition in _DATA["categories"]:
        category = {**definition, "benchmarks": [dict(item) for item in definition["benchmarks"]]}
        if category["id"] == "general":
            scores = []
            for entry in (snapshot or {}).get("scores", []):
                if not isinstance(entry, dict) or not isinstance(entry.get("score"), (int, float)):
                    continue
                if isinstance(entry.get("score"), bool):
                    continue
                if "Artificial Analysis Intelligence Index" not in (entry.get("scale") or ""):
                    continue
                if "artificialanalysis.ai/leaderboards/models" not in (entry.get("source") or ""):
                    continue
                scores.append({"model": entry.get("id"), "score": entry["score"]})
            if scores:
                category["benchmarks"].append({
                    "id": "aa-intelligence-index",
                    "name": "Artificial Analysis Intelligence Index",
                    "metric": "Score (0–100)",
                    "source": "https://artificialanalysis.ai/leaderboards/models",
                    "retrieved_on": (snapshot or {}).get("fetched_at"),
                    "higher_is_better": True,
                    "scores": scores,
                })
            else:
                category["note"] = "No source-verified Intelligence Index scores are cached."
        categories.append(category)

    model_ids = {model["id"] for model in models}
    by_model = {model_id: {} for model_id in model_ids}
    category_performance = {model_id: {} for model_id in model_ids}
    for category in categories:
        category_id = category["id"]
        for benchmark in category["benchmarks"]:
            all_scores = [
                score for score in benchmark.get("scores", [])
                if isinstance(score, dict)
                and isinstance(score.get("score"), (int, float))
                and not isinstance(score.get("score"), bool)
            ]
            scores = [score for score in all_scores if score.get("model") in model_ids]
            if not scores:
                continue
            higher_is_better = benchmark.get("higher_is_better", True)
            best = max(score["score"] for score in all_scores) if higher_is_better else min(score["score"] for score in all_scores)
            for score in scores:
                model_id = score["model"]
                by_model[model_id].setdefault(category_id, []).append({
                    **score,
                    "benchmark": benchmark["name"],
                    "metric": benchmark["metric"],
                    "source": benchmark["source"],
                })
                if best > 0 and score["score"] > 0:
                    normalized = score["score"] / best if higher_is_better else best / score["score"]
                    category_performance[model_id].setdefault(category_id, []).append(normalized * 100)

    performance = {}
    for model_id, category_scores in category_performance.items():
        category_means = [sum(values) / len(values) for values in category_scores.values() if values]
        performance[model_id] = sum(category_means) / len(category_means) if category_means else None
    return categories, by_model, performance


def notes_by_model(snapshot: dict | None) -> dict[str, dict]:
    """Return source-backed qualitative notes keyed by exact model ID."""
    notes = {}
    for entry in (snapshot or {}).get("scores") or []:
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        note = entry.get("note")
        if (
            isinstance(note, dict)
            and isinstance(note.get("text"), str)
            and note["text"].strip()
            and isinstance(note.get("source"), str)
            and note["source"].startswith(("https://", "http://"))
        ):
            notes[entry["id"]] = note
    return notes
