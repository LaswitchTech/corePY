
#!/usr/bin/env python3
# corePY/filesystem/share.py

"""Share mounting utilities for corePY.

Goal
----
Provide a single, cross-platform interface for mounting/unmounting remote shares.

Supported protocols (best-effort, depending on OS and available binaries):
  - SMB (CIFS / Windows shares)
  - FTP
  - SSH (sshfs / SFTP)

Backends
--------
Because mounting is highly OS- and dependency-specific, this module supports
multiple backends and will select the best available one automatically:

  - Native OS tools (preferred when available)
      * Linux: mount.cifs / umount
      * macOS: mount_smbfs / umount
      * Windows: net use

  - FUSE-based tools
      * sshfs (Linux/macOS; Windows variants if present)
      * curlftpfs (Linux/macOS)

  - rclone (cross-platform fallback for FTP and SFTP; also works for SMB)

Binaries can be bundled under:
    src/bin/[name]/[OS]/[cpu_architecture]/bin

Notes
-----
* Many mount operations require elevated privileges depending on target path.
  This class does not force privilege escalation; you can pass `elevate=True`
  to prefix commands with sudo on Unix-like systems.
* On Windows, SMB mounting via `net use` maps network resources, typically to
  a drive letter. This class supports mapping to a drive letter via
  `mount_point='Z:'`.
"""

from __future__ import annotations

import os
import platform
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple


class ShareError(RuntimeError):
    pass


@dataclass
class ShareAuth:
    username: Optional[str] = None
    password: Optional[str] = None
    domain: Optional[str] = None
    private_key: Optional[str] = None  # SSH key path


@dataclass
class ShareTarget:
    protocol: str  # smb|ftp|ssh|sftp
    host: str
    path: str = ""  # share name for SMB, remote path for SFTP/SSH, ftp path for FTP
    port: Optional[int] = None

    def normalized_protocol(self) -> str:
        p = (self.protocol or "").strip().lower()
        if p in {"cifs"}:
            return "smb"
        if p in {"ssh", "sftp"}:
            return "ssh"
        return p


