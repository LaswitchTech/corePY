#!/bin/bash
set -euo pipefail

# -----------------------------------------------------------------------------
# build.sh (generic PyInstaller build script)
# - Designed to be reused across LaswitchTech projects
# - Defaults work out-of-the-box for this repo (Replicator)
# -----------------------------------------------------------------------------

log() {
  echo "$(date +'%Y-%m-%d %H:%M:%S') - $*"
}

die() {
  echo "ERROR: $*" >&2
  exit 1
}

detect_os() {
  case "$(uname -s)" in
    Darwin) echo "macos" ;;
    Linux)  echo "linux" ;;
    WindowsNT|MINGW64*|MINGW32*|MSYS*|CYGWIN*) echo "windows" ;;
    *) echo "unsupported" ;;
  esac
}


# Portable in-place sed (macOS vs GNU)
sed_inplace() {
  # usage: sed_inplace 's/a/b/' file
  if sed --version >/dev/null 2>&1; then
    sed -i "$1" "$2"
  else
    sed -i '' "$1" "$2"
  fi
}

#
# Icon generation
# - macOS: icon.svg -> icon.icns (every build)
# - Windows: icon.svg -> icon.ico (best-effort, every build)
# Requires: iconutil (macOS) + either rsvg-convert (librsvg) OR inkscape.
# Windows ICO generation prefers: ImageMagick (magick/convert) OR Python + Pillow.
# -----------------------------------------------------------------------------

generate_icns_from_svg_macos() {
  [ "$OS" = "macos" ] || return 0

  local svg="src/icons/icon.svg"
  local out="src/icons/icon.icns"

  [ -f "$svg" ] || return 0

  if ! command -v iconutil >/dev/null 2>&1; then
    log "WARN: iconutil not found; cannot generate .icns from $svg"
    return 0
  fi

  local renderer=""
  if command -v rsvg-convert >/dev/null 2>&1; then
    renderer="rsvg"
  elif command -v inkscape >/dev/null 2>&1; then
    renderer="inkscape"
  else
    log "WARN: Neither rsvg-convert nor inkscape found; cannot generate .icns from $svg"
    log "      Install one of them (recommended: brew install librsvg)"
    return 0
  fi

  mkdir -p "build"

  local iconset="build/icon.iconset"
  rm -rf "$iconset"
  mkdir -p "$iconset"

  # Required sizes for iconutil
  local sizes=(16 32 128 256 512)

  log "Regenerating macOS icon: $out (from $svg using $renderer)"

  for s in "${sizes[@]}"; do
    local s2=$((s * 2))

    if [ "$renderer" = "rsvg" ]; then
      rsvg-convert -w "$s"  -h "$s"  "$svg" -o "$iconset/icon_${s}x${s}.png"
      rsvg-convert -w "$s2" -h "$s2" "$svg" -o "$iconset/icon_${s}x${s}@2x.png"
    else
      # inkscape CLI (v1+)
      inkscape "$svg" --export-type=png --export-width="$s"  --export-height="$s"  --export-filename="$iconset/icon_${s}x${s}.png" >/dev/null 2>&1
      inkscape "$svg" --export-type=png --export-width="$s2" --export-height="$s2" --export-filename="$iconset/icon_${s}x${s}@2x.png" >/dev/null 2>&1
    fi
  done

  # Build .icns
  iconutil -c icns "$iconset" -o "$out"

  # Cleanup iconset directory (keep build folder)
  rm -rf "$iconset"
}

# -----------------------------------------------------------------------------
# Windows icon generation: icon.svg -> icon.ico
# Best-effort: uses inkscape/rsvg-convert to render PNGs then combines to ICO.
# Prefers ImageMagick; falls back to Python+Pillow if available.
# -----------------------------------------------------------------------------

