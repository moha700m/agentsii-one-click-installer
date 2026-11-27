"""PySide6 desktop UI."""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

from PySide6.QtCore import QObject, QSize, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QFont, QIcon, QPixmap
from PySide6.QtWidgets import (QApplication, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
                               QMainWindow, QMessageBox, QPushButton, QScrollArea, QSizePolicy,
                               QStackedWidget, QTextEdit, QVBoxLayout, QWidget, QDialog)

from . import APP_NAME, __version__, codex, pipeline
from .i18n import Tr
from .logutil import log_file

BG = "#0A0E16"
CARD = "#111827"
CARD2 = "#0F1522"
BORDER = "#1F2A3D"
TEXT = "#E6EDF7"
MUTED = "#8B98AE"
ACCENT1 = "#22D3EE"
ACCENT2 = "#8B5CF6"
OK = "#34D399"
ERR = "#F87171"
RUN = "#38BDF8"


def resource(name: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    p = base / "assets" / name
    if p.exists():
        return p
    return Path(__file__).resolve().parents[2] / "assets" / name


QSS = f"""
QWidget {{ background: {BG}; color: {TEXT}; font-size: 10.5pt; }}
QLabel {{ background: transparent; }}
#Card {{ background: {CARD}; border: 1px solid {BORDER}; border-radius: 16px; }}
#Title {{ font-size: 18pt; font-weight: 700; }}
#Subtitle {{ color: {MUTED}; font-size: 10pt; }}
#FieldLabel {{ color: {MUTED}; font-size: 9.5pt; font-weight: 600; }}
QLineEdit {{ background: {CARD2}; border: 1px solid {BORDER}; border-radius: 10px;
            padding: 11px 12px; color: {TEXT}; selection-background-color: {ACCENT2}; }}
QLineEdit:focus {{ border: 1px solid {ACCENT1}; }}
#Primary {{ border: none; border-radius: 12px; color: white; font-size: 13pt; font-weight: 800;
           letter-spacing: 1px; padding: 16px;
           background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {ACCENT1}, stop:1 {ACCENT2}); }}
#Primary:hover {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #3FE0F5, stop:1 #9D74FA); }}
#Primary:pressed {{ background: {ACCENT2}; }}
#Primary:disabled {{ background: #2A3346; color: #7C879B; }}
#Secondary {{ background: transparent; border: 1px solid #2C3A55; border-radius: 12px; color: {TEXT};
             font-size: 11pt; font-weight: 700; padding: 13px; }}
#Secondary:hover {{ border-color: {ACCENT1}; color: {ACCENT1}; }}
#Link {{ background: transparent; border: none; color: {ACCENT1}; font-size: 9.5pt; padding: 4px; }}
#Link:hover {{ text-decoration: underline; }}
#Lang {{ background: {CARD}; border: 1px solid {BORDER}; border-radius: 9px; padding: 6px 12px;
        color: {TEXT}; font-weight: 600; }}
#Lang:hover {{ border-color: {ACCENT1}; }}
#Note {{ color: {MUTED}; font-size: 9pt; }}
#StageTitle {{ font-size: 10.5pt; font-weight: 600; }}
#StageDetail {{ color: {MUTED}; font-size: 8.8pt; }}
#SuccessTitle {{ color: {OK}; font-size: 24pt; font-weight: 900; }}
#FailTitle {{ color: {ERR}; font-size: 16pt; font-weight: 800; }}
#ResKey {{ color: {MUTED}; }}
#ResVal {{ font-weight: 700; }}
QScrollArea {{ border: none; }}
QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #2A3550; border-radius: 4px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: #3B4A6B; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QTextEdit {{ background: {CARD2}; border: 1px solid {BORDER}; border-radius: 10px; }}
QMessageBox {{ background: {CARD}; }}
QMessageBox QPushButton {{ background: {CARD2}; border: 1px solid {BORDER}; border-radius: 8px;
                          padding: 6px 18px; min-width: 70px; }}
"""


def _dark_titlebar(widget: QWidget) -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes
        hwnd = int(widget.winId())
        val = ctypes.c_int(1)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (new/old)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(val), 4) == 0:
                break
    except Exception:  # noqa: BLE001
        pass


