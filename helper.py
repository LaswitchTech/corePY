#!/usr/bin/env python3
# src/core/helper.py
import os
import sys
import platform
import subprocess

from pathlib import Path
from PyQt5.QtWidgets import QApplication

class Helper:
    """
    Small collection of cross-app helpers.
    """

    def __init__(self, root_dir: str | None = None, script_dir: str | None = None):
        if script_dir is None:
            if getattr(sys, "frozen", False):
                script_dir = os.path.dirname(sys.executable)
            else:
                script_dir = os.path.dirname(os.path.abspath(__file__))
        self.script_dir = script_dir

        if root_dir is None:
            # If we are in .../src/app, root_dir should be project root (two levels up).
            base = os.path.dirname(self.script_dir)  # e.g. .../src
            parent = os.path.dirname(base)           # e.g. project_root
            self.root_dir = parent
        else:
            self.root_dir = root_dir

        self.home_dir = os.path.expanduser("~")

    def get_path(self, rel_path: str, type: str = "SYS") -> str | None:
        """
        Locate a resource file according to the specified type.
        Wrapper around get_sys_path, get_config_path, get_data_path.

        Types:
        - SYS: Search in standard system locations (PyInstaller, .app Resources, src/)
        - CONFIG: User configuration directory
        - DATA: User data directory
        """
        rel = rel_path.replace("\\", "/")

        if type.upper() == "SYS":
            return self.get_sys_path(rel)
        elif type.upper() == "CONFIG":
            return self.get_config_path(rel)
        elif type.upper() == "DATA":
            return self.get_data_path(rel)
        else:
            print(f"[Helper] Unknown path type: {type}")
            return None

    def get_sys_path(self, rel_path: str) -> str | None:
        rel = rel_path.replace("\\", "/")

        # 1) PyInstaller onefile temp dir
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            p = os.path.join(meipass, rel)
            if os.path.exists(p):
                return p

        # 2) Next to the frozen exe
        if getattr(sys, "frozen", False):
            p = os.path.join(self.script_dir, rel)
            if os.path.exists(p):
                return p

        # 3) macOS .app Resources in onefile
        if getattr(sys, "frozen", False) and self.get_os() == "macos":
            # .../Replicator.app/Contents/MacOS/Replicator -> parents[1] = Contents
            contents = Path(sys.executable).resolve().parents[1]
            p = contents / "Resources" / rel
            if p.exists():
                return str(p)

        # 4) macOS .app Resources
        res = os.path.join(self.root_dir, "Resources", rel)
        if os.path.exists(res):
            return res

        # 5) repo src/
        src = os.path.join(self.root_dir, "src", rel)
        if os.path.exists(src):
            return src

        print(f"[Helper] Could not find resource: {rel}")
        return None

    def _get_app_name(self, app_name: str | None = None) -> str:
        """Resolve the application name used for user directories."""
        if app_name:
            name = str(app_name).strip()
            if name:
                return name

        # Prefer QApplication name when running with a Qt app
        try:
            qapp = QApplication.instance()
            if qapp:
                qname = (qapp.applicationName() or "").strip()
                if qname:
                    return qname
        except Exception:
            pass

        # Fallback: executable/script name
        try:
            base = os.path.basename(sys.executable if getattr(sys, "frozen", False) else sys.argv[0])
            name = os.path.splitext(base)[0].strip()
            return name or "app"
        except Exception:
            return "app"

    def get_data_path(
        self,
        rel_path: str | None = None,
        app_name: str | None = None,
        ensure: bool = True,
    ) -> str:
        """Return the OS-appropriate directory for user *data*.

        - Windows: %LOCALAPPDATA%\<AppName>
        - macOS: ~/Library/Application Support/<AppName>
        - Linux: $XDG_DATA_HOME/<AppName> (fallback ~/.local/share/<AppName>)

        If `rel_path` is provided, it is appended under the app directory.
        If `ensure` is True, the directory is created.
        """
        name = self._get_app_name(app_name)
        os_name = self.get_os()

        if os_name == "windows":
            base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or self.home_dir
            root = Path(base) / name
        elif os_name == "macos":
            root = Path(self.home_dir) / "Library" / "Application Support" / name
        else:
            xdg = os.environ.get("XDG_DATA_HOME")
            if xdg:
                root = Path(xdg) / name
            else:
                root = Path(self.home_dir) / ".local" / "share" / name

        if rel_path:
            rel = rel_path.replace("\\", "/").lstrip("/")
            root = root / rel

        if ensure:
            # If rel_path includes a filename, create parent; otherwise create dir
            target_dir = root.parent if root.suffix else root
            target_dir.mkdir(parents=True, exist_ok=True)

        return str(root)

    def get_config_path(
        self,
        rel_path: str | None = None,
        app_name: str | None = None,
        ensure: bool = True,
    ) -> str:
        """Return the OS-appropriate directory for user *configuration*.

        - Windows: %APPDATA%\<AppName>
        - macOS: ~/Library/Preferences/<AppName>
        - Linux: $XDG_CONFIG_HOME/<AppName> (fallback ~/.config/<AppName>)

        If `rel_path` is provided, it is appended under the app directory.
        If `ensure` is True, the directory is created.
        """
        name = self._get_app_name(app_name)
        os_name = self.get_os()

        if os_name == "windows":
            base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA") or self.home_dir
            root = Path(base) / name
        elif os_name == "macos":
            root = Path(self.home_dir) / "Library" / "Preferences" / name
        else:
            xdg = os.environ.get("XDG_CONFIG_HOME")
            if xdg:
                root = Path(xdg) / name
            else:
                root = Path(self.home_dir) / ".config" / name

        if rel_path:
            rel = rel_path.replace("\\", "/").lstrip("/")
            root = root / rel

        if ensure:
            target_dir = root.parent if root.suffix else root
            target_dir.mkdir(parents=True, exist_ok=True)

        return str(root)

    @staticmethod
    def get_os() -> str:
        name = platform.system()
        if name == "Darwin":
            return "macos"
        if name == "Linux":
            return "linux"
        if name == "Windows":
            return "windows"
        return "unknown"

    @staticmethod
    def get_arch() -> str:
        arch = platform.machine().lower()
        if arch in ("x86_64", "amd64"):
            return "x86_64"
        if arch in ("aarch64", "arm64"):
            return "arm64"
        if arch in ("i386", "i686", "x86", "i86pc"):
            return "x86"
        if arch in ("armv7l", "armv8l", "arm"):
            return "armhf"
        return "unknown"

    @staticmethod
    def get_serial() -> str:
        # Linux / Pi: /proc/cpuinfo usually contains a 'Serial' line
        try:
            if Helper.get_os() == "linux":
                cpuinfo_path = "/proc/cpuinfo"
                if os.path.exists(cpuinfo_path):
                    with open(cpuinfo_path, "r", encoding="utf-8", errors="ignore") as f:
                        for line in f:
                            if line.lower().startswith("serial"):
                                parts = line.split(":", 1)
                                if len(parts) == 2:
                                    return parts[1].strip()
        except Exception:
            # Swallow errors and fall through to empty string
            pass

        # Fallback: nothing suitable found
        return ""

    @staticmethod
    def get_screen_resolution() -> tuple[int, int]:
        app = QApplication.instance()
        if not app:
            return (0, 0)
        screen = app.primaryScreen()
        size = screen.size()
        return (size.width(), size.height())

    @staticmethod
    def get_now() -> str:
        from datetime import datetime
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def file_exists(path: str | None) -> bool:
        return bool(path) and os.path.isfile(path)

    @staticmethod
    def dir_exists(path: str | None) -> bool:
        return bool(path) and os.path.isdir(path)

    @staticmethod
    def join(*paths: str) -> str:
        return os.path.join(*paths)

    @staticmethod
    def qss_url(p: str | None) -> str:
        """
        Return a url("...") QSS literal, or url("") for None.
        """
        if not p:
            return 'url("")'
        return f'url("{p.replace(os.sep, "/")}")'

    @staticmethod
    def run(cmd):
        try:
            p = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=6
            )
            return p.returncode, (p.stdout or "").strip()
        except Exception as e:
            return 1, f"{type(e).__name__}: {e}"

    # ---------- StyleSheet Handling ----------
    def load_stylesheet(self, rel_path: str) -> str | None:
        """
        Load a stylesheet from a relative path.
        """
        p = self.get_path(rel_path)
        if not p:
            return None
        try:
            with open(p, "r", encoding="utf-8") as f:
                return f.read()
        except Exception as e:
            print(f"[Helper] Failed to load stylesheet {rel_path}: {e}")
            return None
