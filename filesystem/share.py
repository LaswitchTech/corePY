#!/usr/bin/env python3
# src/core/filesystem/share.py

"""Share mounting utilities for corePY (SMB-only).

Goal
----
Provide a single, cross-platform interface for mounting/unmounting SMB shares.

Supported protocol:
  - SMB (CIFS / Windows shares)

Backends
--------
Native OS tools (preferred):
  * Linux: mount.cifs / umount
  * macOS: mount_smbfs / umount
  * Windows: net use

Notes
-----
* Many mount operations require elevated privileges depending on target path.
  This class does not force privilege escalation; you can pass `elevate=True`
  to prefix commands with sudo on Unix-like systems.
* On Windows, SMB mounting via `net use` maps network resources to a drive letter.
  - If you pass a drive letter (e.g. `mount_point='Z:'`), it will map directly.
  - If you pass a directory path, corePY will map the share to a free drive letter
    and create a directory junction at `mount_point` pointing to that drive.
"""

from __future__ import annotations

import os
import platform
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote, unquote


class ShareError(RuntimeError):
    pass


@dataclass
class ShareAuth:
    username: Optional[str] = None
    password: Optional[str] = None
    domain: Optional[str] = None


@dataclass
class ShareTarget:
    protocol: str  # smb
    host: str
    path: str = ""  # share name and optional subpath (e.g. "Share" or "Share/dir")
    port: Optional[int] = None  # unused for smb, kept for compatibility

    def normalized_protocol(self) -> str:
        p = (self.protocol or "").strip().lower()
        if p in {"cifs"}:
            return "smb"
        return p

    def decoded_path(self) -> str:
        # Accept either raw share names or percent-encoded ones (e.g. "Test%20with%20spaces").
        try:
            return unquote(self.path or "")
        except Exception:
            return self.path or ""

class ShareLoggerAdapter:
    """Adapter to feed Share's logging into Replicator/corePY style append()."""

    def __init__(self, append_fn: Callable[..., None]):
        self._append = append_fn

    def _call(self, msg: str, level: str) -> None:
        msg = redact_secrets(msg)
        try:
            self._append(msg, level=level)  # core Log.append style
        except TypeError:
            try:
                self._append(msg, level)      # positional callable style
            except TypeError:
                self._append(msg)             # best-effort fallback

    def debug(self, msg: str) -> None:
        self._call(msg, "debug")

    def info(self, msg: str) -> None:
        self._call(msg, "info")

    def warning(self, msg: str) -> None:
        self._call(msg, "warning")

    def error(self, msg: str) -> None:
        self._call(msg, "error")

