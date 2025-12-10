#!/usr/bin/env python3
# src/core/network/diagnostic.py

from __future__ import annotations

from typing import Any, Callable, Optional, Iterable, TYPE_CHECKING

from PyQt5.QtCore import QThread, pyqtSignal, Qt, QObject
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QTextEdit, QWidget,
    QApplication
)
from PyQt5.QtGui import QPixmap, QIcon

try:
    from core.helper import Helper
    from core.log import Log
    from core.ui import SpinningIconLabel
except ImportError:
    from helper import Helper
    from log import Log
    from ui import SpinningIconLabel

from .tools import Tools

if TYPE_CHECKING:
    # For type hints only, avoids circular import at runtime
    try:
        from core.application import Application
    except ImportError:
        from application import Application

class DiagnosticStep:
    def __init__(
        self,
        name: str,
        label: str,
        icon: Optional[str],
        func: Callable[[Callable[[str], None]], bool]
    ):
        self.name = name
        self.label = label
        self.icon = icon
        self.func = func

class DiagnosticThread(QThread):
    log = pyqtSignal(str)
    step_state = pyqtSignal(str, str)  # name, state: idle|running|ok|fail
    finished = pyqtSignal(dict)        # {step_name: bool}

    def __init__(self, steps: Iterable[DiagnosticStep], parent: Optional[QWidget] = None):
        app = QApplication.instance()
        if app is not None and hasattr(app, "logger"):
            self._logger = app.logger
        else:
            self._logger = Log()
        self._logger.append(f"[DiagnosticThread] Initializing with {len(list(steps))} steps", channel="diagnostic", level="debug")
        super().__init__(parent)
        self._steps = list(steps)

    def run(self):
        self._logger.append(f"[DiagnosticThread] run() started with {len(self._steps)} steps", channel="diagnostic", level="debug")
        results = {}

        step_names = [step.name for step in self._steps]
        self._logger.append(f"[DiagnosticThread] Steps to run: {step_names}", channel="diagnostic", level="debug")

        def printer(msg: str) -> None:
            self.log.emit(msg)

        for step in self._steps:
            self._logger.append(f"[DiagnosticThread] Starting step {step.name}", channel="diagnostic", level="debug")
            self.step_state.emit(step.name, "running")
            try:
                res = step.func(printer)
                ok = bool(res)
                self._logger.append(f"[DiagnosticThread] Step {step.name} returned {ok}", channel="diagnostic", level="debug")
            except Exception as e:
                printer(f"[{step.label}] error: {e!r}")
                self._logger.append(f"[DiagnosticThread] Step {step.name} raised exception: {e!r}", channel="diagnostic", level="error")
                ok = False

            results[step.name] = ok
            self.step_state.emit(step.name, "ok" if ok else "fail")

        self.finished.emit(results)
        self._logger.append(f"[DiagnosticThread] run() completed with results: {results}", channel="diagnostic", level="debug")

