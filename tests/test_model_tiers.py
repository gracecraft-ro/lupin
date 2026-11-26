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
    with pytest.raises(route.RouteInputError, match="no tier at or above 'tier2'") as caught:
        route.route("coding", "size-l", quota_rows=[])
    assert str(custom) in str(caught.value)


def test_unavailable_bmo_reports_missing_fallback_tier(tmp_path, monkeypatch):
    custom = tmp_path / "bmo-tiers.json"
    _write_tiers(custom, {
        "coding": {"tiers": {"tier0": [{"model": "bmo:qwen", "effort": "low"}]}}
    })
    monkeypatch.setenv(model_tiers.SETTING, str(custom))

    with pytest.raises(route.RouteInputError, match="no tier at or above 'tier1'") as caught:
        route.route("coding", "size-xs", bmo_available=False, quota_rows=[])
    assert str(custom) in str(caught.value)


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


def _preference_file(config_home):
    target = config_home / "lupin" / "model-tiers.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def test_preference_file_is_used_when_the_setting_is_unset(tmp_path, monkeypatch):
    monkeypatch.delenv(model_tiers.SETTING, raising=False)
    target = _preference_file(tmp_path)
    _write_tiers(target, {"coding": {"tiers": {"tier1": [{"model": "sonnet", "effort": "high"}]}}})
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    assert model_tiers.path() == str(target)


def test_the_setting_wins_over_the_preference_file(tmp_path, monkeypatch):
    target = _preference_file(tmp_path)
    _write_tiers(target, {"coding": {"tiers": {"tier1": [{"model": "sonnet", "effort": "high"}]}}})
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    chosen = tmp_path / "chosen.json"
    monkeypatch.setenv(model_tiers.SETTING, str(chosen))

    assert model_tiers.path() == str(chosen)


def test_a_missing_preference_file_uses_the_bundled_file(tmp_path, monkeypatch):
    monkeypatch.delenv(model_tiers.SETTING, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    assert model_tiers.path() == model_tiers.default_path()


def test_a_directory_at_the_preference_path_is_ignored(tmp_path, monkeypatch):
    monkeypatch.delenv(model_tiers.SETTING, raising=False)
    target = _preference_file(tmp_path)
    target.mkdir(parents=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    assert model_tiers.path() == model_tiers.default_path()


def test_an_explicit_default_is_kept_when_no_preference_exists(tmp_path, monkeypatch):
    monkeypatch.delenv(model_tiers.SETTING, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    assert model_tiers.path("/tmp/x.json") == "/tmp/x.json"


def test_route_reads_the_preference_file(tmp_path, monkeypatch):
    monkeypatch.delenv(model_tiers.SETTING, raising=False)
    target = _preference_file(tmp_path)
    _write_tiers(target, {
        "coding": {"tiers": {"tier1": [{"model": "opencode-go/custom", "effort": "high"}]}}
    })
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    assert route.route("coding", "size-m", quota_rows=[]) == {
        "model": "opencode-go/custom", "effort": "high"
    }
