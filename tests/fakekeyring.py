"""File-backed keyring used ONLY by the test-suite (works across processes, so the
auth-helper subprocess sees the same store). Never shipped in the app."""
import json
import os
from pathlib import Path

from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError


class FileKeyring(KeyringBackend):
    priority = 1

    def _path(self) -> Path:
        return Path(os.environ["AGENTSII_TEST_KEYRING"])

    def _load(self):
        p = self._path()
        return json.loads(p.read_text()) if p.exists() else {}

    def get_password(self, service, username):
        return self._load().get(f"{service}|{username}")

    def set_password(self, service, username, password):
        d = self._load()
        d[f"{service}|{username}"] = password
        self._path().write_text(json.dumps(d))

    def delete_password(self, service, username):
        d = self._load()
        if f"{service}|{username}" not in d:
            raise PasswordDeleteError("missing")
        del d[f"{service}|{username}"]
        self._path().write_text(json.dumps(d))