generate_ico_from_svg_windows() {
  [ "$OS" = "windows" ] || return 0

  local svg="src/icons/icon.svg"
  local out="src/icons/icon.ico"

  [ -f "$svg" ] || return 0

  local renderer=""
  if command -v rsvg-convert >/dev/null 2>&1; then
    renderer="rsvg"
  elif command -v inkscape >/dev/null 2>&1; then
    renderer="inkscape"
  else
    log "WARN: Neither rsvg-convert nor inkscape found; cannot generate .ico from $svg"
    log "      Install one of them (recommended: Inkscape)"
    return 0
  fi

  mkdir -p "build"

  local tmpdir="build/icon.ico.tmp"
  rm -rf "$tmpdir"
  mkdir -p "$tmpdir"

  # Common ICO sizes
  local sizes=(16 24 32 48 64 128 256)

  log "Regenerating Windows icon: $out (from $svg using $renderer)"

  for s in "${sizes[@]}"; do
    if [ "$renderer" = "rsvg" ]; then
      rsvg-convert -w "$s" -h "$s" "$svg" -o "$tmpdir/${s}.png"
    else
      # inkscape CLI (v1+)
      inkscape "$svg" --export-type=png --export-width="$s" --export-height="$s" --export-filename="$tmpdir/${s}.png" >/dev/null 2>&1
    fi
  done

  # Combine PNGs into ICO
  if command -v magick >/dev/null 2>&1; then
    # ImageMagick 7+
    magick "$tmpdir/16.png" "$tmpdir/24.png" "$tmpdir/32.png" "$tmpdir/48.png" "$tmpdir/64.png" "$tmpdir/128.png" "$tmpdir/256.png" "$out" 2>/dev/null || true
  elif command -v convert >/dev/null 2>&1; then
    # ImageMagick 6 (convert)
    convert "$tmpdir/16.png" "$tmpdir/24.png" "$tmpdir/32.png" "$tmpdir/48.png" "$tmpdir/64.png" "$tmpdir/128.png" "$tmpdir/256.png" "$out" 2>/dev/null || true
  else
    # Fallback: Python + Pillow (if available)
    python - <<'PY' "$tmpdir" "$out" 2>/dev/null || true
import sys
from pathlib import Path

tmpdir = Path(sys.argv[1])
out = Path(sys.argv[2])

try:
    from PIL import Image
except Exception:
    raise SystemExit(1)

sizes = [16, 24, 32, 48, 64, 128, 256]
imgs = []
for s in sizes:
    p = tmpdir / f"{s}.png"
    if p.exists():
        imgs.append(Image.open(p))

if not imgs:
    raise SystemExit(1)

# Pillow writes multi-size ICO when you pass sizes
base = imgs[-1]
base.save(out, format="ICO", sizes=[(s, s) for s in sizes])
PY
  fi

  if [ -f "$out" ]; then
    log "Windows icon generated: $out"
  else
    log "WARN: Failed to generate $out (install ImageMagick or Python Pillow for best results)"
  fi

  rm -rf "$tmpdir"
}

# Guess app name from current folder if not provided
infer_name() {
  local base
  base="$(basename "$(pwd)")"
  # keep it simple: only allow alnum, dash, underscore
  echo "$base" | tr -cd '[:alnum:]_-'
}

show_help() {
  cat <<'EOF'
Usage:
  ./build.sh [options]

Options:
  --name NAME                App name (default: folder name)
  --entry PATH               Entry script/module for PyInstaller (default: src/main.py)
  --config PATH              Path to build.cfg (default: ./build.cfg if present)
  --windowed                 Build GUI app (no console) (macOS: --windowed; Linux: still onefile)
  --console                  Build console app (default)
  --icon PATH                Icon path (macOS: .icns recommended)
  --add-data SRC:DST         Add data folder/file (repeatable). Uses PyInstaller --add-data=SRC:DST.
  --hidden-import MOD        Add hidden import (repeatable)
  --onefile                  Force onefile (default on Linux)
  --onedir                   Force onedir (default on macOS)
  --python PY                Python 3.11 binary (default: python3.11 from PATH)
  --system-pyqt              On Linux ARM, prefer APT PyQt5 with --system-site-packages
  --clean                    Remove build/dist artifacts before building
  --debug-pyi                Enable PyInstaller debug output (--log-level=DEBUG --debug=all)
  --dmg                      On macOS, create a DMG (only meaningful for onedir .app)
  --gen-install              Generate install/run wrapper scripts into repo root (default)
  --no-gen-install           Do not generate install/run wrapper scripts
  --venv-dir DIR             Virtualenv directory for wrapper scripts (default: .venv)
  --requirements PATH        Requirements file used by wrapper scripts if present (default: requirements.txt)
  -h, --help                 Show this help

Examples:
  ./build.sh --name Replicator --entry src/main.py --windowed --add-data src/app:app
  ./build.sh --console --hidden-import PyQt5.QtSvg
EOF
}

# -----------------------------------------------------------------------------
# Defaults (good for Replicator)
# -----------------------------------------------------------------------------
OS="$(detect_os)"
[ "$OS" = "unsupported" ] && die "Unsupported operating system."

APP_NAME="$(infer_name)"
ENTRY="src/main.py"
MODE="console"             # console|windowed

# Packaging defaults: macOS prefers onedir (for .app), Linux prefers onefile
PKG=""                     # empty = auto, or onefile/onedir

#
# Python / venv defaults
# Python discovery
# - macOS/Linux: prefer python3.11
# - Windows (Git Bash): prefer Python Launcher `py -3.11`, otherwise `python`
PYTHON_BIN=""
PYTHON_LAUNCH_ARGS=""

if command -v python3.11 >/dev/null 2>&1; then
  PYTHON_BIN="python3.11"
