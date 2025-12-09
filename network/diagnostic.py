#!/usr/bin/env python3
# src/core/network/diagnostic.py

from __future__ import annotations

from typing import Any, Callable, Optional, Iterable

from PyQt5.QtCore import QThread, pyqtSignal, Qt, QObject
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QTextEdit, QWidget
)
from PyQt5.QtGui import QPixmap, QIcon

try:
    from core.helper import Helper
except ImportError:
    from helper import Helper

from .tools import Tools

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
        super().__init__(parent)
        self._steps = list(steps)

    def run(self):
        results = {}

        def printer(msg: str) -> None:
            self.log.emit(msg)

        for step in self._steps:
            self.step_state.emit(step.name, "running")
            try:
                res = step.func(printer)
                ok = bool(res)
            except Exception as e:
                printer(f"[{step.label}] error: {e!r}")
                ok = False

            results[step.name] = ok
            self.step_state.emit(step.name, "ok" if ok else "fail")

        self.finished.emit(results)

class Diagnostic(QObject):
    def __init__(self, host: str, ports: Any, parent: Optional[QWidget] = None):
        super().__init__(parent)

        self._helper = Helper()
        self._tools = Tools()
        self.host = (host or "").strip()
        self.ports = ports
        self._parent: Optional[QWidget] = None

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

    def on_finish(self, fn: Callable[[bool], None]) -> None:
        """
        Register a listener to be called when the diagnostic run finishes.

        The listener will receive a single bool argument indicating overall success.
        """
        if callable(fn):
            self._finished_listeners.append(fn)

    def show(
        self,
        parent: Optional[QWidget] = None,
        finished: Optional[Callable[[bool], None]] = None,
    ) -> QDialog:
        if parent is not None and isinstance(parent, QWidget):
            self._parent = parent

        dlg = DiagnosticDialog(self._steps, parent=self._parent)

        for fn in self._finished_listeners:
            dlg.finished.connect(fn)

        if finished is not None:
            dlg.finished.connect(finished)

        dlg.exec_()
        dlg.raise_()
        dlg.activateWindow()
        return dlg

class DiagnosticDialog(QDialog):
    finished = pyqtSignal(bool)

    def __init__(self, steps: Iterable[DiagnosticStep], parent: Optional[QWidget] = None):
        super().__init__(parent)

        self._helper = Helper()
        self._steps = list(steps)

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
            "idle": QIcon(circle_path).pixmap(48, 48),
            "running": QIcon(spinner_path).pixmap(48, 48),
            "fail": QIcon(error_path).pixmap(48, 48),
            "ok": QIcon(success_path).pixmap(48, 48),
            "success": QIcon(success_path).pixmap(48, 48),
            "error": QIcon(error_path).pixmap(48, 48),
        }

        self._step_icon_labels: dict[str, QLabel] = {}

        top_layout = QHBoxLayout()
        top_layout.setSpacing(24)
        top_layout.setContentsMargins(16, 16, 16, 8)

        for step in self._steps:
            icon_label = QLabel()
            icon_label.setPixmap(self._status_icons["idle"])
            icon_label.setFixedSize(48, 48)
            icon_label.setAlignment(Qt.AlignCenter)

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
        self.close_btn = QPushButton("Close")
        self.run_btn.clicked.connect(self.start_diagnostic)
        self.close_btn.clicked.connect(self.close)
        btns.addStretch(1)
        btns.addWidget(self.run_btn)
        btns.addWidget(self.close_btn)

        root = QVBoxLayout(self)
        root.addLayout(top_layout)
        root.addWidget(self.log)
        root.addLayout(btns)

        self._thr: Optional[DiagnosticThread] = None

    def start_diagnostic(self):
        self.log.clear()
        for label in self._step_icon_labels.values():
            label.setPixmap(self._status_icons["idle"])
        self.run_btn.setEnabled(False)

        self._thr = DiagnosticThread(self._steps, parent=self)
        self._thr.log.connect(self._on_log)
        self._thr.step_state.connect(self._on_step_state)
        self._thr.finished.connect(self._on_finished)
        self._thr.finished.connect(lambda _: self.run_btn.setEnabled(True))
        self._thr.start()

    def _on_log(self, s: str) -> None:
        self.log.append(s)

    def _on_step_state(self, name: str, state: str) -> None:
        label = self._step_icon_labels.get(name)
        if not label:
            return
        if state == "running":
            pix = self._status_icons["running"]
        elif state == "ok":
            pix = self._status_icons["success"]
        elif state == "fail":
            pix = self._status_icons["error"]
        else:
            pix = self._status_icons["idle"]
        label.setPixmap(pix)

    def _on_finished(self, results: dict) -> None:
        success = all(bool(v) for v in results.values()) if results else False
        self.finished.emit(success)
