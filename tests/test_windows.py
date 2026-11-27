"""Runs only on Windows: real Windows Credential Manager + auth helper round-trip."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")


def test_real_credential_manager_and_helper(monkeypatch):
    import keyring
    from keyring.backends.Windows import WinVaultKeyring
    from agentsii import credentials

    keyring.set_keyring(WinVaultKeyring())
    monkeypatch.delenv("AGENTSII_ALLOW_ANY_KEYRING", raising=False)
    pid = "pytest-win-" + os.urandom(3).hex()
    secret = "sk-win-" + os.urandom(10).hex()
    try:
        assert credentials.store_key(pid, secret) is None
        assert "Windows" in credentials.backend_name()
        out = subprocess.run(["cmdkey", "/list"], capture_output=True, text=True).stdout
        assert f"agentsii-codex:{pid}" in out
        env = dict(os.environ)
        env.pop("PYTHON_KEYRING_BACKEND", None)
        helper = Path(credentials.__file__).with_name("auth_helper.py")
        p = subprocess.run([sys.executable, str(helper), "--provider", pid], capture_output=True, env=env)
        assert p.returncode == 0 and p.stdout.decode().strip() == secret
    finally:
        credentials.restore_key(pid, None)
    assert credentials.get_key(pid) is None
