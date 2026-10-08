"""v1.1 T5: the new optimizer settings (availability_curve, minutes_model)."""
import json

import pytest
from fastapi.testclient import TestClient

from app import config as cfgmod
from app.main import create_app


@pytest.fixture()
def client(db_path):
    return TestClient(create_app())


@pytest.fixture()
def hermetic_config(monkeypatch, tmp_path):
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", tmp_path / "config.json")
    for var in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        monkeypatch.delenv(var, raising=False)
    return cfgmod


def test_get_settings_has_new_blocks_with_defaults(client, hermetic_config):
    o = client.get("/api/settings").json()["optimizer"]
    assert o["availability_curve"] == {"play_75": 0.60, "play_50": 0.50, "play_25": 0.05}
    assert o["minutes_model"] == {"enabled": True, "weight": 0.5}
    # the previously UI-less buckets are part of the payload too
    assert {"chance_75", "chance_25"} <= set(o["availability"])


def test_put_settings_persists_new_values(client, hermetic_config):
    d = client.get("/api/settings").json()
    d["optimizer"]["availability_curve"].update(play_75=0.7, play_50=0.4, play_25=0.1)
    d["optimizer"]["minutes_model"].update(enabled=False, weight=0.25)
    d["optimizer"]["availability"].update(chance_75=0.8, chance_25=0.3)
    r = client.put("/api/settings", json=d)
    assert r.status_code == 200, r.text
    saved = json.loads(hermetic_config.CONFIG_PATH.read_text(encoding="utf-8"))["optimizer"]
    assert saved["availability_curve"] == {"play_75": 0.7, "play_50": 0.4, "play_25": 0.1}
    assert saved["minutes_model"] == {"enabled": False, "weight": 0.25}
    assert saved["availability"]["chance_75"] == 0.8 and saved["availability"]["chance_25"] == 0.3
    again = client.get("/api/settings").json()["optimizer"]
    assert again["availability_curve"]["play_75"] == 0.7
    assert again["minutes_model"]["enabled"] is False


@pytest.mark.parametrize("path,value", [
    (("availability_curve", "play_75"), 1.5),
    (("availability_curve", "play_25"), -0.1),
    (("minutes_model", "weight"), 1.01),
    (("minutes_model", "weight"), -0.5),
])
def test_put_settings_rejects_out_of_range(client, hermetic_config, path, value):
    d = client.get("/api/settings").json()
    d["optimizer"][path[0]][path[1]] = value
    before = hermetic_config.load_config().model_dump()
    r = client.put("/api/settings", json=d)
    assert r.status_code == 422
    # FastAPI reports {loc, msg}; the field is named in loc
    assert path[1] in json.dumps(r.json()["detail"])
    assert hermetic_config.load_config().model_dump() == before      # nothing saved


def test_config_without_new_keys_loads_with_defaults(hermetic_config):
    hermetic_config.CONFIG_PATH.write_text(
        json.dumps({"optimizer": {"weights": {"ep": 0.59, "form": 0.24, "fixture": 0.17}}}),
        encoding="utf-8")
    c = hermetic_config.load_config()
    assert c.optimizer.weights.ep == 0.59
    assert c.optimizer.availability_curve.play_75 == 0.60
    assert c.optimizer.minutes_model.weight == 0.5


def test_invalid_config_is_backed_up_not_lost(hermetic_config):
    """A hand-edited out-of-range value falls back to defaults, but the file the
    user wrote is kept next to config.json."""
    bad = {"optimizer": {"minutes_model": {"weight": 7}}, "llm": {"api_key": "sk-secret"}}
    hermetic_config.CONFIG_PATH.write_text(json.dumps(bad), encoding="utf-8")
    c = hermetic_config.load_config()
    assert c.optimizer.minutes_model.weight == 0.5
    kept = hermetic_config.CONFIG_PATH.with_name("config.json.invalid")
    assert json.loads(kept.read_text(encoding="utf-8")) == bad
