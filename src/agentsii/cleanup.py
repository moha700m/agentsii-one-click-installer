"""Uninstall cleanup: detach everything Agentsii added, keep everything else.

Run by the Windows uninstaller (`AgentsiiInstaller.exe --uninstall-cleanup`).
Backs config.toml up first, removes Agentsii providers, switches an active
Agentsii provider back to Codex's default, removes the Agentsii model catalog
and profile files, deletes the stored keys, stops the background service and
removes autostart. Account login, auth.json, MCP servers and backups are kept.
"""
from __future__ import annotations

import shutil
import tomllib

import tomlkit

from . import bridge, catalog, codex, credentials, system
from .logutil import log


def run() -> int:
    reg = bridge.load_registry()
    ids = set(reg.get("providers", {}))
    inst_home = codex.codex_home()
    cfg = inst_home / "config.toml"
    ok = True
    if cfg.exists():
        try:
            text = cfg.read_text(encoding="utf-8")
            tomllib.loads(text)
            changes = codex.ChangeSet(config_path=cfg)
            codex.backup_config(codex.CodexInstall(home=inst_home, home_exists=True, config_path=cfg), changes)
            doc = tomlkit.parse(text)
            provs = doc.get("model_providers") or {}
            for pid in list(provs.keys()):
                tbl = provs[pid]
                auth = tbl.get("auth") if hasattr(tbl, "get") else None
                cmd = str(auth.get("command", "")) if auth is not None else ""
                if pid in ids or "agentsii-auth" in cmd.lower():
                    ids.add(pid)
                    del provs[pid]
            if doc.get("model_provider") in ids:
                doc.pop("model_provider", None)
                doc.pop("model", None)
            if catalog.config_catalog_owner(doc) == "agentsii":
                doc.pop("model_catalog_json", None)
            prof = doc.get("profiles")
            if prof is not None:
                for name in list(prof.keys()):
                    if name.startswith("agentsii-"):
                        del prof[name]
            new = tomlkit.dumps(doc)
            tomllib.loads(new)
            cfg.write_text(new, encoding="utf-8", newline="\n")
        except Exception as e:  # noqa: BLE001
            log.error("cleanup: config not changed: %s", e)
            ok = False
    for f in inst_home.glob("agentsii-*.config.toml"):
        if codex.PROFILE_MARKER in f.read_text(encoding="utf-8", errors="ignore"):
            f.unlink(missing_ok=True)
    catalog.catalog_path(inst_home).unlink(missing_ok=True)
    shutil.rmtree(inst_home / "agentsii", ignore_errors=True)
    for pid in ids:
        credentials.restore_key(pid, None)
    system.stop_service()
    system.set_autostart(False)
    reg["providers"] = {}
    bridge.save_registry(reg)
    log.info("uninstall cleanup finished ok=%s providers=%s", ok, sorted(ids))
    return 0 if ok else 1
