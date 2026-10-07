"""Tests for `model_fetch.py` -- the daily model-list/price snapshot
(issue #16). Network calls are mocked; `tests/test_quota.py` has the same
pattern for the credential files and `urlopen` calls this module reuses.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from unittest import mock

from lupin import cli, model_fetch

CATALOG = {
    "anthropic": {
        "models": {
            "claude-opus-4-5": {"cost": {"input": 5, "output": 25, "cache_read": 0.5}},
        }
    },
    "opencode-go": {
        "models": {
            "deepseek-v4-flash": {"cost": {"input": 0, "output": 0}},
        }
    },
    "openai": {
        "models": {
            "gpt-5.4": {"cost": {"input": 2.5, "output": 15}},
        }
    },
}


class CatalogPriceTests(unittest.TestCase):
    def test_exact_id_match(self):
        price = model_fetch._catalog_price(CATALOG, "openai", "gpt-5.4")
        self.assertEqual(price, {"input": 2.5, "output": 15})

    def test_date_suffix_is_stripped_for_matching(self):
        price = model_fetch._catalog_price(CATALOG, "anthropic", "claude-opus-4-5-20251101")
        self.assertEqual(price, {"input": 5, "output": 25, "cache_read": 0.5})

    def test_no_match_is_none(self):
        self.assertIsNone(model_fetch._catalog_price(CATALOG, "anthropic", "claude-made-up"))

    def test_no_catalog_is_none(self):
        self.assertIsNone(model_fetch._catalog_price(None, "anthropic", "claude-opus-4-5"))


class FetchPriceCatalogTests(unittest.TestCase):
    def test_success_returns_catalog_no_error(self):
        with mock.patch.object(
            model_fetch.urllib.request,
            "urlopen",
            return_value=io.BytesIO(json.dumps(CATALOG).encode()),
        ):
            catalog, error = model_fetch.fetch_price_catalog()
        self.assertEqual(catalog, CATALOG)
        self.assertIsNone(error)

    def test_failure_returns_none_and_error(self):
        with mock.patch.object(
            model_fetch.urllib.request, "urlopen", side_effect=OSError("boom")
        ):
            catalog, error = model_fetch.fetch_price_catalog()
        self.assertIsNone(catalog)
        self.assertIn("unavailable", error)


class ClaudeModelsTests(unittest.TestCase):
    def test_live_list_priced_from_catalog(self):
        payload = {"data": [{"id": "claude-opus-4-5-20251101", "display_name": "Claude Opus 4.5"}]}
        with (
            mock.patch(
                "builtins.open",
                mock.mock_open(read_data=json.dumps({"claudeAiOauth": {"accessToken": "test-token"}})),
            ),
            mock.patch.object(
                model_fetch.urllib.request,
                "urlopen",
                return_value=io.BytesIO(json.dumps(payload).encode()),
            ) as fetch,
        ):
            result = model_fetch.claude_models(CATALOG)

        self.assertTrue(result["live"])
        self.assertEqual(
            result["models"],
            [{
                "id": "claude-opus-4-5-20251101",
                "price": {"input": 5, "output": 25, "cache_read": 0.5},
                "price_source": "models.dev",
                "promo": None,
                "display_name": "Claude Opus 4.5",
            }],
        )
        self.assertEqual(
            fetch.call_args.args[0].get_header("Authorization"), "Bearer test-token"
        )

    def test_missing_credentials_file_is_not_live(self):
        with mock.patch("builtins.open", side_effect=FileNotFoundError):
            result = model_fetch.claude_models(CATALOG)
        self.assertFalse(result["live"])
        self.assertEqual(result["models"], [])
        self.assertIn("error", result)


class OpencodeGoModelsTests(unittest.TestCase):
    def test_live_list_priced_from_catalog(self):
        payload = {"data": [{"id": "deepseek-v4-flash"}]}
        with (
            mock.patch.dict(os.environ, {"OPENCODE_API_KEY": ""}),
            mock.patch(
                "builtins.open",
                mock.mock_open(read_data=json.dumps({"opencode-go": {"type": "api", "key": "test-key"}})),
            ),
            mock.patch.object(
                model_fetch.urllib.request,
                "urlopen",
                return_value=io.BytesIO(json.dumps(payload).encode()),
            ) as fetch,
        ):
            result = model_fetch.opencode_go_models(CATALOG)

        self.assertTrue(result["live"])
        self.assertEqual(
            result["models"],
            [{"id": "deepseek-v4-flash", "price": {"input": 0, "output": 0}, "price_source": "models.dev", "promo": None}],
        )
        self.assertEqual(fetch.call_args.args[0].get_header("Authorization"), "Bearer test-key")

    def test_no_key_anywhere_is_not_live(self):
        with (
            mock.patch.dict(os.environ, {"OPENCODE_API_KEY": ""}),
            mock.patch("builtins.open", side_effect=FileNotFoundError),
        ):
            result = model_fetch.opencode_go_models(CATALOG)
        self.assertFalse(result["live"])
        self.assertEqual(result["models"], [])


class CodexModelsTests(unittest.TestCase):
    def test_falls_back_to_catalog_openai_models_marked_not_live(self):
        result = model_fetch.codex_models(CATALOG)
        self.assertFalse(result["live"])
        self.assertIn("stale_reason", result)
        self.assertEqual(
            result["models"],
            [{"id": "gpt-5.4", "price": {"input": 2.5, "output": 15}, "price_source": "models.dev", "promo": None}],
        )

    def test_no_catalog_at_all_is_empty_not_live(self):
        result = model_fetch.codex_models(None)
        self.assertFalse(result["live"])
        self.assertEqual(result["models"], [])
        self.assertIn("error", result)


class SnapshotTests(unittest.TestCase):
    def test_combines_all_three_subscriptions_and_catalog_error(self):
        with (
            mock.patch.object(model_fetch, "fetch_price_catalog", return_value=(None, "unavailable (OSError)")),
            mock.patch.object(
                model_fetch, "claude_models", return_value={"subscription": "claude", "live": False, "models": []}
            ),
            mock.patch.object(
                model_fetch,
                "opencode_go_models",
                return_value={"subscription": "opencode-go", "live": False, "models": []},
            ),
            mock.patch.object(
                model_fetch, "codex_models", return_value={"subscription": "codex", "live": False, "models": []}
            ),
        ):
            data = model_fetch.snapshot()

        self.assertEqual(data["price_catalog_error"], "unavailable (OSError)")
        self.assertIsNone(data["promo_source"])
        self.assertEqual(set(data["subscriptions"]), {"claude", "opencode-go", "codex"})
        self.assertIn("fetched_at", data)


class SaveSnapshotTests(unittest.TestCase):
    def test_round_trips_through_disk(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = os.path.join(tempdir, "nested", "model-snapshot.json")
            data = {"fetched_at": "2026-10-07T00:00:00+00:00", "subscriptions": {}}

            model_fetch.save_snapshot(data, path=path)

            with open(path, encoding="utf-8") as handle:
                self.assertEqual(json.load(handle), data)

    def test_overwrites_existing_file(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = os.path.join(tempdir, "model-snapshot.json")
            model_fetch.save_snapshot({"n": 1}, path=path)
            model_fetch.save_snapshot({"n": 2}, path=path)

            with open(path, encoding="utf-8") as handle:
                self.assertEqual(json.load(handle), {"n": 2})


class CliFetchModelsTests(unittest.TestCase):
    def test_cli_writes_snapshot_and_prints_summary(self):
        fixed_snapshot = {
            "fetched_at": "2026-10-07T00:00:00+00:00",
            "price_catalog_source": model_fetch.MODELS_DEV_CATALOG_URL,
            "price_catalog_error": None,
            "promo_source": None,
            "subscriptions": {
                "claude": {"subscription": "claude", "live": True, "models": [{"id": "claude-opus-4-5"}]},
                "opencode-go": {"subscription": "opencode-go", "live": False, "models": []},
                "codex": {"subscription": "codex", "live": False, "models": []},
            },
        }
        with tempfile.TemporaryDirectory() as tempdir:
            snapshot_path = os.path.join(tempdir, "model-snapshot.json")
            with mock.patch.object(model_fetch, "snapshot", return_value=fixed_snapshot):
                code = cli.main(["fetch-models", "--snapshot-file", snapshot_path])

            self.assertEqual(code, 0)
            with open(snapshot_path, encoding="utf-8") as handle:
                self.assertEqual(json.load(handle), fixed_snapshot)

    def test_no_write_skips_saving(self):
        fixed_snapshot = {
            "fetched_at": "2026-10-07T00:00:00+00:00",
            "subscriptions": {
                "claude": {"subscription": "claude", "live": True, "models": []},
                "opencode-go": {"subscription": "opencode-go", "live": False, "models": []},
                "codex": {"subscription": "codex", "live": False, "models": []},
            },
        }
        with tempfile.TemporaryDirectory() as tempdir:
            snapshot_path = os.path.join(tempdir, "model-snapshot.json")
            with mock.patch.object(model_fetch, "snapshot", return_value=fixed_snapshot):
                code = cli.main(["fetch-models", "--snapshot-file", snapshot_path, "--no-write"])

            self.assertEqual(code, 0)
            self.assertFalse(os.path.exists(snapshot_path))


if __name__ == "__main__":
    unittest.main()