elif [ "$OS" = "windows" ] && command -v py >/dev/null 2>&1; then
  PYTHON_BIN="py"
  PYTHON_LAUNCH_ARGS="-3.11"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="python3"
elif command -v python >/dev/null 2>&1; then
  PYTHON_BIN="python"
fi

python_exec() {
  # Wrapper so we can call: python_exec -m venv ...
  if [ -n "${PYTHON_LAUNCH_ARGS:-}" ]; then
    "$PYTHON_BIN" "$PYTHON_LAUNCH_ARGS" "$@"
  else
    "$PYTHON_BIN" "$@"
  fi
}

venv_python_path() {
  if [ "$OS" = "windows" ]; then
    echo "$VENV_DIR/Scripts/python.exe"
  else
    echo "$VENV_DIR/bin/python"
  fi
}

venv_activate_path() {
  if [ "$OS" = "windows" ]; then
    echo "$VENV_DIR/Scripts/activate"
  else
    echo "$VENV_DIR/bin/activate"
  fi
}

USE_SYSTEM_PYQT=0

# macOS DMG toggle
MAKE_DMG=0

# PyInstaller debug toggle
PYI_DEBUG=0

# Icon (optional)
ICON_FILE=""

# Repeatable arrays
declare -a ADD_DATA
ADD_DATA=()

declare -a HIDDEN_IMPORTS
HIDDEN_IMPORTS=()

# Common default data folders (only add if they exist)
# Replicator usually has icons/app/styles/img in some projects; keep generic.
DEFAULT_DATA_CANDIDATES=(
  "src/app:app"
  "src/styles:styles"
  "src/icons:icons"
  "src/img:img"
  "assets:assets"
)

# Optional config file
CFG_FILE=""

# Wrapper script defaults
GENERATE_INSTALL=1
VENV_DIR=".venv"
REQ_FILE="requirements.txt"

# Load simple KEY=VALUE config (no code execution)
# Supported keys:
#   NAME, ENTRY, MODE, PKG, ICON, PYTHON, SYSTEM_PYQT, DMG, CLEAN
#   ADD_DATA, HIDDEN_IMPORTS
# Notes:
# - Lines beginning with # or ; are ignored
# - Whitespace around keys/values is trimmed
# - ADD_DATA / HIDDEN_IMPORTS can be comma or semicolon separated
trim_ws() {
  # trim leading/trailing whitespace
  local s="$1"
  s="${s#${s%%[![:space:]]*}}"
  s="${s%${s##*[![:space:]]}}"
  echo "$s"
}

split_list() {
  # split comma/semicolon separated list into lines
  echo "$1" | tr ';' '\n' | tr ',' '\n'
}

apply_cfg_kv() {
  local key="$1" val="$2"
  key="$(trim_ws "$key")"
  val="$(trim_ws "$val")"
  [ -z "$key" ] && return 0

  case "$key" in
    NAME)
      APP_NAME="$val"
      ;;
    ENTRY)
      ENTRY="$val"
      ;;
    MODE)
      # console|windowed
      if [ "$val" = "console" ] || [ "$val" = "windowed" ]; then
        MODE="$val"
      fi
      ;;
    PKG)
      # onefile|onedir|auto
      if [ "$val" = "onefile" ] || [ "$val" = "onedir" ]; then
        PKG="$val"
      elif [ "$val" = "auto" ]; then
        PKG=""
      fi
      ;;
    ICON)
      ICON_FILE="$val"
      ;;
    PYTHON)
      PYTHON_BIN="$val"
      ;;
    SYSTEM_PYQT)
      if [ "$val" = "1" ] || [ "$val" = "true" ] || [ "$val" = "yes" ]; then
        USE_SYSTEM_PYQT=1
      elif [ "$val" = "0" ] || [ "$val" = "false" ] || [ "$val" = "no" ]; then
        USE_SYSTEM_PYQT=0
      fi
      ;;
    DMG)
      if [ "$val" = "1" ] || [ "$val" = "true" ] || [ "$val" = "yes" ]; then
        MAKE_DMG=1
      elif [ "$val" = "0" ] || [ "$val" = "false" ] || [ "$val" = "no" ]; then
        MAKE_DMG=0
      fi
      ;;
    CLEAN)
      if [ "$val" = "1" ] || [ "$val" = "true" ] || [ "$val" = "yes" ]; then
        CLEAN=1
      elif [ "$val" = "0" ] || [ "$val" = "false" ] || [ "$val" = "no" ]; then
        CLEAN=0
      fi
      ;;
    ADD_DATA)
      # replaces current list
      ADD_DATA=()
      while IFS= read -r item; do
        item="$(trim_ws "$item")"
        [ -n "$item" ] && ADD_DATA+=("$item")
      done < <(split_list "$val")
      ;;
    HIDDEN_IMPORTS)
      # replaces current list
      HIDDEN_IMPORTS=()
      while IFS= read -r item; do
        item="$(trim_ws "$item")"
        [ -n "$item" ] && HIDDEN_IMPORTS+=("$item")
      done < <(split_list "$val")
      ;;
    GEN_INSTALL)
      if [ "$val" = "1" ] || [ "$val" = "true" ] || [ "$val" = "yes" ]; then
        GENERATE_INSTALL=1
      elif [ "$val" = "0" ] || [ "$val" = "false" ] || [ "$val" = "no" ]; then
        GENERATE_INSTALL=0
      fi
      ;;
    VENV_DIR)
      VENV_DIR="$val"
      ;;
    REQUIREMENTS)
      REQ_FILE="$val"
      ;;
    *)
      # unknown keys ignored for forward-compat
      ;;
  esac
}