class Diagnostic(QObject):
    def __init__(self, host: str, ports: Any, parent: Optional[QWidget] = None):
        super().__init__(parent)

        # Retrieve the application instance
        self._app: Application = QApplication.instance()

        # Ensure Client is created after Application
        if self._app is None:
            raise RuntimeError("Client must be created after QApplication/Application.")

        self._logger = self._app.logger or Log()
        self._helper = Helper()
        self._tools = Tools()
        self.host = (host or "").strip()
        self.ports = ports
        self._parent: Optional[QWidget] = None
        self._logger.append(f"[Diagnostic] Initialized with host={self.host!r}, ports={self.ports!r}", channel="diagnostic", level="debug")

        self._steps: list[DiagnosticStep] = []
        self._finished_listeners: list[Callable[[bool], None]] = []

    def add(
        self,
        name: str,
        label: str,
        icon: Optional[str],
        func: Callable[[Callable[[str], None]], bool]
    ) -> None:
        self._steps.append(DiagnosticStep(name, label, icon, func))
        self._logger.append(f"[Diagnostic] Added step: name={name}, label={label}, icon_provided={icon is not None}", channel="diagnostic", level="debug")

    def on_finish(self, fn: Callable[[bool], None]) -> None:
        """
        Register a listener to be called when the diagnostic run finishes.

        The listener will receive a single bool argument indicating overall success.
        """
        if callable(fn):
            self._finished_listeners.append(fn)
            self._logger.append(f"[Diagnostic] Added finished listener: {fn!r}", channel="diagnostic", level="debug")

    def show(
        self,
        parent: Optional[QWidget] = None,
        finished: Optional[Callable[[bool], None]] = None,
    ) -> QDialog:
        self._logger.append(f"[Diagnostic] show() called with parent type: {type(parent)}", channel="diagnostic", level="debug")
        if parent is not None and isinstance(parent, QWidget):
            self._parent = parent

        dlg = DiagnosticDialog(self._steps, parent=self._parent)
        self._logger.append(f"[Diagnostic] DiagnosticDialog created with {len(self._steps)} steps", channel="diagnostic", level="debug")

        for fn in self._finished_listeners:
            dlg.finished.connect(fn)

        if finished is not None:
            dlg.finished.connect(finished)

        self._logger.append("[Diagnostic] Diagnostics dialog about to be executed modally", channel="diagnostic", level="debug")
        dlg.exec_()
        self._logger.append("[Diagnostic] Diagnostics dialog returned from exec_", channel="diagnostic", level="debug")
        dlg.raise_()
        dlg.activateWindow()
        return dlg