# ------------------------------------------------------------------ worker

class _Bridge(QObject):
    started = Signal(int)
    done = Signal(int, str)
    failed = Signal(int, str, str)
    progress = Signal(int, str)
    ask = Signal(str, str)
    finished = Signal(object)


class _Callbacks(pipeline.Callbacks):
    def __init__(self, bridge: _Bridge):
        self.b = bridge
        self.answer = False
        self.event = threading.Event()

    def stage_started(self, i): self.b.started.emit(i)
    def stage_done(self, i, d): self.b.done.emit(i, d)
    def stage_failed(self, i, c, d): self.b.failed.emit(i, c, d)
    def progress(self, i, t): self.b.progress.emit(i, t)

    def confirm_overwrite(self, pid, existing):
        self.event.clear()
        self.b.ask.emit(pid, str(existing.get("base_url", "")))
        self.event.wait()
        return self.answer


class Worker(QThread):
    def __init__(self, name, url, key, bridge: _Bridge):
        super().__init__()
        self.args = (name, url, key)
        self.bridge = bridge
        self.cb = _Callbacks(bridge)

    def run(self):
        res = pipeline.run(*self.args, self.cb)
        self.bridge.finished.emit(res)


# ------------------------------------------------------------------ widgets

class StageRow(QWidget):
    SPIN = "◐◓◑◒"

    def __init__(self, idx: int):
        super().__init__()
        self.idx = idx
        self.state = "pending"
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 5, 4, 5)
        lay.setSpacing(12)
        self.icon = QLabel()
        self.icon.setFixedSize(26, 26)
        self.icon.setAlignment(Qt.AlignCenter)
        self.title = QLabel(objectName="StageTitle")
        self.detail = QLabel(objectName="StageDetail")
        self.detail.setMaximumWidth(300)
        self.detail.setAlignment(Qt.AlignTrailing | Qt.AlignVCenter)
        self.detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.icon)
        lay.addWidget(self.title, 1)
        lay.addWidget(self.detail)
        self.raw = None
        self._spin = 0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.set_state("pending")

    def _tick(self):
        self._spin = (self._spin + 1) % 4
        self._paint(self.SPIN[self._spin], RUN)

    def _paint(self, glyph, color, filled=False):
        bg = color if filled else "transparent"
        fg = BG if filled else color
        self.icon.setText(glyph)
        self.icon.setStyleSheet(
            f"border:2px solid {color}; border-radius:13px; background:{bg}; color:{fg};"
            f"font-weight:900; font-size:11pt;")

    def set_state(self, state, detail: str | None = None):
        self.state = state
        self.timer.stop()
        if state == "pending":
            self._paint("", "#334155")
            self.title.setStyleSheet(f"color:{MUTED}")
        elif state == "running":
            self.title.setStyleSheet(f"color:{TEXT}")
            self._paint(self.SPIN[0], RUN)
            self.timer.start(140)
        elif state == "done":
            self.title.setStyleSheet(f"color:{TEXT}")
            self._paint("✓", OK, filled=True)
        elif state == "failed":
            self.title.setStyleSheet(f"color:{ERR}")
            self._paint("✕", ERR, filled=True)
        elif state == "skipped":
            self.title.setStyleSheet(f"color:{MUTED}")
            self._paint("–", "#475569")
        if detail is not None:
            self.set_detail(detail)

    def set_detail(self, text: str):
        self.detail.setToolTip(text if len(text) > 40 else "")
        fm = self.detail.fontMetrics()
        self.detail.setText(fm.elidedText(text, Qt.ElideMiddle, 300))
        self.detail.setVisible(bool(text))