load_cfg_file() {
  local f="$1"
  [ -f "$f" ] || return 0
  log "Loading config: $f"

  while IFS= read -r line || [ -n "$line" ]; do
    # strip CR (Windows line endings)
    line="${line%$'\r'}"

    # ignore comments/blank lines
    case "$(trim_ws "$line")" in
      ""|\#*|\;*) continue ;;
    esac

    # allow inline comments after a value using #
    # (only if there is at least one space before #)
    if echo "$line" | grep -q "[[:space:]]#"; then
      line="$(echo "$line" | sed 's/[[:space:]]#.*$//')"
    fi

    if echo "$line" | grep -q "="; then
      local k v
      k="${line%%=*}"
      v="${line#*=}"
      apply_cfg_kv "$k" "$v"
    fi
  done < "$f"
}

# -----------------------------------------------------------------------------
# Parse args
# -----------------------------------------------------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    --name)
      shift
      [ $# -gt 0 ] || die "--name requires a value"
      APP_NAME="$1"
      ;;
    --entry)
      shift
      [ $# -gt 0 ] || die "--entry requires a value"
      ENTRY="$1"
      ;;
    --config)
      shift
      [ $# -gt 0 ] || die "--config requires a value"
      CFG_FILE="$1"
      ;;
    --windowed)
      MODE="windowed"
      ;;
    --console)
      MODE="console"
      ;;
    --icon)
      shift
      [ $# -gt 0 ] || die "--icon requires a value"
      ICON_FILE="$1"
      ;;
    --add-data)
      shift
      [ $# -gt 0 ] || die "--add-data requires a value like SRC:DST"
      ADD_DATA+=("$1")
      ;;
    --hidden-import)
      shift
      [ $# -gt 0 ] || die "--hidden-import requires a module name"
      HIDDEN_IMPORTS+=("$1")
      ;;
    --onefile)
      PKG="onefile"
      ;;
    --onedir)
      PKG="onedir"
      ;;
    --python)
      shift
      [ $# -gt 0 ] || die "--python requires a path/binary"
      PYTHON_BIN="$1"
      ;;
    --system-pyqt)
      USE_SYSTEM_PYQT=1
      ;;
    --clean)
      CLEAN=1
      ;;
    --debug-pyi)
      PYI_DEBUG=1
      ;;
    --dmg)
      MAKE_DMG=1
      ;;
    --gen-install)
      GENERATE_INSTALL=1
      ;;
    --no-gen-install)
      GENERATE_INSTALL=0
      ;;
    --venv-dir)
      shift
      [ $# -gt 0 ] || die "--venv-dir requires a value"
      VENV_DIR="$1"
      ;;
    --requirements)
      shift
      [ $# -gt 0 ] || die "--requirements requires a value"
      REQ_FILE="$1"
      ;;
    -h|--help)
      show_help
      exit 0
      ;;
    *)
      die "Unknown option: $1 (use --help)"
      ;;
  esac
  shift
done

# -----------------------------------------------------------------------------
# Load config (if provided, or if ./build.cfg exists)
# CLI flags should override config values.
# -----------------------------------------------------------------------------

# Capture what the user explicitly set via CLI so we can re-apply after config
CLI_APP_NAME="$APP_NAME"
CLI_ENTRY="$ENTRY"
CLI_MODE="$MODE"
CLI_PKG="$PKG"
CLI_ICON="$ICON_FILE"
CLI_PYTHON_BIN="$PYTHON_BIN"
CLI_USE_SYSTEM_PYQT="$USE_SYSTEM_PYQT"
CLI_MAKE_DMG="$MAKE_DMG"
CLI_CLEAN="${CLEAN:-0}"
CLI_ADD_DATA=("${ADD_DATA[@]+${ADD_DATA[@]}}")
CLI_HIDDEN_IMPORTS=("${HIDDEN_IMPORTS[@]+${HIDDEN_IMPORTS[@]}}")
CLI_GENERATE_INSTALL="$GENERATE_INSTALL"
CLI_VENV_DIR="$VENV_DIR"
CLI_REQ_FILE="$REQ_FILE"

