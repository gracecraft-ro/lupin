"""Sort an issue into (task category, size) for the model-tier lookup.

Moved from ghostbook.nix's hosts/jesus/loopgui/classify.py (issue #203, part
of #198's plan) at source commit
12c007705e63b08c363062487195c8003d5347d3. `_label_names`/`_size` below are
copied from that same commit's hosts/jesus/loopgui/roadmap.py (not the whole
65K-line file -- just these two helpers, which is all classify.py needs from
it). Everything else is unchanged.

Category comes from a keyword scan of the title, body, and labels: CAD/
geometry terms win outright, then a UI/screenshot signal, then translation/
prose terms, else "coding". See issue #183 and the research on #179 for why
these groups.
"""

from __future__ import annotations

import re

# --- copied from roadmap.py, commit 12c007705e63b08c363062487195c8003d5347d3 ---


def _label_names(issue: dict) -> list[str]:
    return sorted(
        label.get("name", "") if isinstance(label, dict) else str(label)
        for label in issue.get("labels", [])
    )


def _size(labels: list[str]) -> str:
    for label in labels:
        match = re.fullmatch(r"size[-/](XS|S|M|L|XL)", label, re.IGNORECASE)
        if match:
            return f"size-{match.group(1).lower()}"
    return "size-?"


# --- end copied section ---

# Plain words, matched case-insensitively with word boundaries.
_CAD_WORDS = (
    "cad",
    "parametric",
    "geometry",
    "mesh",
    "extrude",
    "sketch",
    "assembly",
    "spatial",
    "blender",
)
# File-format names. "step" and "stl" are also common English words/abbreviations,
# so these only count when written in all caps (STEP file, .STL), not as prose.
_CAD_FILE_WORDS = ("STEP", "STL")
_UI_WORDS = ("screenshot", "ui", "frontend", "ux", "css")
_TRANSLATION_WORDS = ("translate", "translation", "localization", "localize", "i18n")
_PROSE_WORDS = ("prose", "copywriting", "copy-edit", "essay", "narrative")


def _has_word(text: str, words: tuple[str, ...]) -> bool:
    return any(re.search(rf"\b{re.escape(word)}\b", text) for word in words)


def _category(issue: dict, labels: list[str]) -> str:
    # "general" (model-tiers.json) has no branch here on purpose. It's a
    # catch-all/baseline lane (AA Intelligence Index) for non-coding work --
    # but every issue this function sees is a GitHub issue tied to a code
    # diff, so there's no keyword or label that tells "general reasoning"
    # apart from "coding" at this point. Making one up would be a fake
    # signal, not a real one. "general" stays reachable only by a caller
    # passing it in directly, not by anything classify() infers. See #183
    # and the PR for #189's review fix for the reasoning.
    raw = " ".join(
        [str(issue.get("title") or ""), str(issue.get("body") or ""), " ".join(labels)]
    )
    lowered = raw.lower()

    if _has_word(lowered, _CAD_WORDS) or _has_word(raw, _CAD_FILE_WORDS):
        return "cad-spatial"
    if _has_word(lowered, _UI_WORDS):
        return "frontend-ui"
    if _has_word(lowered, _TRANSLATION_WORDS):
        return "translation"
    if _has_word(lowered, _PROSE_WORDS):
        return "prose"
    return "coding"


def _refine_size(size: str, diff_stat: str | None) -> str:
    """Downgrade a label-based size if the real diff turned out trivial."""
    if not diff_stat or size in ("size-?", "size-xs", "size-s"):
        return size
    totals = re.findall(r"(\d+) insertions?\(\+\)|(\d+) deletions?\(-\)", diff_stat)
    changed = sum(int(ins or dele) for ins, dele in totals)
    # Matches both text-diff lines ("foo.py | 3 ++-") and binary-diff lines
    # ("asset.png | Bin 0 -> 12345 bytes"), which end in "bytes" instead of a
    # +/- count.
    files = len(
        re.findall(r"\|.*(?:\d+ [+-]*|Bin \d+ -> \d+ bytes)$", diff_stat, re.MULTILINE)
    )
    if files <= 1 and changed <= 5:
        return "size-xs"
    return size


def classify(issue: dict, diff_stat: str | None = None) -> tuple[str, str]:
    """Return (category, size) for an issue, the lookup key into model-tiers.json.

    `diff_stat` (a `git diff --stat` block) is an optional secondary signal —
    it only refines size (e.g. a size-l issue that barely touched any code),
    never category.
    """
    labels = _label_names(issue)
    category = _category(issue, labels)
    size = _refine_size(_size(labels), diff_stat)
    return category, size
