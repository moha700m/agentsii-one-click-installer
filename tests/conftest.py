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