# Determine config path
if [ -z "$CFG_FILE" ] && [ -f "build.cfg" ]; then
  CFG_FILE="build.cfg"
fi

# Apply config
if [ -n "$CFG_FILE" ]; then
  load_cfg_file "$CFG_FILE"
fi

# Re-apply CLI values only when the user actually supplied flags.
# Heuristic: if arrays were empty pre-config but are non-empty post-config,
# we only override them when CLI provided values (captured arrays non-empty).
# Scalars: if CLI value differs from the default-initialized value AND the flag was used,
# we treat it as explicitly set. For simplicity, we treat any CLI value as authoritative
# when it differs from what config loaded.

# Scalars
[ "$CLI_APP_NAME" != "$(infer_name)" ] && APP_NAME="$CLI_APP_NAME" || true
[ "$CLI_ENTRY" != "src/main.py" ] && ENTRY="$CLI_ENTRY" || true
[ "$CLI_MODE" != "console" ] && MODE="$CLI_MODE" || true
[ -n "$CLI_PKG" ] && PKG="$CLI_PKG" || true
[ -n "$CLI_ICON" ] && ICON_FILE="$CLI_ICON" || true
[ "$CLI_PYTHON_BIN" != "$(command -v python3.11 || true)" ] && PYTHON_BIN="$CLI_PYTHON_BIN" || true
[ "$CLI_USE_SYSTEM_PYQT" != "0" ] && USE_SYSTEM_PYQT="$CLI_USE_SYSTEM_PYQT" || true
[ "$CLI_MAKE_DMG" != "0" ] && MAKE_DMG="$CLI_MAKE_DMG" || true
if [ "$CLI_CLEAN" = "1" ]; then CLEAN=1; fi
# New CLI re-apply for wrapper script settings
if [ "$CLI_GENERATE_INSTALL" != "1" ]; then
  GENERATE_INSTALL="$CLI_GENERATE_INSTALL"
fi
if [ "$CLI_VENV_DIR" != ".venv" ]; then
  VENV_DIR="$CLI_VENV_DIR"
fi
if [ "$CLI_REQ_FILE" != "requirements.txt" ]; then
  REQ_FILE="$CLI_REQ_FILE"
fi

# Arrays
if [ "${#CLI_ADD_DATA[@]}" -gt 0 ]; then
  nonempty=0
  for v in "${CLI_ADD_DATA[@]}"; do
    [ -n "$v" ] && nonempty=1
  done
  if [ "$nonempty" -eq 1 ]; then
    ADD_DATA=("${CLI_ADD_DATA[@]}")
  fi
fi

if [ "${#CLI_HIDDEN_IMPORTS[@]}" -gt 0 ]; then
  nonempty=0
  for v in "${CLI_HIDDEN_IMPORTS[@]}"; do
    [ -n "$v" ] && nonempty=1
  done
  if [ "$nonempty" -eq 1 ]; then
    HIDDEN_IMPORTS=("${CLI_HIDDEN_IMPORTS[@]}")
  fi
fi

# -----------------------------------------------------------------------------
# Regenerate platform icons from icon.svg on every build (if present)
# -----------------------------------------------------------------------------
generate_icns_from_svg_macos
generate_ico_from_svg_windows

