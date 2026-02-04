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
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Callable, Optional
from PyQt5.QtWidgets import QApplication


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
        name = self._service_label()

        # Installing a Windows service requires elevation. We cannot elevate an
        # already-running process; instead, we relaunch with UAC and return.
        if not self._windows_is_admin():
            self._log("Admin privileges required to install the service. Requesting elevation...", level="info")
            if self._windows_relaunch_elevated(sys.argv):
                return
            raise RuntimeError("Admin privileges required to install the service.")

        program, argv, workdir = self._service_command("start")

        # binPath must be a single string; include working directory by using cmd.exe /c cd ... && ...
        # We also set COREPY_RUN_AS_SERVICE so `start()` knows it should run the loop.
        quoted_program = program.replace('"', '')
        args_str = " ".join(f'"{a}"' for a in argv)

        cmd = (
            'cmd.exe /c "'
            'set COREPY_RUN_AS_SERVICE=1 && '
            f'set COREPY_SERVICE_LABEL={name} && '
            f'cd /d "{workdir}" && '
            f'"{quoted_program}" {args_str}'
            '"'
        )

        # Create service (requires admin)
        r = subprocess.run(
            ["sc", "create", name, "binPath=", cmd, "start=", "auto"],
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            raise RuntimeError(f"sc create failed: {(r.stdout or '') + (r.stderr or '')}")

        self._log(f"Installed Windows service: {name}")

    def _uninstall_windows_service(self) -> None:
        name = self._service_label()
        # Uninstalling a Windows service requires elevation.
        if not self._windows_is_admin():
            self._log("Admin privileges required to uninstall the service. Requesting elevation...", level="info")
            if self._windows_relaunch_elevated(sys.argv):
                return
            raise RuntimeError("Admin privileges required to uninstall the service.")
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
