"""Codex detection and config.toml management.

Follows the current Codex configuration reference:
  * user config lives in $CODEX_HOME/config.toml (default ~/.codex)
  * custom providers: [model_providers.<id>] with name, base_url, wire_api
  * `wire_api = "responses"` is the only supported value
  * command-backed auth: [model_providers.<id>.auth] command/args/timeout_ms/
    refresh_interval_ms; must not be combined with env_key,
    experimental_bearer_token or requires_openai_auth
  * profiles (Codex >= 0.134): $CODEX_HOME/<name>.config.toml with top-level keys,
    selected with `codex --profile <name>`; older versions use [profiles.<name>]
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import tomlkit

from .logutil import log

RESERVED_IDS = {"openai", "ollama", "lmstudio", "amazon-bedrock", "amazon-bedrock-runtime", "gcp"}
PROFILE_MARKER = "# Managed by Agentsii One-Click API Installer"
NEW_PROFILE_FILES_VERSION = (0, 134, 0)
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


# ------------------------------------------------------------------ detection

def _user_env_var(name: str) -> str | None:
    val = os.environ.get(name)
    if val:
        return val
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                v, _ = winreg.QueryValueEx(k, name)
                return os.path.expandvars(str(v)) or None
        except OSError:
            return None
    return None


def codex_home() -> Path:
    env = _user_env_var("CODEX_HOME")
    if env:
        return Path(os.path.expanduser(env))
    return Path.home() / ".codex"


@dataclass
class CodexInstall:
    home: Path
    home_exists: bool
    config_path: Path
    cli_path: str | None = None
    cli_version: tuple[int, int, int] | None = None
    cli_version_text: str = ""
    desktop_path: str | None = None
    desktop_appid: str | None = None

    @property
    def found(self) -> bool:
        return bool(self.home_exists or self.cli_path or self.desktop_path or self.desktop_appid)

    @property
    def uses_profile_files(self) -> bool:
        return self.cli_version is None or self.cli_version >= NEW_PROFILE_FILES_VERSION


def _find_cli() -> str | None:
    found = shutil.which("codex")
    if found:
        return found
    if sys.platform == "win32":
        cands = []
        appdata = os.environ.get("APPDATA")
        local = os.environ.get("LOCALAPPDATA")
        if appdata:
            cands += [Path(appdata) / "npm" / "codex.cmd", Path(appdata) / "npm" / "codex.ps1"]
        if local:
            cands += [Path(local) / "Microsoft" / "WinGet" / "Links" / "codex.exe",
                      Path(local) / "Programs" / "codex" / "codex.exe"]
        cands += [Path.home() / ".codex" / "bin" / "codex.exe", Path.home() / "scoop" / "shims" / "codex.exe"]
        for c in cands:
            if c.exists():
                return str(c)
    return None


def _cli_version(cli: str) -> tuple[tuple[int, int, int] | None, str]:
    try:
        cmd = [cli, "--version"]
        if cli.lower().endswith((".cmd", ".bat")):
            cmd = ["cmd.exe", "/c", cli, "--version"]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=25, creationflags=_NO_WINDOW)
        text = (out.stdout or out.stderr).strip()
        m = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
        if m:
            return (int(m[1]), int(m[2]), int(m[3])), text
        return None, text
    except (OSError, subprocess.SubprocessError):
        return None, ""


def _find_desktop() -> tuple[str | None, str | None]:
    if sys.platform != "win32":
        return None, None
    local = os.environ.get("LOCALAPPDATA", "")
    for c in (Path(local) / "Programs" / "Codex" / "Codex.exe",
              Path(local) / "Programs" / "codex-desktop" / "Codex.exe",
              Path(local) / "OpenAI" / "Codex" / "Codex.exe"):
        if c.exists():
            return str(c), None
    try:
        ps = ("Get-StartApps | Where-Object { $_.Name -like 'Codex*' } | "
              "Select-Object -First 1 -ExpandProperty AppID")
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                             capture_output=True, text=True, timeout=20, creationflags=_NO_WINDOW)
        appid = (out.stdout or "").strip()
        if appid:
            return None, appid
    except (OSError, subprocess.SubprocessError):
        pass
    return None, None


def detect() -> CodexInstall:
    home = codex_home()
    inst = CodexInstall(home=home, home_exists=home.is_dir(), config_path=home / "config.toml")
    inst.cli_path = _find_cli()
    if inst.cli_path:
        inst.cli_version, inst.cli_version_text = _cli_version(inst.cli_path)
    inst.desktop_path, inst.desktop_appid = _find_desktop()
    log.info("Codex detect: home=%s exists=%s cli=%s ver=%s desktop=%s appid=%s",
             home, inst.home_exists, inst.cli_path, inst.cli_version_text, inst.desktop_path, inst.desktop_appid)
    return inst


# ------------------------------------------------------------------ helpers

def provider_id_from_name(name: str) -> str:
    pid = re.sub(r"[^a-z0-9_-]+", "-", name.strip().lower()).strip("-_")
    pid = re.sub(r"-{2,}", "-", pid)[:40] or "provider"
    if pid in RESERVED_IDS:
        pid = f"{pid}-custom"
    return pid


def profile_name(provider_id: str, model: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", model).strip("-_")
    return f"agentsii-{provider_id}-{slug}"[:96].rstrip("-_")


def load_config(path: Path) -> tomlkit.TOMLDocument:
    if path.exists():
        return tomlkit.parse(path.read_text(encoding="utf-8"))
    return tomlkit.document()


def existing_provider(path: Path, provider_id: str) -> dict | None:
    if not path.exists():
        return None
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    prov = (data.get("model_providers") or {}).get(provider_id)
    return prov if isinstance(prov, dict) else None


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".agentsii-tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    tomllib.loads(text)  # never write an unparsable config
    os.replace(tmp, path)


# ------------------------------------------------------------------ changes

@dataclass
class ChangeSet:
    """Everything needed to undo a run."""
    config_path: Path
    backup_path: Path | None = None
    config_existed: bool = False
    created_files: list[Path] = field(default_factory=list)
    overwritten_files: dict[Path, bytes] = field(default_factory=dict)

    def rollback(self) -> None:
        try:
            if self.backup_path and self.backup_path.exists():
                shutil.copy2(self.backup_path, self.config_path)
            elif not self.config_existed and self.config_path.exists():
                self.config_path.unlink()
        except OSError as e:
            log.error("rollback config failed: %s", e)
        for f in self.created_files:
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass
        for f, data in self.overwritten_files.items():
            try:
                f.write_bytes(data)
            except OSError:
                pass
        log.info("rollback finished")


def backup_config(inst: CodexInstall, changes: ChangeSet) -> Path | None:
    inst.home.mkdir(parents=True, exist_ok=True)
    if not inst.config_path.exists():
        changes.config_existed = False
        return None
    changes.config_existed = True
    bdir = inst.home / "agentsii-backups"
    bdir.mkdir(exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    dest = bdir / f"config.toml.{stamp}.bak"
    shutil.copy2(inst.config_path, dest)
    if dest.read_bytes() != inst.config_path.read_bytes():
        raise OSError("backup verification failed")
    changes.backup_path = dest
    log.info("backup created: %s", dest)
    return dest


def apply_provider(
    inst: CodexInstall,
    changes: ChangeSet,
    *,
    provider_id: str,
    display_name: str,
    base_url: str,
    auth_command: str,
    auth_args: list[str],
    models: list[str],
    default_model: str,
) -> list[str]:
    """Write provider, default model and one profile per compatible model.
    Returns the profile names created."""
    doc = load_config(inst.config_path)

    doc["model_provider"] = provider_id
    doc["model"] = default_model

    if "model_providers" not in doc:
        doc["model_providers"] = tomlkit.table(is_super_table=True)
    providers = doc["model_providers"]

    prov = tomlkit.table()
    prov.add(tomlkit.comment("Added by Agentsii One-Click API Installer. API key is in Windows Credential Manager."))
    prov["name"] = display_name
    prov["base_url"] = base_url
    prov["wire_api"] = "responses"
    auth = tomlkit.table()
    auth["command"] = auth_command
    auth["args"] = auth_args
    auth["timeout_ms"] = 20000
    auth["refresh_interval_ms"] = 0
    prov["auth"] = auth
    if provider_id in providers:
        del providers[provider_id]
    providers[provider_id] = prov

    profiles: list[str] = []
    if inst.uses_profile_files:
        for m in models:
            name = profile_name(provider_id, m)
            path = inst.home / f"{name}.config.toml"
            if path.exists():
                old = path.read_bytes()
                if PROFILE_MARKER.encode() not in old:
                    log.info("skip profile %s: file not managed by Agentsii", path)
                    continue
                changes.overwritten_files[path] = old
            else:
                changes.created_files.append(path)
            content = (f"{PROFILE_MARKER}\n# Use with: codex --profile {name}\n"
                       f"model_provider = {json.dumps(provider_id)}\nmodel = {json.dumps(m)}\n")
            _atomic_write(path, content)
            profiles.append(name)
    else:
        if "profiles" not in doc:
            doc["profiles"] = tomlkit.table(is_super_table=True)
        ptab = doc["profiles"]
        for m in models:
            name = profile_name(provider_id, m)
            if name in ptab:
                del ptab[name]
            t = tomlkit.table()
            t["model_provider"] = provider_id
            t["model"] = m
            ptab[name] = t
            profiles.append(name)

    _atomic_write(inst.config_path, tomlkit.dumps(doc))
    write_manifest(inst, changes, provider_id, display_name, base_url, models, default_model, profiles)
    return profiles


def write_manifest(inst, changes, provider_id, display_name, base_url, models, default_model, profiles):
    mdir = inst.home / "agentsii"
    mdir.mkdir(exist_ok=True)
    path = mdir / f"{provider_id}.json"
    if path.exists():
        changes.overwritten_files[path] = path.read_bytes()
    else:
        changes.created_files.append(path)
    data = {
        "provider_id": provider_id,
        "name": display_name,
        "base_url": base_url,
        "default_model": default_model,
        "models": models,
        "profiles": profiles,
        "backup": str(changes.backup_path) if changes.backup_path else None,
        "updated": _dt.datetime.now().isoformat(timespec="seconds"),
        "credential": "Windows Credential Manager (agentsii-codex:" + provider_id + ")",
    }
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def verify_config(inst: CodexInstall, provider_id: str, base_url: str, default_model: str,
                  auth_command: str, profiles: list[str]) -> list[str]:
    """Re-read what was written. Returns a list of problems (empty == OK)."""
    problems: list[str] = []
    try:
        data = tomllib.loads(inst.config_path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return [f"config.toml cannot be parsed: {e}"]
    if data.get("model_provider") != provider_id:
        problems.append("model_provider not set")
    if data.get("model") != default_model:
        problems.append("model not set")
    prov = (data.get("model_providers") or {}).get(provider_id)
    if not isinstance(prov, dict):
        return problems + ["provider table missing"]
    if prov.get("base_url") != base_url:
        problems.append("base_url mismatch")
    if prov.get("wire_api") != "responses":
        problems.append("wire_api is not responses")
    for bad in ("env_key", "experimental_bearer_token", "requires_openai_auth"):
        if bad in prov:
            problems.append(f"{bad} must not be combined with auth")
    auth = prov.get("auth") or {}
    if auth.get("command") != auth_command or not Path(auth_command).exists():
        problems.append("auth command missing")
    if inst.uses_profile_files:
        for p in profiles:
            f = inst.home / f"{p}.config.toml"
            try:
                pd = tomllib.loads(f.read_text(encoding="utf-8"))
                if pd.get("model_provider") != provider_id:
                    problems.append(f"profile {p} invalid")
            except Exception:  # noqa: BLE001
                problems.append(f"profile {p} unreadable")
    return problems


# ------------------------------------------------------------------ launch

def open_codex(inst: CodexInstall) -> bool:
    try:
        if inst.desktop_path:
            subprocess.Popen([inst.desktop_path], close_fds=True)
            return True
        if inst.desktop_appid and sys.platform == "win32":
            os.startfile(f"shell:AppsFolder\\{inst.desktop_appid}")  # type: ignore[attr-defined]
            return True
        if inst.cli_path and sys.platform == "win32":
            subprocess.Popen(["cmd.exe", "/c", "start", "Codex", "cmd.exe", "/k", inst.cli_path],
                             cwd=str(Path.home()), close_fds=True)
            return True
        if inst.cli_path:
            subprocess.Popen([inst.cli_path], cwd=str(Path.home()))
            return True
    except OSError as e:
        log.error("open codex failed: %s", e)
    return False