# -----------------------------------------------------------------------------
# Generate developer-friendly install/run wrapper scripts (if enabled)
# -----------------------------------------------------------------------------
generate_wrapper_scripts() {
  [ "${GENERATE_INSTALL:-1}" -eq 1 ] || return 0

  local out_dir="$1"  # e.g. . (repo root)
  mkdir -p "$out_dir"

  # ---------------------------------------------------------------------------
  # POSIX shell wrapper (macOS/Linux)
  # ---------------------------------------------------------------------------
  cat >"$out_dir/run.sh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail

# Replicator dev/run wrapper
# - Creates a virtualenv if missing
# - Installs runtime deps (prefers requirements.txt if present)
# - Runs src/main.py

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

VENV_DIR="__VENV_DIR__"
REQ_FILE="__REQ_FILE__"
PY_BIN="python3.11"

if command -v "$PY_BIN" >/dev/null 2>&1; then
  :
elif command -v python3 >/dev/null 2>&1; then
  PY_BIN="python3"
elif command -v python >/dev/null 2>&1; then
  PY_BIN="python"
else
  echo "ERROR: Python not found in PATH" >&2
  exit 1
fi

if [ ! -x "$VENV_DIR/bin/python" ]; then
  echo "Creating virtualenv: $VENV_DIR"
  "$PY_BIN" -m venv "$VENV_DIR"
fi

# shellcheck disable=SC1090
source "$VENV_DIR/bin/activate"

python -m pip install --upgrade pip wheel

if [ -f "$REQ_FILE" ]; then
  echo "Installing requirements from $REQ_FILE"
  python -m pip install -r "$REQ_FILE"
else
  echo "Installing minimal runtime deps (PyQt5)"
  python -m pip install "PyQt5>=5.15,<6"
fi

exec python "src/main.py" "$@"
SH

  # Patch placeholders
  sed_inplace "s|__VENV_DIR__|$VENV_DIR|g" "$out_dir/run.sh"
  sed_inplace "s|__REQ_FILE__|$REQ_FILE|g" "$out_dir/run.sh"
  chmod +x "$out_dir/run.sh" || true

  # ---------------------------------------------------------------------------
  # Windows PowerShell wrapper + .bat convenience launcher
  # ---------------------------------------------------------------------------
  cat >"$out_dir/run.ps1" <<'PS1'
Param(
  [Parameter(ValueFromRemainingArguments=$true)]
  [string[]]$Args
)

# Replicator dev/run wrapper
# - Creates a virtualenv if missing
# - Installs runtime deps (prefers requirements.txt if present)
# - Runs src/main.py

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RootDir = Resolve-Path $ScriptDir
Set-Location $RootDir

$VenvDir = "__VENV_DIR__"
$ReqFile = "__REQ_FILE__"

$Py = "python"
if (Get-Command "python3.11" -ErrorAction SilentlyContinue) { $Py = "python3.11" }
elseif (Get-Command "python" -ErrorAction SilentlyContinue) { $Py = "python" }
else { throw "Python not found in PATH" }

if (-not (Test-Path (Join-Path $VenvDir "Scripts\python.exe"))) {
  Write-Host "Creating virtualenv: $VenvDir"
  & $Py -m venv $VenvDir
}

& (Join-Path $VenvDir "Scripts\python.exe") -m pip install --upgrade pip wheel

if (Test-Path $ReqFile) {
  Write-Host "Installing requirements from $ReqFile"
  & (Join-Path $VenvDir "Scripts\python.exe") -m pip install -r $ReqFile
} else {
  Write-Host "Installing minimal runtime deps (PyQt5)"
  & (Join-Path $VenvDir "Scripts\python.exe") -m pip install "PyQt5>=5.15,<6"
}

& (Join-Path $VenvDir "Scripts\python.exe") "src\main.py" @Args
PS1

  # Patch placeholders (use sed for cross-platform simplicity)
  sed_inplace "s|__VENV_DIR__|$VENV_DIR|g" "$out_dir/run.ps1"
  sed_inplace "s|__REQ_FILE__|$REQ_FILE|g" "$out_dir/run.ps1"

  cat >"$out_dir/run.bat" <<'BAT'
@echo off
setlocal

REM Replicator launcher (Windows)
REM - Double-click (no args): starts hidden and exits immediately
REM - CLI usage (with args like --help): runs in the current console so you can see output

set "SCRIPT_DIR=%~dp0"

REM If the user provided args, run in the current console (do not hide), so output is visible.
if not "%~1"=="" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%run.ps1" %*
  endlocal
  exit /b %ERRORLEVEL%
)

REM No args: start hidden and exit right away.
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -WindowStyle Hidden -FilePath 'powershell' -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File', (Join-Path '%SCRIPT_DIR%' 'run.ps1'))"

endlocal
exit /b 0
BAT

  cat >"$out_dir/run-cli.bat" <<'BAT'
@echo off
setlocal

REM Replicator CLI wrapper (Windows)
REM Always runs in the current console and forwards all arguments.

set "SCRIPT_DIR=%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%run.ps1" %*

endlocal
exit /b %ERRORLEVEL%
BAT

  cat >"$out_dir/run.vbs" <<'VBS'
' Replicator Windows launcher (no console)
' Double-click this file to start the app without a prompt window.

Dim shell, scriptDir, ps1
Set shell = CreateObject("WScript.Shell")
scriptDir = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
ps1 = Chr(34) & scriptDir & "\\run.ps1" & Chr(34)

' 0 = hidden window
shell.Run "powershell -NoProfile -ExecutionPolicy Bypass -File " & ps1, 0, False
VBS
}

# -----------------------------------------------------------------------------
# Validate
# -----------------------------------------------------------------------------
[ -n "$PYTHON_BIN" ] || die "Python not found. Install Python 3.11+ and ensure it is available as python3.11 (macOS/Linux) or via the Windows Python Launcher: py -3.11"
[ -f "$ENTRY" ] || die "Entry not found: $ENTRY"

