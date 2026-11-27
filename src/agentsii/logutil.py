"""Logging with secret redaction. API keys must never reach a log file."""
from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path

_SECRETS: set[str] = set()
_PATTERNS = [
    re.compile(r"(sk-[A-Za-z0-9_\-]{6})[A-Za-z0-9_\-]+"),
    re.compile(r"(Bearer\s+)\S+", re.IGNORECASE),
    re.compile(r"(api[_-]?key[\"'=:\s]+)[^\s\"',}]+", re.IGNORECASE),
]


def register_secret(value: str | None) -> None:
    if value and len(value) >= 4:
        _SECRETS.add(value)


def redact(text: str) -> str:
    for s in _SECRETS:
        if s in text:
            text = text.replace(s, "***REDACTED***")
    for p in _PATTERNS:
        text = p.sub(lambda m: m.group(1) + "***", text)
    return text


class _RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        return True


def app_data_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "Agentsii"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "agentsii"


def log_file() -> Path:
    return app_data_dir() / "logs" / "agentsii.log"


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("agentsii")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    flt = _RedactFilter()
    try:
        path = log_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(path, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        fh.addFilter(flt)
        logger.addHandler(fh)
    except OSError:
        pass
    return logger


log = setup_logging()
