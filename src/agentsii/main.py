"""Entry point.

  AgentsiiInstaller.exe                      -> desktop app
  AgentsiiInstaller.exe --lang ar            -> desktop app in Arabic
  AgentsiiInstaller.exe --headless --name N --base-url U --api-key-env VAR [--yes] [--json-out F]
        -> same pipeline without UI (automation / CI). The key is read from an
           environment variable so it never appears on a command line.
  AgentsiiInstaller.exe --screenshots DIR    -> render UI states to PNG (QA)
"""
from __future__ import annotations

import argparse
import json
import os
import sys


class _HeadlessCB:
    def __init__(self, yes: bool):
        self.yes = yes
        self.lines: list[str] = []

    def _p(self, s):
        self.lines.append(s)

    def stage_started(self, i):
        from .pipeline import STAGES
        self._p(f"[{i + 1}] {STAGES[i]} ...")

    def stage_done(self, i, d):
        self._p(f"[{i + 1}] OK {d}")

    def stage_failed(self, i, c, d):
        self._p(f"[{i + 1}] FAILED {c} {d}")

    def progress(self, i, t):
        pass

    def confirm_overwrite(self, pid, existing):
        self._p(f"provider {pid} exists -> {'replace' if self.yes else 'keep (use --yes to replace)'}")
        return self.yes


def _emit(text: str, path: str | None):
    if path:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    if sys.stdout is not None:
        try:
            print(text)
        except (OSError, ValueError):
            pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="AgentsiiInstaller", add_help=True)
    ap.add_argument("--lang", choices=["en", "ar"])
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--name")
    ap.add_argument("--base-url")
    ap.add_argument("--api-key-env")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--json-out")
    ap.add_argument("--screenshots")
    ap.add_argument("--tray", action="store_true", help="background service (bridge + model sync)")
    ap.add_argument("--bridge", action="store_true", help="bridge only, no UI")
    ap.add_argument("--uninstall-cleanup", action="store_true")
    ap.add_argument("--repair-codex", action="store_true")
    ap.add_argument("--version", action="store_true")
    args = ap.parse_args(argv)

    if args.version:
        from . import __version__
        _emit(__version__, args.json_out)
        return 0

    if args.uninstall_cleanup:
        from .cleanup import run as cleanup_run
        return cleanup_run()

    if args.repair_codex:
        from . import repair
        _emit(json.dumps(repair.run(), ensure_ascii=False), args.json_out)
        return 0

    if args.tray:
        from .tray import run_tray
        return run_tray()

    if args.bridge:
        from . import bridge
        return bridge.serve()

    if args.screenshots:
        from .gui import screenshots
        return screenshots(args.screenshots)

    if args.headless:
        from . import pipeline
        key = os.environ.get(args.api_key_env or "", "")
        cb = _HeadlessCB(args.yes)
        res = pipeline.run(args.name or "", args.base_url or "", key, cb)
        out = res.to_json()
        out["log"] = cb.lines
        _emit(json.dumps(out, indent=2, ensure_ascii=False, default=str), args.json_out)
        return 0 if res.ok else 1

    from .gui import main as gui_main
    return gui_main(args.lang)


if __name__ == "__main__":
    sys.exit(main())
