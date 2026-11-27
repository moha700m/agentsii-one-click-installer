"""API key storage in the OS credential store (Windows Credential Manager on
Windows, via keyring). There is deliberately no plaintext fallback."""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

from .logutil import app_data_dir, log

USERNAME = "api_key"
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


class CredentialError(Exception):
    pass


def service_name(provider_id: str) -> str:
    return f"agentsii-codex:{provider_id}"


def backend_name() -> str:
    kr = keyring.get_keyring()
    return f"{type(kr).__module__}.{type(kr).__name__}"


def _check_backend() -> None:
    name = backend_name().lower()
    if "fail" in name or "null" in name or "plaintext" in name:
        raise CredentialError(f"No secure credential store available ({backend_name()})")
    if sys.platform == "win32" and "windows" not in name and os.environ.get("AGENTSII_ALLOW_ANY_KEYRING") != "1":
        raise CredentialError(f"Windows Credential Manager backend not active ({backend_name()})")


def get_key(provider_id: str) -> str | None:
    return keyring.get_password(service_name(provider_id), USERNAME)


def store_key(provider_id: str, key: str) -> str | None:
    """Store the key; returns the previous value (for rollback)."""
    _check_backend()
    try:
        previous = get_key(provider_id)
        keyring.set_password(service_name(provider_id), USERNAME, key)
        if get_key(provider_id) != key:
            raise CredentialError("credential read-back mismatch")
    except KeyringError as e:
        raise CredentialError(str(e)) from e
    log.info("credential stored in %s for provider %s", backend_name(), provider_id)
    return previous


def restore_key(provider_id: str, previous: str | None) -> None:
    try:
        if previous is None:
            keyring.delete_password(service_name(provider_id), USERNAME)
        else:
            keyring.set_password(service_name(provider_id), USERNAME, previous)
    except (KeyringError, PasswordDeleteError):
        pass


# ------------------------------------------------------------------ auth helper

def helper_install_path() -> Path:
    name = "agentsii-auth.exe" if sys.platform == "win32" else "agentsii-auth"
    return app_data_dir() / "bin" / name


def _bundled_helper() -> Path | None:
    base = getattr(sys, "_MEIPASS", None)
    candidates = []
    if base:
        candidates.append(Path(base) / "helper" / "agentsii-auth.exe")
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).parent / "agentsii-auth.exe")
    for c in candidates:
        if c.exists():
            return c
    return None


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def ensure_auth_helper() -> tuple[str, list[str]]:
    """Return (command, base_args) Codex should run to obtain the token.

    Frozen build: copy the bundled agentsii-auth.exe to a stable per-user
    location so it keeps working even if the portable EXE is moved/deleted.
    Dev mode: run the helper module with the current interpreter."""
    if os.environ.get("AGENTSII_AUTH_COMMAND"):
        return os.environ["AGENTSII_AUTH_COMMAND"], []
    bundled = _bundled_helper()
    if bundled:
        dest = helper_install_path()
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists() or _sha(dest) != _sha(bundled):
            tmp = dest.with_suffix(".new")
            shutil.copy2(bundled, tmp)
            try:
                os.replace(tmp, dest)
            except PermissionError:
                # Old helper is running right now; use a versioned name.
                dest = dest.with_name(f"agentsii-auth-{_sha(bundled)[:8]}.exe")
                os.replace(tmp, dest)
        return str(dest), []
    script = Path(__file__).resolve().parent / "auth_helper.py"
    return sys.executable, [str(script)]


def run_auth_helper(command: str, base_args: list[str], provider_id: str, timeout: float = 30) -> str:
    out = subprocess.run([command, *base_args, "--provider", provider_id],
                         capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL,
                         creationflags=_NO_WINDOW)
    if out.returncode != 0:
        raise CredentialError(f"auth helper exited {out.returncode}: {out.stderr.decode(errors='replace')[:200]}")
    return out.stdout.decode("utf-8", errors="replace").strip()