ARCH="$(uname -m)"
if [ "$OS" = "linux" ] && { [ "$ARCH" = "aarch64" ] || [ "$ARCH" = "armv7l" ] || [ "$ARCH" = "armhf" ]; }; then
  # Auto-enable system PyQt on ARM unless explicitly overridden by user (flag)
  # Keeping your prior behavior but behind the switch.
  :
fi

# Auto package choice
if [ -z "$PKG" ]; then
  if [ "$OS" = "macos" ]; then
    PKG="onedir"
  else
    PKG="onefile"
  fi
fi

# Auto icon discovery if not provided
if [ -z "$ICON_FILE" ]; then
  if [ "$OS" = "macos" ] && [ -f "src/icons/icon.icns" ]; then
    ICON_FILE="src/icons/icon.icns"
  elif [ "$OS" = "windows" ] && [ -f "src/icons/icon.ico" ]; then
    ICON_FILE="src/icons/icon.ico"
  elif [ -f "src/icons/icon.png" ]; then
    ICON_FILE="src/icons/icon.png"
  fi
fi

# Add default data folders if present (and not already provided)
for cand in "${DEFAULT_DATA_CANDIDATES[@]}"; do
  src="${cand%%:*}"
  if [ -e "$src" ]; then
    # Avoid duplicates
    dup=0
    for existing in "${ADD_DATA[@]+${ADD_DATA[@]}}"; do
      [ "$existing" = "$cand" ] && dup=1
    done
    [ "$dup" -eq 0 ] && ADD_DATA+=("$cand")
  fi
done

# -----------------------------------------------------------------------------
# Prepare output
# -----------------------------------------------------------------------------
FINAL_DIR="dist/$OS"

if [ "${CLEAN:-0}" -eq 1 ]; then
  log "Cleaning build artifacts..."
  rm -rf build dist *.spec || true
fi

mkdir -p "$FINAL_DIR"
generate_wrapper_scripts "."

#
# -----------------------------------------------------------------------------
# Create/verify venv (Python 3.11)
# -----------------------------------------------------------------------------
NEED_RECREATE=0
if [ ! -x "$(venv_python_path)" ]; then
  NEED_RECREATE=1
else
  VENV_VER="$("$(venv_python_path)" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' || echo unknown)"
  if [ "$VENV_VER" != "3.11" ]; then
    NEED_RECREATE=1
  fi
fi

if [ "$NEED_RECREATE" -eq 1 ]; then
  log "Creating fresh Python 3.11 virtual environment in $VENV_DIR..."
  rm -rf "$VENV_DIR"
  if [ "$USE_SYSTEM_PYQT" -eq 1 ]; then
    python_exec -m venv --system-site-packages "$VENV_DIR"
  else
    python_exec -m venv "$VENV_DIR"
  fi
fi

# shellcheck disable=SC1091
source "$(venv_activate_path)"

ACTIVE_VER="$(python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
[ "$ACTIVE_VER" = "3.11" ] || die "Active Python is $ACTIVE_VER, expected 3.11. On Windows, install Python 3.11 and/or use: py -3.11"
log "Using Python $(python -V)"

log "Updating pip..."
python -m pip install --upgrade pip wheel

log "Installing build dependencies..."
python -m pip install "pyinstaller>=6.9,<7" "sip>=6.9,<7"

# Optional: system PyQt5 on Linux ARM (APT)
if [ "$USE_SYSTEM_PYQT" -eq 1 ]; then
  if [ "$OS" != "linux" ]; then
    die "--system-pyqt is only supported on Linux"
  fi
  if command -v apt-get >/dev/null 2>&1; then
    log "Installing system PyQt5 via APT (requires sudo)..."
    sudo apt-get update
    sudo apt-get install -y python3-pyqt5 python3-pyqt5.qtsvg
  else
    die "APT not found; cannot install system PyQt5. Install PyQt5 manually or omit --system-pyqt."
  fi
else
  # Default: wheels
  # (Only install PyQt5 if the project uses it; safe to install regardless.)
  python -m pip install "PyQt5>=5.15,<6" || true
fi

# -----------------------------------------------------------------------------
# Build
# -----------------------------------------------------------------------------
log "Building $APP_NAME for $OS ($PKG, $MODE) from entry: $ENTRY"

PYI_ARGS=(
  --name "$APP_NAME"
  --noconfirm
)

# Optional PyInstaller debug
if [ "${PYI_DEBUG:-0}" -eq 1 ]; then
  PYI_ARGS+=(--log-level=DEBUG --debug=all)
fi

# Packaging
if [ "$PKG" = "onefile" ]; then
  PYI_ARGS+=(--onefile)
else
  PYI_ARGS+=(--onedir)
fi

# Window mode
if [ "$MODE" = "windowed" ]; then
  PYI_ARGS+=(--windowed)
fi

# Icon
if [ -n "$ICON_FILE" ] && [ -f "$ICON_FILE" ]; then
  PYI_ARGS+=(--icon "$ICON_FILE")
