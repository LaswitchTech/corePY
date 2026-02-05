#!/usr/bin/env python3
# src/core/service.py

from __future__ import annotations

import os
import sys
import time
import signal
import shutil
import subprocess
import platform
import ctypes
import json
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Callable, Optional
from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import (
    QApplication,
    QDialog,
    QTabWidget,
    QGroupBox,
    QVBoxLayout,
    QHBoxLayout,
    QFormLayout,
    QLabel,
    QPushButton,
    QPlainTextEdit,
    QMessageBox,
)


@dataclass
class _Task:
    description: str
    callable: Callable[..., Any]
    interval: float = 60.0
    last_run: float = 0.0


class Service:
    """Service module for corePY.

    This class is **not** an application runner like CommandLine / Application.
    It is a module that:
      - Adds service-management commands to the CLI (start/stop/restart/install/uninstall)
      - Allows apps to register tasks to be executed by the service loop

    Usage pattern (from an app/handler):

        def cli(self, cli):
            cli.add("run", "Run jobs", self.run)
            cli.service.add("run", "Run jobs", self.run, interval=60)

    Then:
        python main.py --start

    runs all registered service tasks in a loop.
    """

    def __init__(self, logger=None, configuration=None):
        self._tasks: dict[str, _Task] = {}
        self._stop_requested = False
        self._loop_sleep = 1.0
        self._pidfile_path: Optional[str] = None

        # Optional integrations (passed by CommandLine)
        self._logger = logger
        self._configuration = configuration

        # Service configuration defaults
        if self._configuration is not None:
            # How often the main loop wakes up to check due tasks (seconds)
            self._configuration.add("service.loopSleep", 1, "number", label="Service loop sleep (s)", min=1, max=60)
            # Default interval used when registering tasks (seconds)
            self._configuration.add("service.defaultInterval", 3600, "number", label="Service default task interval (s)", min=1, max=86400)
            # Reverse-DNS prefix used to name/register the service (e.g. com.laswitchtech)
            # The final label becomes: <domain>.<appname> (lowercased and sanitized)
            self._configuration.add("service.domain", "com.corepy", "text", label="Service domain (reverse DNS)")
            # Persist any new defaults
            self._configuration.save()

            # Initial visibility update
            self._refresh_action_visibility()

            # Keep visibility in sync when configuration is saved/changed
            try:
                self._configuration.configChanged.connect(lambda _cfg: self._refresh_action_visibility())
            except Exception:
                pass
    def _service_domain(self) -> str:
        """Return reverse-DNS domain used for service labels.

        Prefer configuration key `service.domain` when available; fall back to
        an environment override COREPY_SERVICE_DOMAIN; then to 'com.corepy'.
        """
        # 1) Configuration
        try:
            if self._configuration is not None:
                v = (self._configuration.get("service.domain", "") or "").strip()
                if v:
                    return v
        except Exception:
            pass

        # 2) Environment override
        v = (os.environ.get("COREPY_SERVICE_DOMAIN") or "").strip()
        if v:
            return v

        return "com.corepy"
    def _refresh_action_visibility(self) -> None:
        """Show/hide configuration action buttons based on install/running state."""
        if self._configuration is None:
            return

        installed = self._is_installed()
        running = self._is_service_active() if installed else self._is_running()

        # Start/Stop are mutually exclusive when installed; when not installed, start runs in-process.
        self._configuration.visibility("service.actions.start", not running)
        self._configuration.visibility("service.actions.stop", running)

        # Restart only makes sense when running
        self._configuration.visibility("service.actions.restart", running)

        # Install/Uninstall are mutually exclusive
        self._configuration.visibility("service.actions.install", not installed)
        self._configuration.visibility("service.actions.uninstall", installed)

    # ------------------------------------------------------------------
    # CLI integration
    # ------------------------------------------------------------------

    def cli(self, cli=None) -> None:
        """Register service management commands into CommandLine."""
        if cli is None:
            return

        cli.add("service.status", "Show service status.", self.status)
        cli.add("service.start", "Start the service loop.", self.start)
        cli.add("service.stop", "Stop the running service.", self.stop)
        cli.add("service.restart", "Restart the running service.", self.restart)
        cli.add("service.install", "Install as a python service.", self.install)
        cli.add("service.uninstall", "Uninstall the python service.", self.uninstall)

    # ------------------------------------------------------------------
    # Task registry
    # ------------------------------------------------------------------

    def add(
        self,
        command: str,
        description: str = "",
        callable: Optional[Callable[..., Any]] = None,
        *,
        interval: Optional[float] = None,
    ) -> None:
        """Register a command to run as part of the service loop.

        Notes:
          - The command name is informational here (used for logs / debugging).
          - The callable is invoked with **no arguments** by the service loop.
          - Use closures/lambdas if you need to bind context.
        """
        if callable is None:
            raise ValueError("Service.add() requires a callable")

        # Keep the name consistent with CLI commands (no leading -- expected here)
        normalized = command[2:] if command.startswith("--") else command

        # Resolve effective interval from configuration (if present)
        # - interval is None: use configured service.defaultInterval (default 3600)
        # - interval == 0: run every tick
        # - interval > 0: use that value (and allow override via per-task config)
        if interval is None:
            effective_interval = 0.0
        else:
            effective_interval = float(interval)

        if self._configuration is not None:
            key = f"service.intervals.{normalized}"

            # Schema default for this task's interval
            if interval is None:
                schema_default = int(self._configuration.get("service.defaultInterval", 3600) or 3600)
            else:
                schema_default = int(effective_interval)

            self._configuration.add(
                key,
                schema_default,
                "spin",
                label=f"Service interval: {normalized} (s)",
                min=0,
                max=86400,
            )

            try:
                configured = self._configuration.get(key, schema_default)
                effective_interval = float(configured) if configured is not None else float(schema_default)
            except Exception:
                effective_interval = float(schema_default)

            self._configuration.save()
        else:
            # No configuration system; if interval is None, fall back to 3600
            if interval is None:
                effective_interval = 3600.0

        self._tasks[normalized] = _Task(
            description=description,
            callable=callable,
            interval=effective_interval,
            last_run=0.0,
        )

    def remove(self, command: str) -> None:
        normalized = command[2:] if command.startswith("--") else command
        if normalized in self._tasks:
            del self._tasks[normalized]

    def clear(self) -> None:
        self._tasks.clear()

    def set_loop_sleep(self, seconds: float) -> None:
        """How often to wake up to check due tasks."""
        self._loop_sleep = max(0.1, float(seconds))

    def _log(self, message: str, level: str = "info") -> None:
        """Log to core Log module if available, else print."""
        try:
            if self._logger is not None and hasattr(self._logger, "append"):
                self._logger.append(message, channel="service", level=level)
                return
        except Exception:
            pass

        # Fallback to console
        if level == "error":
            print(message, file=sys.stderr)
        else:
            print(message)

    # ------------------------------------------------------------------
    # Service control
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Enter the service loop in the current process."""
        self._stop_requested = False

        # If installed, either:
        # - We are a *manager* request (user invoked --start): delegate to OS manager, OR
        # - We are the *service process* (OS invoked --start): run the loop in-process.
        if self._is_installed() and not self._running_under_service_manager():
            self._service_manager_start()
            self._refresh_action_visibility()
            return

        # Apply configured loop sleep if available
        if self._configuration is not None:
            try:
                self._loop_sleep = float(self._configuration.get("service.loopSleep", 1) or 1)
            except Exception:
                self._loop_sleep = 1.0

        self._install_signal_handlers()

        pidfile = self._pidfile()
        if self._is_running():
            self._log(f"Service already running (pidfile: {pidfile}).", level="info")
            return

        os.makedirs(os.path.dirname(pidfile), exist_ok=True)
        with open(pidfile, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))

        self._log(f"Service started (PID {os.getpid()}).", level="info")

        try:
            self._service_loop()
        finally:
            self._cleanup_pidfile()
            self._log("Service stopped.", level="info")

    def stop(self) -> None:
        """Stop a running service by signaling the PID from the pidfile."""
        # If installed, delegate to platform service manager.
        if self._is_installed():
            self._service_manager_stop()
            self._refresh_action_visibility()
            return

        pid = self._read_pidfile()
        if not pid:
            self._log("Service is not running (no pidfile).", level="info")
            return

        if not self._pid_exists(pid):
            self._log("Service pidfile found but process is not running. Cleaning up.", level="warning")
            self._cleanup_pidfile()
            return

        try:
            os.kill(pid, signal.SIGTERM)
            self._log(f"Stop signal sent to PID {pid}.", level="info")
        except PermissionError:
            self._log(f"Permission denied sending SIGTERM to PID {pid}.", level="error")
        except ProcessLookupError:
            self._log(f"Process {pid} not found. Cleaning up pidfile.", level="error")
            self._cleanup_pidfile()

    def restart(self) -> None:
        # If installed, delegate to platform service manager.
        if self._is_installed():
            self._service_manager_restart()
            self._refresh_action_visibility()
            return

        # Otherwise run in-process restart
        self.stop()
        time.sleep(0.5)
        self.start()

    def status(self) -> None:
        """Show current service status."""
        pidfile = self._pidfile()
        pid = self._read_pidfile()

        installed = self._is_installed()
        running = self._is_service_active() if installed else bool(pid and self._pid_exists(pid))

        # Build a readable status
        lines = []
        lines.append(f"Service: {self._app_name()}")
        lines.append(f"PID file: {pidfile}")
        lines.append(f"Installed: {'yes' if installed else 'no'}")
        lines.append(f"Running: {'yes' if running else 'no'}")
        if pid:
            lines.append(f"PID: {pid}")
        lines.append(f"Tasks registered: {len(self._tasks)}")

        # Show intervals (effective)
        if self._configuration is not None and self._tasks:
            lines.append("Task intervals (s):")
            for name in sorted(self._tasks.keys()):
                key = f"service.intervals.{name}"
                interval = self._configuration.get(key, self._tasks[name].interval)
                lines.append(f"  - {name}: {interval}")

        msg = "\n".join(lines)
        print(msg)
        self._log(msg, level="info")


    # ------------------------------------------------------------------
    # UI helper
    # ------------------------------------------------------------------

    def open_manager_dialog(self, parent=None) -> None:
        """Open a small GUI dialog to manage the service (install/start/stop + logs).

        This is intentionally optional: apps can call it when running with a GUI.
        """
        try:
            dlg = ServiceManagerDialog(self, parent=parent)
            dlg.exec_()
        except Exception as e:
            # If called from a non-GUI context, fail gracefully.
            self._log(f"Failed to open service manager dialog: {e}", level="error")

    def request_stop(self) -> None:
        """Request the current service loop to stop (used by signal handlers)."""
        self._stop_requested = True

    # ------------------------------------------------------------------
    # Scheduler loop
    # ------------------------------------------------------------------

    def _service_loop(self) -> None:
        if not self._tasks:
            self._log("Warning: no tasks registered for service mode.", level="warning")

        while not self._stop_requested:
            now = time.time()

            for name, task in list(self._tasks.items()):
                # interval == 0 means "run every tick"
                due = (task.interval <= 0) or (now - task.last_run >= task.interval)
                if not due:
                    continue

                try:
                    task.callable()
                    task.last_run = now
                except Exception as e:
                    # Keep the service alive; print error.
                    self._log(f"Task {name} failed: {e}", level="error")

            time.sleep(self._loop_sleep)

    # ------------------------------------------------------------------
    # systemd integration (Linux only)
    # ------------------------------------------------------------------

    def install(self) -> None:

        if sys.platform.startswith("win"):
            self._install_windows_service()
        elif sys.platform == "darwin":
            self._install_macos_launchd()
        else:
            self._install_systemd()

        self._refresh_action_visibility()

    def uninstall(self) -> None:

        if sys.platform.startswith("win"):
            self._uninstall_windows_service()
        elif sys.platform == "darwin":
            self._uninstall_macos_launchd()
        else:
            self._uninstall_systemd()

        self._refresh_action_visibility()

    def _install_systemd(self) -> None:
        """Install a systemd unit that runs `<python> <main.py> --start`."""
        if shutil.which("systemctl") is None:
            raise RuntimeError("systemctl not found; systemd does not appear available")

        service_name = f"{self._app_name()}.service"
        unit_path = os.path.join("/etc/systemd/system", service_name)

        program_start, argv_start, workdir = self._service_command("start")
        program_stop, argv_stop, _ = self._service_command("stop")

        unit = self._systemd_unit(
            service_name=service_name,
            python_exe=program_start,
            entrypoint=" ".join(argv_start),
            working_dir=workdir,
            pidfile=self._pidfile(),
            execstop_program=program_stop,
            execstop_args=" ".join(argv_stop),
        )

        with open(unit_path, "w", encoding="utf-8") as f:
            f.write(unit)

        subprocess.run(["systemctl", "daemon-reload"], check=False)
        subprocess.run(["systemctl", "enable", "--now", service_name], check=False)

        print(f"Installed and started: {unit_path}")

    def _uninstall_systemd(self) -> None:
        if shutil.which("systemctl") is None:
            raise RuntimeError("systemctl not found; systemd does not appear available")

        service_name = f"{self._app_name()}.service"
        unit_path = os.path.join("/etc/systemd/system", service_name)

        subprocess.run(["systemctl", "disable", "--now", service_name], check=False)
        if os.path.exists(unit_path):
            os.remove(unit_path)
        subprocess.run(["systemctl", "daemon-reload"], check=False)

        print(f"Uninstalled: {unit_path}")

    def _systemd_unit(
        self,
        *,
        service_name: str,
        python_exe: str,
        entrypoint: str,
        working_dir: str,
        pidfile: str,
        execstop_program: str,
        execstop_args: str,
    ) -> str:
        app_name = self._app_name()
        return (
            "[Unit]\n"
            f"Description={app_name}\n"
            "After=network.target\n\n"
            "[Service]\n"
            "Type=simple\n"
            "Environment=COREPY_RUN_AS_SERVICE=1\n"
            f"Environment=COREPY_SERVICE_LABEL={self._service_label()}\n"
            f"WorkingDirectory={working_dir}\n"
            f"ExecStart={python_exe} {entrypoint}\n"
            f"ExecStop={execstop_program} {execstop_args}\n"
            "Restart=on-failure\n"
            "RestartSec=2\n"
            f"PIDFile={pidfile}\n"
            "\n"
            "[Install]\n"
            "WantedBy=multi-user.target\n"
        )
    def _running_under_service_manager(self) -> bool:
        """Return True if this process should run the service loop.

        When installed, `start()` is used both to *request* the service manager to
        start the job and as the entrypoint that the service manager executes.

        We differentiate the two cases using an explicit env flag set by the
        service definitions (launchd/systemd/Windows), plus a couple of platform
        hints as fallback.
        """
        # Explicit flag (preferred)
        flag = (os.environ.get("COREPY_RUN_AS_SERVICE") or "").strip().lower()
        if flag in ("1", "true", "yes", "on"):
            return True

        # Fallback hints
        if sys.platform == "darwin":
            # launchd commonly sets this for jobs
            xpc = (os.environ.get("XPC_SERVICE_NAME") or "").strip()
            if xpc and xpc == self._service_label():
                return True

        if not sys.platform.startswith("win"):
            # systemd sets INVOCATION_ID for services
            if os.environ.get("INVOCATION_ID"):
                return True

        return False

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _app_name(self) -> str:
        # Try to read from Qt application if present, else fallback.
        try:
            inst = QApplication.instance()
            if inst and inst.applicationName():
                return inst.applicationName()
        except Exception:
            pass
        return "corepy"

    def _pidfile(self) -> str:
        if self._pidfile_path:
            return self._pidfile_path
        self._pidfile_path = self._default_pidfile_path()
        return self._pidfile_path

    def _default_pidfile_path(self) -> str:
        name = self._app_name() or "corepy"
        candidate = os.path.join("/run", f"{name}.pid")
        try:
            os.makedirs(os.path.dirname(candidate), exist_ok=True)
            if os.access(os.path.dirname(candidate), os.W_OK):
                return candidate
        except Exception:
            pass
        return os.path.join("/tmp", f"{name}.pid")

    def _read_pidfile(self) -> Optional[int]:
        try:
            with open(self._pidfile(), "r", encoding="utf-8") as f:
                raw = f.read().strip()
            return int(raw) if raw else None
        except Exception:
            return None

    def _cleanup_pidfile(self) -> None:
        try:
            path = self._pidfile()
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass

    def _pid_exists(self, pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except Exception:
            return False

    def _is_running(self) -> bool:
        pid = self._read_pidfile()
        return bool(pid and self._pid_exists(pid))

    def _install_signal_handlers(self) -> None:
        def _handler(_sig, _frame):
            self.request_stop()

        try:
            signal.signal(signal.SIGTERM, _handler)
            signal.signal(signal.SIGINT, _handler)
        except Exception:
            pass


    # ------------------------------------------------------------------
    # Windows elevation helpers
    # ------------------------------------------------------------------

    def _windows_is_admin(self) -> bool:
        """Return True if the current process has admin rights on Windows."""
        if not sys.platform.startswith("win"):
            return False
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False

    def _windows_relaunch_elevated(self, argv: list[str]) -> bool:
        """Relaunch the current executable with UAC elevation.

        Returns True if the elevation prompt was successfully triggered.
        """
        if not sys.platform.startswith("win"):
            return False

        # Avoid infinite loops if something goes wrong.
        if (os.environ.get("COREPY_ELEVATED_RELAUNCH") or "").strip() == "1":
            return False

        try:
            # Build argument string for ShellExecuteW.
            # Use subprocess.list2cmdline to quote correctly for Windows.
            args = list(argv[1:]) + ["--elevated-relaunch"]
            params = subprocess.list2cmdline(args)

            # Pass a marker to the elevated process.
            # We cannot reliably set environment variables via ShellExecute,
            # so we append an internal flag and also set an env var for same-process codepaths.
            os.environ["COREPY_ELEVATED_RELAUNCH"] = "1"

            # 1 = SW_SHOWNORMAL
            rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", argv[0], params, None, 1)
            return int(rc) > 32
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Cross-platform service manager helpers
    # ------------------------------------------------------------------

    def _service_label(self) -> str:
        """Stable label used for OS service registration.

        - macOS launchd prefers reverse-DNS labels.
        - Windows 'sc' service names and systemd unit names also benefit from stability.

        Result: <domain>.<appname> (lowercased and sanitized)
        """
        domain = (self._service_domain() or "com.corepy").strip().strip(".")
        app = (self._app_name() or "corepy").strip()

        base = f"{domain}.{app}" if domain else app
        base = base.lower()

        # Keep only characters typically safe across platforms; replace others with '-'
        safe = "".join(c if c.isalnum() or c in ("-", "_", ".") else "-" for c in base)
        # Avoid accidental leading/trailing dots
        safe = safe.strip(".")
        return safe or "com.corepy.corepy"

    def _entry_command(self) -> tuple[str, str, str]:
        """Return (python_exe, entrypoint, working_dir) for service registration.

        Notes:
        - When `sys.argv[0]` is a relative path and the current working directory
          is already inside `src/`, it's easy to end up with paths like `src/src/main.py`.
        - launchd will happily try to execute that path, but the process will fail.

        This helper resolves the path and applies a small normalization pass.
        """
        python_exe = sys.executable

        # Resolve argv[0] robustly (handles relative paths)
        try:
            p = Path(sys.argv[0]).expanduser()
            entry_path = (p if p.is_absolute() else (Path.cwd() / p)).resolve()
        except Exception:
            entry_path = Path(os.path.abspath(sys.argv[0]))

        def _dedupe_consecutive(parts: list[str]) -> list[str]:
            out: list[str] = []
            for seg in parts:
                if out and out[-1] == seg:
                    continue
                out.append(seg)
            return out

        # If we ended up with duplicated segments like .../src/src/..., collapse them.
        parts = list(entry_path.parts)
        deduped = Path(*_dedupe_consecutive(parts))

        # Prefer the deduped version if it exists.
        if deduped.exists():
            entry_path = deduped
        else:
            # Common case: /.../src/src/main.py -> /.../src/main.py
            # If the path contains two consecutive 'src' segments, drop one.
            if "src" in parts:
                try:
                    new_parts: list[str] = []
                    i = 0
                    while i < len(parts):
                        if i + 1 < len(parts) and parts[i] == parts[i + 1]:
                            # Drop the duplicate segment
                            new_parts.append(parts[i])
                            i += 2
                            continue
                        new_parts.append(parts[i])
                        i += 1
                    candidate = Path(*new_parts)
                    if candidate.exists():
                        entry_path = candidate
                except Exception:
                    pass

        entry = str(entry_path)
        workdir = str(entry_path.parent)
        return python_exe, entry, workdir

    def _is_installed(self) -> bool:
        """Best-effort check whether this app is installed as a service."""
        try:
            if sys.platform.startswith("win"):
                name = self._service_label()
                # sc query returns non-zero if missing
                r = subprocess.run(["sc", "query", name], capture_output=True, text=True)
                return r.returncode == 0
            if sys.platform == "darwin":
                return self._macos_plist_path().exists()
            # linux
            if shutil.which("systemctl") is None:
                return False
            service_name = f"{self._service_label()}.service"
            r = subprocess.run(["systemctl", "is-enabled", service_name], capture_output=True, text=True)
            return r.returncode == 0
        except Exception:
            return False

    def _is_service_active(self) -> bool:
        """Check if the installed service is running (best-effort)."""
        try:
            if sys.platform.startswith("win"):
                name = self._service_label()
                r = subprocess.run(["sc", "query", name], capture_output=True, text=True)
                if r.returncode != 0:
                    return False
                out = (r.stdout or "") + (r.stderr or "")
                return "RUNNING" in out.upper()
            if sys.platform == "darwin":
                label = self._service_label()
                r = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{label}"], capture_output=True, text=True)
                if r.returncode != 0:
                    return False
                out = (r.stdout or "") + (r.stderr or "")
                o = out.lower()
                # Typical indicators in `launchctl print` output
                if "job state = running" in o:
                    return True
                if "state = running" in o:
                    return True
                # If explicitly exited, consider not running
                if "job state = exited" in o:
                    return False
                # Fallback: loaded but ambiguous => treat as active
                return True
            # linux
            if shutil.which("systemctl") is None:
                return False
            service_name = f"{self._service_label()}.service"
            r = subprocess.run(["systemctl", "is-active", service_name], capture_output=True, text=True)
            return r.returncode == 0
        except Exception:
            return False

    def _service_manager_start(self) -> None:
        label = self._service_label()
        try:
            if sys.platform.startswith("win"):
                # Prefer NSSM if bundled
                if self._windows_has_nssm():
                    self._windows_nssm_run(["start", label])
                else:
                    subprocess.run(["sc", "start", label], check=False)
                self._log(f"Requested start for Windows service: {label}")
                return
            if sys.platform == "darwin":
                plist = self._macos_plist_path()
                domain = f"gui/{os.getuid()}"
                target = f"{domain}/{label}"

                # If it's already loaded, bootstrap may fail with error 5.
                already_loaded = False
                try:
                    r = subprocess.run(["launchctl", "print", target], capture_output=True, text=True)
                    already_loaded = (r.returncode == 0)
                except Exception:
                    already_loaded = False

                if plist.exists() and not already_loaded:
                    subprocess.run(["launchctl", "bootstrap", domain, str(plist)], check=False)

                # Enable + kickstart regardless (these are safe even if already loaded)
                subprocess.run(["launchctl", "enable", target], check=False)
                subprocess.run(["launchctl", "kickstart", "-k", target], check=False)

                self._log(f"Requested start for launchd agent: {label}")
                return
            # linux
            service_name = f"{label}.service"
            subprocess.run(["systemctl", "start", service_name], check=False)
            self._log(f"Requested start for systemd service: {service_name}")
        except Exception as e:
            self._log(f"Failed to start service via manager: {e}", level="error")

    def _service_manager_stop(self) -> None:
        label = self._service_label()
        try:
            if sys.platform.startswith("win"):
                # Prefer NSSM if bundled
                if self._windows_has_nssm():
                    self._windows_nssm_run(["stop", label])
                else:
                    subprocess.run(["sc", "stop", label], check=False)
                self._log(f"Requested stop for Windows service: {label}")
                return
            if sys.platform == "darwin":
                subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"], check=False)
                self._log(f"Requested stop for launchd agent: {label}")
                return
            service_name = f"{label}.service"
            subprocess.run(["systemctl", "stop", service_name], check=False)
            self._log(f"Requested stop for systemd service: {service_name}")
        except Exception as e:
            self._log(f"Failed to stop service via manager: {e}", level="error")

    def _service_manager_restart(self) -> None:
        label = self._service_label()
        try:
            if sys.platform.startswith("win"):
                if self._windows_has_nssm():
                    self._windows_nssm_run(["restart", label])
                else:
                    subprocess.run(["sc", "stop", label], check=False)
                    time.sleep(0.5)
                    subprocess.run(["sc", "start", label], check=False)
                self._log(f"Requested restart for Windows service: {label}")
                return
            if sys.platform == "darwin":
                subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"], check=False)
                self._log(f"Requested restart for launchd agent: {label}")
                return
            service_name = f"{label}.service"
            subprocess.run(["systemctl", "restart", service_name], check=False)
            self._log(f"Requested restart for systemd service: {service_name}")
        except Exception as e:
            self._log(f"Failed to restart service via manager: {e}", level="error")

    # ------------------------------------------------------------------
    # Windows service install/uninstall (sc.exe)
    # ------------------------------------------------------------------

    def _install_windows_service(self) -> None:
        """Install Windows service.

        Prefer NSSM when available (bundled in corePY).

        Why:
        - `sc create` + a long `cmd.exe /c ...` binPath is fragile.
        - NSSM hosts a normal console program and reports service state properly.
        """
        name = self._service_label()

        # Installing a Windows service requires elevation.
        if not self._windows_is_admin():
            self._log("Admin privileges required to install the service. Requesting elevation...", level="info")
            if self._windows_relaunch_elevated(sys.argv):
                return
            raise RuntimeError("Admin privileges required to install the service.")

        # Use NSSM if present.
        if not self._windows_has_nssm():
            raise RuntimeError(
                "NSSM was not found. Expected one of: "
                "src/bin/nssm/windows/x86/nssm.exe or src/bin/nssm/windows/x86_64/nssm.exe (or the same under core/src/bin)."
            )

        # Resolve the command the service should run.
        program, argv, workdir = self._service_command("start")

        # Install directory: stable place for logs and (optionally) copied binaries.
        install_dir = self._windows_install_dir()
        install_dir.mkdir(parents=True, exist_ok=True)

        logs_dir = install_dir.parent / "log"
        logs_dir.mkdir(parents=True, exist_ok=True)

        stdout_log = logs_dir / f"{name}.out.log"
        stderr_log = logs_dir / f"{name}.err.log"

        # In frozen mode, it's safer to point NSSM at a stable path.
        # If we're already running from Program Files/ProgramData, we can use it directly.
        # Otherwise (common during dev / unpackaged runs), we still allow it, but you may
        # want to set COREPY_SERVICE_EXECUTABLE to an installed *-cli.exe.
        app_program = program
        app_dir = workdir

        # Install (create) service
        self._windows_nssm_run(["install", name, app_program] + argv)

        # Configure working directory
        self._windows_nssm_run(["set", name, "AppDirectory", app_dir])

        # Environment variables so `start()` knows this is the service process
        env_extra = "COREPY_RUN_AS_SERVICE=1\r\n" + f"COREPY_SERVICE_LABEL={name}" + "\r\n"
        self._windows_nssm_run(["set", name, "AppEnvironmentExtra", env_extra])

        # Logging
        self._windows_nssm_run(["set", name, "AppStdout", str(stdout_log)])
        self._windows_nssm_run(["set", name, "AppStderr", str(stderr_log)])
        self._windows_nssm_run(["set", name, "AppRotateFiles", "1"])
        self._windows_nssm_run(["set", name, "AppRotateOnline", "1"])
        self._windows_nssm_run(["set", name, "AppRotateSeconds", "86400"])
        self._windows_nssm_run(["set", name, "AppRotateBytes", str(10 * 1024 * 1024)])

        # Startup
        self._windows_nssm_run(["set", name, "Start", "SERVICE_AUTO_START"])

        # Restart policy
        self._windows_nssm_run(["set", name, "AppExit", "Default", "Restart"])

        self._log(f"Installed Windows service (NSSM): {name}")

    def _uninstall_windows_service(self) -> None:
        name = self._service_label()

        # Uninstalling a Windows service requires elevation.
        if not self._windows_is_admin():
            self._log("Admin privileges required to uninstall the service. Requesting elevation...", level="info")
            if self._windows_relaunch_elevated(sys.argv):
                return
            raise RuntimeError("Admin privileges required to uninstall the service.")

        # Prefer NSSM removal if available
        if self._windows_has_nssm():
            # Stop (best-effort)
            self._windows_nssm_run(["stop", name], check=False)
            # remove <name> confirm
            self._windows_nssm_run(["remove", name, "confirm"], check=False)
            self._log(f"Uninstalled Windows service (NSSM): {name}")
            return

        # Fallback to sc
        subprocess.run(["sc", "stop", name], check=False)
        r = subprocess.run(["sc", "delete", name], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"sc delete failed: {(r.stdout or '') + (r.stderr or '')}")
        self._log(f"Uninstalled Windows service: {name}")

    # ------------------------------------------------------------------
    # macOS launchd install/uninstall (per-user LaunchAgent)
    # ------------------------------------------------------------------

    def _macos_plist_path(self) -> Path:
        # Per-user agent (no root required)
        return Path.home() / "Library" / "LaunchAgents" / f"{self._service_label()}.plist"

    def _install_macos_launchd(self) -> None:
        label = self._service_label()
        program, argv, workdir = self._service_command("start")
        plist_path = self._macos_plist_path()
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        # Ensure logs directory exists
        logs_dir = Path.home() / "Library" / "Logs" / self._app_name()
        logs_dir.mkdir(parents=True, exist_ok=True)

        # Simple LaunchAgent; keep it alive, run at load.
        plist = (
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
            "<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n"
            "<plist version=\"1.0\">\n"
            "<dict>\n"
            f"  <key>Label</key><string>{label}</string>\n"
            "  <key>ProgramArguments</key>\n"
            "  <array>\n"
            f"    <string>{program}</string>\n"
            + "".join(f"    <string>{a}</string>\n" for a in argv)
            + "  </array>\n"
            f"  <key>WorkingDirectory</key><string>{workdir}</string>\n"
            "  <key>EnvironmentVariables</key>\n"
            "  <dict>\n"
            "    <key>COREPY_RUN_AS_SERVICE</key><string>1</string>\n"
            f"    <key>COREPY_SERVICE_LABEL</key><string>{label}</string>\n"
            "  </dict>\n"
            "  <key>RunAtLoad</key><true/>\n"
            "  <key>KeepAlive</key><true/>\n"
            f"  <key>StandardOutPath</key><string>{Path.home() / 'Library' / 'Logs' / self._app_name() / 'service.out.log'}</string>\n"
            f"  <key>StandardErrorPath</key><string>{Path.home() / 'Library' / 'Logs' / self._app_name() / 'service.err.log'}</string>\n"
            "</dict>\n"
            "</plist>\n"
        )

        plist_path.write_text(plist, encoding="utf-8")

        # Load into the current user's launchd
        subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist_path)], check=False)
        subprocess.run(["launchctl", "enable", f"gui/{os.getuid()}/{label}"], check=False)
        subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"], check=False)

        self._log(f"Installed launchd LaunchAgent: {plist_path}")

    def _uninstall_macos_launchd(self) -> None:
        label = self._service_label()
        plist_path = self._macos_plist_path()

        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"], check=False)
        if plist_path.exists():
            try:
                plist_path.unlink()
            except Exception:
                pass

        self._log(f"Uninstalled launchd LaunchAgent: {label}")

    def _service_command(self, action: str) -> tuple[str, list[str], str]:
        """Return (program, argv, working_dir) for service manager registration.

        Supports both source runs (python + entry script) and frozen bundles (.exe/.app).

        Environment overrides:
          - COREPY_SERVICE_EXECUTABLE: absolute/relative path to the executable to register
            (useful to force a sibling *-cli.exe on Windows).
        """
        action = (action or "").strip().lstrip("-")
        if action not in ("start", "stop"):
            raise ValueError(f"Invalid service action: {action}")

        # Frozen bundle: run the executable directly.
        if getattr(sys, "frozen", False):
            exe_path = Path(sys.executable).expanduser()

            # Optional override (preferred for Windows to target a console *-cli.exe)
            override = (os.environ.get("COREPY_SERVICE_EXECUTABLE") or "").strip().strip('"')
            if override:
                ov = Path(override).expanduser()
                if not ov.is_absolute():
                    ov = (Path.cwd() / ov).resolve()
                if ov.exists():
                    exe_path = ov

            # Best-effort Windows: if running GUI exe, prefer sibling "-cli.exe".
            if sys.platform.startswith("win"):
                try:
                    s = str(exe_path)
                    if s.lower().endswith(".exe") and not s.lower().endswith("-cli.exe"):
                        cand = Path(s[:-4] + "-cli.exe")
                        if cand.exists():
                            exe_path = cand
                except Exception:
                    pass

            program = str(exe_path)
            workdir = str(exe_path.parent)
            argv = [f"--{action}"]
            return program, argv, workdir

        # Source run: python + entrypoint
        python_exe, entry, workdir = self._entry_command()
        program = python_exe
        argv = [entry, f"--{action}"]
        return program, argv, workdir

    def _windows_is_64bit(self) -> bool:
        try:
            return platform.machine().endswith("64") or ("PROGRAMFILES(X86)" in os.environ)
        except Exception:
            return True

    def _windows_install_dir(self) -> Path:
        """Stable per-machine install dir for service-related assets/logs."""
        base = Path(os.environ.get("PROGRAMDATA", r"C:\\ProgramData"))
        return base / (self._app_name() or "corepy") / "bin"

    def _windows_bin_dir(self) -> Path:
        """Best-effort directory where vendored tools live on Windows.

        Priority:
          1) Stable install dir under ProgramData (created by this module)
          2) If frozen, beside the executable (typical for unpacked builds)
          3) Source-tree locations (dev)

        Returns a directory that *may* not exist; callers should validate.
        """
        # 1) Preferred stable location
        try:
            d = self._windows_install_dir()
            if d:
                return d
        except Exception:
            pass

        # 2) Frozen bundle: beside the executable
        if getattr(sys, "frozen", False):
            try:
                return Path(sys.executable).resolve().parent
            except Exception:
                pass

        # 3) Fallback: current working directory
        return Path.cwd()

    def _windows_copy_tool_to_install_dir(self, tool_path: Path) -> Path:
        """Copy a bundled tool (like nssm.exe) into the stable install dir.

        This avoids services pointing into PyInstaller's temporary _MEI folder.
        """
        install_dir = self._windows_install_dir()
        install_dir.mkdir(parents=True, exist_ok=True)

        dest = install_dir / tool_path.name
        try:
            # Only copy if missing or different size/mtime; keep it simple.
            if not dest.exists():
                shutil.copy2(str(tool_path), str(dest))
            else:
                try:
                    if tool_path.stat().st_size != dest.stat().st_size:
                        shutil.copy2(str(tool_path), str(dest))
                except Exception:
                    # If we can't stat, just overwrite.
                    shutil.copy2(str(tool_path), str(dest))
        except Exception:
            # Last resort: attempt overwrite
            try:
                shutil.copy2(str(tool_path), str(dest))
            except Exception:
                pass

        return dest

    def _windows_nssm_candidates(self) -> list[Path]:
        """Return candidate NSSM locations (supports both corePY and vendored corePY inside apps)."""
        # Running inside corePY source tree
        here = Path(__file__).resolve()
        core_dir = here.parent  # .../src/core

        rels = [
            # Common repo layouts
            Path("src") / "bin" / "nssm" / "windows" / "x86" / "nssm.exe",
            Path("src") / "bin" / "nssm" / "windows" / "x86_64" / "nssm.exe",
            Path("bin") / "nssm" / "windows" / "x86" / "nssm.exe",
            Path("bin") / "nssm" / "windows" / "x86_64" / "nssm.exe",

            # Vendored corePY inside another repo (e.g. Replicator: src/core/src/bin/...)
            Path("src") / "core" / "src" / "bin" / "nssm" / "windows" / "x86" / "nssm.exe",
            Path("src") / "core" / "src" / "bin" / "nssm" / "windows" / "x86_64" / "nssm.exe",

            # Flat copies in a bin dir (after we copy tools into ProgramData)
            Path("nssm.exe"),
            Path("bin") / "nssm.exe",
            Path("tools") / "nssm.exe",
        ]

        roots = [
            Path.cwd(),
            core_dir.parent,                # .../src
            core_dir.parent.parent,         # project root
            here.parent.parent,             # .../src (safe-ish)
        ]

        # Preferred stable install dir (ProgramData) for services
        try:
            roots.insert(0, self._windows_install_dir())
        except Exception:
            pass

        # If frozen, also look beside the executable
        if getattr(sys, "frozen", False):
            try:
                roots.append(Path(sys.executable).resolve().parent)
            except Exception:
                pass

        out: list[Path] = []
        for root in roots:
            for rel in rels:
                p = (root / rel).resolve()
                out.append(p)
        return out

    def _windows_find_nssm(self) -> Optional[Path]:
        if not sys.platform.startswith("win"):
            return None

        # Allow explicit override
        override = (os.environ.get("COREPY_NSSM") or "").strip().strip('"')
        if override:
            p = Path(override).expanduser()
            if not p.is_absolute():
                p = (Path.cwd() / p).resolve()
            if p.exists():
                return p

        want_64 = self._windows_is_64bit()
        # Prefer matching arch first
        preferred = ["x86_64"] if want_64 else ["x86"]
        preferred += ["x86"] if want_64 else ["x86_64"]

        candidates = self._windows_nssm_candidates()
        candidate: Optional[Path] = None
        # Prefer matching arch
        for arch in preferred:
            for p in candidates:
                if f"\\{arch}\\" in str(p).lower() and p.exists():
                    candidate = p
                    break
            if candidate is not None:
                break
        # Any existing
        if candidate is None:
            for p in candidates:
                if p.exists():
                    candidate = p
                    break
        if candidate is not None:
            # If it's in a temp unpack dir, copy to stable dir.
            try:
                s = str(candidate)
                if "_mei" in s.lower() or "\\appdata\\local\\temp\\" in s.lower():
                    stable = self._windows_copy_tool_to_install_dir(candidate)
                    if stable.exists():
                        return stable
            except Exception:
                pass
            return candidate

        # If we found NSSM inside a PyInstaller temp folder (_MEI...), copy it to stable ProgramData
        # and return the stable path so services never reference a temporary location.
        try:
            found = None
            for arch in preferred:
                for p in candidates:
                    if f"\\{arch}\\" in str(p).lower() and p.exists():
                        found = p
                        break
                if found:
                    break
            if found is None:
                for p in candidates:
                    if p.exists():
                        found = p
                        break

            if found is not None:
                s = str(found)
                if "_mei" in s.lower() or "\\appdata\\local\\temp\\" in s.lower():
                    stable = self._windows_copy_tool_to_install_dir(found)
                    if stable.exists():
                        return stable
        except Exception:
            pass

        return None

    def _windows_has_nssm(self) -> bool:
        return bool(self._windows_find_nssm())


    def _windows_nssm_run(self, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
        """Run NSSM with the given args.

        Uses the bundled NSSM, and captures output for better error messages.
        """
        nssm = self._windows_find_nssm()
        # Prefer a stable copy under ProgramData if available
        try:
            stable = self._windows_install_dir() / "nssm.exe"
            if stable.exists():
                nssm = stable
        except Exception:
            pass

        if not nssm:
            raise RuntimeError("NSSM not found (COREPY_NSSM override not set and no bundled binary found).")

        cmd = [str(nssm)] + list(args)
        r = subprocess.run(cmd, capture_output=True, text=True)
        if check and r.returncode != 0:
            raise RuntimeError(f"nssm failed: {cmd}\n{(r.stdout or '') + (r.stderr or '')}")
        return r


# ------------------------------------------------------------------
# Service Manager Dialog UI (PyQt5)
# ------------------------------------------------------------------

class _TailReader:
    """Tiny file tailer used by the ServiceManagerDialog.

    Keeps an in-memory cursor per file and reads new bytes on each poll.
    """

    def __init__(self) -> None:
        self._pos: dict[str, int] = {}

    def read_new_text(self, path: str, *, max_bytes: int = 256 * 1024) -> str:
        if not path:
            return ""
        try:
            if not os.path.exists(path):
                return ""

            last = self._pos.get(path, 0)
            size = os.path.getsize(path)

            # If file was truncated/rotated, restart.
            if size < last:
                last = 0

            # Avoid reading gigantic chunks at once.
            start = max(0, size - max_bytes) if last == 0 and size > max_bytes else last

            with open(path, "rb") as f:
                f.seek(start)
                data = f.read()

            self._pos[path] = start + len(data)

            # Best-effort decode
            try:
                return data.decode("utf-8", errors="replace")
            except Exception:
                return data.decode(errors="replace")
        except Exception:
            return ""


class ServiceManagerDialog(QDialog):
    """Cross-platform service manager UI.

    Focus:
      - Install / Uninstall
      - Start / Stop / Restart
      - Status (installed/running)
      - Live-ish log view (stdout/stderr files when available)

    Note: On Linux, services often log to journald; this dialog will show file logs
    when present, otherwise it shows a hint.
    """

    def __init__(self, service: Service, parent=None) -> None:
        super().__init__(parent)
        self._svc = service
        self._tail = _TailReader()
        self._poll_ms = 1000

        self.setWindowTitle(f"{self._svc._app_name()} – Service")
        self.setModal(True)
        self.resize(860, 560)

        # -----------------------------
        # Status group
        # -----------------------------
        self._lbl_name = QLabel("")
        self._lbl_label = QLabel("")
        self._lbl_installed = QLabel("")
        self._lbl_running = QLabel("")
        self._lbl_pid = QLabel("")
        self._lbl_mgr = QLabel("")
        self._lbl_nssm = QLabel("")
        self._lbl_stdout = QLabel("")
        self._lbl_stderr = QLabel("")

        status_box = QGroupBox("Status")
        status_form = QFormLayout(status_box)
        status_form.addRow("Application:", self._lbl_name)
        status_form.addRow("Service label:", self._lbl_label)
        status_form.addRow("Manager:", self._lbl_mgr)
        status_form.addRow("Installed:", self._lbl_installed)
        status_form.addRow("Running:", self._lbl_running)
        status_form.addRow("PID:", self._lbl_pid)

        # Windows-only bits (kept visible but may be empty)
        status_form.addRow("NSSM:", self._lbl_nssm)
        status_form.addRow("Stdout log:", self._lbl_stdout)
        status_form.addRow("Stderr log:", self._lbl_stderr)

        # -----------------------------
        # Controls
        # -----------------------------
        self._btn_install = QPushButton("Install")
        self._btn_uninstall = QPushButton("Uninstall")
        self._btn_start = QPushButton("Start")
        self._btn_stop = QPushButton("Stop")
        self._btn_restart = QPushButton("Restart")
        self._btn_refresh = QPushButton("Refresh")
        self._btn_close = QPushButton("Close")

        self._btn_install.clicked.connect(self._on_install)
        self._btn_uninstall.clicked.connect(self._on_uninstall)
        self._btn_start.clicked.connect(self._on_start)
        self._btn_stop.clicked.connect(self._on_stop)
        self._btn_restart.clicked.connect(self._on_restart)
        self._btn_refresh.clicked.connect(self.refresh)
        self._btn_close.clicked.connect(self.close)

        controls = QGroupBox("Controls")
        controls_layout = QHBoxLayout(controls)
        controls_layout.addWidget(self._btn_install)
        controls_layout.addWidget(self._btn_uninstall)
        controls_layout.addStretch(1)
        controls_layout.addWidget(self._btn_start)
        controls_layout.addWidget(self._btn_stop)
        controls_layout.addWidget(self._btn_restart)
        controls_layout.addStretch(1)
        controls_layout.addWidget(self._btn_refresh)
        controls_layout.addWidget(self._btn_close)

        # -----------------------------
        # Logs
        # -----------------------------
        self._tabs = QTabWidget()

        self._txt_out = QPlainTextEdit()
        self._txt_out.setReadOnly(True)
        self._txt_err = QPlainTextEdit()
        self._txt_err.setReadOnly(True)

        self._tabs.addTab(self._txt_out, "Stdout")
        self._tabs.addTab(self._txt_err, "Stderr")

        logs_box = QGroupBox("Logs")
        logs_layout = QVBoxLayout(logs_box)
        logs_layout.addWidget(self._tabs)

        # Layout root
        root = QVBoxLayout(self)
        root.addWidget(status_box)
        root.addWidget(controls)
        root.addWidget(logs_box, 1)

        # Poll timer
        self._timer = QTimer(self)
        self._timer.setInterval(self._poll_ms)
        self._timer.timeout.connect(self._poll_logs)

        # Initial refresh
        self.refresh()
        self._timer.start()

    # -----------------------------
    # Helpers
    # -----------------------------

    def _manager_name(self) -> str:
        if sys.platform.startswith("win"):
            return "Windows Service Control Manager (NSSM)" if self._svc._windows_has_nssm() else "Windows Service Control Manager"
        if sys.platform == "darwin":
            return "launchd"
        return "systemd" if shutil.which("systemctl") else "(unknown)"

    def _windows_log_paths(self) -> tuple[str, str]:
        label = self._svc._service_label()
        try:
            logs_dir = self._svc._windows_install_dir().parent / "log"
            return str(logs_dir / f"{label}.out.log"), str(logs_dir / f"{label}.err.log")
        except Exception:
            return "", ""

    def _macos_log_paths(self) -> tuple[str, str]:
        try:
            logs_dir = Path.home() / "Library" / "Logs" / self._svc._app_name()
            return str(logs_dir / "service.out.log"), str(logs_dir / "service.err.log")
        except Exception:
            return "", ""

    def _log_paths(self) -> tuple[str, str]:
        if sys.platform.startswith("win"):
            return self._windows_log_paths()
        if sys.platform == "darwin":
            return self._macos_log_paths()
        # Linux: systemd typically logs to journal; still try common file paths
        return "", ""

    def _set_bool_label(self, lbl: QLabel, value: bool) -> None:
        lbl.setText("yes" if value else "no")

    def refresh(self) -> None:
        try:
            name = self._svc._app_name()
            label = self._svc._service_label()
            installed = self._svc._is_installed()
            running = self._svc._is_service_active() if installed else self._svc._is_running()
            pid = self._svc._read_pidfile() if not installed else None

            self._lbl_name.setText(name)
            self._lbl_label.setText(label)
            self._lbl_mgr.setText(self._manager_name())
            self._set_bool_label(self._lbl_installed, installed)
            self._set_bool_label(self._lbl_running, running)
            self._lbl_pid.setText(str(pid) if pid else "-")

            # NSSM path (Windows only)
            if sys.platform.startswith("win"):
                try:
                    p = self._svc._windows_find_nssm()
                    self._lbl_nssm.setText(str(p) if p else "(not found)")
                except Exception:
                    self._lbl_nssm.setText("(unknown)")
            else:
                self._lbl_nssm.setText("-")

            out_path, err_path = self._log_paths()
            self._lbl_stdout.setText(out_path if out_path else "(no file log configured)")
            self._lbl_stderr.setText(err_path if err_path else "(no file log configured)")

            # Button states
            self._btn_install.setEnabled(not installed)
            self._btn_uninstall.setEnabled(installed)

            self._btn_start.setEnabled(installed and not running)
            self._btn_stop.setEnabled(installed and running)
            self._btn_restart.setEnabled(installed and running)

            # If not installed, we still allow in-process start/stop, but it can be confusing.
            # Keep the UI focused on service-manager mode.
            if not installed:
                self._btn_start.setEnabled(False)
                self._btn_stop.setEnabled(False)
                self._btn_restart.setEnabled(False)

            # Show a helpful note in logs pane when file logs aren't available.
            if not out_path and not err_path:
                note = (
                    "Logs are not available as files on this platform/configuration.\n"
                    "- Windows: logs are written to ProgramData via NSSM settings.\n"
                    "- macOS: logs are written to ~/Library/Logs/<AppName>/.\n"
                    "- Linux: consider using journald (journalctl -u <service>).\n"
                )
                if not self._txt_out.toPlainText():
                    self._txt_out.setPlainText(note)
                if not self._txt_err.toPlainText():
                    self._txt_err.setPlainText(note)

        except Exception as e:
            QMessageBox.warning(self, "Service", f"Failed to refresh status: {e}")

    # -----------------------------
    # Actions
    # -----------------------------

    def _run_action(self, fn: Callable[[], None], title: str) -> None:
        try:
            fn()
        except Exception as e:
            QMessageBox.critical(self, "Service", f"{title} failed:\n\n{e}")
        finally:
            self.refresh()

    def _on_install(self) -> None:
        self._run_action(self._svc.install, "Install")

    def _on_uninstall(self) -> None:
        self._run_action(self._svc.uninstall, "Uninstall")

    def _on_start(self) -> None:
        self._run_action(self._svc._service_manager_start, "Start")

    def _on_stop(self) -> None:
        self._run_action(self._svc._service_manager_stop, "Stop")

    def _on_restart(self) -> None:
        self._run_action(self._svc._service_manager_restart, "Restart")

    # -----------------------------
    # Log polling
    # -----------------------------

    def _append_text(self, widget: QPlainTextEdit, text: str) -> None:
        if not text:
            return
        try:
            widget.moveCursor(widget.textCursor().End)
            widget.insertPlainText(text)
            widget.moveCursor(widget.textCursor().End)
        except Exception:
            # Safe fallback
            try:
                widget.setPlainText(widget.toPlainText() + text)
            except Exception:
                pass

    def _poll_logs(self) -> None:
        out_path, err_path = self._log_paths()
        if out_path:
            self._append_text(self._txt_out, self._tail.read_new_text(out_path))
        if err_path:
            self._append_text(self._txt_err, self._tail.read_new_text(err_path))

    def closeEvent(self, event) -> None:  # type: ignore[override]
        try:
            if self._timer.isActive():
                self._timer.stop()
        except Exception:
            pass
        super().closeEvent(event)