class Share:
    """Mount and unmount remote shares."""

    def __init__(
        self,
        logger=None,
        bin_root: Optional[str] = None,
    ):
        self._logger = logger
        # bin_root can be explicitly set by applications.
        # If not set, we try to resolve a path relative to this file.
        self._bin_root = Path(bin_root) if bin_root else None

        self._backend: Optional[str] = None  # native_smb_linux|native_smb_macos|native_smb_windows|sshfs|curlftpfs|rclone
        self._last_cmd: Optional[List[str]] = None
        self._last_mount_point: Optional[str] = None

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
            smb|ftp|ssh (ssh implies sftp/sshfs style)
        host:
            Hostname or IP.
        remote:
            For SMB: share name and optional path (e.g. "Share" or "Share/dir").
            For FTP: remote path (e.g. "/pub").
            For SSH: remote path (e.g. "/home/user/data").
        mount_point:
            Local mount directory (Linux/macOS) or drive letter like "Z:" (Windows).
        auth:
            Username/password/domain/private_key.
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

        self._log_debug(f"Mount request: protocol={p} host={host} remote={remote} mount_point={mount_point}")

        # Ensure mountpoint exists for directory-based mounts
        if not self._is_windows_drive_letter(mount_point):
            Path(mount_point).mkdir(parents=True, exist_ok=True)

        if p == "smb":
            self._mount_smb(target, mount_point, auth, options, read_only, elevate, timeout)
        elif p == "ftp":
            self._mount_ftp(target, mount_point, auth, options, read_only, elevate, timeout)
        elif p == "ssh":
            self._mount_ssh(target, mount_point, auth, options, read_only, elevate, timeout)
        else:
            raise ShareError(f"Unsupported protocol: {protocol}")

        self._last_mount_point = mount_point

    def umount(self, mount_point: Optional[str] = None, *, elevate: bool = False, timeout: int = 60) -> None:
        """Unmount a share."""
        mp = mount_point or self._last_mount_point
        if not mp:
            raise ShareError("No mount point provided, and no previous mount point is known.")

        self._log_debug(f"Unmount request: mount_point={mp} backend={self._backend}")

        # Prefer backend-specific unmount where required
        if self._backend == "rclone":
            rclone = self._bin("rclone")
            self._run([rclone, "unmount", mp], elevate=elevate, timeout=timeout)
            return

        system = platform.system().lower()
        if system == "windows":
            # For SMB we typically used net use; deleting by mount point is OK.
            # Example: net use Z: /delete /y
            self._run(["net", "use", mp, "/delete", "/y"], elevate=False, timeout=timeout)
            return

        # Linux/macOS
        umount_bin = shutil.which("umount") or "umount"
        self._run((["sudo"] if elevate else []) + [umount_bin, mp], elevate=False, timeout=timeout)

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
            # `net use` expects a UNC path and (optionally) a drive letter as mount_point
            # Example: net use Z: \\server\Share /user:DOMAIN\\user password
            unc = self._smb_unc(target.host, target.path)
            cmd = ["net", "use", mount_point, unc]
            if auth.username:
                if auth.domain:
                    cmd += [f"/user:{auth.domain}\\{auth.username}"]
                else:
                    cmd += [f"/user:{auth.username}"]
            if auth.password:
                cmd += [auth.password]
            cmd += ["/persistent:no"]
            self._run(cmd, elevate=False, timeout=timeout)
            return

        if system == "darwin":
            self._backend = "native_smb_macos"
            mount_smbfs = self._bin("mount_smbfs")

            # Format: //user:pass@server/share /mnt/point
            # Password may contain special chars; we percent-encode minimally.
            userinfo = ""
            if auth.username:
                userinfo = auth.username
                if auth.password:
                    userinfo += ":" + self._url_escape(auth.password)
                userinfo += "@"
            url = f"//{userinfo}{target.host}/{target.path.lstrip('/')}"
            cmd = (["sudo"] if elevate else []) + [mount_smbfs, url, mount_point]
            if read_only:
                # mount_smbfs uses -o for options
                cmd = (["sudo"] if elevate else []) + [mount_smbfs, "-o", "ro", url, mount_point]
            self._run(cmd, elevate=False, timeout=timeout)
            return

        # Linux
        self._backend = "native_smb_linux"
        mount_cifs = self._bin("mount.cifs")
        share, subpath = self._split_share_and_subpath(target.path)
        unc = f"//{target.host}/{share}"

        mount_opts = {}
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

        # If a subpath is provided, mount share then user can access subpath.
        # Alternative would be to bind-mount; keep simple.
        opts_str = self._kv_to_mount_opts(mount_opts)

        cmd = (["sudo"] if elevate else []) + [mount_cifs, unc, mount_point]
        if opts_str:
            cmd += ["-o", opts_str]

        self._run(cmd, elevate=False, timeout=timeout)

        # If subpath requested, validate it exists
        if subpath:
            check_path = os.path.join(mount_point, subpath)
            if not os.path.exists(check_path):
                self._log_debug(f"Warning: SMB subpath does not exist after mount: {check_path}")

    # ---------------------------------------------------------------------
    # FTP
    # ---------------------------------------------------------------------

    def _mount_ftp(
        self,
        target: ShareTarget,
        mount_point: str,
        auth: ShareAuth,
        options: Dict[str, str],
        read_only: bool,
        elevate: bool,
        timeout: int,
    ) -> None:
        # Prefer curlftpfs when available (Linux/macOS). Otherwise fallback to rclone.
        if self._is_windows():
            # Windows: use rclone as it supports FTP mounts cross-platform.
            self._mount_rclone("ftp", target, mount_point, auth, options, read_only, timeout)
            return

        curlftpfs = self._which_or_bin("curlftpfs")
        if curlftpfs:
            self._backend = "curlftpfs"
            url = self._ftp_url(target, auth)
            cmd = (["sudo"] if elevate else []) + [curlftpfs, url, mount_point]

            fuse_opts = {}
            if read_only:
                fuse_opts["ro"] = ""
            fuse_opts.update(options)
            opt_str = self._kv_to_mount_opts(fuse_opts)
            if opt_str:
                cmd += ["-o", opt_str]

            self._run(cmd, elevate=False, timeout=timeout)
            return

        # Fallback: rclone
        self._mount_rclone("ftp", target, mount_point, auth, options, read_only, timeout)

    # ---------------------------------------------------------------------
    # SSH / SFTP
    # ---------------------------------------------------------------------

    def _mount_ssh(
        self,
        target: ShareTarget,
        mount_point: str,
        auth: ShareAuth,
        options: Dict[str, str],
        read_only: bool,
        elevate: bool,
        timeout: int,
    ) -> None:
        # Prefer sshfs when available; otherwise rclone (sftp).
        sshfs = self._which_or_bin("sshfs")
        if sshfs and not self._is_windows():
            self._backend = "sshfs"

            user = auth.username or os.getenv("USER") or os.getenv("USERNAME")
            if not user:
                raise ShareError("SSH mount requires a username (auth.username).")

            port = target.port or 22
            remote_path = target.path or "/"
            spec = f"{user}@{target.host}:{remote_path}"

            cmd = (["sudo"] if elevate else []) + [sshfs, spec, mount_point]

            ssh_opts = {}
            if port:
                ssh_opts["port"] = str(port)
            if auth.private_key:
                ssh_opts["IdentityFile"] = auth.private_key
            if read_only:
                ssh_opts["ro"] = ""

            # sshfs uses -o and forwards many options to ssh via -o ssh_command=...,
            # but also accepts options like reconnect, ServerAliveInterval, etc.
            ssh_opts.update(options)
            opt_str = self._kv_to_mount_opts(ssh_opts)
            if opt_str:
                cmd += ["-o", opt_str]

            self._run(cmd, elevate=False, timeout=timeout)
            return

        # Windows (or sshfs not available): rclone mount sftp
        self._mount_rclone("sftp", target, mount_point, auth, options, read_only, timeout)

    # ---------------------------------------------------------------------
    # rclone backend
    # ---------------------------------------------------------------------

    def _mount_rclone(
        self,
        scheme: str,  # ftp|sftp|smb
        target: ShareTarget,
        mount_point: str,
        auth: ShareAuth,
        options: Dict[str, str],
        read_only: bool,
        timeout: int,
    ) -> None:
        self._backend = "rclone"
        rclone = self._bin("rclone")

        # rclone can mount using an on-the-fly remote definition:
        #   rclone mount ":ftp,host=example.com,user=u,pass=...,": /mnt
        #   rclone mount ":sftp,host=example.com,user=u,pass=...,port=22": /mnt
        #   rclone mount ":smb,host=example.com,user=u,pass=...,share=Share": /mnt
        # Note: rclone expects password in plain; it may be obscured in process list.

        params = {
            "host": target.host,
        }
        if auth.username:
            params["user"] = auth.username
        if auth.password:
            params["pass"] = auth.password
        if scheme == "sftp":
            params["port"] = str(target.port or 22)
            if target.path:
                params["path"] = target.path
        elif scheme == "ftp":
            if target.port:
                params["port"] = str(target.port)
            if target.path:
                params["path"] = target.path
        elif scheme == "smb":
            share, subpath = self._split_share_and_subpath(target.path)
            params["share"] = share
            if subpath:
                params["path"] = subpath
        else:
            raise ShareError(f"Unsupported rclone scheme: {scheme}")

        # caller options override
        params.update({k: str(v) for k, v in options.items()})

        remote = ":" + scheme + "," + ",".join(f"{k}={self._rclone_escape(v)}" for k, v in params.items()) + ":"

        cmd = [rclone, "mount", remote, mount_point]
        if read_only:
            cmd += ["--read-only"]

        # Windows: rclone mount is foreground by default; user can run it in service mode.
        self._run(cmd, elevate=False, timeout=timeout)

    # ---------------------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------------------

    def _run(self, cmd: List[str], *, elevate: bool, timeout: int) -> None:
        # `elevate` here is used only if caller explicitly wants it; we keep it as a hook.
        # Most commands are constructed with sudo already when needed.
        self._last_cmd = cmd
        self._log_debug("Executing: " + " ".join(shlex.quote(c) for c in cmd))

        try:
            completed = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
                check=False,
            )
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
        """Resolve an executable using PATH first, then bundled binaries."""
        # Windows executables often require .exe
        candidates = [name]
        if platform.system().lower() == "windows" and not name.lower().endswith(".exe"):
            candidates.insert(0, name + ".exe")

        for c in candidates:
            p = shutil.which(c)
            if p:
                return p

        # Bundled layout: src/bin/[name]/[OS]/[cpu_architecture]/bin/[exe]
        root = self._resolve_bin_root()
        os_name = self._os_folder()
        arch = self._arch_folder()

        for c in candidates:
            bundled = root / name / os_name / arch / "bin" / c
            if bundled.exists() and bundled.is_file():
                return str(bundled)

        # Last resort: return original name and let caller fail with a clearer message
        return candidates[0]

    def _which_or_bin(self, name: str) -> Optional[str]:
        p = shutil.which(name)
        if p:
            return p
        # Try bundled
        b = self._bin(name)
        return b if os.path.isabs(b) and os.path.exists(b) else None

    def _resolve_bin_root(self) -> Path:
        if self._bin_root:
            return self._bin_root
        # We expect this file under src/core/... or corePY/... and bin under src/bin
        here = Path(__file__).resolve()
        # Walk up a bit and look for "src/bin"
        for parent in [here.parent] + list(here.parents):
            candidate = parent / "src" / "bin"
            if candidate.exists():
                self._bin_root = candidate
                return candidate
        # fallback relative to file
        self._bin_root = here.parent.parent / "bin"
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

    def _is_windows(self) -> bool:
        return platform.system().lower() == "windows"

    def _is_windows_drive_letter(self, s: str) -> bool:
        return len(s) == 2 and s[1] == ":" and s[0].isalpha()

    def _smb_unc(self, host: str, remote: str) -> str:
        # remote might be Share or Share/dir
        remote = remote.replace("/", "\\").lstrip("\\")
        return f"\\\\{host}\\{remote}"

    def _split_share_and_subpath(self, remote: str) -> Tuple[str, str]:
        # "Share" -> ("Share", "")
        # "Share/dir/sub" -> ("Share", "dir/sub")
        r = (remote or "").lstrip("/")
        if not r:
            return "", ""
        parts = r.split("/")
        share = parts[0]
        sub = "/".join(parts[1:]) if len(parts) > 1 else ""
        return share, sub

    def _kv_to_mount_opts(self, d: Dict[str, str]) -> str:
        # Convert dict to "k=v,k2=v2,flag" style
        parts = []
        for k, v in d.items():
            if v is None:
                continue
            if v == "":
                parts.append(str(k))
            else:
                parts.append(f"{k}={v}")
        return ",".join(parts)

    def _ftp_url(self, target: ShareTarget, auth: ShareAuth) -> str:
        # curlftpfs accepts URL form: ftp://user:pass@host/path
        userinfo = ""
        if auth.username:
            userinfo = auth.username
            if auth.password:
                userinfo += ":" + self._url_escape(auth.password)
            userinfo += "@"
        port = f":{target.port}" if target.port else ""
        path = target.path or "/"
        if not path.startswith("/"):
            path = "/" + path
        return f"ftp://{userinfo}{target.host}{port}{path}"

    def _url_escape(self, s: str) -> str:
        # Minimal escaping suitable for passwords in smb/ftp URLs
        safe = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~"
        out = []
        for ch in s:
            if ch in safe:
                out.append(ch)
            else:
                out.append("%" + format(ord(ch), "02X"))
        return "".join(out)

    def _rclone_escape(self, s: str) -> str:
        # rclone remote definition uses commas and colons as separators; escape them.
        # Keep it simple: backslash-escape commas and colons.
        return str(s).replace("\\", "\\\\").replace(",", "\\,").replace(":", "\\:")

    def _log_debug(self, msg: str) -> None:
        if self._logger is not None:
            # Support corePY Log-like interface if present
            for method in ("debug", "info"):
                if hasattr(self._logger, method):
                    getattr(self._logger, method)(msg)
                    return
        # Silent by default

