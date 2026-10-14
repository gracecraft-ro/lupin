import json

import pytest

from lupin import model_tiers, route


def _write_tiers(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def test_setting_selects_a_file_and_defaults_to_the_bundled_file(tmp_path, monkeypatch):
    monkeypatch.delenv(model_tiers.SETTING, raising=False)
    assert model_tiers.path() == model_tiers.default_path()

    custom = tmp_path / "my-tiers.json"
    monkeypatch.setenv(model_tiers.SETTING, str(custom))
    assert model_tiers.path() == str(custom)


def test_route_uses_the_configured_file_and_reports_a_missing_category(tmp_path, monkeypatch):
    custom = tmp_path / "my-tiers.json"
    _write_tiers(custom, {
        "coding": {"tiers": {"tier1": [{"model": "opencode-go/custom", "effort": "high"}]}}
    })
    monkeypatch.setenv(model_tiers.SETTING, str(custom))

    assert route.route("coding", "size-m", quota_rows=[]) == {
        "model": "opencode-go/custom", "effort": "high"
    }
    with pytest.raises(route.RouteInputError, match="unknown category 'prose'") as caught:
        route.route("prose", "size-m", quota_rows=[])
    assert str(custom) in str(caught.value)
    assert "coding" in str(caught.value)


@pytest.mark.parametrize(
    ("value", "detail"),
    [
        ([], "expected a JSON object"),
        ({"coding": {}}, "needs a non-empty 'tiers' object"),
        ({"coding": {"tiers": {"tier1": [{"model": "custom"}]}}}, "needs an effort"),
        ({"coding": {"tiers": {"tier1": [{"model": "custom", "effort": []}]}}}, "needs an effort"),
    ],
)
def test_load_rejects_bad_shapes_with_the_path(tmp_path, monkeypatch, value, detail):
    custom = tmp_path / "bad-tiers.json"
    _write_tiers(custom, value)
    monkeypatch.setenv(model_tiers.SETTING, str(custom))

    with pytest.raises(model_tiers.ModelTiersError, match=detail) as caught:
        model_tiers.load()
    assert str(custom) in str(caught.value)
