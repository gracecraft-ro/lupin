"""Source-backed benchmark scores for the Models page."""

from __future__ import annotations

import json
from importlib import resources

_DATA = json.loads(resources.files("lupin").joinpath("benchmark-data.json").read_text(encoding="utf-8"))


def _names_intelligence_index(scale) -> bool:
    """Does this `scale` describe the AA Intelligence Index?

    The fetch agent names the index in full when it can ("Artificial
    Analysis Intelligence Index v4.1.1, normalized 0-100") but reports the
    bare "0-100" from a per-model AA page. Both are the same published
    number; a scale that names some other metric (an Elo, a rubric, a
    different index) is not.
    """
    if not isinstance(scale, str) or not scale.strip():
        return False
    text = scale.strip().lower()
    if "intelligence index" in text:
        return True
    return text in ("0-100", "score (0-100)", "score (0–100)")


def matrix(models: list[dict], snapshot: dict | None) -> tuple[list[dict], dict, dict]:
    """Return category metadata, per-model scores, and normalized performance."""
    categories = []
    for definition in _DATA["categories"]:
        category = {**definition, "benchmarks": [dict(item) for item in definition["benchmarks"]]}
        if category["id"] == "general":
            scores = []
            for entry in (snapshot or {}).get("scores", []):
                if not isinstance(entry, dict):
                    continue
                score = entry.get("score")
                if not isinstance(score, (int, float)) or isinstance(score, bool):
                    continue
                source = entry.get("source") or ""
                if "artificialanalysis.ai" not in source:
                    continue
                # The scale must name the index. A per-model AA page often
                # reports only "0-100"; that page's headline number is the
                # index, so a bare "0-100" from an AA page is accepted. Any
                # other scale (a coding index, an Elo, a rubric) is not.
                if not _names_intelligence_index(entry.get("scale")):
                    continue
                scores.append({"model": entry.get("id"), "score": score, "source": source})
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
                    "source": score.get("source") or benchmark["source"],
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
