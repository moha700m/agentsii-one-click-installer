"""Agentsii background service (system tray).

* runs the local bridge for Chat-Completions-only providers (127.0.0.1 only)
* keeps `model_provider` in config.toml matched to the model picked in Codex
* starts with Windows (HKCU Run), single instance
"""
from __future__ import annotations

import json
import os
import sys
import threading

from PySide6.QtCore import QTimer
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from . import APP_NAME, bridge, catalog, codex, system
from .gui import resource
from .logutil import log


class Service:
    def __init__(self):
        self.port, _ = bridge.ensure_port()
        self.server = bridge.make_server(self.port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.config_path = codex.codex_home() / "config.toml"
        self._mtime = None
        self._bundled: set[str] | None = None

    def start(self):
        self.thread.start()
        log.info("background service started on 127.0.0.1:%s (pid %s)", self.port, os.getpid())

    def stop(self):
        self.server.shutdown()

    def bundled_slugs(self) -> set[str]:
        if self._bundled is None:
            providers = bridge.load_registry().get("providers", {})
            ours = {m for p in providers.values() for m in p.get("models") or []}
            try:
                cat = json.loads(catalog.catalog_path(codex.codex_home()).read_text(encoding="utf-8"))
                self._bundled = {m.get("slug") for m in cat.get("models", []) if m.get("slug") not in ours}
            except (OSError, ValueError):
                self._bundled = set()
        return self._bundled

    def sync_model_provider(self):
        try:
            mtime = self.config_path.stat().st_mtime
        except OSError:
            return
        if mtime == self._mtime:
            return
        self._mtime = mtime
        try:
            providers = bridge.load_registry().get("providers", {})
            if providers and catalog.reconcile(self.config_path, providers, self.bundled_slugs()):
                self._mtime = self.config_path.stat().st_mtime
        except Exception as e:  # noqa: BLE001
            log.error("model sync failed: %s", e)


def run_tray() -> int:
    port = int(bridge.load_registry().get("port") or 0)
    if port and bridge.is_bridge_running(port):
        return 0  # already running (single instance)
    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    try:
        svc = Service()
    except OSError as e:
        log.error("service could not bind: %s", e)
        return 3
    svc.start()

    timer = QTimer()
    timer.timeout.connect(svc.sync_model_provider)
    timer.start(1500)

    if QSystemTrayIcon.isSystemTrayAvailable():
        tray = QSystemTrayIcon(QIcon(str(resource("icon.ico"))))
        tray.setToolTip(f"Agentsii — Codex bridge on 127.0.0.1:{svc.port}")
        menu = QMenu()
        status = QAction(f"● Running · 127.0.0.1:{svc.port}")
        status.setEnabled(False)
        menu.addAction(status)
        menu.addSeparator()
        a_open = QAction("Open Agentsii / فتح Agentsii")
        a_open.triggered.connect(lambda: system.launch_detached(system.app_command()))
        menu.addAction(a_open)
        a_codex = QAction("Open Codex / فتح Codex")
        a_codex.triggered.connect(lambda: codex.open_codex(codex.detect()))
        menu.addAction(a_codex)
        def do_repair():
            from . import repair
            from PySide6.QtWidgets import QMessageBox
            if system.running_codex_desktop():
                QMessageBox.information(None, "Agentsii", "Quit Codex completely first, then choose Repair again.\n"
                                        "أغلق Codex بالكامل أولاً ثم اختر الإصلاح مرة أخرى.")
                return
            r = repair.run()
            QMessageBox.information(None, "Agentsii", f"Codex repaired / تم الإصلاح\n{r}")
        a_repair = QAction("Repair Codex / إصلاح Codex")
        a_repair.triggered.connect(do_repair)
        menu.addAction(a_repair)
        a_auto = QAction("Start with Windows / التشغيل مع Windows")
        a_auto.setCheckable(True)
        a_auto.setChecked(system.autostart_enabled())
        a_auto.toggled.connect(lambda on: system.set_autostart(on))
        menu.addAction(a_auto)
        menu.addSeparator()
        a_quit = QAction("Quit / إنهاء")

        def quit_():
            svc.stop()
            app.quit()
        a_quit.triggered.connect(quit_)
        menu.addAction(a_quit)
        tray.setContextMenu(menu)
        tray.show()
        app._agentsii_keep = (tray, menu, status, a_open, a_codex, a_repair, a_auto, a_quit)  # type: ignore[attr-defined]
    app.setApplicationName(APP_NAME)
    return app.exec()
