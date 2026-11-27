import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
os.environ["PYTHONPATH"] = os.pathsep.join([str(ROOT / "src"), str(ROOT / "tests"),
                                            os.environ.get("PYTHONPATH", "")])


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    home = tmp_path / "codex-home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setenv("AGENTSII_TEST_KEYRING", str(tmp_path / "keyring.json"))
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "fakekeyring.FileKeyring")
    monkeypatch.setenv("AGENTSII_ALLOW_ANY_KEYRING", "1")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    import keyring
    import fakekeyring
    keyring.set_keyring(fakekeyring.FileKeyring())
    yield home
    # stop any bridge a test started (it belongs to this test's registry)
    import json as _json, os as _os, signal as _signal
    from agentsii import bridge
    port = int(bridge.load_registry().get("port") or 0)
    if port and bridge.is_bridge_running(port):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        c.request("GET", bridge.HEALTH_PATH)
        pid = _json.loads(c.getresponse().read()).get("pid")
        if pid and pid != _os.getpid():
            try:
                _os.kill(pid, _signal.SIGTERM)
            except OSError:
                pass
