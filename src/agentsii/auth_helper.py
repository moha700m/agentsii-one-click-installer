"""agentsii-auth: Codex command-backed auth helper.

Codex runs `agentsii-auth.exe --provider <id>` and reads the bearer token from
stdout ([model_providers.<id>.auth] in config.toml). The key is read from the
Windows Credential Manager and never written anywhere else.

Built as a windowed (no console) executable so no window flashes when Codex
starts it; output is written straight to the inherited stdout handle.
"""
from __future__ import annotations

import sys


def _write_stdout(data: bytes) -> bool:
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetStdHandle.restype = wintypes.HANDLE
        h = k32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        if h and h != wintypes.HANDLE(-1).value:
            written = wintypes.DWORD(0)
            ok = k32.WriteFile(h, data, len(data), ctypes.byref(written), None)
            if ok and written.value == len(data):
                return True
    if sys.stdout is not None:
        try:
            sys.stdout.buffer.write(data)
            sys.stdout.flush()
            return True
        except (AttributeError, OSError, ValueError):
            pass
    return False


def _write_stderr(msg: str) -> None:
    data = (msg + "\n").encode("utf-8", errors="replace")
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.GetStdHandle.restype = wintypes.HANDLE
            h = k32.GetStdHandle(-12)
            if h and h != wintypes.HANDLE(-1).value:
                k32.WriteFile(h, data, len(data), ctypes.byref(wintypes.DWORD(0)), None)
                return
        except OSError:
            pass
    if sys.stderr is not None:
        try:
            sys.stderr.write(msg + "\n")
        except (OSError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--version" in argv:
        _write_stdout(b"agentsii-auth 1.0.0\n")
        return 0
    if "--provider" not in argv or argv.index("--provider") + 1 >= len(argv):
        _write_stderr("usage: agentsii-auth --provider <provider_id>")
        return 2
    provider_id = argv[argv.index("--provider") + 1]
    try:
        import keyring
        key = keyring.get_password(f"agentsii-codex:{provider_id}", "api_key")
    except Exception as e:  # noqa: BLE001
        _write_stderr(f"agentsii-auth: credential store error: {type(e).__name__}")
        return 3
    if not key:
        _write_stderr(f"agentsii-auth: no credential stored for provider '{provider_id}'. "
                      "Run Agentsii One-Click API Installer again.")
        return 4
    return 0 if _write_stdout(key.encode("utf-8")) else 5


if __name__ == "__main__":
    sys.exit(main())