class DiagnosticDialog(QDialog):
    finished = pyqtSignal(bool)

    def __init__(self, steps: Iterable[DiagnosticStep], parent: Optional[QWidget] = None):
        app = QApplication.instance()
        if app is not None and hasattr(app, "logger"):
            self._logger = app.logger
        else:
            self._logger = Log()

        super().__init__(parent)

        self._helper = Helper()
        self._steps = list(steps)
        self._logger.append(f"[DiagnosticDialog] Initialized with steps: {[step.name for step in self._steps]} (count={len(self._steps)})", channel="diagnostic", level="debug")

        self.setWindowTitle("Diagnostic")
        self.setObjectName("DiagnosticDialog")
        self.setWindowFlags(
            Qt.Dialog
            | Qt.WindowTitleHint
            | Qt.CustomizeWindowHint
            | Qt.WindowCloseButtonHint
        )
        self.setMinimumSize(700, 420)

        icons_path = self._helper.get_path("core/icons")
        circle_path = self._helper.join(icons_path, "circle.svg")
        spinner_path = self._helper.join(icons_path, "spinner.svg")
        error_path = self._helper.join(icons_path, "error.svg")
        success_path = self._helper.join(icons_path, "success.svg")

        self._status_icons = {
            "idle": QIcon(circle_path).pixmap(32, 32),
            "running": QIcon(spinner_path).pixmap(32, 32),
            "fail": QIcon(error_path).pixmap(32, 32),
            "ok": QIcon(success_path).pixmap(32, 32),
            "success": QIcon(success_path).pixmap(32, 32),
            "error": QIcon(error_path).pixmap(32, 32),
        }

        self._step_icon_labels: dict[str, QLabel] = {}

        top_layout = QHBoxLayout()
        top_layout.setSpacing(24)
        top_layout.setContentsMargins(16, 16, 16, 8)

        for step in self._steps:
            icon_label = SpinningIconLabel(spinner_path, size=32)
            icon_label.setStyleSheet("padding: 0px; margin: 0px; border: none;")
            icon_label.setFixedSize(32, 32)
            icon_label.setAlignment(Qt.AlignCenter)
            icon_label.setScaledContents(True)

            # initial state = idle (static)
            icon_label.setPixmap(self._status_icons["idle"])
            icon_label.stop()  # make sure timer isn't running

            text_label = QLabel(step.label)
            text_label.setAlignment(Qt.AlignCenter)

            step_layout = QVBoxLayout()
            step_layout.setSpacing(4)
            step_layout.addWidget(icon_label, alignment=Qt.AlignCenter)
            step_layout.addWidget(text_label, alignment=Qt.AlignCenter)
            step_layout.addStretch(1)

            container = QWidget()
            container.setLayout(step_layout)
            top_layout.addWidget(container, 1)

            self._step_icon_labels[step.name] = icon_label

        self.log = QTextEdit()
        self.log.setReadOnly(True)

        btns = QHBoxLayout()
        btns.setContentsMargins(16, 8, 16, 16)
        self.run_btn = QPushButton("Run Diagnostics")
        self.run_btn.clicked.connect(self.start_diagnostic)
        self.close_btn = QPushButton("Close")
        self.close_btn.clicked.connect(self.close)
        btns.addStretch(1)
        btns.addWidget(self.run_btn)
        if self._logger is not None:
            self.log_btn = QPushButton("Open Log")
            self.log_btn.clicked.connect(self._logger.show)
            btns.addWidget(self.log_btn)
        btns.addWidget(self.close_btn)

        root = QVBoxLayout(self)
        root.addLayout(top_layout)
        root.addWidget(self.log)
        root.addLayout(btns)

        self._thr: Optional[DiagnosticThread] = None

    def start_diagnostic(self):
        self._logger.append(f"[DiagnosticDialog] Starting diagnostics with {len(self._steps)} steps...", channel="diagnostic", level="debug")
        self.log.clear()

        for label in self._step_icon_labels.values():
            if isinstance(label, SpinningIconLabel):
                label.stop()
            label.setPixmap(self._status_icons["idle"])

        self.run_btn.setEnabled(False)

        self._thr = DiagnosticThread(self._steps, parent=self)
        self._thr.log.connect(self._on_log)
        self._thr.step_state.connect(self._on_step_state)
        self._thr.finished.connect(self._on_finished)
        self._thr.finished.connect(lambda _: self.run_btn.setEnabled(True))
        self._thr.start()
        self._logger.append("[DiagnosticDialog] DiagnosticThread created and started", channel="diagnostic", level="debug")

    def _on_log(self, s: str) -> None:
        self._logger.append(s, channel="diagnostic", level="debug")
        self.log.append(s)

    def _on_step_state(self, name: str, state: str) -> None:
        self._logger.append(
            f"[DiagnosticDialog] Step {name} state changed to {state}",
            channel="diagnostic",
            level="debug",
        )

        label = self._step_icon_labels.get(name)
        if not label:
            return

        # We only created SpinningIconLabel instances, but be defensive:
        if isinstance(label, SpinningIconLabel):
            if state == "idle":
                # QLabel behavior (static idle icon)
                label.stop()
                label.setPixmap(self._status_icons["idle"])

            elif state == "running":
                # SpinningIconLabel behavior (spinner icon + animation)
                label.setPixmap(self._status_icons["running"])
                label.start()

            elif state == "ok":
                # QLabel behavior (static success icon)
                label.stop()
                label.setPixmap(self._status_icons["ok"])

            elif state == "fail":
                # QLabel behavior (static error icon)
                label.stop()
                label.setPixmap(self._status_icons["fail"])

            else:
                # Unknown state -> default back to idle, no animation
                self._logger.append(
                    f"[DiagnosticDialog] Unknown state '{state}' for step '{name}', defaulting to idle",
                    channel="diagnostic",
                    level="warning",
                )
                label.stop()
                label.setPixmap(self._status_icons["idle"])

            return

        # Fallback for plain QLabel (just in case)
        pix = self._status_icons.get(state)
        if pix is not None:
            label.setPixmap(pix)

    def _on_finished(self, results: dict) -> None:
        success = all(bool(v) for v in results.values()) if results else False
        self._logger.append(f"[DiagnosticDialog] Diagnostics finished with results: {results}, success={success}", channel="diagnostic", level="debug")
        self.finished.emit(success)