fi

# Hidden imports
# NOTE:
# - PyQt5 typically exposes SIP bindings as `PyQt5.sip` (and ships `PyQt5_sip` as a wheel).
# - Users sometimes add `sip` as a hidden import; that module often does not exist and causes warnings.
# We normalize `sip` -> `PyQt5.sip` when available, and skip hidden imports that cannot be resolved.
can_import() {
  # usage: can_import module.name
  python - <<PY >/dev/null 2>&1
import importlib.util
spec = importlib.util.find_spec("$1")
raise SystemExit(0 if spec is not None else 1)
PY
}

for hi in "${HIDDEN_IMPORTS[@]+${HIDDEN_IMPORTS[@]}}"; do
  [ -n "$hi" ] || continue

  # Normalize common SIP hidden import for PyQt5
  if [ "$hi" = "sip" ]; then
    if can_import "sip"; then
      :
    elif can_import "PyQt5.sip"; then
      log "Normalizing hidden import: sip -> PyQt5.sip"
      hi="PyQt5.sip"
    fi
  fi

  if can_import "$hi"; then
    PYI_ARGS+=(--hidden-import "$hi")
  else
    log "WARN: Skipping hidden import not found: $hi"
  fi
done

# Data
# PyInstaller (on some versions) requires the equals form: --add-data=SRC:DST
for ad in "${ADD_DATA[@]+${ADD_DATA[@]}}"; do
  # Allow config/CLI to provide either:
  #   - SRC:DST
  #   - --add-data=SRC:DST
  #   - --add-data SRC:DST  (accidentally split by callers)
  [ -n "$ad" ] || continue
  if [ "$ad" = "--add-data" ]; then
    # Skip stray flag (the next token would have been the value)
    continue
  fi

  if [[ "$ad" == --add-data=* ]]; then
    PYI_ARGS+=("$ad")
  else
    PYI_ARGS+=("--add-data=$ad")
  fi

done

# Extra sanity checks (don’t fail builds for optional modules)
log "Running a quick import sanity check (best-effort)..."
python - <<'PY' || true
import sys
print("Python:", sys.version)
try:
    import PyInstaller  # noqa
    print("PyInstaller OK")
except Exception as e:
    print("PyInstaller import failed:", e)
PY

# Print PyInstaller command for debug
log "PyInstaller command:"
printf '  %q ' pyinstaller "${PYI_ARGS[@]}" "$ENTRY"
printf '\n'

# Execute PyInstaller
pyinstaller "${PYI_ARGS[@]}" "$ENTRY"

# -----------------------------------------------------------------------------
# Collect output
# -----------------------------------------------------------------------------
if [ "$OS" = "macos" ]; then
  # If entry builds a .app bundle, it'll appear under dist/<name>.app for onedir+windowed.
  if [ -d "dist/$APP_NAME.app" ]; then
    log "Moving .app to $FINAL_DIR/"
    rm -rf "$FINAL_DIR/$APP_NAME.app" || true
    mv "dist/$APP_NAME.app" "$FINAL_DIR/"

    if [ "$MAKE_DMG" -eq 1 ]; then
      log "Creating DMG..."
      DMG_NAME="$FINAL_DIR/$APP_NAME.dmg"
      hdiutil create "$DMG_NAME" -volname "$APP_NAME" -srcfolder "$FINAL_DIR/$APP_NAME.app" -ov -format UDZO
      log "DMG created at $DMG_NAME"
    fi
  else
    # Non-windowed or non-app outputs (e.g., console onedir)
    log "Moving build output to $FINAL_DIR/"
    rm -rf "$FINAL_DIR/$APP_NAME" || true
    mv "dist/$APP_NAME" "$FINAL_DIR/"
  fi
else
  log "Moving build output to $FINAL_DIR/"
  rm -rf "$FINAL_DIR/$APP_NAME" || true
  mv "dist/$APP_NAME" "$FINAL_DIR/"
fi

log "Build completed successfully. Output: $FINAL_DIR"

deactivate

# -----------------------------------------------------------------------------
# Example build.cfg
# -----------------------------------------------------------------------------
# NAME=Replicator
# ENTRY=src/main.py
# MODE=console            # or windowed
# PKG=auto                # auto|onefile|onedir
# ICON=src/icons/icon.icns
# PYTHON=python3.11
# SYSTEM_PYQT=0           # 1 on Linux ARM if you want APT PyQt5
# DMG=0                   # 1 on macOS to create a DMG
# CLEAN=1
# ADD_DATA=src/app:app;src/styles:styles;src/icons:icons
# HIDDEN_IMPORTS=PyQt5.QtSvg;some_package.some_module
# GEN_INSTALL=1
# VENV_DIR=.venv
# REQUIREMENTS=requirements.txt
