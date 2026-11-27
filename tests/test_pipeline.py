import tomllib
from pathlib import Path

import pytest

from agentsii import codex, credentials, pipeline
from agentsii.provider import ProviderError, normalize_base_url
from mock_provider import MockProvider

KEY = "sk-test-agentsii-0123456789abcdef"

EXISTING = """# my precious comment
model = "gpt-5.5"
approval_policy = "on-request"

[mcp_servers.docs]
command = "npx"   # keep me
args = ["-y", "docs-mcp"]

[model_providers.other]
name = "Other"
base_url = "https://other.example/v1"
env_key = "OTHER_KEY"
"""


class CB(pipeline.Callbacks):
    def __init__(self, approve=False):
        self.events = []
        self.approve = approve
        self.asked = False

    def stage_started(self, i): self.events.append(("start", i))
    def stage_done(self, i, d): self.events.append(("done", i))
    def stage_failed(self, i, c, d): self.events.append(("fail", i, c))

    def confirm_overwrite(self, pid, existing):
        self.asked = True
        return self.approve


def detect_factory(home: Path, version=(0, 163, 0)):
    def detect():
        return codex.CodexInstall(home=home, home_exists=True, config_path=home / "config.toml",
                                  cli_path="codex", cli_version=version, cli_version_text="codex-cli x")
    return detect


def run(url, home, key=KEY, cb=None, name="My Gateway", **kw):
    return pipeline.run(name, url, key, cb or CB(), detect_fn=detect_factory(home, **kw))


# ------------------------------------------------------------------ URL rules

@pytest.mark.parametrize("raw,expected", [
    ("https://api.x.com/v1/", "https://api.x.com/v1"),
    ("api.x.com/v1", "https://api.x.com/v1"),
    ("https://api.x.com/v1/chat/completions", "https://api.x.com/v1"),
    ("https://api.x.com/v1/models", "https://api.x.com/v1"),
    ("http://localhost:4000/v1", "http://localhost:4000/v1"),
    ("http://192.168.1.10:8000", "http://192.168.1.10:8000"),
])
def test_normalize_ok(raw, expected):
    assert normalize_base_url(raw) == expected


@pytest.mark.parametrize("raw,code", [
    ("", "url_empty"), ("ftp://x.com", "url_scheme"), ("http://api.x.com/v1", "url_insecure"),
    ("https://user:pw@x.com/v1", "url_credentials"),
])
def test_normalize_bad(raw, code):
    with pytest.raises(ProviderError) as e:
        normalize_base_url(raw)
    assert e.value.code == code


# ------------------------------------------------------------------ happy path

def test_full_success_preserves_existing_config(isolated):
    home = isolated
    cfg = home / "config.toml"
    cfg.write_text(EXISTING, encoding="utf-8")
    with MockProvider(key=KEY) as mp:
        cb = CB()
        res = run(mp.url + "/v1", home, cb=cb)
        assert res.ok, (res.error_code, res.error_detail)
        # the real compat test streamed with tools against /v1/responses
        posts = [r for r in mp.record if r["method"] == "POST" and r["path"] == "/v1/responses"]
        assert posts and all(r["stream"] and r["has_tools"] and r["auth_ok"] for r in posts)

    assert res.discovered == 5
    assert sorted(res.added) == ["agent-codex-1", "agent-pro-2"]
    assert res.default_model == "agent-codex-1"
    assert res.supports_responses and res.supports_chat
    rejected = dict(res.rejected)
    assert "text-embedding-3-small" in rejected and "chat-only-3" in rejected and "broken-4" in rejected
    assert [e for e in cb.events if e[0] == "done"] == [("done", i) for i in range(8)]

    text = cfg.read_text(encoding="utf-8")
    assert "# my precious comment" in text and "# keep me" in text
    data = tomllib.loads(text)
    assert data["model_provider"] == "my-gateway"
    assert data["model"] == "agent-codex-1"
    assert data["approval_policy"] == "on-request"
    assert data["mcp_servers"]["docs"]["command"] == "npx"
    assert data["model_providers"]["other"]["env_key"] == "OTHER_KEY"
    prov = data["model_providers"]["my-gateway"]
    assert prov["base_url"] == mp.url + "/v1"
    assert prov["wire_api"] == "responses"
    assert "env_key" not in prov and "experimental_bearer_token" not in prov
    assert prov["auth"]["args"][-2:] == ["--provider", "my-gateway"]
    assert KEY not in text

    # backup equals the original
    assert Path(res.backup_path).read_text(encoding="utf-8") == EXISTING
    # one profile per compatible model
    for m in res.added:
        p = home / f"{codex.profile_name('my-gateway', m)}.config.toml"
        pd = tomllib.loads(p.read_text())
        assert pd == {"model_provider": "my-gateway", "model": m}
    # key is in the credential store and the helper returns it
    assert credentials.get_key("my-gateway") == KEY
    cmd = prov["auth"]["command"]
    assert credentials.run_auth_helper(cmd, prov["auth"]["args"][:-2], "my-gateway") == KEY
    # key never in logs / manifest
    import logging
    logs = [h.baseFilename for h in logging.getLogger("agentsii").handlers if hasattr(h, "baseFilename")]
    assert logs
    for lf in logs:
        assert KEY not in Path(lf).read_text(encoding="utf-8")
    assert KEY not in (home / "agentsii" / "my-gateway.json").read_text(encoding="utf-8")


