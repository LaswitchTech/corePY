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

# -----------------------------------------------------------------------------
# corePY vendored bin sync
# Some projects vendor corePY under src/core/src and keep binaries there.
# To make PyInstaller data inclusion consistent, mirror vendored binaries into
# src/bin (overwriting existing content).
# -----------------------------------------------------------------------------

sync_vendored_bins() {
  local src_dir="src/core/src/bin"
  local dst_dir="src/bin"

  [ -d "$src_dir" ] || return 0

  log "Syncing vendored binaries: $src_dir -> $dst_dir"
  mkdir -p "$dst_dir"

  # Use cp -R to stay portable across macOS/Linux/Windows Git Bash.
  # Trailing '/.' copies contents (including hidden files) into destination.
  cp -R "$src_dir/." "$dst_dir/" 2>/dev/null || cp -R "$src_dir"/* "$dst_dir/" 2>/dev/null || true
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
# Python discovery
# - macOS/Linux: prefer python3.11
# - Windows (Git Bash): prefer the Python Launcher when available, but only if the requested runtime exists.
PYTHON_BIN=""
PYTHON_LAUNCH_ARGS=""

python_cmd_ok() {
  # usage: python_cmd_ok <bin> [args...]
  # returns 0 if command runs and Python version is >= 3.11
  "$@" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' >/tmp/.replicator_pyver 2>/dev/null || return 1
  local ver
  ver="$(cat /tmp/.replicator_pyver 2>/dev/null || true)"
  rm -f /tmp/.replicator_pyver 2>/dev/null || true
  case "$ver" in
    3.11|3.12|3.13|3.14|3.15|3.16|3.17|3.18|3.19|4.*) return 0 ;;
    *) return 1 ;;
  esac
}

# Candidate selection (ordered)
if command -v python3.11 >/dev/null 2>&1 && python_cmd_ok python3.11; then
  PYTHON_BIN="python3.11"
  PYTHON_LAUNCH_ARGS=""
elif [ "$OS" = "windows" ] && command -v py >/dev/null 2>&1; then
  # Try explicit 3.11 first, then any 3.x that satisfies >=3.11
  if python_cmd_ok py -3.11; then
    PYTHON_BIN="py"
    PYTHON_LAUNCH_ARGS="-3.11"
  elif python_cmd_ok py -3.12; then
    PYTHON_BIN="py"
    PYTHON_LAUNCH_ARGS="-3.12"
  elif python_cmd_ok py -3.13; then
    PYTHON_BIN="py"
    PYTHON_LAUNCH_ARGS="-3.13"
  elif python_cmd_ok py -3; then
    PYTHON_BIN="py"
    PYTHON_LAUNCH_ARGS="-3"
  fi
fi

# Fallbacks (Git Bash often exposes only `python`)
if [ -z "$PYTHON_BIN" ]; then
  if command -v python3 >/dev/null 2>&1 && python_cmd_ok python3; then
    PYTHON_BIN="python3"
    PYTHON_LAUNCH_ARGS=""
  elif command -v python >/dev/null 2>&1 && python_cmd_ok python; then
    PYTHON_BIN="python"
    PYTHON_LAUNCH_ARGS=""
  fi
fi

# If user supplied --python, we still validate it later in the script.

python_exec() {
  # Wrapper so we can call: python_exec -m venv ...
  [ -n "${PYTHON_BIN:-}" ] || die "Python not found. Install Python 3.11+ and ensure it is in PATH (python) or via the Windows Python Launcher (py)."

  if [ -n "${PYTHON_LAUNCH_ARGS:-}" ]; then
    # shellcheck disable=SC2086
    "$PYTHON_BIN" ${PYTHON_LAUNCH_ARGS} "$@"
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
sync_vendored_bins
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
  # POSIX shell wrapper (macOS/Linux + Windows Git Bash)
  # ---------------------------------------------------------------------------
  cat >"$out_dir/launch.sh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail

# Replicator dev launcher (macOS/Linux + Windows Git Bash)
# - Creates a virtualenv if missing
# - Installs runtime deps (prefers requirements.txt if present)
# - Runs src/main.py

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

VENV_DIR="__VENV_DIR__"
REQ_FILE="__REQ_FILE__"

# Detect Windows Git Bash (MSYS/MINGW/CYGWIN)
UNAME_S="$(uname -s 2>/dev/null || echo '')"
IS_WINDOWS=0
case "$UNAME_S" in
  MINGW*|MSYS*|CYGWIN*) IS_WINDOWS=1 ;;
  *) IS_WINDOWS=0 ;;
esac

# Choose python command
PY_CMD=""
PY_ARGS=()

# Prefer explicit python3.11 on macOS/Linux
if command -v python3.11 >/dev/null 2>&1; then
  PY_CMD="python3.11"
elif [ "$IS_WINDOWS" -eq 1 ] && command -v py >/dev/null 2>&1; then
  # Prefer Windows Python Launcher (does NOT require python.exe in PATH)
  PY_CMD="py"
  PY_ARGS=(-3.11)
elif command -v python3 >/dev/null 2>&1; then
  PY_CMD="python3"
elif command -v python >/dev/null 2>&1; then
  PY_CMD="python"
else
  echo "ERROR: Python not found in PATH. On Windows, install Python 3.11+ and/or ensure the Python Launcher (py) is available." >&2
  exit 1
fi

# Resolve venv python/activate paths
if [ "$IS_WINDOWS" -eq 1 ]; then
  VENV_PY="$VENV_DIR/Scripts/python.exe"
  ACTIVATE_SH="$VENV_DIR/Scripts/activate"
else
  VENV_PY="$VENV_DIR/bin/python"
  ACTIVATE_SH="$VENV_DIR/bin/activate"
fi

# Create venv if needed
if [ ! -x "$VENV_PY" ]; then
  echo "Creating virtualenv: $VENV_DIR"
  # shellcheck disable=SC2086
  "$PY_CMD" "${PY_ARGS[@]}" -m venv "$VENV_DIR"
fi

# Activate venv
# shellcheck disable=SC1090
source "$ACTIVATE_SH"

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
  sed_inplace "s|__VENV_DIR__|$VENV_DIR|g" "$out_dir/launch.sh"
  sed_inplace "s|__REQ_FILE__|$REQ_FILE|g" "$out_dir/launch.sh"
  chmod +x "$out_dir/launch.sh" || true

  # CLI convenience (same as launch.sh but kept as a stable name for docs/scripts)
  cat >"$out_dir/cli.sh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

exec "./launch.sh" "$@"
SH
  chmod +x "$out_dir/cli.sh" || true

  # ---------------------------------------------------------------------------
  # Windows: VBS launcher (launch.vbs) and CLI .bat launcher (cli.bat)
  # ---------------------------------------------------------------------------
  cat >"$out_dir/launch.vbs" <<'VBS'
' Replicator Windows launcher (no console)
' - Prefers launching the built binary if present
' - Falls back to launching Git Bash + launch.sh if available

Option Explicit

Dim shell, fso, scriptDir, exePath, bashPath, cmd
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)

' Try built EXE first
exePath = scriptDir & "\\dist\\windows\\Replicator.exe"
If fso.FileExists(exePath) Then
  shell.CurrentDirectory = scriptDir
  shell.Run Chr(34) & exePath & Chr(34), 1, False
  WScript.Quit 0
End If

' Fallback: Git Bash launch.sh (if user is in a dev checkout)
bashPath = shell.ExpandEnvironmentStrings("%ProgramFiles%") & "\\Git\\bin\\bash.exe"
If fso.FileExists(bashPath) Then
  cmd = Chr(34) & bashPath & Chr(34) & " -lc " & Chr(34) & "cd \"" & scriptDir & "\" && ./launch.sh" & Chr(34)
  shell.Run cmd, 0, False
End If
VBS

  cat >"$out_dir/cli.bat" <<'BAT'
@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM ---------------------------------------------------------------------------
REM Replicator CLI launcher (Windows)
REM - Ensures a local venv exists (.venv)
REM - Installs minimal runtime deps (PyQt5)
REM - Runs src\main.py with a visible console
REM
REM Note: If you double-click this file, the console may flash and close.
REM       Run it from an existing Command Prompt for persistent output.
REM ---------------------------------------------------------------------------

set "SCRIPT_DIR=%~dp0"
set "VENV_DIR=%SCRIPT_DIR%.venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"

REM Prefer Windows Python Launcher
set "PY_CMD="
set "PY_ARGS="
where py >nul 2>&1
if %ERRORLEVEL%==0 (
  set "PY_CMD=py"
  set "PY_ARGS=-3.11"
) else (
  where python >nul 2>&1
  if %ERRORLEVEL%==0 (
    set "PY_CMD=python"
    set "PY_ARGS="
  )
)

if "%PY_CMD%"=="" (
  echo ERROR: Python not found. Install Python 3.11+ and ensure either `py` or `python` is available in PATH.
  exit /b 1
)

REM Create venv if missing
if not exist "%VENV_PY%" (
  echo Creating virtualenv: %VENV_DIR%
  %PY_CMD% %PY_ARGS% -m venv "%VENV_DIR%"
  if %ERRORLEVEL% NEQ 0 (
    echo ERROR: Failed to create virtualenv.
    exit /b %ERRORLEVEL%
  )
)

REM Upgrade pip/wheel
"%VENV_PY%" -m pip install --upgrade pip wheel >nul

REM Install minimal deps (idempotent)
echo Installing minimal runtime deps (PyQt5)...
"%VENV_PY%" -m pip install "PyQt5>=5.15,<6" >nul

REM Prefer the built console companion if available
if exist "%SCRIPT_DIR%dist\windows\Replicator-cli.exe" (
  "%SCRIPT_DIR%dist\windows\Replicator-cli.exe" %*
  endlocal
  exit /b %ERRORLEVEL%
)

REM Fallback: Run the source entry in console mode (dev checkout)
if not exist "%SCRIPT_DIR%src\main.py" (
  echo ERROR: Entry not found: %SCRIPT_DIR%src\main.py
  echo NOTE: Replicator-cli.exe not found at %SCRIPT_DIR%dist\windows\Replicator-cli.exe
  exit /b 1
)

"%VENV_PY%" "%SCRIPT_DIR%src\main.py" %*
endlocal
exit /b %ERRORLEVEL%
BAT
}

# -----------------------------------------------------------------------------
# Validate
# -----------------------------------------------------------------------------
[ -n "$PYTHON_BIN" ] || die "Python not found. Install Python 3.11+ and ensure it is available as python (Windows/Git Bash) or via the Windows Python Launcher (py), or as python3.11 (macOS/Linux)."
[ -f "$ENTRY" ] || die "Entry not found: $ENTRY"

# Validate python runtime is actually runnable and >= 3.11
if ! python_exec -c "import sys; assert (sys.version_info.major, sys.version_info.minor) >= (3, 11), sys.version" >/dev/null 2>&1; then
  if [ "$OS" = "windows" ] && [ "$PYTHON_BIN" = "py" ]; then
    die "Python launcher found, but no suitable Python 3.11+ runtime is installed. Install Python 3.11+ (check 'Add python.exe to PATH') or run: py --list to verify installed versions."
  fi
  die "Python is not runnable or is < 3.11. Install Python 3.11+ and try again."
fi

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
python -m pip install "pyinstaller>=6.9,<7"

# PyInstaller on Windows requires .ico (or Pillow for automatic conversion from .png).
# Install Pillow when building on Windows so we can safely use .png icons or generate .ico.
if [ "$OS" = "windows" ]; then
  python -m pip install "pillow>=10,<12" || true
fi


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

# Ensure SIP bindings are bundled for PyQt5.
# Note: the importable module is typically `PyQt5.sip` (not `sip`).
# PyInstaller may still emit a non-fatal warning about hidden import "sip" depending on hooks.
has_pyqt5=0
if python -c "import importlib.util; import sys; sys.exit(0 if importlib.util.find_spec('PyQt5') else 1)" >/dev/null 2>&1; then
  has_pyqt5=1
fi

if [ "$has_pyqt5" -eq 1 ]; then
  # Add PyQt5.sip unless the user already provided it.
  found=0
  for h in "${HIDDEN_IMPORTS[@]+${HIDDEN_IMPORTS[@]}}"; do
    [ "$h" = "PyQt5.sip" ] && found=1
  done
  if [ "$found" -eq 0 ]; then
    HIDDEN_IMPORTS+=("PyQt5.sip")
  fi

  # If the user ever provided `sip`, normalize it early.
  for i in "${!HIDDEN_IMPORTS[@]}"; do
    if [ "${HIDDEN_IMPORTS[$i]}" = "sip" ]; then
      HIDDEN_IMPORTS[$i]="PyQt5.sip"
    fi
  done
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
  if [ "$OS" = "windows" ]; then
    ext="${ICON_FILE##*.}"
    ext="$(echo "$ext" | tr '[:upper:]' '[:lower:]')"

    # On Windows, PyInstaller only accepts .ico/.exe as icon inputs unless Pillow is installed.
    # If the provided icon is .png, convert it to a temporary .ico using Pillow.
    if [ "$ext" = "png" ]; then
      mkdir -p build
      ICO_FROM_PNG="build/icon_from_png.ico"
      python - <<'PY' "$ICON_FILE" "$ICO_FROM_PNG" || true
import sys
from pathlib import Path

src = Path(sys.argv[1])
dst = Path(sys.argv[2])

try:
    from PIL import Image
except Exception as e:
    raise SystemExit(1)

img = Image.open(src).convert("RGBA")
# Common Windows icon sizes
sizes = [(16,16), (24,24), (32,32), (48,48), (64,64), (128,128), (256,256)]
img.save(dst, format="ICO", sizes=sizes)
print(dst)
PY
      if [ -f "$ICO_FROM_PNG" ]; then
        PYI_ARGS+=(--icon "$ICO_FROM_PNG")
      else
        log "WARN: Failed to convert PNG icon to ICO; continuing without --icon"
      fi
    else
      # .ico/.exe already
      PYI_ARGS+=(--icon "$ICON_FILE")
    fi
  else
    PYI_ARGS+=(--icon "$ICON_FILE")
  fi
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

  # Normalize common SIP hidden import for PyQt5.
  # The standalone `sip` module is often NOT importable even if the sip build tooling is installed.
  if [ "$hi" = "sip" ]; then
    if can_import "PyQt5.sip"; then
      log "Normalizing hidden import: sip -> PyQt5.sip"
      hi="PyQt5.sip"
    else
      log "WARN: Skipping hidden import not found: sip (and PyQt5.sip not available)"
      continue
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

# ---------------------------------------------------------------------------
# Windows: also build a console-subsystem companion EXE for CLI usage.
# - Keeps the main EXE as --windowed (nice for double-click)
# - Produces <APP_NAME>-cli.exe without --windowed so stdout/stderr work in terminals
# ---------------------------------------------------------------------------
if [ "$OS" = "windows" ] && [ "$MODE" = "windowed" ]; then
  CLI_APP_NAME="${APP_NAME}-cli"

  log "Building $CLI_APP_NAME for $OS ($PKG, console) from entry: $ENTRY"

  # Copy args and adjust for console build
  PYI_CLI_ARGS=("${PYI_ARGS[@]}")

  # Replace --name value
  for i in "${!PYI_CLI_ARGS[@]}"; do
    if [ "${PYI_CLI_ARGS[$i]}" = "--name" ]; then
      PYI_CLI_ARGS[$((i+1))]="$CLI_APP_NAME"
    fi
  done

  # Remove --windowed if present
  PYI_CLI_ARGS_STRIPPED=()
  for a in "${PYI_CLI_ARGS[@]}"; do
    if [ "$a" = "--windowed" ]; then
      continue
    fi
    PYI_CLI_ARGS_STRIPPED+=("$a")
  done

  log "PyInstaller command (CLI companion):"
  printf '  %q ' pyinstaller "${PYI_CLI_ARGS_STRIPPED[@]}" "$ENTRY"
  printf '\n'

  pyinstaller "${PYI_CLI_ARGS_STRIPPED[@]}" "$ENTRY"
fi

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

  # Windows onefile creates dist/<name>.exe; onedir creates dist/<name>/
  # Move all matching outputs for this app (and its -cli companion).
  if [ "$OS" = "windows" ]; then
    # Clean old outputs
    rm -f "$FINAL_DIR/${APP_NAME}.exe" "$FINAL_DIR/${APP_NAME}-cli.exe" || true
    rm -rf "$FINAL_DIR/$APP_NAME" "$FINAL_DIR/${APP_NAME}-cli" || true

    # Prefer moving EXEs if they exist
    if [ -f "dist/${APP_NAME}.exe" ]; then
      mv "dist/${APP_NAME}.exe" "$FINAL_DIR/"
    elif [ -f "dist/$APP_NAME" ]; then
      mv "dist/$APP_NAME" "$FINAL_DIR/" || true
    fi

    if [ -f "dist/${APP_NAME}-cli.exe" ]; then
      mv "dist/${APP_NAME}-cli.exe" "$FINAL_DIR/"
    elif [ -f "dist/${APP_NAME}-cli" ]; then
      mv "dist/${APP_NAME}-cli" "$FINAL_DIR/" || true
    fi

    # If onedir outputs were produced, move them too
    if [ -d "dist/$APP_NAME" ]; then
      rm -rf "$FINAL_DIR/$APP_NAME" || true
      mv "dist/$APP_NAME" "$FINAL_DIR/"
    fi
    if [ -d "dist/${APP_NAME}-cli" ]; then
      rm -rf "$FINAL_DIR/${APP_NAME}-cli" || true
      mv "dist/${APP_NAME}-cli" "$FINAL_DIR/"
    fi

  else
    rm -rf "$FINAL_DIR/$APP_NAME" || true
    mv "dist/$APP_NAME" "$FINAL_DIR/"
  fi
fi

log "Build completed successfully. Output: $FINAL_DIR"

# ---------------------------------------------------------------------------
# Windows: create a .lnk shortcut (best-effort)
# ---------------------------------------------------------------------------
if [ "$OS" = "windows" ]; then
  EXE_PATH="$(pwd)/$FINAL_DIR/$APP_NAME.exe"
  LNK_PATH="$(pwd)/$FINAL_DIR/$APP_NAME.lnk"

  # Create a shortcut inside dist/windows (so we can ship it alongside the exe)
  if [ -f "$EXE_PATH" ]; then
    mkdir -p "$FINAL_DIR" || true
    cat >"build/make_shortcut.vbs" <<'VBS'
Option Explicit
Dim shell, args, exePath, lnkPath
Set shell = CreateObject("WScript.Shell")
Set args = WScript.Arguments
exePath = args.Item(0)
lnkPath = args.Item(1)
Dim sc
Set sc = shell.CreateShortcut(lnkPath)
sc.TargetPath = exePath
sc.WorkingDirectory = CreateObject("Scripting.FileSystemObject").GetParentFolderName(exePath)
sc.WindowStyle = 1
sc.Description = "Replicator"
sc.Save
VBS

    if command -v cscript >/dev/null 2>&1; then
      cscript //nologo "build/make_shortcut.vbs" "$(cygpath -w "$EXE_PATH" 2>/dev/null || echo "$EXE_PATH")" "$(cygpath -w "$LNK_PATH" 2>/dev/null || echo "$LNK_PATH")" >/dev/null 2>&1 || true
    fi

    # Fallback: create a .url shortcut (always works)
    if [ ! -f "$LNK_PATH" ]; then
      cat >"$FINAL_DIR/$APP_NAME.url" <<URL
[InternetShortcut]
URL=file:///${EXE_PATH}
IconFile=${EXE_PATH}
IconIndex=0
URL
    fi

    if [ -f "$LNK_PATH" ]; then
      log "Windows shortcut created: $LNK_PATH"
    elif [ -f "$FINAL_DIR/$APP_NAME.url" ]; then
      log "Windows shortcut created: $FINAL_DIR/$APP_NAME.url"
    else
      log "WARN: Could not create Windows shortcut (cscript not available)"
    fi
  fi

  # Optional: create a shortcut for the CLI companion
  CLI_EXE_PATH="$(pwd)/$FINAL_DIR/${APP_NAME}-cli.exe"
  CLI_LNK_PATH="$(pwd)/$FINAL_DIR/${APP_NAME}-cli.lnk"
  if [ -f "$CLI_EXE_PATH" ]; then
    if command -v cscript >/dev/null 2>&1; then
      cscript //nologo "build/make_shortcut.vbs" "$(cygpath -w "$CLI_EXE_PATH" 2>/dev/null || echo "$CLI_EXE_PATH")" "$(cygpath -w "$CLI_LNK_PATH" 2>/dev/null || echo "$CLI_LNK_PATH")" >/dev/null 2>&1 || true
    fi
    if [ ! -f "$CLI_LNK_PATH" ]; then
      cat >"$FINAL_DIR/${APP_NAME}-cli.url" <<URL
[InternetShortcut]
URL=file:///${CLI_EXE_PATH}
IconFile=${CLI_EXE_PATH}
IconIndex=0
URL
    fi
  fi
fi

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
