"""Daily snapshot: which model IDs each subscription can call today, and
what they cost (issue #16).

Two questions per subscription (opencode-go, claude, codex):

1. Which model IDs are callable right now? Provider catalogs change, so
   this is a live fetch, not model-tiers.json's hand-edited tier list.
2. What does each model cost? A baseline $/Mtok price, plus a `promo`
   slot for a time-boxed discount -- separate from the baseline because a
   promo has its own start/end and should never overwrite the baseline
   number. No source for live promo data was found during this issue's
   research (see `snapshot()`), so `promo` is always `None` for now --
   the field exists so issue #18 has somewhere to put real data later.

What's live and what's not, confirmed against this sandbox:

- opencode-go: `OPENCODE_GO_MODELS_URL`, same API key quota.py's
  `opencode_go_quota()` already reads from `OPENCODE_GO_AUTH_FILE`. Live.
- claude: `ANTHROPIC_MODELS_URL`, same OAuth token quota.py's
  `claude_oauth_quota()` already reads from `CLAUDE_CREDENTIALS_FILE`.
  Live, but carries no price -- Anthropic's API never returns one.
- codex: no live source. `~/.codex` holds no auth.json or session
  rollouts in this sandbox (quota.py's `codex_session_quota()` already
  treats that as normal -- there's just nothing to read), and OpenAI's
  public `/v1/models` needs a billing API key, which a Codex Pro OAuth
  login doesn't provide. `codex_models()` falls back to models.dev's
  published OpenAI catalog -- every ID in it is real, but it's OpenAI's
  general API lineup, not a check of what this Codex Pro subscription
  specifically includes. Marked `live: False` for that reason.

Baseline price for all three comes from `MODELS_DEV_CATALOG_URL`
(models.dev's public model/price catalog, no auth needed) rather than
guessed numbers, matched by model ID (`anthropic`, `opencode-go`, `openai`
are its provider keys for these three subscriptions). A live model ID with
no match in that catalog gets `price: None` -- never a made-up number.

Both opencode.ai and models.dev sit behind Cloudflare, which blocks
Python's default `Python-urllib/x.y` user agent (confirmed: the exact
request quota.py's `opencode_go_quota()` sends gets a 403 in this sandbox
today -- a pre-existing bug in that function, not something this module
fixes). `_USER_AGENT` below works around it for the requests this module
makes.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone

from .quota import CLAUDE_CREDENTIALS_FILE, OPENCODE_GO_AUTH_FILE

ANTHROPIC_MODELS_URL = "https://api.anthropic.com/v1/models"
OPENCODE_GO_MODELS_URL = "https://opencode.ai/zen/go/v1/models"
MODELS_DEV_CATALOG_URL = "https://models.dev/api.json"

SNAPSHOT_FILE = os.path.expanduser("~/.local/state/lupin/model-snapshot.json")

_USER_AGENT = "lupin-model-fetch/1.0"
_PRICE_FIELDS = ("input", "output", "cache_read", "cache_write")
_DATE_SUFFIX = re.compile(r"-\d{8}$")


def _get_json(url: str, headers: dict | None = None, timeout: float = 10.0):
    request = urllib.request.Request(
        url, headers={"User-Agent": _USER_AGENT, **(headers or {})}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def fetch_price_catalog() -> tuple[dict | None, str | None]:
    """models.dev's public catalog: `{provider: {models: {id: {cost: ...}}}}`.

    Returns `(catalog, None)` on success or `(None, error)` on failure --
    callers fall back to `price: None` per model rather than guessing.
    """
    try:
        return _get_json(MODELS_DEV_CATALOG_URL), None
    except (OSError, ValueError, urllib.error.URLError) as error:
        return None, f"unavailable ({type(error).__name__})"


def _catalog_price(catalog: dict | None, provider: str, model_id: str) -> dict | None:
    if not catalog:
        return None
    models = (catalog.get(provider) or {}).get("models") or {}
    entry = models.get(model_id) or models.get(_DATE_SUFFIX.sub("", model_id))
    cost = entry.get("cost") if isinstance(entry, dict) else None
    if not isinstance(cost, dict):
        return None
    return {field: cost[field] for field in _PRICE_FIELDS if field in cost}


def _priced_model(catalog: dict | None, provider: str, model_id: str, display_name: str | None = None) -> dict:
    price = _catalog_price(catalog, provider, model_id)
    model = {
        "id": model_id,
        "price": price,
        "price_source": "models.dev" if price else None,
        "promo": None,
    }
    if display_name:
        model["display_name"] = display_name
    return model


def claude_models(catalog: dict | None = None) -> dict:
    """Live Claude model IDs this OAuth subscription can call today."""
    try:
        with open(CLAUDE_CREDENTIALS_FILE, encoding="utf-8") as credentials_file:
            token = json.load(credentials_file)["claudeAiOauth"]["accessToken"]
        if not isinstance(token, str) or not token:
            raise ValueError("missing OAuth access token")
        data = _get_json(
            ANTHROPIC_MODELS_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "anthropic-beta": "oauth-2025-04-20",
                "anthropic-version": "2023-06-01",
            },
        )
    except (OSError, KeyError, TypeError, ValueError, urllib.error.URLError) as error:
        return {
            "subscription": "claude",
            "live": False,
            "error": f"unavailable ({type(error).__name__})",
            "models": [],
        }

    rows = data.get("data") if isinstance(data, dict) else None
    models = [
        _priced_model(catalog, "anthropic", row["id"], row.get("display_name"))
        for row in rows or []
        if isinstance(row, dict) and row.get("id")
    ]
    return {"subscription": "claude", "live": True, "source": ANTHROPIC_MODELS_URL, "models": models}


def opencode_go_models(catalog: dict | None = None) -> dict:
    """Live opencode-go model IDs this subscription can call today."""
    key = ""
    try:
        with open(OPENCODE_GO_AUTH_FILE, encoding="utf-8") as auth_file:
            auth = json.load(auth_file)
        entry = auth.get("opencode-go") or {}
        if entry.get("type") == "api":
            key = entry.get("key", "").strip()
    except (OSError, ValueError, AttributeError):
        pass
    if not key:
        key = os.environ.get("OPENCODE_API_KEY", "").strip()
    if not key:
        return {
            "subscription": "opencode-go",
            "live": False,
            "error": "no OpenCode Go API key available",
            "models": [],
        }

    try:
        data = _get_json(
            OPENCODE_GO_MODELS_URL,
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
        )
    except (OSError, ValueError, urllib.error.URLError) as error:
        return {
            "subscription": "opencode-go",
            "live": False,
            "error": f"unavailable ({type(error).__name__})",
            "models": [],
        }

    rows = data.get("data") if isinstance(data, dict) else None
    models = [
        _priced_model(catalog, "opencode-go", row["id"])
        for row in rows or []
        if isinstance(row, dict) and row.get("id")
    ]
    return {"subscription": "opencode-go", "live": True, "source": OPENCODE_GO_MODELS_URL, "models": models}


def codex_models(catalog: dict | None = None) -> dict:
    """No live source: see this module's docstring. Falls back to
    models.dev's published OpenAI catalog -- real IDs, but OpenAI's general
    API lineup, not a check of this Codex Pro subscription specifically.
    """
    if not catalog:
        return {
            "subscription": "codex",
            "live": False,
            "error": "models.dev catalog unavailable; no fallback model list either",
            "models": [],
        }
    models_raw = (catalog.get("openai") or {}).get("models") or {}
    models = [
        _priced_model(catalog, "openai", model_id)
        for model_id in models_raw
    ]
    return {
        "subscription": "codex",
        "live": False,
        "stale_reason": (
            "no ~/.codex credentials in this sandbox, and OpenAI's public model-list API "
            "needs a billing API key that a Codex Pro OAuth login doesn't provide -- these "
            "are models.dev's published OpenAI models, not a live check of this subscription"
        ),
        "source": "models.dev (openai)",
        "models": models,
    }


def snapshot() -> dict:
    """One day's record: model list + price per subscription.

    `promo` is always `None` right now -- no live source for time-boxed
    discounts turned up in this issue's research (`grep -rn -i
    "promo|discount" src/lupin/*.py` found nothing to build on, and neither
    opencode.ai, Anthropic's API, nor models.dev expose one). The field
    stays in the shape so issue #18 has somewhere real to put it.
    """
    catalog, catalog_error = fetch_price_catalog()
    return {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "price_catalog_source": MODELS_DEV_CATALOG_URL,
        "price_catalog_error": catalog_error,
        "promo_source": None,
        "subscriptions": {
            "claude": claude_models(catalog),
            "opencode-go": opencode_go_models(catalog),
            "codex": codex_models(catalog),
        },
    }


def save_snapshot(data: dict, path: str = SNAPSHOT_FILE) -> None:
    """Overwrite `path` with `data`, atomically (same pattern as
    roadmap.py's `_persist_cache`). One file, latest fetch only -- a
    history of days is issue #17's concern if it wants one, not this one's.
    """
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=directory, delete=False
    ) as handle:
        json.dump(data, handle, indent=2)
        temporary_path = handle.name
    try:
        os.replace(temporary_path, path)
    except OSError:
        os.unlink(temporary_path)
        raise