def test_base_without_v1_is_discovered(isolated):
    with MockProvider(key=KEY) as mp:
        res = run(mp.url, isolated)
    assert res.ok
    assert res.base_url == mp.url + "/v1"


def test_new_config_created_when_missing(isolated):
    with MockProvider(key=KEY) as mp:
        res = run(mp.url + "/v1", isolated)
    assert res.ok and res.backup_path == ""
    assert tomllib.loads((isolated / "config.toml").read_text())["model_provider"] == "my-gateway"


def test_old_codex_uses_profiles_table(isolated):
    with MockProvider(key=KEY) as mp:
        res = run(mp.url + "/v1", isolated, version=(0, 120, 0))
    assert res.ok
    data = tomllib.loads((isolated / "config.toml").read_text())
    assert data["profiles"][codex.profile_name("my-gateway", "agent-codex-1")]["model"] == "agent-codex-1"
    assert not list(isolated.glob("agentsii-*.config.toml"))


# ------------------------------------------------------------------ failures

def _snapshot(home):
    return {p.name: p.read_bytes() for p in home.iterdir() if p.is_file()}


def test_invalid_key(isolated):
    (isolated / "config.toml").write_text(EXISTING)
    before = _snapshot(isolated)
    with MockProvider(key=KEY) as mp:
        res = run(mp.url + "/v1", isolated, key="sk-wrong-key-000000")
    assert not res.ok and res.error_code == "key_invalid" and res.failed_stage == 1
    assert _snapshot(isolated) == before
    assert credentials.get_key("my-gateway") is None




def test_models_endpoint_missing(isolated):
    with MockProvider(key=KEY, models_path="/nothing") as mp:
        res = run(mp.url + "/v1", isolated)
    assert not res.ok and res.error_code == "models_endpoint_missing" and res.failed_stage == 2


def test_unreachable(isolated):
    res = run("http://127.0.0.1:9/v1", isolated)
    assert not res.ok and res.error_code in ("unreachable", "timeout") and res.failed_stage == 0


def test_codex_missing(isolated):
    def detect():
        return codex.CodexInstall(home=isolated / "nope", home_exists=False,
                                  config_path=isolated / "nope" / "config.toml")
    with MockProvider(key=KEY) as mp:
        res = pipeline.run("X", mp.url + "/v1", KEY, CB(), detect_fn=detect)
    assert not res.ok and res.error_code == "codex_missing" and res.failed_stage == 4


def test_existing_provider_declined_keeps_everything(isolated):
    (isolated / "config.toml").write_text(EXISTING)
    before = _snapshot(isolated)
    with MockProvider(key=KEY) as mp:
        cb = CB(approve=False)
        res = run(mp.url + "/v1", isolated, cb=cb, name="Other")
    assert cb.asked and not res.ok and res.error_code == "overwrite_declined"
    assert _snapshot(isolated) == before
    assert credentials.get_key("other") is None


def test_existing_provider_approved_replaces_only_it(isolated):
    (isolated / "config.toml").write_text(EXISTING)
    with MockProvider(key=KEY) as mp:
        res = run(mp.url + "/v1", isolated, cb=CB(approve=True), name="Other")
    assert res.ok
    data = tomllib.loads((isolated / "config.toml").read_text())
    assert data["model_providers"]["other"]["base_url"] == mp.url + "/v1"
    assert "env_key" not in data["model_providers"]["other"]
    assert data["mcp_servers"]["docs"]["command"] == "npx"


def test_rollback_when_credential_store_fails(isolated, monkeypatch):
    (isolated / "config.toml").write_text(EXISTING)
    before = _snapshot(isolated)

    def boom(pid, key):
        raise credentials.CredentialError("vault locked")
    monkeypatch.setattr(credentials, "store_key", boom)
    with MockProvider(key=KEY) as mp:
        res = run(mp.url + "/v1", isolated)
    assert not res.ok and res.error_code == "credential_failed" and res.failed_stage == 6
    assert (isolated / "config.toml").read_bytes() == before["config.toml"]
    assert not list(isolated.glob("agentsii-*.config.toml"))
    assert not (isolated / "agentsii" / "my-gateway.json").exists()


def test_unparsable_config_is_never_touched(isolated):
    (isolated / "config.toml").write_text("this is = = not toml [")
    before = _snapshot(isolated)
    with MockProvider(key=KEY) as mp:
        res = run(mp.url + "/v1", isolated)
    assert not res.ok and res.error_code == "config_unreadable"
    assert _snapshot(isolated) == before


def test_reserved_provider_ids():
    assert codex.provider_id_from_name("OpenAI") == "openai-custom"
    assert codex.provider_id_from_name("  My Cool  Gateway!! ") == "my-cool-gateway"
    assert codex.provider_id_from_name("عربي") == "provider"
