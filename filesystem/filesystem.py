#!/usr/bin/env python3
# corePY/filesystem/filesystem.py

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union, Iterable, Tuple

from PyQt5.QtWidgets import QApplication

try:
    from core.helper import Helper
except ImportError:
    from helper import Helper

PathLike = Union[str, os.PathLike]

@dataclass(frozen=True)
class CommandSpec:
    """A simple command container useful for logging/execution."""

    argv: list[str]
    label: str
    sensitive: bool = False


class FileSystem:
    """Cross-platform filesystem helper.

    Goals:
      - Provide *basic* operations (read/copy/delete) for files/folders.
      - Prefer native tools when asked to preserve metadata:
          - Windows: robocopy (best for NTFS ACLs)
          - Linux/macOS: rsync (best for POSIX perms/ACL/xattrs when supported)
      - Fall back to Python stdlib when tools are missing.

    Notes:
      - UNC paths (\\\\server\\share\\path) are treated as normal paths on Windows.
      - "preserve_metadata" is best-effort; some filesystems/mounts can't store all metadata.
    """

    def __init__(
        self,
        helper: Optional[Helper] = None,
        logger: Optional[object] = None,
    ):
        super().__init__()

        # --- auto-wire from QApplication if not provided ---
        if helper is None or logger is None:
            app = QApplication.instance()
            if app is None:
                raise RuntimeError("FileSystem must be created after QApplication/Application.")
            helper = helper or app.helper  # type: ignore[attr-defined]
            logger = logger or getattr(app, "logger", None)

        self._helper: Helper = helper
        self._logger = logger
        self._os: str = self._helper.get_os()

        # Cache for tool capability detection
        self._rsync_caps: Optional[dict[str, bool]] = None

    # ------------------------------------------------------------------
    # Small helpers
    # ------------------------------------------------------------------

    def _p(self, path: PathLike) -> str:
        return str(path)

    def _log(self, msg: str, level: str = "debug", channel: str = "filesystem") -> None:
        if self._logger is not None and hasattr(self._logger, "append"):
            try:
                self._logger.append(msg, level=level, channel=channel)  # type: ignore[call-arg]
                return
            except Exception:
                pass
        # Fallback: silent unless explicitly needed

    def _rsync_capabilities(self) -> dict[str, bool]:
        """Detect which rsync flags are supported on this host.

        macOS often ships an older rsync that does not support -A/-X.
        We detect capabilities once and cache them.
        """
        if self._rsync_caps is not None:
            return self._rsync_caps

        caps = {
            "has_rsync": False,
            "acls": False,        # -A / --acls
            "xattrs": False,      # -X / --xattrs
            "hardlinks": False,   # -H / --hard-links
            "numeric_ids": False, # --numeric-ids
        }

        rsync = shutil.which("rsync")
        if not rsync:
            self._rsync_caps = caps
            return caps

        caps["has_rsync"] = True

        # Try `--help` (most reliable to see flag availability)
        rc, out = self._helper.run([rsync, "--help"])
        if rc != 0:
            # If help fails, assume minimal support
            self._rsync_caps = caps
            return caps

        help_text = out or ""

        # Look for common help strings
        if "--acls" in help_text or " -A" in help_text:
            caps["acls"] = True
        if "--xattrs" in help_text or " -X" in help_text:
            caps["xattrs"] = True
        if "--hard-links" in help_text or " -H" in help_text:
            caps["hardlinks"] = True
        if "--numeric-ids" in help_text:
            caps["numeric_ids"] = True

        self._rsync_caps = caps
        return caps

    def _build_rsync_args(self, *, allow_deletion: bool, preserve_metadata: bool) -> Optional[list[str]]:
        """Build a safe rsync argv for the current host.

        We always include `-a` (archive) when preserve_metadata=True.
        Additional flags (-H/-A/-X/--numeric-ids) are appended only if supported.
        """
        caps = self._rsync_capabilities()
        if not caps.get("has_rsync"):
            return None

        rsync = shutil.which("rsync") or "rsync"

        args: list[str] = [rsync]

        if preserve_metadata:
            # archive mode preserves perms, times, symlinks, etc.
            args.append("-a")
        else:
            # minimal recursion + times (best-effort)
            args.extend(["-r", "-t"])

        if caps.get("hardlinks"):
            args.append("-H")
        if preserve_metadata and caps.get("acls"):
            args.append("-A")
        if preserve_metadata and caps.get("xattrs"):
            args.append("-X")
        if preserve_metadata and caps.get("numeric_ids"):
            args.append("--numeric-ids")

        if allow_deletion:
            args.append("--delete")

        return args

    def exists(self, path: PathLike) -> bool:
        return os.path.exists(self._p(path))

    def is_file(self, path: PathLike) -> bool:
        return os.path.isfile(self._p(path))

    def is_dir(self, path: PathLike) -> bool:
        return os.path.isdir(self._p(path))

    def mkdirs(self, path: PathLike) -> None:
        os.makedirs(self._p(path), exist_ok=True)

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def read_bytes(self, path: PathLike) -> bytes:
        p = self._p(path)
        with open(p, "rb") as f:
            return f.read()

    def read_text(self, path: PathLike, encoding: str = "utf-8", errors: str = "strict") -> str:
        p = self._p(path)
        with open(p, "r", encoding=encoding, errors=errors) as f:
            return f.read()

    # ------------------------------------------------------------------
    # Delete operations
    # ------------------------------------------------------------------

    def delete(self, path: PathLike) -> bool:
        """Delete a file or folder (recursive). Returns True if deleted, False if not found."""
        p = self._p(path)
        if not os.path.exists(p):
            return False

        if os.path.isdir(p) and not os.path.islink(p):
            shutil.rmtree(p)
        else:
            os.remove(p)
        return True

    # ------------------------------------------------------------------
    # Copy operations
    # ------------------------------------------------------------------

    def copy(
        self,
        src: PathLike,
        dst: PathLike,
        *,
        preserve_metadata: bool = True,
        allow_deletion: bool = False,
    ) -> bool:
        """Copy file or directory from src to dst.

        If src is a directory:
          - preserve_metadata=True prefers robocopy (Windows) or rsync (Linux/macOS)
          - allow_deletion=True makes it a mirror (best-effort with stdlib fallback)

        Returns:
          - True on success
          - False on failure
        """
        src_s = self._p(src)
        dst_s = self._p(dst)

        if not os.path.exists(src_s):
            self._log(f"[FileSystem] copy: source not found: {src_s}", level="warning")
            return False

        try:
            if os.path.isdir(src_s):
                return self._copy_dir(src_s, dst_s, preserve_metadata=preserve_metadata, allow_deletion=allow_deletion)
            else:
                return self._copy_file(src_s, dst_s, preserve_metadata=preserve_metadata)
        except Exception as e:
            self._log(f"[FileSystem] copy failed: {src_s} -> {dst_s} ({e})", level="error")
            return False

    def _copy_file(self, src: str, dst: str, *, preserve_metadata: bool = True) -> bool:
        self.mkdirs(os.path.dirname(dst) or ".")

        # shutil.copy2 preserves basic metadata (mtime/atime + mode where possible)
        if preserve_metadata:
            shutil.copy2(src, dst)
        else:
            shutil.copy(src, dst)

        return True

    def _copy_dir(
        self,
        src: str,
        dst: str,
        *,
        preserve_metadata: bool = True,
        allow_deletion: bool = False,
    ) -> bool:
        # Prefer native tools when metadata matters
        if preserve_metadata:
            cmd = self.build_mirror_command(src, dst, allow_deletion=allow_deletion)
            if cmd is not None:
                self._log(f"[FileSystem] executing: {cmd.label}: {' '.join(cmd.argv)}")
                rc, out = self._helper.run(cmd.argv)

                # robocopy has special return codes: 0-7 are generally success-ish.
                if cmd.argv and os.path.basename(cmd.argv[0]).lower().startswith("robocopy"):
                    ok = rc <= 7
                else:
                    ok = rc == 0

                if not ok:
                    self._log(f"[FileSystem] tool copy failed rc={rc}: {out}", level="warning")
                return ok

        # Fallback: Python stdlib directory copy
        # Ensure destination exists
        os.makedirs(dst, exist_ok=True)

        # Copy/update files
        for root, dirs, files in os.walk(src):
            rel = os.path.relpath(root, src)
            target_root = dst if rel == "." else os.path.join(dst, rel)
            os.makedirs(target_root, exist_ok=True)

            for d in dirs:
                os.makedirs(os.path.join(target_root, d), exist_ok=True)

            for f in files:
                s = os.path.join(root, f)
                t = os.path.join(target_root, f)
                # copy2 for metadata best-effort
                shutil.copy2(s, t)

        # Mirror delete extras if requested (best-effort)
        if allow_deletion:
            self._mirror_delete_extras(src, dst)

        return True

    def _mirror_delete_extras(self, src: str, dst: str) -> None:
        """Delete files/dirs that exist in dst but not in src (best-effort)."""
        # Walk destination and compare
        for root, dirs, files in os.walk(dst, topdown=False):
            rel = os.path.relpath(root, dst)
            src_root = src if rel == "." else os.path.join(src, rel)

            # Remove files not present in src
            for f in files:
                d_path = os.path.join(root, f)
                s_path = os.path.join(src_root, f)
                if not os.path.exists(s_path):
                    try:
                        os.remove(d_path)
                    except Exception as e:
                        self._log(f"[FileSystem] mirror delete file failed: {d_path} ({e})", level="warning")

            # Remove dirs not present in src
            for d in dirs:
                d_path = os.path.join(root, d)
                s_path = os.path.join(src_root, d)
                if not os.path.exists(s_path):
                    try:
                        shutil.rmtree(d_path)
                    except Exception as e:
                        self._log(f"[FileSystem] mirror delete dir failed: {d_path} ({e})", level="warning")

    # ------------------------------------------------------------------
    # Command generation (reusable by Replicator)
    # ------------------------------------------------------------------

    def build_mirror_command(self, src_dir: PathLike, dst_dir: PathLike, *, allow_deletion: bool) -> Optional[CommandSpec]:
        """Build the best command to mirror src_dir -> dst_dir on this host.

        Returns a CommandSpec or None if no suitable tool is available.
        """
        src = self._p(src_dir)
        dst = self._p(dst_dir)

        # -------------------------
        # Windows: robocopy
        # -------------------------
        if self._os == "windows":
            # robocopy is typically available in PATH on Windows
            robocopy = shutil.which("robocopy") or "robocopy"

            # /MIR implies /E + delete extras; /E copies subdirs including empty
            # /COPY:DATSOU preserves Data, Attributes, Timestamps, Security (ACL), Owner, Auditing
            # /DCOPY:DAT preserves directory timestamps
            # /R /W keep retries low for UI responsiveness
            args = [
                robocopy,
                src,
                dst,
                "/MIR" if allow_deletion else "/E",
                "/COPY:DATSOU",
                "/DCOPY:DAT",
                "/R:1",
                "/W:1",
            ]
            return CommandSpec(argv=args, label="robocopy")

        # -------------------------
        # Linux/macOS: rsync
        # -------------------------
        if self._os in ("linux", "macos"):
            # Use trailing slashes to copy contents of src into dst
            src_arg = src.rstrip("/") + "/"
            dst_arg = dst.rstrip("/") + "/"

            base = self._build_rsync_args(allow_deletion=allow_deletion, preserve_metadata=True)
            if not base:
                return None

            args = base + [src_arg, dst_arg]
            return CommandSpec(argv=args, label="rsync")

        return None
