
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
import time
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
        self._last_probe_ok: Optional[bool] = None
        self._last_probe_message: Optional[str] = None
        self._rclone_obscure_cache: Dict[str, str] = {}

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

        # Probe the mount to confirm it is usable.
        ok, msg = self._probe_mount(mount_point, timeout=timeout)
        self._last_probe_ok = ok
        self._last_probe_message = msg
        self._log_debug(f"Share mount probe: mount_point={mount_point} ok={ok} msg={msg}")

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

        # Prefer backend-specific unmount where required
        if self._backend == "rclone":
            rclone = self._bin("rclone")
            self._run([rclone, "unmount", mp], elevate=elevate, timeout=timeout)
            ok_after, msg_after = self._probe_mount(mp, timeout=min(5, timeout))
            self._log_debug(f"Share post-unmount probe: mount_point={mp} ok={ok_after} msg={msg_after}")
            return

        system = platform.system().lower()
        if system == "windows":
            # For SMB we typically used net use; deleting by mount point is OK.
            # Example: net use Z: /delete /y
            self._run(["net", "use", mp, "/delete", "/y"], elevate=False, timeout=timeout)
            ok_after, msg_after = self._probe_mount(mp, timeout=min(5, timeout))
            self._log_debug(f"Share post-unmount probe: mount_point={mp} ok={ok_after} msg={msg_after}")
            return

        # Linux/macOS
        umount_bin = shutil.which("umount") or "umount"
        self._run((["sudo"] if elevate else []) + [umount_bin, mp], elevate=False, timeout=timeout)
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
        # Note: rclone expects password in obscured form for :ftp,pass=...: etc.

        params = {
            "host": target.host,
        }
        if auth.username:
            params["user"] = auth.username
        if auth.password:
            params["pass"] = self._rclone_obscure(auth.password)
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

        # On Unix-like systems, prefer daemon mode so the call returns.
        if not self._is_windows():
            cmd += ["--daemon"]

        self._run(cmd, elevate=False, timeout=timeout)

    # ---------------------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------------------

    def _run(self, cmd: List[str], *, elevate: bool, timeout: int) -> None:
        # `elevate` here is used only if caller explicitly wants it; we keep it as a hook.
        # Most commands are constructed with sudo already when needed.
        self._last_cmd = cmd
        # Redact secrets from command for logging
        printable = []
        for c in cmd:
            s = str(c)
            # Redact common credential patterns (covers rclone on-the-fly remotes)
            for token in ("pass=", "password=", "pwd="):
                if token in s:
                    # replace value until next comma or end
                    parts = s.split(token)
                    rebuilt = [parts[0]]
                    for rest in parts[1:]:
                        # rest begins with secret
                        secret_end = len(rest)
                        for sep in (",", ":"):
                            idx = rest.find(sep)
                            if idx != -1:
                                secret_end = min(secret_end, idx)
                        rebuilt.append(token + "***" + rest[secret_end:])
                    s = "".join(rebuilt)
            printable.append(shlex.quote(s))
        self._log_debug("Executing: " + " ".join(printable))

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

    def _rclone_obscure(self, plain: str) -> str:
        """Return rclone-obscured form of a password.

        When using on-the-fly remotes like ":ftp,...,pass=...:", rclone expects the
        password to be in its obscured form (same as in rclone.conf), not plain text.
        """
        if plain is None:
            return ""
        key = str(plain)
        if key in self._rclone_obscure_cache:
            return self._rclone_obscure_cache[key]

        rclone = self._bin("rclone")
        try:
            completed = subprocess.run(
                [rclone, "obscure", key],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
                check=False,
            )
        except FileNotFoundError as e:
            raise ShareError("rclone executable not found for password obfuscation") from e

        if completed.returncode != 0:
            msg = (completed.stderr or completed.stdout or "").strip()
            raise ShareError(f"Failed to obscure password with rclone (code {completed.returncode}): {msg}")

        obscured = (completed.stdout or "").strip()
        if not obscured:
            raise ShareError("Failed to obscure password with rclone: empty output")

        self._rclone_obscure_cache[key] = obscured
        return obscured

    def _log_debug(self, msg: str) -> None:
        if self._logger is not None:
            # Support corePY Log-like interface if present
            for method in ("debug", "info"):
                if hasattr(self._logger, method):
                    getattr(self._logger, method)(msg)
                    return
        # Silent by default


    def _probe_mount(self, mount_point: str, *, timeout: int = 10) -> Tuple[bool, str]:
        """Best-effort probe to determine if a mount point is usable.

        We avoid relying solely on os.path.ismount (unreliable for some FUSE setups).
        Strategy:
          - Windows drive letter: check if it exists and list root.
          - Directory mounts: attempt listdir and optional backend-specific checks.
        """
        try:
            if self._is_windows_drive_letter(mount_point):
                return self._probe_windows_drive(mount_point)

            mp = str(mount_point)
            if not os.path.exists(mp):
                return False, "mount point does not exist"
            if not os.path.isdir(mp):
                return False, "mount point is not a directory"

            # For rclone mounts, a listdir may succeed even if backend is not ready.
            # So we attempt a quick rclone ls/lsf when possible.
            if self._backend == "rclone":
                try:
                    rclone = self._bin("rclone")
                    # lsf will fail if the mount is not accessible.
                    completed = subprocess.run(
                        [rclone, "lsf", mp, "--max-depth", "1"],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        timeout=max(2, min(timeout, 5)),
                        check=False,
                    )
                    if completed.returncode == 0:
                        return True, "rclone lsf ok"
                    msg = (completed.stderr or completed.stdout or "").strip() or f"rclone lsf failed ({completed.returncode})"
                    return False, msg
                except Exception as e:
                    # Fall back to listdir below
                    self._log_debug(f"Probe: rclone lsf check failed, falling back to listdir: {e}")

            # Generic: listdir with short retry window (useful right after mount)
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
