import json
import os
import stat
import sys
import tomllib

import pytest

from agentsii import bridge, catalog, cleanup, codex, credentials, pipeline
from mock_provider import MockProvider
from test_pipeline import CB, KEY

BUNDLED = {"models": [{"slug": "gpt-5.5", "display_name": "GPT-5.5", "base_instructions": "BASE",
                       "visibility": "list", "supported_in_api": True}]}

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="fake CLI is a POSIX script")


def _make_cli(tmp_path, reject=False):
    p = tmp_path / ("codex-reject" if reject else "codex")
    effective = ('echo "Error: failed to load model catalog: unknown field" >&2; exit 1' if reject
                 else 'cat "$CODEX_HOME/agentsii-model-catalog.json"; exit 0')
    p.write_text("#!/bin/sh\n"
                 "if [ \"$1 $2 $3\" = \"debug models --bundled\" ]; then cat <<'J'\n" + json.dumps(BUNDLED) + "\nJ\nexit 0; fi\n"
                 "if [ \"$1 $2\" = \"debug models\" ]; then " + effective + "; fi\nexit 2\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


@pytest.fixture
def fake_cli(tmp_path):
    return _make_cli(tmp_path)


def detect(home, cli):
    return lambda: codex.CodexInstall(home=home, home_exists=True, config_path=home / "config.toml",
                                      cli_path=cli, cli_version=(0, 172, 0), cli_version_text="codex-cli 0.172.0")


def test_catalog_published_and_synced(isolated, fake_cli):
    home = isolated
    (home / "config.toml").write_text('model = "gpt-5.5"\n[mcp_servers.x]\ncommand = "y"\n')
    with MockProvider(key=KEY) as mp:
        res = pipeline.run("My Gateway", mp.url + "/v1", KEY, CB(), detect_fn=detect(home, fake_cli))
    assert res.ok and res.picker == "updated" and res.picker_check == "ok" and res.service_running
    cfg = tomllib.loads((home / "config.toml").read_text())
    cat_path = cfg["model_catalog_json"]
    cat = json.loads(open(cat_path).read())
    slugs = [m["slug"] for m in cat["models"]]
    assert slugs[0] == "gpt-5.5" and {"agent-codex-1", "agent-pro-2"} <= set(slugs)
    entry = next(m for m in cat["models"] if m["slug"] == "agent-pro-2")
    assert entry["base_instructions"] == "BASE" and entry["display_name"].startswith("My Gateway")
    assert cfg["mcp_servers"]["x"]["command"] == "y"

    providers = bridge.load_registry()["providers"]
    cfgp = home / "config.toml"
    # user picks a built-in model in Codex -> back to default provider
    doc = cfgp.read_text().replace('model = "agent-codex-1"', 'model = "gpt-5.5"')
    cfgp.write_text(doc)
    assert catalog.reconcile(cfgp, providers, {"gpt-5.5"})
    assert "model_provider" not in tomllib.loads(cfgp.read_text())
    # user picks a provider model -> provider set
    cfgp.write_text(cfgp.read_text().replace('model = "gpt-5.5"', 'model = "agent-pro-2"'))
    assert catalog.reconcile(cfgp, providers, {"gpt-5.5"})
    assert tomllib.loads(cfgp.read_text())["model_provider"] == "my-gateway"
    assert not catalog.reconcile(cfgp, providers, {"gpt-5.5"})

    # uninstall cleanup removes only Agentsii things
    assert cleanup.run() == 0
    after = tomllib.loads(cfgp.read_text())
    assert "my-gateway" not in after.get("model_providers", {})
    assert "model_catalog_json" not in after and "model_provider" not in after
    assert after["mcp_servers"]["x"]["command"] == "y"
    assert credentials.get_key("my-gateway") is None
    assert not list(home.glob("agentsii-*.config.toml"))


def test_user_catalog_is_never_replaced(isolated, fake_cli):
    home = isolated
    (home / "config.toml").write_text('model_catalog_json = "C:/mine/catalog.json"\n')
    with MockProvider(key=KEY) as mp:
        res = pipeline.run("GW", mp.url + "/v1", KEY, CB(), detect_fn=detect(home, fake_cli))
    assert res.ok and res.picker == "user_catalog"
    assert tomllib.loads((home / "config.toml").read_text())["model_catalog_json"] == "C:/mine/catalog.json"


def test_catalog_rejected_by_codex_is_detached(isolated, tmp_path):
    cli = _make_cli(tmp_path, reject=True)
    with MockProvider(key=KEY) as mp:
        res = pipeline.run("GW", mp.url + "/v1", KEY, CB(), detect_fn=detect(isolated, cli))
    assert res.ok and res.picker == "rejected" and res.picker_check == "invalid"
    cfg = tomllib.loads((isolated / "config.toml").read_text())
    assert "model_catalog_json" not in cfg and cfg["model_provider"] == "gw"
    assert not catalog.catalog_path(isolated).exists()


def test_catalog_entry_uses_codex_template():
    tpl = {"slug": "gpt-x", "visibility": "list", "supported_in_api": True, "base_instructions": "B",
           "default_reasoning_level": "medium", "supported_reasoning_levels": [{"effort": "low"}],
           "context_window": 272000, "weird_new_field": {"keep": 1}, "priority": 3}
    cat, n = catalog.build_catalog({"models": [tpl]}, {"p": {"name": "P", "models": ["m1"], "context": {"m1": 64000}}})
    e = cat["models"][1]
    assert n == 1 and e["slug"] == "m1" and e["weird_new_field"] == {"keep": 1}
    assert e["default_reasoning_level"] is None and e["supported_reasoning_levels"] == []
    assert e["context_window"] == 64000 and e["priority"] > 3 and "max_context_window" not in e


def test_repair_renames_state_and_keeps_auth(isolated, monkeypatch):
    from agentsii import repair
    (isolated / ".codex-global-state.json").write_text("{broken")
    (isolated / "auth.json").write_text("{}")
    monkeypatch.setattr(codex, "detect", lambda: codex.CodexInstall(home=isolated, home_exists=True,
                                                                    config_path=isolated / "config.toml"))
    r = repair.run()
    assert not (isolated / ".codex-global-state.json").exists()
    assert len(r["renamed"]) == 1 and (isolated / "auth.json").exists()