class MainWindow(QMainWindow):
    def __init__(self, lang: str = "en"):
        super().__init__()
        self.tr_ = Tr(lang)
        self.result: pipeline.Result | None = None
        self.worker: Worker | None = None
        self.bridge = _Bridge()
        self.bridge.started.connect(self._on_started)
        self.bridge.done.connect(self._on_done)
        self.bridge.failed.connect(self._on_failed)
        self.bridge.progress.connect(self._on_progress)
        self.bridge.ask.connect(self._on_ask)
        self.bridge.finished.connect(self._on_finished)

        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(QIcon(str(resource("icon.ico"))))
        self.resize(620, 900)
        self.setMinimumSize(520, 640)

        root = QWidget()
        outer = QVBoxLayout(root)
        outer.setContentsMargins(26, 22, 26, 18)
        outer.setSpacing(16)

        # header
        head = QHBoxLayout()
        head.setSpacing(14)
        logo = QLabel()
        pm = QPixmap(str(resource("logo@2x.png")))
        pm.setDevicePixelRatio(2.0)
        logo.setPixmap(pm.scaled(QSize(112, 112), Qt.KeepAspectRatio, Qt.SmoothTransformation))
        logo.setFixedSize(56, 56)
        tcol = QVBoxLayout()
        tcol.setSpacing(0)
        self.title = QLabel("Agentsii", objectName="Title")
        self.subtitle = QLabel(objectName="Subtitle")
        tcol.addWidget(self.title)
        tcol.addWidget(self.subtitle)
        self.lang_btn = QPushButton(objectName="Lang", cursor=Qt.PointingHandCursor)
        self.lang_btn.clicked.connect(self.toggle_lang)
        head.addWidget(logo)
        head.addLayout(tcol, 1)
        head.addWidget(self.lang_btn, 0, Qt.AlignTop)
        outer.addLayout(head)

        self.stack = QStackedWidget()
        outer.addWidget(self.stack, 1)
        self.stack.addWidget(self._build_form())
        self.stack.addWidget(self._build_run())

        self.footer = QLabel(f"v{__version__}", objectName="Note")
        self.footer.setAlignment(Qt.AlignCenter)
        outer.addWidget(self.footer)
        self.setCentralWidget(root)
        self.retranslate()

    # ---------------------------------------------------------- pages
    def _field(self, lay, attr_label, attr_edit, password=False):
        lbl = QLabel(objectName="FieldLabel")
        edit = QLineEdit()
        edit.setMinimumHeight(44)
        if password:
            edit.setEchoMode(QLineEdit.Password)
            act = QAction("👁", edit)
            act.setToolTip("Show / hide")
            act.triggered.connect(lambda: edit.setEchoMode(
                QLineEdit.Normal if edit.echoMode() == QLineEdit.Password else QLineEdit.Password))
            edit.addAction(act, QLineEdit.TrailingPosition)
        lay.addWidget(lbl)
        lay.addWidget(edit)
        lay.addSpacing(6)
        setattr(self, attr_label, lbl)
        setattr(self, attr_edit, edit)

    def _build_form(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        card = QFrame(objectName="Card")
        c = QVBoxLayout(card)
        c.setContentsMargins(24, 24, 24, 24)
        c.setSpacing(6)
        self._field(c, "l_name", "e_name")
        self._field(c, "l_url", "e_url")
        self._field(c, "l_key", "e_key", password=True)
        self.e_key.returnPressed.connect(self.start)
        c.addSpacing(10)
        self.btn_connect = QPushButton(objectName="Primary", cursor=Qt.PointingHandCursor)
        self.btn_connect.setMinimumHeight(60)
        self.btn_connect.clicked.connect(self.start)
        c.addWidget(self.btn_connect)
        c.addSpacing(8)
        self.l_privacy = QLabel(objectName="Note")
        self.l_privacy.setWordWrap(True)
        self.l_privacy.setAlignment(Qt.AlignCenter)
        c.addWidget(self.l_privacy)
        v.addWidget(card)
        v.addStretch(1)
        return page

    def _build_run(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        scroll = self.scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        iv = QVBoxLayout(inner)
        iv.setContentsMargins(0, 0, 0, 0)
        iv.setSpacing(14)

        card = QFrame(objectName="Card")
        c = QVBoxLayout(card)
        c.setContentsMargins(18, 14, 18, 14)
        c.setSpacing(0)
        self.rows = [StageRow(i) for i in range(len(pipeline.STAGES))]
        for r in self.rows:
            c.addWidget(r)
        iv.addWidget(card)

        # result card
        self.res_card = QFrame(objectName="Card")
        rc = QVBoxLayout(self.res_card)
        rc.setContentsMargins(24, 20, 24, 20)
        rc.setSpacing(8)
        self.res_title = QLabel()
        self.res_title.setAlignment(Qt.AlignCenter)
        self.res_sub = QLabel()
        self.res_sub.setAlignment(Qt.AlignCenter)
        self.res_sub.setWordWrap(True)
        self.res_sub.setTextInteractionFlags(Qt.TextSelectableByMouse)
        rc.addWidget(self.res_title)
        rc.addWidget(self.res_sub)
        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(18)
        self.grid.setVerticalSpacing(6)
        rc.addLayout(self.grid)
        self.res_note = QLabel(objectName="Note")
        self.res_note.setWordWrap(True)
        self.res_note.setAlignment(Qt.AlignCenter)
        rc.addWidget(self.res_note)
        rc.addSpacing(6)
        self.btn_main = QPushButton(objectName="Primary", cursor=Qt.PointingHandCursor)
        self.btn_main.setMinimumHeight(56)
        self.btn_main.clicked.connect(self._main_action)
        self.btn_second = QPushButton(objectName="Secondary", cursor=Qt.PointingHandCursor)
        self.btn_second.clicked.connect(self._second_action)
        rc.addWidget(self.btn_main)
        rc.addWidget(self.btn_second)
        links = QHBoxLayout()
        self.btn_models = QPushButton(objectName="Link", cursor=Qt.PointingHandCursor)
        self.btn_models.clicked.connect(self.show_models)
        self.btn_log = QPushButton(objectName="Link", cursor=Qt.PointingHandCursor)
        self.btn_log.clicked.connect(self.open_log)
        links.addStretch(1)
        links.addWidget(self.btn_models)
        links.addWidget(self.btn_log)
        links.addStretch(1)
        rc.addLayout(links)
        self.res_card.hide()
        iv.addWidget(self.res_card)
        iv.addStretch(1)
        scroll.setWidget(inner)
        v.addWidget(scroll)
        return page

    # ---------------------------------------------------------- i18n
    def toggle_lang(self):
        self.set_lang("en" if self.tr_.lang == "ar" else "ar")

    def set_lang(self, lang):
        self.tr_.lang = lang
        self.retranslate()

    def retranslate(self):
        t = self.tr_
        QApplication.instance().setLayoutDirection(Qt.RightToLeft if t.rtl else Qt.LeftToRight)
        for e in (getattr(self, "e_url", None), getattr(self, "e_key", None)):
            if e is not None:
                e.setLayoutDirection(Qt.LeftToRight)
        self.subtitle.setText(t("subtitle"))
        self.lang_btn.setText(t("lang_toggle"))
        self.l_name.setText(t("provider_name"))
        self.e_name.setPlaceholderText(t("provider_name_ph"))
        self.l_url.setText(t("base_url"))
        self.e_url.setPlaceholderText(t("base_url_ph"))
        self.l_key.setText(t("api_key"))
        self.e_key.setPlaceholderText(t("api_key_ph"))
        self.btn_connect.setText(t("connect"))
        self.l_privacy.setText(t("privacy"))
        for i, r in enumerate(self.rows):
            r.title.setText(f"[{i + 1}]  {t(pipeline.STAGES[i])}")
            if r.raw and r.raw[0] == "done":
                self._render_done(i, r.raw[1])
            elif r.raw and r.raw[0] == "failed":
                r.set_state("failed", t(f"err_{r.raw[1]}"))
        self.btn_models.setText(t("show_models"))
        self.btn_log.setText(t("open_log"))
        if self.result is not None:
            self._render_result()

    # ---------------------------------------------------------- run
    def start(self):
        if self.worker and self.worker.isRunning():
            return
        self.result = None
        for r in self.rows:
            r.raw = None
            r.set_state("pending", "")
        self.res_card.hide()
        self.stack.setCurrentIndex(1)
        self.btn_connect.setEnabled(False)
        self.lang_btn.setEnabled(False)
        self.worker = Worker(self.e_name.text(), self.e_url.text(), self.e_key.text(), self.bridge)
        self.worker.start()

    def _on_started(self, i):
        for j in range(i):
            if self.rows[j].state == "pending":
                self.rows[j].set_state("skipped")
        self.rows[i].set_state("running", "")

    def _on_done(self, i, detail):
        self.rows[i].raw = ("done", detail)
        self._render_done(i, detail)

    def _render_done(self, i, detail):
        t = self.tr_
        text = detail
        if i == 1 and detail == "unverified":
            self.rows[i].set_state("skipped", "")
            return
        if i == 1:
            text = ""
        elif i == 2:
            text = f"{t('r_discovered')}: {detail}"
        elif i == 3:
            text = f"{t('r_added')}: {detail}"
        elif i == 6:
            text = "Windows Credential Manager" if "windows" in detail.lower() else detail
        elif i == 7:
            text = ""
        self.rows[i].set_state("done", text)

    def _on_progress(self, i, text):
        self.rows[i].set_detail(f"{self.tr_('testing')} {text}")

    def _on_failed(self, i, code, detail):
        self.rows[i].raw = ("failed", code)
        self.rows[i].set_state("failed", self.tr_(f"err_{code}"))

    def _on_ask(self, pid, url):
        t = self.tr_
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle(t("overwrite_title"))
        box.setText(t("overwrite_body", pid=pid, url=url))
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        box.setDefaultButton(QMessageBox.No)
        ans = box.exec() == QMessageBox.Yes
        self.worker.cb.answer = ans
        self.worker.cb.event.set()

    def _on_finished(self, res):
        self.result = res
        self.btn_connect.setEnabled(True)
        self.lang_btn.setEnabled(True)
        self._render_result()

    def _clear_grid(self):
        while self.grid.count():
            w = self.grid.takeAt(0).widget()
            if w:
                w.hide()
                w.setParent(None)
                w.deleteLater()

    def _render_result(self):
        t, res = self.tr_, self.result
        self._clear_grid()
        if res.ok:
            self.res_title.setObjectName("SuccessTitle")
            self.res_title.setText(t("success_title"))
            self.res_sub.setText(t("success_sub"))
            rows = [
                (t("r_provider"), res.provider_name),
                (t("r_discovered"), str(res.discovered)),
                (t("r_added"), str(len(res.added))),
                (t("r_default"), res.default_model),
                (t("r_config"), t("r_updated")),
                (t("r_creds"), t("r_secured")),
            ]
            for n, (k, val) in enumerate(rows):
                kl = QLabel(k + ":", objectName="ResKey")
                vl = QLabel(val, objectName="ResVal")
                vl.setTextInteractionFlags(Qt.TextSelectableByMouse)
                if k in (t("r_config"), t("r_creds")):
                    vl.setStyleSheet(f"color:{OK}")
                self.grid.addWidget(kl, n, 0)
                self.grid.addWidget(vl, n, 1)
            self.res_note.setText(
                f"{t('r_backup')}: {res.backup_path or t('r_no_backup')}\n{res.config_path}")
            self.btn_main.setText(t("open_codex"))
            self.btn_second.setText(t("add_another"))
            self.btn_models.show()
        else:
            self.res_title.setObjectName("FailTitle")
            self.res_title.setText(t("failed_title"))
            msg = t(f"err_{res.error_code}")
            if res.error_detail and res.error_code not in ("overwrite_declined",):
                msg += f"\n\n{res.error_detail}"
            self.res_sub.setText(msg)
            self.res_note.setText(t("rollback_note") if res.failed_stage >= 5 else "")
            self.btn_main.setText(t("try_again"))
            self.btn_second.setText(t("add_another"))
            self.btn_models.setVisible(bool(res.rejected))
        self.res_title.style().unpolish(self.res_title)
        self.res_title.style().polish(self.res_title)
        self.res_card.show()
        QTimer.singleShot(50, lambda: self.scroll.ensureWidgetVisible(self.btn_second, 0, 20))

    def _main_action(self):
        if self.result and self.result.ok:
            inst = self.result.codex or codex.detect()
            if not codex.open_codex(inst):
                QMessageBox.information(self, APP_NAME, self.tr_("codex_open_failed"))
        else:
            self.stack.setCurrentIndex(0)

    def _second_action(self):
        self.e_name.clear()
        self.e_url.clear()
        self.e_key.clear()
        self.result = None
        self.stack.setCurrentIndex(0)
        self.e_name.setFocus()

    def show_models(self):
        res = self.result
        if not res:
            return
        lines = []
        if res.added:
            lines.append(f"✓ {self.tr_('r_added')} ({len(res.added)}):")
            lines += [f"   {m}" + ("   ★" if m == res.default_model else "") for m in res.added]
        if res.rejected:
            lines.append("")
            lines.append(f"✕ ({len(res.rejected)}):")
            lines += [f"   {m} — {why}" for m, why in res.rejected]
        if res.profiles:
            lines.append("")
            lines.append(self.tr_("profiles_hint", p=res.profiles[0]))
        dlg = QDialog(self)
        dlg.setWindowTitle(self.tr_("models_dialog"))
        dlg.resize(560, 480)
        lay = QVBoxLayout(dlg)
        te = QTextEdit()
        te.setReadOnly(True)
        te.setLayoutDirection(Qt.LeftToRight)
        te.setPlainText("\n".join(lines))
        lay.addWidget(te)
        dlg.exec()

    def open_log(self):
        p = log_file()
        if p.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))

    def showEvent(self, e):
        super().showEvent(e)
        _dark_titlebar(self)


