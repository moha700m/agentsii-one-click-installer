"""Windows process helpers: background service launch, autostart, Codex Desktop."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from .logutil import log

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "Agentsii"
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def app_command() -> list[str]:
    """Command that starts THIS application (frozen exe or dev module)."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, "-m", "agentsii.main"]


def service_command() -> list[str]:
    """Command for the background service (tray + bridge + model sync)."""
    override = os.environ.get("AGENTSII_SERVICE_CMD")
    if override:
        import shlex
        parts = shlex.split(override, posix=(sys.platform != "win32"))
        return [x[1:-1] if len(x) >= 2 and x[0] == x[-1] == '"' else x for x in parts]
    return app_command() + ["--tray"]


def launch_detached(cmd: list[str]) -> None:
    """Start a process that does not inherit any of our handles and outlives us."""
    if sys.platform == "win32" and getattr(sys, "frozen", False) and len(cmd) >= 1 and cmd[0].lower().endswith(".exe"):
        # ShellExecute never passes inheritable handles to the child.
        os.startfile(cmd[0], arguments=subprocess.list2cmdline(cmd[1:]), show_cmd=0)  # type: ignore[call-arg]
        return
    kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                    "close_fds": True}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x00000008 | 0x00000200 | _NO_WINDOW  # DETACHED | NEW_GROUP
    else:
        kwargs["start_new_session"] = True
        src = str(Path(__file__).resolve().parents[1])
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join([src, env.get("PYTHONPATH", "")])
        kwargs["env"] = env
    subprocess.Popen(cmd, **kwargs)


def ensure_service(wait: float = 25.0) -> bool:
    from . import bridge
    port = int(bridge.load_registry().get("port") or 0)
    if not port:
        port, _ = bridge.ensure_port()
    if bridge.is_bridge_running(port):
        return True
    try:
        launch_detached(service_command())
    except OSError as e:
        log.error("could not start background service: %s", e)
        return False
    deadline = time.time() + wait
    while time.time() < deadline:
        if bridge.is_bridge_running(port, timeout=0.5):
            return True
        time.sleep(0.25)
    log.error("background service did not start on port %s", port)
    return False


def stop_service() -> None:
    from . import bridge
    pid = bridge.running_pid()
    if pid and pid != os.getpid():
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, creationflags=_NO_WINDOW)
            else:
                import signal
                os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


# ------------------------------------------------------------------ autostart

def set_autostart(enabled: bool) -> bool:
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if enabled:
                winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, subprocess.list2cmdline(service_command()))
            else:
                try:
                    winreg.DeleteValue(k, RUN_VALUE)
                except FileNotFoundError:
                    pass
        return True
    except OSError as e:
        log.error("autostart change failed: %s", e)
        return False


def autostart_enabled() -> bool:
    if sys.platform != "win32":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, RUN_VALUE)
            return True
    except OSError:
        return False


# ------------------------------------------------------------------ Codex Desktop

def running_codex_desktop() -> list[int]:
    if sys.platform != "win32":
        return []
    ps = ("Get-Process -Name Codex -ErrorAction SilentlyContinue | "
          "Where-Object { $_.Path -like '*OpenAI.Codex*' -or $_.Path -like '*\\Programs\\Codex*' } | "
          "Select-Object -ExpandProperty Id")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                             capture_output=True, text=True, timeout=20, creationflags=_NO_WINDOW)
        return [int(x) for x in out.stdout.split() if x.strip().isdigit()]
    except (OSError, subprocess.SubprocessError):
        return []


def close_codex_desktop(pids: list[int]) -> None:
    for pid in pids:
        subprocess.run(["taskkill", "/PID", str(pid), "/T"], capture_output=True, creationflags=_NO_WINDOW)
    time.sleep(2.5)
    for pid in pids:
        subprocess.run(["taskkill", "/F", "/PID", str(pid), "/T"], capture_output=True, creationflags=_NO_WINDOW)
