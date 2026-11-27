"""Codex model picker integration.

Codex (CLI and Desktop) builds its model picker from a model catalog. Setting
`model_catalog_json` replaces the bundled catalog, so Agentsii takes Codex's own
bundled catalog (`codex debug models --bundled`), keeps every built-in model and
appends the provider models. Provider models also get proper metadata, so
Codex stops falling back to "unknown model" defaults.

`model_provider` is kept in sync with the model picked in Codex by the
Agentsii background service (see tray.py / reconcile()).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import tomlkit

from .logutil import log

CATALOG_FILE = "agentsii-model-catalog.json"
DEFAULT_CONTEXT = 128_000
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def cli_command(cli: str) -> list[str]:
    low = cli.lower()
    if low.endswith((".cmd", ".bat")):
        return [os.environ.get("ComSpec", "cmd.exe"), "/d", "/c", cli]
    if low.endswith(".ps1"):
        return ["powershell", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", cli]
    return [cli]


def bundled_catalog(cli: str | None) -> dict | None:
    if not cli:
        return None
    import tempfile
    try:
        # Empty CODEX_HOME: read the client's static defaults only, never account caches.
        with tempfile.TemporaryDirectory(prefix="agentsii-bundled-") as tmp:
            env = dict(os.environ, CODEX_HOME=tmp)
            for name in ("OPENAI_API_KEY", "CODEX_API_KEY"):
                env.pop(name, None)
            out = subprocess.run(cli_command(cli) + ["debug", "models", "--bundled"], capture_output=True,
                                 timeout=30, stdin=subprocess.DEVNULL, env=env, cwd=tmp,
                                 creationflags=_NO_WINDOW)
        if out.returncode != 0:
            log.info("codex debug models failed: %s", out.stderr[:300])
            return None
        data = json.loads(out.stdout.decode("utf-8", errors="replace"))
        if isinstance(data, dict) and isinstance(data.get("models"), list) and data["models"]:
            return data
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        log.info("codex bundled catalog unavailable: %s", e)
    return None


def display_name(provider_name: str, model: str) -> str:
    spaced = re.sub(r"\s+", " ", re.sub(r"[_\-/]+", " ", model)).strip() or model
    return spaced if model.lower().startswith(provider_name.lower()) else f"{provider_name} · {model}"


def _template(models: list) -> dict | None:
    for m in models:
        if isinstance(m, dict) and m.get("visibility") == "list" and m.get("supported_in_api") and m.get("slug"):
            return m
    return next((m for m in models if isinstance(m, dict) and m.get("slug")), None)


def catalog_entry(provider_name: str, model: str, template: dict | None, priority: int,
                  context_window: int | None) -> dict:
    """A provider model entry built from one of Codex's OWN bundled entries, so the
    field set always matches the Codex version that will read it. Only values are
    changed: no reasoning levels, no speed tiers, text input, its own context."""
    ctx = max(4096, min(int(context_window or DEFAULT_CONTEXT), 4_000_000))
    if template is None:
        e = {"base_instructions": "You are a coding assistant. Use the provided tools to inspect and change files.",
             "shell_type": "unified_exec", "supports_reasoning_summary_parameter": False,
             "default_reasoning_summary": "none", "support_verbosity": False, "default_verbosity": None,
             "apply_patch_tool_type": None, "truncation_policy": {"mode": "tokens", "limit": 10000},
             "effective_context_window_percent": 90, "experimental_supported_tools": [],
             "additional_speed_tiers": [], "service_tiers": [], "default_service_tier": None,
             "availability_nux": None, "upgrade": None, "context_window": ctx,
             "input_modalities": ["text"], "default_reasoning_level": None, "supported_reasoning_levels": []}
    else:
        e = json.loads(json.dumps(template))
    e.update(slug=model, display_name=display_name(provider_name, model),
             description=f"{provider_name} model added by Agentsii.", visibility="list", supported_in_api=True)
    overrides = {
        "priority": priority, "upgrade": None, "availability_nux": None,
        "default_reasoning_level": None, "supported_reasoning_levels": [],
        "context_window": ctx, "max_context_window": ctx, "input_modalities": ["text"],
        "service_tiers": [], "additional_speed_tiers": [], "default_service_tier": None,
        "supports_reasoning_summaries": False, "supports_reasoning_summary_parameter": False,
        "default_reasoning_summary": "none", "support_verbosity": False, "default_verbosity": None,
        "experimental_supported_tools": [], "supports_search_tool": False,
    }
    for k, v in overrides.items():
        if k in e:
            e[k] = v
    return e


def build_catalog(bundled: dict, providers: dict) -> tuple[dict, int]:
    """providers: registry providers {id: {"name", "models", "context": {model: n}}}."""
    catalog = json.loads(json.dumps(bundled))
    models = catalog["models"]
    slugs = {m.get("slug") for m in models if isinstance(m, dict)}
    template = _template(models)
    priority = max([m.get("priority", 0) for m in models if isinstance(m, dict) and isinstance(m.get("priority"), int)] or [0]) + 100
    added = 0
    for pid in sorted(providers):
        p = providers[pid]
        for model in sorted(p.get("models") or []):
            if model in slugs:
                continue
            slugs.add(model)
            models.append(catalog_entry(p.get("name") or pid, model, template, priority,
                                        (p.get("context") or {}).get(model)))
            priority += 1
            added += 1
    return catalog, added


def validate(cli: str | None, home: Path, expect: list[str]) -> tuple[str, str]:
    """Ask Codex itself to load config.toml + the catalog.
    Returns ("ok"|"invalid"|"unknown", detail)."""
    if not cli:
        return "unknown", "no codex binary"
    try:
        env = dict(os.environ, CODEX_HOME=str(home))
        out = subprocess.run(cli_command(cli) + ["debug", "models"], capture_output=True, timeout=45,
                             stdin=subprocess.DEVNULL, env=env, cwd=str(home), creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as e:
        return "unknown", str(e)[:200]
    text = out.stdout.decode("utf-8", "replace")
    err = out.stderr.decode("utf-8", "replace")
    if out.returncode == 0:
        try:
            slugs = {m.get("slug") for m in json.loads(text).get("models", []) if isinstance(m, dict)}
        except ValueError:
            return "unknown", "unparsable output"
        missing = [m for m in expect if m not in slugs]
        return ("ok", "") if not missing else ("invalid", f"models missing from Codex catalog: {missing[:5]}")
    low = err.lower()
    if any(k in low for k in ("catalog", "config", "toml", "parse", "invalid", "deserializ", "unknown field", "missing field")):
        return "invalid", err.strip()[-400:]
    return "unknown", err.strip()[-300:]


def catalog_path(home: Path) -> Path:
    return home / CATALOG_FILE


def config_catalog_owner(doc) -> str:
    """'none', 'agentsii' or 'user' (someone else's catalog: never touched)."""
    val = doc.get("model_catalog_json")
    if val is None:
        return "none"
    return "agentsii" if str(val).replace("\\", "/").endswith(CATALOG_FILE) else "user"


def set_catalog_key(doc, path: Path) -> None:
    doc["model_catalog_json"] = str(path)


# ------------------------------------------------------------------ model_provider sync

def resolve_provider(model: str, current: str | None, providers: dict, bundled_slugs: set[str]) -> str | None:
    """Return the provider id that should serve `model`, '' for Codex default
    (openai), or None if nothing should change."""
    owners = [pid for pid, p in providers.items() if model in (p.get("models") or [])]
    if current in owners:
        return None
    if owners:
        return sorted(owners)[0]
    if current in providers and model in bundled_slugs:
        return ""
    return None


def reconcile(config_path: Path, providers: dict, bundled_slugs: set[str]) -> bool:
    if not config_path.exists():
        return False
    import tomllib
    text = config_path.read_text(encoding="utf-8")
    try:
        data = tomllib.loads(text)
    except ValueError:
        return False
    model = data.get("model")
    if not isinstance(model, str):
        return False
    target = resolve_provider(model, data.get("model_provider"), providers, bundled_slugs)
    if target is None:
        return False
    doc = tomlkit.parse(text)
    if target == "":
        doc.pop("model_provider", None)
    else:
        doc["model_provider"] = target
    new = tomlkit.dumps(doc)
    tomllib.loads(new)
    tmp = config_path.with_name(config_path.name + ".agentsii-tmp")
    tmp.write_text(new, encoding="utf-8", newline="\n")
    os.replace(tmp, config_path)
    log.info("model picker sync: model=%s -> provider=%s", model, target or "openai")
    return True