def make_app(argv=None) -> QApplication:
    app = QApplication.instance() or QApplication(argv or sys.argv)
    app.setApplicationName(APP_NAME)
    f = QFont("Segoe UI")
    f.setStyleHint(QFont.SansSerif)
    app.setFont(f)
    app.setStyleSheet(QSS)
    return app


def main(lang: str | None = None) -> int:
    app = make_app()
    if lang is None:
        from PySide6.QtCore import QLocale
        lang = "ar" if QLocale.system().language() == QLocale.Arabic else "en"
    w = MainWindow(lang)
    w.show()
    return app.exec()


# ------------------------------------------------------------------ screenshots (QA)

def screenshots(outdir: str) -> int:
    """Render the real window in its main states to PNG files (used in CI)."""
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    app = make_app()
    w = MainWindow("en")
    w.show()
    app.processEvents()

    def snap(name):
        app.processEvents()
        w.grab().save(str(out / name))

    w.e_name.setText("My Gateway")
    w.e_url.setText("https://api.example.com/v1")
    w.e_key.setText("sk-demo-not-a-real-key")
    snap("1_form_en.png")
    w.set_lang("ar")
    snap("2_form_ar.png")
    w.set_lang("en")
    w.stack.setCurrentIndex(1)
    for i in range(3):
        w._on_started(i)
        w._on_done(i, {0: "https://api.example.com/v1", 1: "ok", 2: "14"}[i])
    w._on_started(3)
    w._on_progress(3, "5/12  model-x")
    snap("3_running_en.png")
    for i in range(3, 8):
        w._on_started(i)
        w._on_done(i, {3: "9/12", 4: "codex-cli 0.163.0", 5: "my-gateway", 6: "keyring.backends.Windows.WinVaultKeyring", 7: "ok"}[i])
    r = pipeline.Result(ok=True, provider_name="My Gateway", provider_id="my-gateway", discovered=14,
                        tested=12, added=[f"model-{i}" for i in range(9)], default_model="model-0",
                        backup_path=r"C:\Users\you\.codex\agentsii-backups\config.toml.bak",
                        config_path=r"C:\Users\you\.codex\config.toml")
    w._on_finished(r)
    snap("4_success_en.png")
    w.set_lang("ar")
    snap("5_success_ar.png")
    w.set_lang("en")
    for row in w.rows:
        row.raw = None
        row.set_state("pending", "")
    w._on_started(0)
    w._on_done(0, "x")
    w._on_started(1)
    w._on_failed(1, "key_invalid", "")
    w._on_finished(pipeline.Result(ok=False, failed_stage=1, error_code="key_invalid",
                                   error_detail="Incorrect API key provided."))
    snap("6_failed_en.png")
    w.close()
    return 0