class Share:
    """Mount and unmount SMB shares."""

    def __init__(self, logger=None, bin_root: Optional[str] = None):
        self._logger = logger
        self._bin_root = Path(bin_root) if bin_root else None

        self._backend: Optional[str] = None  # native_smb_linux|native_smb_macos|native_smb_windows
        self._last_cmd: Optional[List[str]] = None
        self._last_mount_point: Optional[str] = None
        self._last_probe_ok: Optional[bool] = None
        self._last_probe_message: Optional[str] = None
        self._last_windows_drive: Optional[str] = None
        self._last_windows_junction: Optional[str] = None
        self._last_windows_unc: Optional[str] = None

    # ---------------------------------------------------------------------
    # Public API
    # ---------------------------------------------------------------------

    def mount(
        self,
        protocol: str,
        host: str,
        remote: str,
        mount_point: str,
        auth: Optional[ShareAuth] = None,
        *,
        port: Optional[int] = None,
        options: Optional[Dict[str, str]] = None,
        read_only: bool = False,
        elevate: bool = False,
        timeout: int = 60,
    ) -> None:
        """Mount a share.

        Parameters
        ----------
        protocol:
            smb
        host:
            Hostname or IP.
        remote:
            SMB share name and optional path (e.g. "Share" or "Share/dir").
        mount_point:
            Local mount directory (Linux/macOS) or drive letter like "Z:" (Windows).
        auth:
            Username/password/domain.
        options:
            Backend-specific mount options.
        read_only:
            Attempt to mount read-only.
        elevate:
            On Linux/macOS, prefix with sudo.
        timeout:
            Seconds.
        """
        auth = auth or ShareAuth()
        options = options or {}

        target = ShareTarget(protocol=protocol, host=host, path=remote or "", port=port)
        p = target.normalized_protocol()

        # Normalize remote path early (accept percent-encoded share names)
        target.path = target.decoded_path()

        self._log_debug(f"Mount request: protocol={p} host={host} remote={remote} mount_point={mount_point}")

        if p != "smb":
            raise ShareError(f"Unsupported protocol: {protocol}. Supported protocol(s): smb")

        # Ensure mountpoint exists for directory-based mounts
        # On Windows, directory mounts are implemented as symlinks and any pre-created empty directory will be replaced.
        if not self._is_windows_drive_letter(mount_point):
            Path(mount_point).mkdir(parents=True, exist_ok=True)

        self._mount_smb(target, mount_point, auth, options, read_only, elevate, timeout)

        # Probe the mount to confirm it is usable.
        ok, msg = self._probe_mount(mount_point, timeout=timeout)
        self._last_probe_ok = ok
        self._last_probe_message = msg
        self._log_debug(f"Share mount probe: mount_point={mount_point} ok={ok} msg={msg}")

        if not ok:
            raise ShareError(f"Mount completed but mount probe failed: {msg}")

        self._last_mount_point = mount_point

    def umount(self, mount_point: Optional[str] = None, *, elevate: bool = False, timeout: int = 60) -> None:
        """Unmount a share."""
        mp = mount_point or self._last_mount_point
        if not mp:
            raise ShareError("No mount point provided, and no previous mount point is known.")

        self._log_debug(f"Unmount request: mount_point={mp} backend={self._backend}")

        # Best-effort probe before unmount.
        ok_before, msg_before = self._probe_mount(mp, timeout=min(5, timeout))
        self._log_debug(f"Share pre-unmount probe: mount_point={mp} ok={ok_before} msg={msg_before}")

        system = platform.system().lower()
        if system == "windows":
            # If we created a directory symlink, remove it first, then disconnect.
            drive: Optional[str] = None
            unc: Optional[str] = None

            if mp and not self._is_windows_drive_letter(mp):
                # Directory-style mount (symlink)
                junction = mp
                unc = self._last_windows_unc
                try:
                    if junction and os.path.exists(junction):
                        # Remove symlink directory (best-effort)
                        self._run(["cmd", "/c", "rmdir", "/S", "/Q", junction], timeout=timeout)
                except Exception:
                    pass
            else:
                # Drive-letter mount
                drive = mp

            if drive:
                # Example: net use Z: /delete /y
                self._run(["net", "use", drive, "/delete", "/y"], timeout=timeout)

            if unc:
                # Example: net use \\server\Share /delete /y
                self._run(["net", "use", unc, "/delete", "/y"], timeout=timeout)

            # Clear remembered mapping
            self._last_windows_drive = None
            self._last_windows_junction = None
            self._last_windows_unc = None

            ok_after, msg_after = self._probe_mount(mp, timeout=min(5, timeout))
            self._log_debug(f"Share post-unmount probe: mount_point={mp} ok={ok_after} msg={msg_after}")
            return

        # Linux/macOS
        umount_bin = shutil.which("umount") or "umount"
        cmd = (["sudo"] if elevate else []) + [umount_bin, mp]
        self._run(cmd, timeout=timeout)

        ok_after, msg_after = self._probe_mount(mp, timeout=min(5, timeout))
        self._log_debug(f"Share post-unmount probe: mount_point={mp} ok={ok_after} msg={msg_after}")

    # ---------------------------------------------------------------------
    # SMB
    # ---------------------------------------------------------------------

    def _mount_smb(
        self,
        target: ShareTarget,
        mount_point: str,
        auth: ShareAuth,
        options: Dict[str, str],
        read_only: bool,
        elevate: bool,
        timeout: int,
    ) -> None:
        system = platform.system().lower()

        if system == "windows":
            self._backend = "native_smb_windows"

            # `net use` supports mapping to a drive letter.
            # If the caller provided a directory path, we:
            #   1) create an authenticated connection to the UNC path (no drive letter)
            #   2) create a directory symlink at mount_point pointing to that UNC path
            #
            # IMPORTANT: `mklink /J` (junction) cannot target mapped drives / UNC paths
            # and will fail with "Local volumes are required...". For UNC we must use
            # a directory symbolic link (`mklink /D`).

            unc = self._smb_unc(target.host, target.path)

            junction_path: Optional[str] = None
            drive_letter: Optional[str] = None

            if self._is_windows_drive_letter(mount_point):
                # Drive-letter style mount
                drive_letter = mount_point.upper()

                cmd = ["net", "use", drive_letter, unc]
                if auth.username:
                    if auth.domain:
                        cmd += [f"/user:{auth.domain}\\{auth.username}"]
                    else:
                        cmd += [f"/user:{auth.username}"]
                if auth.password is not None:
                    cmd += [auth.password]
                cmd += ["/persistent:no"]

                # Retry once on 1219 by clearing existing connections to that host
                try:
                    self._run(cmd, timeout=timeout)
                except ShareError as e:
                    if "1219" in str(e):
                        self._log_debug(f"Windows SMB: detected error 1219; clearing connections to host {target.host} and retrying")
                        try:
                            self._run(["net", "use", f"\\\\{target.host}\\*", "/delete", "/y"], timeout=timeout)
                        except Exception:
                            pass
                        self._run(cmd, timeout=timeout)
                    else:
                        raise

                self._last_windows_drive = drive_letter
                self._last_windows_junction = None
                self._last_windows_unc = None
                return

            # Directory-style mount (symlink to UNC)
            junction_path = mount_point

            # 1) Create (or refresh) an authenticated connection to the UNC path
            #    without assigning a drive letter.
            cmd = ["net", "use", unc]
            if auth.username:
                if auth.domain:
                    cmd += [f"/user:{auth.domain}\\{auth.username}"]
                else:
                    cmd += [f"/user:{auth.username}"]
            # Avoid interactive password prompt: pass empty string when password is None.
            cmd += [(auth.password if auth.password is not None else "")]
            cmd += ["/persistent:no"]

            try:
                self._run(cmd, timeout=timeout)
            except ShareError as e:
                if "1219" in str(e):
                    self._log_debug(f"Windows SMB: detected error 1219; clearing connections to host {target.host} and retrying")
                    try:
                        self._run(["net", "use", f"\\\\{target.host}\\*", "/delete", "/y"], timeout=timeout)
                    except Exception:
                        pass
                    self._run(cmd, timeout=timeout)
                else:
                    raise

            # 2) Create a directory symlink pointing to the UNC path.
            try:
                if os.path.exists(junction_path):
                    self._run(["cmd", "/c", "rmdir", "/S", "/Q", junction_path], timeout=timeout)

                # NOTE: Creating symlinks may require admin privileges unless Developer Mode is enabled.
                self._run(["cmd", "/c", "mklink", "/D", junction_path, unc], timeout=timeout)

                self._last_windows_drive = None
                self._last_windows_junction = junction_path
                self._last_windows_unc = unc
            except Exception:
                # Best-effort rollback of the UNC connection
                try:
                    self._run(["net", "use", unc, "/delete", "/y"], timeout=timeout)
                except Exception:
                    pass
                raise

            return

        if system == "darwin":
            self._backend = "native_smb_macos"
            mount_smbfs = self._bin("mount_smbfs")

            # Format: //user:pass@server/share /mnt/point
            userinfo = ""
            if auth.username:
                userinfo = auth.username
                if auth.password:
                    userinfo += ":" + self._url_escape(auth.password)
                userinfo += "@"

            # For SMB URLs, the path portion must be URL-encoded (but keep '/').
            path = (target.path or "").lstrip("/")
            path = quote(path, safe="/")
            url = f"//{userinfo}{target.host}/{path}"
            if read_only:
                cmd = (["sudo"] if elevate else []) + [mount_smbfs, "-o", "ro", url, mount_point]
            else:
                cmd = (["sudo"] if elevate else []) + [mount_smbfs, url, mount_point]

            self._run(cmd, timeout=timeout)
            return

        # Linux
        self._backend = "native_smb_linux"
        mount_cifs = self._bin("mount.cifs")

        share, subpath = self._split_share_and_subpath(target.path)
        if not share:
            raise ShareError("SMB mount requires a share name (remote='Share' or 'Share/subpath').")

        unc = f"//{target.host}/{share}"

        mount_opts: Dict[str, str] = {}
        if auth.username is not None:
            mount_opts["username"] = auth.username
        if auth.password is not None:
            mount_opts["password"] = auth.password
        if auth.domain:
            mount_opts["domain"] = auth.domain
        if read_only:
            mount_opts["ro"] = ""

        # Allow caller overrides
        mount_opts.update(options)

        opts_str = self._kv_to_mount_opts(mount_opts)

        cmd = (["sudo"] if elevate else []) + [mount_cifs, unc, mount_point]
        if opts_str:
            cmd += ["-o", opts_str]

        self._run(cmd, timeout=timeout)

        # If a subpath was requested, verify it exists (informational only)
        if subpath:
            check_path = os.path.join(mount_point, subpath)
            if not os.path.exists(check_path):
                self._log_debug(f"Warning: SMB subpath does not exist after mount: {check_path}")

    # ---------------------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------------------

    def _run(self, cmd: List[str], *, timeout: int) -> None:
        self._last_cmd = cmd
        printable = [shlex.quote(str(c)) for c in cmd]
        self._log_debug("Executing: " + " ".join(printable))

        try:
            # On Windows, launching console utilities from a GUI process can flash a console window.
            # Use CREATE_NO_WINDOW / SW_HIDE best-effort to avoid visible windows.
            run_kwargs: Dict[str, object] = {
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "text": True,
                "timeout": timeout,
                "check": False,
                "stdin": subprocess.DEVNULL,
            }

            if platform.system().lower() == "windows":
                # Hide console window (best-effort)
                create_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                if create_no_window:
                    run_kwargs["creationflags"] = int(create_no_window)

                # Some Windows builds still flash; STARTUPINFO can help in many cases.
                try:
                    si = subprocess.STARTUPINFO()  # type: ignore[attr-defined]
                    si.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 1)
                    si.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
                    run_kwargs["startupinfo"] = si
                except Exception:
                    pass

            completed = subprocess.run(cmd, **run_kwargs)  # type: ignore[arg-type]
        except FileNotFoundError as e:
            raise ShareError(f"Required executable not found: {cmd[0]}") from e
        except subprocess.TimeoutExpired as e:
            raise ShareError(f"Command timed out after {timeout}s: {' '.join(cmd)}") from e

        if completed.returncode != 0:
            msg = (completed.stderr or completed.stdout or "").strip()
            raise ShareError(f"Mount command failed (code {completed.returncode}): {msg}")

        if completed.stdout:
            self._log_debug(completed.stdout.strip())

    def _bin(self, name: str) -> str:
        """Resolve an executable.

        Resolution order:
        1) PATH
        2) Bundled binary under src/bin/[name]/[OS]/[arch]/(bin/)[exe] (best-effort)
        """
        candidates = [name]
        if platform.system().lower() == "windows" and not name.lower().endswith(".exe"):
            candidates.insert(0, name + ".exe")

        # PATH first
        for c in candidates:
            p = shutil.which(c)
            if p:
                return p

        # Bundled fallback
        root = self._resolve_bin_root()
        os_name = self._os_folder()
        arch = self._arch_folder()

        for c in candidates:
            bundled_bin = root / name / os_name / arch / "bin" / c
            bundled_flat = root / name / os_name / arch / c
            for bundled in (bundled_bin, bundled_flat):
                if bundled.exists() and bundled.is_file():
                    return str(bundled)

        return candidates[0]

    def _resolve_bin_root(self) -> Path:
        if self._bin_root:
            return self._bin_root

        here = Path(__file__).resolve()

        search_roots: List[Path] = []
        try:
            search_roots.append(Path.cwd())
        except Exception:
            pass

        search_roots.append(here.parent)
        search_roots.extend(list(here.parents))
        search_roots.append(here.parent.parent)
        search_roots.append(here.parent.parent.parent)

        for root in search_roots:
            candidate = root / "src" / "bin"
            if candidate.exists():
                self._bin_root = candidate
                return candidate

        for root in search_roots:
            candidate = root / "bin"
            if candidate.exists():
                self._bin_root = candidate
                return candidate

        self._bin_root = Path.cwd() / "src" / "bin"
        return self._bin_root

    def _os_folder(self) -> str:
        s = platform.system().lower()
        if s == "darwin":
            return "macos"
        if s == "windows":
            return "windows"
        return "linux"

    def _arch_folder(self) -> str:
        m = (platform.machine() or "").lower()
        if m in {"x86_64", "amd64"}:
            return "x86_64"
        if m in {"aarch64", "arm64"}:
            return "arm64"
        if m.startswith("arm"):
            return "arm"
        if m in {"i386", "i686", "x86"}:
            return "x86"
        return m or "unknown"

    def _is_windows_drive_letter(self, s: str) -> bool:
        return len(s) == 2 and s[1] == ":" and s[0].isalpha()

    def _smb_unc(self, host: str, remote: str) -> str:
        remote = unquote(remote or "")
        remote = remote.replace("/", "\\").lstrip("\\")
        return f"\\\\{host}\\{remote}"

    def _split_share_and_subpath(self, remote: str) -> Tuple[str, str]:
        r = unquote(remote or "").lstrip("/")
        if not r:
            return "", ""
        parts = r.split("/")
        share = parts[0]
        sub = "/".join(parts[1:]) if len(parts) > 1 else ""
        return share, sub

    def _kv_to_mount_opts(self, d: Dict[str, str]) -> str:
        parts = []
        for k, v in d.items():
            if v is None:
                continue
            if v == "":
                parts.append(str(k))
            else:
                parts.append(f"{k}={v}")
        return ",".join(parts)

    def _url_escape(self, s: str) -> str:
        # Minimal escaping suitable for passwords in smb URLs
        safe = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~"
        out = []
        for ch in s:
            if ch in safe:
                out.append(ch)
            else:
                out.append("%" + format(ord(ch), "02X"))
        return "".join(out)

    def _log_debug(self, msg: str) -> None:
        if self._logger is not None:
            for method in ("debug", "info"):
                if hasattr(self._logger, method):
                    getattr(self._logger, method)(msg)
                    return

    def _probe_mount(self, mount_point: str, *, timeout: int = 10) -> Tuple[bool, str]:
        """Best-effort probe to determine if a mount point is usable."""
        try:
            if self._is_windows_drive_letter(mount_point):
                return self._probe_windows_drive(mount_point)

            mp = str(mount_point)
            if not os.path.exists(mp):
                return False, "mount point does not exist"
            if not os.path.isdir(mp):
                return False, "mount point is not a directory"

            deadline = time.time() + float(timeout)
            last_err: Optional[str] = None
            while time.time() < deadline:
                try:
                    _ = os.listdir(mp)
                    return True, "listdir ok"
                except Exception as e:
                    last_err = str(e)
                    time.sleep(0.25)

            return False, f"listdir failed: {last_err or 'unknown error'}"
        except Exception as e:
            return False, f"probe exception: {e}"

    def _probe_windows_drive(self, drive: str) -> Tuple[bool, str]:
        try:
            d = drive.upper()
            if not d.endswith(":"):
                d += ":"
            root = d + "\\"
            if not os.path.exists(root):
                return False, "drive root does not exist"
            try:
                _ = os.listdir(root)
                return True, "drive listdir ok"
            except Exception as e:
                return False, f"drive listdir failed: {e}"
        except Exception as e:
            return False, f"drive probe exception: {e}"
