#!/usr/bin/env python3
# src/core/service.py

from __future__ import annotations

import os
import sys
import time
import signal
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Optional


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
            print("Initializing service configuration defaults...")
            # How often the main loop wakes up to check due tasks (seconds)
            self._configuration.add("service.loopSleep", 1, "number", label="Service loop sleep (s)", min=1, max=60)
            # Default interval used when registering tasks (seconds)
            self._configuration.add("service.defaultInterval", 3600, "number", label="Service default task interval (s)", min=0, max=86400)
            # Persist any new defaults
            self._configuration.save()

    # ------------------------------------------------------------------
    # CLI integration
    # ------------------------------------------------------------------

    def cli(self, cli=None) -> None:
        """Register service management commands into CommandLine."""
        if cli is None:
            return

        cli.add("start", "Start the service loop.", self.start)
        cli.add("stop", "Stop the running service.", self.stop)
        cli.add("restart", "Restart the running service.", self.restart)
        cli.add("status", "Show service status.", self.status)
        cli.add("install", "Install as a systemd service (Linux).", self.install)
        cli.add("uninstall", "Uninstall the systemd service (Linux).", self.uninstall)

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
        """Stop running service (if any), then start the loop."""
        self.stop()
        time.sleep(0.5)
        self.start()

    def status(self) -> None:
        """Show current service status."""
        pidfile = self._pidfile()
        pid = self._read_pidfile()

        running = bool(pid and self._pid_exists(pid))

        # Build a readable status
        lines = []
        lines.append(f"Service: {self._app_name()}")
        lines.append(f"PID file: {pidfile}")
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
    # systemd integration (Linux)
    # ------------------------------------------------------------------

    def install(self) -> None:
        """Install a systemd unit that runs `<python> <main.py> --start`.

        This is intentionally Linux/systemd-only. On other platforms, raise.
        """
        if sys.platform.startswith("win"):
            raise NotImplementedError("systemd install is not supported on Windows")

        if shutil.which("systemctl") is None:
            raise RuntimeError("systemctl not found; systemd does not appear available")

        service_name = f"{self._app_name()}.service"
        unit_path = os.path.join("/etc/systemd/system", service_name)

        python_exe = sys.executable
        entry = os.path.abspath(sys.argv[0])
        workdir = os.path.dirname(entry)

        unit = self._systemd_unit(
            service_name=service_name,
            python_exe=python_exe,
            entrypoint=entry,
            working_dir=workdir,
            pidfile=self._pidfile(),
        )

        with open(unit_path, "w", encoding="utf-8") as f:
            f.write(unit)

        subprocess.run(["systemctl", "daemon-reload"], check=False)
        subprocess.run(["systemctl", "enable", "--now", service_name], check=False)

        print(f"Installed and started: {unit_path}")

    def uninstall(self) -> None:
        if sys.platform.startswith("win"):
            raise NotImplementedError("systemd uninstall is not supported on Windows")

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
    ) -> str:
        app_name = self._app_name()
        return (
            "[Unit]\n"
            f"Description={app_name}\n"
            "After=network.target\n\n"
            "[Service]\n"
            "Type=simple\n"
            f"WorkingDirectory={working_dir}\n"
            f"ExecStart={python_exe} {entrypoint} --start\n"
            f"ExecStop={python_exe} {entrypoint} --stop\n"
            "Restart=on-failure\n"
            "RestartSec=2\n"
            f"PIDFile={pidfile}\n"
            "\n"
            "[Install]\n"
            "WantedBy=multi-user.target\n"
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _app_name(self) -> str:
        # Try to read from Qt application if present, else fallback.
        try:
            from PyQt5.QtWidgets import QApplication

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
