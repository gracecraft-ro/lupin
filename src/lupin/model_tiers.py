"""Load and validate tiers for routing, the dashboard, and benchmark fallback."""

from __future__ import annotations

import json
import os
from importlib import resources

SETTING = "LUPIN_MODEL_TIERS"
TIER_ORDER = ("tier0", "tier1", "tier2")
_EFFORTS = {"low", "medium", "high", "xhigh"}


class ModelTiersError(ValueError):
    """The selected model-tier file cannot be read or used."""


def default_path() -> str:
    return str(resources.files("lupin").joinpath("model-tiers.json"))


def path(default: str | None = None) -> str:
    """Return the configured file, or the bundled file when unset."""
    configured = os.environ.get(SETTING)
    if configured is not None:
        return configured
    return default if default is not None else default_path()


def load(source: str | None = None) -> dict:
    """Read and validate the selected JSON model-tier file."""
    source = path() if source is None else source
    if not source:
        raise ModelTiersError(f"{SETTING} must name a file")
    try:
        with open(source, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ModelTiersError(f"cannot read model tiers file {source!r}: {error}") from error
    return validate(data, source)


def validate(data: object, source: str) -> dict:
    """Check the fields that routing and the Models page read."""
    if not isinstance(data, dict):
        raise ModelTiersError(f"{source!r}: expected a JSON object of categories")

    categories = 0
    for category, entry in data.items():
        if category.startswith("_"):
            continue
        categories += 1
        if not category.strip():
            raise ModelTiersError(f"{source!r}: category names cannot be empty")
        if not isinstance(entry, dict):
            raise ModelTiersError(f"{source!r}: category {category!r} must be an object")
        for field in ("source", "last_verified", "note"):
            if field in entry and not isinstance(entry[field], str):
                raise ModelTiersError(
                    f"{source!r}: category {category!r} field {field!r} must be text"
                )

        tiers = entry.get("tiers")
        if not isinstance(tiers, dict) or not tiers:
            raise ModelTiersError(
                f"{source!r}: category {category!r} needs a non-empty 'tiers' object"
            )
        unknown = sorted(set(tiers) - set(TIER_ORDER))
        if unknown:
            raise ModelTiersError(
                f"{source!r}: category {category!r} has unknown tiers: {', '.join(unknown)}"
            )
        for tier, picks in tiers.items():
            if not isinstance(picks, list) or not picks:
                raise ModelTiersError(
                    f"{source!r}: category {category!r} tier {tier!r} needs a non-empty list"
                )
            for index, pick in enumerate(picks, start=1):
                if not isinstance(pick, dict):
                    raise ModelTiersError(
                        f"{source!r}: category {category!r} tier {tier!r} pick {index} must be an object"
                    )
                model = pick.get("model")
                if not isinstance(model, str) or not model.strip():
                    raise ModelTiersError(
                        f"{source!r}: category {category!r} tier {tier!r} pick {index} needs a model"
                    )
                effort = pick.get("effort")
                if not isinstance(effort, str) or effort not in _EFFORTS:
                    raise ModelTiersError(
                        f"{source!r}: category {category!r} tier {tier!r} pick {index} needs an effort "
                        "of low, medium, high, or xhigh"
                    )

    if categories == 0:
        raise ModelTiersError(f"{source!r}: the file needs at least one category")
    return data
