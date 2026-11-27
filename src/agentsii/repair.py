"""One-click "Repair Codex".

Codex Desktop can fail with "Connection for host ID local not found" when its
UI state file (.codex-global-state.json) is damaged, e.g. after Codex was killed
while writing it. Repair (with Codex closed):
  1. renames the state file(s) - nothing is deleted, sign-in (auth.json) is kept
  2. asks Codex itself to load config.toml + the Agentsii model catalog and, if
     this Codex version rejects the catalog, detaches it (providers stay)
"""
from __future__ import annotations

import datetime as _dt

from . import catalog, codex
from .logutil import log

STATE_FILES = (".codex-global-state.json", ".codex-global-state.json.bak")


def run() -> dict:
    inst = codex.detect()
    home = inst.home
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    renamed = []
    for name in STATE_FILES:
        f = home / name
        if f.exists():
            dest = f.with_name(f"{name}.agentsii-repair-{stamp}")
            f.rename(dest)
            renamed.append(dest.name)
    result = {"renamed": renamed, "catalog": "unchanged", "config": "unknown"}
    try:
        doc = codex.load_config(inst.config_path)
    except Exception as e:  # noqa: BLE001
        result["config"] = f"unreadable: {e}"[:300]
        return result
    from . import bridge
    expect = [m for p in bridge.load_registry().get("providers", {}).values() for m in p.get("models") or []]
    status, detail = catalog.validate(inst.catalog_cli, home, expect if catalog.config_catalog_owner(doc) == "agentsii" else [])
    result["config"] = status
    if status == "invalid" and catalog.config_catalog_owner(doc) == "agentsii":
        changes = codex.ChangeSet(config_path=inst.config_path)
        codex.backup_config(inst, changes)
        doc.pop("model_catalog_json", None)
        codex._atomic_write(inst.config_path, __import__("tomlkit").dumps(doc))
        result["catalog"] = "detached"
        result["config"], detail = catalog.validate(inst.catalog_cli, home, [])
    result["detail"] = detail
    log.info("repair: %s", result)
    return result
