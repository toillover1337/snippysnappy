#!/bin/bash
#
# snippysnappy installer
#
# Must be run with sudo (package installation needs root), but everything
# under your home directory is done AS YOU, not as root - via
# `sudo -u "$SUDO_USER"`. Running the whole thing as root would create
# root-owned files under your home directory and break things later.
#
# Usage: sudo -E ./install.sh
#   -E preserves your environment (DISPLAY/WAYLAND_DISPLAY) so session-type
#   auto-detection has a chance of working. Without -E, sudo may reset your
#   environment and the script will fall back to asking you directly.
#
# Deliberately does NOT use `set -e`. Failures are handled per-step: each
# significant action is reported as it happens, recorded for the final
# summary, and on failure you're asked whether to continue with the rest of
# the install or stop - rather than the whole script silently dying on the
# first error, or silently continuing past a real problem.
set -uo pipefail

INSTALLER_VERSION="0.10.0" # X.0.0 = stable release; 0.X.0 = feature addition/major tweak; 0.0.X = bug fix/small tweak.
# Tracked separately from snippysnappy.py's own version - the two only share parity on the leading X (stable-release) digit.

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

STEP_LOG=() # "OK: description" / "FAILED: description" / "SKIPPED: description"
LOG_FILE="" # set once SNIPPYSNAPPY_DIR is created - see the directory-creation block below

log_to_file() {
    # Silently no-ops until LOG_FILE is set (the brief window before we know
    # which user/directory to write to - just the root/SUDO_USER checks).
    # Shares the exact same file as snippysnappy.py's own LOG_PATH, in the
    # same "HH:MM:SS [LEVEL] message" format, so the installer's actions and
    # the program's own runtime log read as one continuous timeline.
    [[ -n "$LOG_FILE" ]] || return 0
    printf '%s [%s] %s\n' "$(date '+%H:%M:%S')" "$1" "$2" >>"$LOG_FILE" 2>/dev/null
}

log() {
    printf '\033[1;34m==>\033[0m %s\n' "$1"
    log_to_file "INFO" "$1"
}
warn() {
    printf '\033[1;33mWARNING:\033[0m %s\n' "$1" >&2
    log_to_file "WARNING" "$1"
}
err() {
    printf '\033[1;31mERROR:\033[0m %s\n' "$1" >&2
    log_to_file "ERROR" "$1"
}
fail_hard() {
    err "$1"
    print_summary
    exit 1
} # for setup problems with no sensible way to continue

record_step() { STEP_LOG+=("$1: $2"); }

# Yes-by-default: for progressing through ordinary, non-destructive choices.
confirm() {
    local reply
    read -r -p "$1 [Y/n] " reply || true
    [[ -z "$reply" || "$reply" =~ ^[Yy] ]]
}

# No-by-default: for anything that could overwrite or otherwise destroy
# something that already exists.
confirm_destructive() {
    local reply
    read -r -p "$1 [y/N] " reply || true
    [[ "$reply" =~ ^[Yy] ]]
}

# Runs a step; reports and records outcome either way. On failure, asks
# whether to continue with the rest of the install (returns 1, caller
# decides what "continuing" means for that step) or stop entirely (prints
# the summary so far and exits).
run_step() {
    local desc="$1"
    shift
    "$@"
    local rc=$?
    if [[ $rc -eq 0 ]]; then
        record_step "OK" "$desc"
        return 0
    fi
    err "$desc failed (exit code $rc)."
    record_step "FAILED" "$desc"
    if confirm "Continue with the rest of the install anyway?"; then
        return 1
    else
        fail_hard "Stopped after: $desc"
    fi
}

print_summary() {
    echo
    log "Summary of steps attempted:"
    for s in "${STEP_LOG[@]}"; do
        echo "  - $s"
        log_to_file "INFO" "  - $s"
    done
}

# ---------------------------------------------------------------------------
# Root check + figuring out who "you" actually are
# ---------------------------------------------------------------------------

log "snippysnappy installer v${INSTALLER_VERSION}"

if [[ $EUID -ne 0 ]]; then
    fail_hard "This script must be run with sudo (package installation requires root). Try: sudo -E $0"
fi

if [[ -z "${SUDO_USER:-}" || "$SUDO_USER" == "root" ]]; then
    fail_hard "Could not determine which user to install for (run via 'sudo -E ./install.sh' from your normal user account, not a root login)."
fi

TARGET_USER="$SUDO_USER"
TARGET_HOME=$(getent passwd "$TARGET_USER" | cut -d: -f6)
[[ -n "$TARGET_HOME" && -d "$TARGET_HOME" ]] || fail_hard "Could not resolve a home directory for user '$TARGET_USER'."

SNIPPYSNAPPY_DIR="$TARGET_HOME/.local/bin/snippysnappy.d"
SNIPPYSNAPPY_PY="$SNIPPYSNAPPY_DIR/snippysnappy.py"
WRAPPER_PATH="$TARGET_HOME/.local/bin/snippysnappy"

log "Installing for user: $TARGET_USER (home: $TARGET_HOME)"

run_as_user() {
    # Runs a command as the ORIGINAL invoking user, not root - for anything
    # that touches $TARGET_HOME. -H sets HOME correctly inside the subshell.
    sudo -u "$TARGET_USER" -H bash -c "$1"
}

# ---------------------------------------------------------------------------
# Directories created FIRST, before anything else - specifically so
# SNIPPYSNAPPY_DIR (and therefore LOG_FILE) exists as early as possible.
# Without this, package manager detection, session-type detection, and the
# entire dependency-installation phase (the most consequential, most
# error-prone part) would all happen before a log file existed to capture
# them in - only the final file-writing steps would get logged.
# ---------------------------------------------------------------------------

ensure_user_dir() {
    local dir="$1"
    if run_as_user "[ -d '$dir' ]"; then
        log "$dir already exists - skipping."
        return 0
    fi
    run_as_user "mkdir -p '$dir'"
}

run_step "Create $SNIPPYSNAPPY_DIR" ensure_user_dir "$SNIPPYSNAPPY_DIR"

LOG_FILE="$SNIPPYSNAPPY_DIR/snippysnappy.log" # same path snippysnappy.py's own LOG_PATH resolves to
run_as_user "touch '$LOG_FILE'"
log_to_file "INFO" "===== snippysnappy installer starting ====="

run_step "Create $SNIPPYSNAPPY_DIR/sniptext" ensure_user_dir "$SNIPPYSNAPPY_DIR/sniptext"
run_step "Create $TARGET_HOME/Pictures/Screenshots" ensure_user_dir "$TARGET_HOME/Pictures/Screenshots"
run_step "Create $TARGET_HOME/Pictures/Slop-o-graphs" ensure_user_dir "$TARGET_HOME/Pictures/Slop-o-graphs"

# ---------------------------------------------------------------------------
# Package manager detection
# ---------------------------------------------------------------------------

detect_pkg_manager() {
    if command -v emerge &>/dev/null; then
        echo "portage"
    elif command -v apt-get &>/dev/null; then
        echo "apt"
    elif command -v dnf &>/dev/null; then
        echo "dnf"
    elif command -v pacman &>/dev/null; then
        echo "pacman"
    elif command -v zypper &>/dev/null; then
        echo "zypper"
    else
        echo "unknown"
    fi
}

PKG_MANAGER=$(detect_pkg_manager)
log "Detected package manager: $PKG_MANAGER"
if [[ "$PKG_MANAGER" == "unknown" ]]; then
    fail_hard "No supported package manager found (emerge/apt-get/dnf/pacman/zypper). Install dependencies manually and adapt this script."
fi

# ---------------------------------------------------------------------------
# Session type detection (X11 vs Wayland vs both)
# ---------------------------------------------------------------------------

detect_session_type() {
    # Checked as the TARGET USER's session where possible, since we're
    # running as root and root's own env vars aren't the relevant ones.
    local wl x11
    wl=$(run_as_user 'echo -n "${WAYLAND_DISPLAY:-}"')
    x11=$(run_as_user 'echo -n "${DISPLAY:-}"')
    if [[ -n "$wl" && -n "$x11" ]]; then
        echo "both"
    elif [[ -n "$wl" ]]; then
        echo "wayland"
    elif [[ -n "$x11" ]]; then
        echo "x11"
    else
        echo "unknown"
    fi
}

DETECTED_SESSION=$(detect_session_type)
log "Auto-detected session type: $DETECTED_SESSION"
echo "Which backend(s) should be installed?"
select choice in "X11 only" "Wayland only" "Both" "Use detected ($DETECTED_SESSION)"; do
    case $choice in
    "X11 only")
        SESSION_TYPE="x11"
        break
        ;;
    "Wayland only")
        SESSION_TYPE="wayland"
        break
        ;;
    "Both")
        SESSION_TYPE="both"
        break
        ;;
    "Use detected ($DETECTED_SESSION)")
        [[ "$DETECTED_SESSION" == "unknown" ]] && {
            warn "Nothing was actually detected - pick a real option."
            continue
        }
        SESSION_TYPE="$DETECTED_SESSION"
        break
        ;;
    *) echo "Please choose 1-4." ;;
    esac
done
log "Installing for: $SESSION_TYPE"

# ---------------------------------------------------------------------------
# Dependency installation - each path checks what's ALREADY present/correct
# first, and only asks to install/rebuild what's actually missing.
# ---------------------------------------------------------------------------

install_gentoo() {
    local pkgs=("app-text/tesseract")
    if [[ "$SESSION_TYPE" == "x11" || "$SESSION_TYPE" == "both" ]]; then
        pkgs+=("media-gfx/maim" "x11-misc/xclip" "x11-misc/xdotool" "x11-apps/xrandr")
    fi
    if [[ "$SESSION_TYPE" == "wayland" || "$SESSION_TYPE" == "both" ]]; then
        pkgs+=("gui-apps/grim" "gui-apps/slurp" "gui-apps/wl-clipboard")
        if emerge -s wlr-randr 2>/dev/null | grep -q "gui-apps/wlr-randr"; then
            pkgs+=("gui-apps/wlr-randr")
        else
            warn "gui-apps/wlr-randr isn't in your configured tree (it wasn't in the main Gentoo tree as of this project - you may need the GURU overlay). Skipping it - Wayland monitor detection will fall back to treating the display as one monitor until it's available."
            record_step "SKIPPED" "gui-apps/wlr-randr (not in tree)"
        fi
    fi

    # dev-lang/python's tk USE flag is REQUIRED (Tkinter itself won't work
    # without it). dev-python/pillow's tk USE flag is OPTIONAL - it only
    # unlocks the magnifier's faster in-memory rendering path; the script
    # falls back to a slower (but working) method without it.
    local need_python=0 need_pillow=0
    if portageq has_version / "dev-lang/python[tk]" &>/dev/null; then
        log "dev-lang/python already installed with USE=tk - skipping."
        record_step "SKIPPED" "dev-lang/python (already has USE=tk)"
    else
        need_python=1
    fi
    if portageq has_version / "dev-python/pillow[tk]" &>/dev/null; then
        log "dev-python/pillow already installed with USE=tk - skipping."
        record_step "SKIPPED" "dev-python/pillow (already has USE=tk)"
    else
        need_pillow=1
    fi

    local needs_install=()
    for pkg in "${pkgs[@]}"; do
        if portageq has_version / "$pkg" &>/dev/null; then
            log "$pkg already installed - skipping."
            record_step "SKIPPED" "$pkg (already installed)"
        else
            needs_install+=("$pkg")
        fi
    done
    [[ $need_python -eq 1 ]] && needs_install+=("dev-lang/python")
    [[ $need_pillow -eq 1 ]] && needs_install+=("dev-python/pillow")

    if [[ $need_python -eq 1 || $need_pillow -eq 1 ]]; then
        local use_file="/etc/portage/package.use/snippysnappy"
        local use_content=$'dev-lang/python tk\ndev-python/pillow tk\n'
        if [[ -f "$use_file" ]]; then
            if [[ "$(cat "$use_file" 2>/dev/null)" == "${use_content%$'\n'}" ]]; then
                log "$use_file already has the required content - leaving as-is."
                record_step "SKIPPED" "$use_file (already correct)"
            elif confirm_destructive "$use_file already exists with different content. Overwrite it?"; then
                printf '%s' "$use_content" >"$use_file"
                record_step "OK" "wrote $use_file"
            else
                warn "Not overwriting $use_file - the required USE flags may not be set, and the emerge below may not build python/pillow with tk support."
                record_step "SKIPPED" "$use_file (user declined overwrite)"
            fi
        else
            printf '%s' "$use_content" >"$use_file"
            log "Wrote USE flags to $use_file"
            record_step "OK" "wrote $use_file"
        fi
    fi

    if [[ ${#needs_install[@]} -eq 0 ]]; then
        log "All dependencies already satisfied - nothing to emerge."
        return 0
    fi

    echo
    log "About to run: emerge --newuse --ask ${needs_install[*]}"
    if [[ $need_python -eq 1 || $need_pillow -eq 1 ]]; then
        warn "This includes rebuilding python and/or pillow with new USE flags - python especially can take a while to compile."
    fi
    if ! confirm "Proceed?"; then
        record_step "SKIPPED" "emerge (user declined)"
        return 1
    fi
    emerge --newuse --ask "${needs_install[@]}"
}

install_apt() {
    warn "apt support is best-effort and less thoroughly verified than the Gentoo path used throughout this project's development - please double check these package names on your system."
    local pkgs=("python3" "python3-pil" "python3-pil.imagetk" "python3-tk" "tesseract-ocr")
    if [[ "$SESSION_TYPE" == "x11" || "$SESSION_TYPE" == "both" ]]; then
        pkgs+=("maim" "xclip" "xdotool" "x11-xserver-utils")
    fi
    if [[ "$SESSION_TYPE" == "wayland" || "$SESSION_TYPE" == "both" ]]; then
        pkgs+=("grim" "slurp" "wl-clipboard" "wlr-randr")
    fi
    local needs_install=()
    for pkg in "${pkgs[@]}"; do
        if dpkg -s "$pkg" &>/dev/null; then
            log "$pkg already installed - skipping."
            record_step "SKIPPED" "$pkg (already installed)"
        else
            needs_install+=("$pkg")
        fi
    done
    if [[ ${#needs_install[@]} -eq 0 ]]; then
        log "All dependencies already satisfied - nothing to install."
        return 0
    fi
    echo
    log "About to run: apt-get install -y ${needs_install[*]}"
    confirm "Proceed?" || {
        record_step "SKIPPED" "apt-get install (user declined)"
        return 1
    }
    apt-get update && apt-get install -y "${needs_install[@]}"
}

install_dnf() {
    warn "dnf support is best-effort and less thoroughly verified than the Gentoo path used throughout this project's development - please double check these package names on your system."
    local pkgs=("python3" "python3-pillow" "python3-pillow-tk" "python3-tkinter" "tesseract")
    if [[ "$SESSION_TYPE" == "x11" || "$SESSION_TYPE" == "both" ]]; then
        pkgs+=("maim" "xclip" "xdotool" "xrandr")
    fi
    if [[ "$SESSION_TYPE" == "wayland" || "$SESSION_TYPE" == "both" ]]; then
        pkgs+=("grim" "slurp" "wl-clipboard" "wlr-randr")
    fi
    local needs_install=()
    for pkg in "${pkgs[@]}"; do
        if rpm -q "$pkg" &>/dev/null; then
            log "$pkg already installed - skipping."
            record_step "SKIPPED" "$pkg (already installed)"
        else
            needs_install+=("$pkg")
        fi
    done
    if [[ ${#needs_install[@]} -eq 0 ]]; then
        log "All dependencies already satisfied - nothing to install."
        return 0
    fi
    echo
    log "About to run: dnf install -y ${needs_install[*]}"
    confirm "Proceed?" || {
        record_step "SKIPPED" "dnf install (user declined)"
        return 1
    }
    dnf install -y "${needs_install[@]}"
}

install_pacman() {
    warn "pacman support is best-effort and less thoroughly verified than the Gentoo path used throughout this project's development - please double check these package names on your system."
    local pkgs=("python" "python-pillow" "tk" "tesseract")
    if [[ "$SESSION_TYPE" == "x11" || "$SESSION_TYPE" == "both" ]]; then
        pkgs+=("maim" "xclip" "xdotool" "xorg-xrandr")
    fi
    if [[ "$SESSION_TYPE" == "wayland" || "$SESSION_TYPE" == "both" ]]; then
        pkgs+=("grim" "slurp" "wl-clipboard" "wlr-randr")
    fi
    local needs_install=()
    for pkg in "${pkgs[@]}"; do
        if pacman -Qi "$pkg" &>/dev/null; then
            log "$pkg already installed - skipping."
            record_step "SKIPPED" "$pkg (already installed)"
        else
            needs_install+=("$pkg")
        fi
    done
    if [[ ${#needs_install[@]} -eq 0 ]]; then
        log "All dependencies already satisfied - nothing to install."
        return 0
    fi
    echo
    log "About to run: pacman -S ${needs_install[*]}"
    confirm "Proceed?" || {
        record_step "SKIPPED" "pacman -S (user declined)"
        return 1
    }
    pacman -S --noconfirm "${needs_install[@]}"
}

case "$PKG_MANAGER" in
portage) run_step "Install dependencies (portage)" install_gentoo ;;
apt) run_step "Install dependencies (apt)" install_apt ;;
dnf) run_step "Install dependencies (dnf)" install_dnf ;;
pacman) run_step "Install dependencies (pacman)" install_pacman ;;
esac

# ---------------------------------------------------------------------------
# User-level setup - all done AS THE USER, not root. Existing files are
# never silently overwritten - identical content is left alone with a note,
# different content prompts before overwriting.
# ---------------------------------------------------------------------------

log "Setting up remaining files as $TARGET_USER..."

write_snippysnappy_py() {
    local tmp_new
    tmp_new=$(mktemp)
    cat >"$tmp_new" <<'SNIPPYSNAPPY_PYEOF'
#!/usr/bin/env python3
"""
snippysnappy - a lightweight screenshot capture with adjustable selection,
free-hand edit (pen/highlighter), magnifier, measurement tool, and OCR text extraction.

Invocations:
    snippysnappy               Select mode: overlay on the monitor under your
                              cursor, drag to select a region.
    snippysnappy --fullscreen  Whole monitor under your cursor is captured and
                              pre-selected immediately (no drag needed) in a
                              normal decorated window; drag the corner
                              handles if you want to crop it further.
    snippysnappy --window      Click a window to capture just its content, in
                              a normal decorated window.

Main toolbar:
    - Copy: Straight to clipboard, no file written, closes immediately.
    - Save: "Normal Save" (prompts for a filename, default snipsnap-<timestamp>, into ~/Pictures/Screenshots) or "Slop Save" (no prompt, fixed name slopsnap-<timestamp>.png, into a separate folder for quick/incidental shots). A checkbox (on by default) also copies to clipboard on save.
    - Edit: Resize, Pen (opaque) or Highlighter (translucent), Done bakes it in.
    - Extract Text: Optionally paint over just the text you want OCR'd (full capture default), then Run OCR; result is copied to clipboard.
    - Magnify: Create a "lens" that magnifies a portion of the capture.
    - Measure: Use click-and-drag boxes to measure the size in pixels of capture elements.

Dependencies:
    Dependecies needed (based on portage): media-gfx/maim x11-misc/xclip app-text/tesseract dev-python/pillow x11-misc/xdotool x11-apps/xrandr dev-lang/python (tk use flag needed on python for tkinter support).

    Make sure to create a snippysnappy.d folder in ~/.local/bin; script and logging goes here by default. For better calling, use this wrapper script written to PATH:

    cat > ~/.local/bin/snippysnappy << 'EOF'
    #!/bin/bash
    exec python3 "$HOME/.local/bin/snippysnappy/snippysnappy.py" "$@"
    EOF
    chmod +x ~/.local/bin/snippysnappy

Usage:
    Write this file to ~/.local/bin/snippysnappy.d/snippysnappy.py; write wrapper as above.

    chmod +x ~/.local/bin/snippysnappy.d/snippysnappy.py

    snippysnappy                  # bind to Print
    snippysnappy --fullscreen     # bind to Shift+Print
    snippysnappy --window         # bind to Ctrl+Shift+Print

    To change default behaviors edit relevant lines in this file.

Logging:
  Logs to console always, and to LOG_PATH below unless that line is commented
  out. Uses a size-capped ROTATING log (appends across runs, only rotates to
  a fresh file once it exceeds MAX_LOG_BYTES, keeping LOG_BACKUP_COUNT old
  copies) rather than wiping on every single invocation - so a crash from a
  few runs ago is still readable, not destroyed the instant you press the
  hotkey again. If the file can't be opened for any reason, we log a warning
  and continue with console-only logging rather than crashing.
"""
import argparse
import logging
import logging.handlers
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path

LOG_PATH = Path(__file__).resolve().parent / "snippysnappy.log"   # comment out this line to disable file logging entirely
MAX_LOG_BYTES = 1_000_000   # rotate once the log exceeds ~1MB
LOG_BACKUP_COUNT = 3        # keep this many rotated-out old logs (snippysnappy.log.1, .2, .3)

SCREENSHOT_DIR = Path.home() / "Pictures" / "Screenshots"
SLOP_DIR = Path.home() / "Pictures" / "Slop-o-graphs"   # incidental screenshot path (Slop Save).
TEXT_DIR = Path("/tmp")   # incidental OCR text lands here (Slop Save Text)
SNIPTEXT_DIR = Path(__file__).resolve().parent / "sniptext"   # persistent OCR text lands here (Save Text)
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

HANDLE_SIZE = 8
OCR_BRUSH_WIDTH = 18
OCR_MASK_MIN_PADDING = 20   # px - minimum mask padding regardless of brush width
DEFAULT_COPY_ON_SAVE = True   # default state of the "Also copy to clipboard" checkbox on the Save/OCR-save screens
NOTIFICATION_BACKEND = "toast"   # "system" (notify-send, falls back to the in-app toast if it fails) or "toast" (in-app toast is primary, falls back to notify-send if it fails)
OCR_RETURN_TO_TOOLBAR_AFTER_ACTION = True   # after Copy Text/Save Text, return to the main toolbar so you can still Save/Copy the underlying image too. Set to False to close the app immediately instead "Close" on this screen always closes immediately either way.
TOOLBAR_HEIGHT = 90
MIN_POINT_DIST = 3   # px - skip adding a new stroke point closer than this to the last one
MIN_WINDOW_WIDTH = 560   # decorated windows are at least this wide, so the toolbar always fits

EDIT_PEN_WIDTH = 4
EDIT_HIGHLIGHT_WIDTH = 22
EDIT_HIGHLIGHT_ALPHA = 90   # 0-255, translucency of highlighter mode
PALETTE = ["#FF0000", "#00A000", "#0000FF", "#000000", "#FFD400"]
TEXT_FONT_SIZE_DEFAULT = 24
MEASURE_COLORS = ["#FFD400", "#00FFFF", "#FF00FF", "#FF8800", "#00FF00"]
MAGNIFIER_LENS_SIZE_DEFAULT = 100   # diameter in on-screen pixels
MAGNIFIER_LEVEL_DEFAULT = 4         # magnification factor
SERIF_FONT_PATHS = [
    # Tried in order; falls back to PIL's built-in bitmap font (always available, ignores size) if none of these exist.
    "/usr/share/fonts/dejavu/DejaVuSerif.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
    "/usr/share/fonts/liberation-fonts/LiberationSerif-Regular.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
    "/usr/share/fonts/TTF/DejaVuSerif.ttf",
]


def load_serif_font(size):
    from PIL import ImageFont
    for path in SERIF_FONT_PATHS:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    log.warning("No serif TTF found in SERIF_FONT_PATHS - falling back to PIL's default bitmap font (won't respect size)")
    return ImageFont.load_default()

def setup_logging():
    logger = logging.getLogger("snippysnappy")
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    logger.addHandler(console)

    log_path = globals().get("LOG_PATH")
    if log_path is not None:
        try:
            file_handler = logging.handlers.RotatingFileHandler(
                log_path, mode="a", maxBytes=MAX_LOG_BYTES, backupCount=LOG_BACKUP_COUNT,
            )
            file_handler.setFormatter(fmt)
            logger.addHandler(file_handler)
            logger.debug(f"File logging active at {log_path} (rotating at {MAX_LOG_BYTES} bytes, {LOG_BACKUP_COUNT} backups)")
        except OSError as e:
            logger.warning(f"Could not open log file {log_path} ({e}); continuing with console logging only.")
    else:
        logger.debug("File logging disabled (LOG_PATH not set)")
    return logger


log = setup_logging()


def run_logged(cmd, **kwargs):
    """
    subprocess.run wrapper that logs the command and its outcome.
    """
    log.debug(f"Running: {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, **kwargs)
        log.debug(f"Finished: {cmd[0]} (rc={result.returncode})")
        return result
    except subprocess.CalledProcessError as e:
        log.error(f"{cmd[0]} failed (rc={e.returncode}): {getattr(e, 'stderr', '')}")
        raise
    except Exception:
        log.exception(f"Unexpected error running {cmd[0]}")
        raise


def popen_logged(cmd, **kwargs):
    """
    subprocess.Popen wrapper that logs the command being launched (fire-and-forget).
    """
    log.debug(f"Launching (detached): {' '.join(cmd)}")
    try:
        return subprocess.Popen(cmd, **kwargs)
    except Exception:
        log.exception(f"Unexpected error launching {cmd[0]}")
        raise


def detect_backend():
    """
    'wayland' or 'x11'. Override with SNIPPYSNAPPY_BACKEND=wayland|x11 for
    testing. Auto-detection just checks WAYLAND_DISPLAY.
    """
    override = os.environ.get("SNIPPYSNAPPY_BACKEND")
    if override in ("x11", "wayland"):
        return override
    return "wayland" if os.environ.get("WAYLAND_DISPLAY") else "x11"


BACKEND = detect_backend()


def capture_full_screen_x11(path):
    run_logged(["maim", "-m", "1", "-f", "png", str(path)], check=True)


def get_mouse_position():
    """
    X11 only - Wayland's security model blocks arbitrary cursor-position
    querying, so --fullscreen mode uses an interactive slurp -o pick instead.
    """
    out = run_logged(
        ["xdotool", "getmouselocation", "--shell"], capture_output=True, text=True, check=True
    ).stdout
    vals = dict(line.split("=") for line in out.strip().splitlines())
    pos = int(vals["X"]), int(vals["Y"])
    log.debug(f"Mouse position: {pos}")
    return pos


def get_monitors_x11():
    """
    Returns [(name, x, y, w, h), ...] from `xrandr --listmonitors`.
    """
    out = run_logged(
        ["xrandr", "--listmonitors"], capture_output=True, text=True, check=True
    ).stdout
    monitors = []
    for line in out.strip().splitlines()[1:]:
        parts = line.split()
        geom_token = next(p for p in parts if re.match(r"\d+/\d+x\d+/\d+\+\d+\+\d+", p))
        w, h, x, y = (int(v) for v in re.match(r"(\d+)/\d+x(\d+)/\d+\+(\d+)\+(\d+)", geom_token).groups())
        monitors.append((parts[-1], x, y, w, h))
    if not monitors:
        log.error("xrandr --listmonitors returned no monitors")
        raise RuntimeError("xrandr --listmonitors returned no monitors")
    log.debug(f"Detected monitors: {monitors}")
    return monitors


_WLR_RANDR_OUTPUT_RE = re.compile(r'^(\S+)\s+"')
_WLR_RANDR_POS_RE = re.compile(r"Position:\s*(-?\d+),(-?\d+)")
_WLR_RANDR_MODE_RE = re.compile(r"(\d+)x(\d+)\s.*\bcurrent\b")


def get_monitors_wayland():
    """
    Returns [(name, x, y, w, h), ...] by parsing `wlr-randr`'s text output.
    """
    out = run_logged(["wlr-randr"], capture_output=True, text=True, check=True).stdout
    monitors = []
    name = None
    w = h = x = y = None
    for line in out.splitlines():
        m = _WLR_RANDR_OUTPUT_RE.match(line)
        if m:
            if name and None not in (w, h, x, y):
                monitors.append((name, x, y, w, h))
            name = m.group(1)
            w = h = x = y = None
            continue
        m = _WLR_RANDR_POS_RE.search(line)
        if m:
            x, y = int(m.group(1)), int(m.group(2))
            continue
        m = _WLR_RANDR_MODE_RE.search(line)
        if m:
            w, h = int(m.group(1)), int(m.group(2))
    if name and None not in (w, h, x, y):
        monitors.append((name, x, y, w, h))
    if not monitors:
        raise RuntimeError("wlr-randr produced no parseable monitor output")
    log.debug(f"Detected monitors (wayland): {monitors}")
    return monitors


def get_monitors():
    if BACKEND == "wayland":
        try:
            return get_monitors_wayland()
        except Exception:
            log.warning("get_monitors_wayland() failed - falling back to treating the display as one monitor")
            fd, tmp_name = tempfile.mkstemp(suffix=".png")
            os.close(fd)
            tmp_path = Path(tmp_name)
            try:
                run_logged(["grim", tmp_name], check=True)
                from PIL import Image
                with Image.open(tmp_path) as img:
                    w, h = img.size
            finally:
                tmp_path.unlink(missing_ok=True)
            return [("default", 0, 0, w, h)]
    return get_monitors_x11()


def monitor_at_point(monitors, px, py):
    for mon in monitors:
        _, x, y, w, h = mon
        if x <= px < x + w and y <= py < y + h:
            log.debug(f"Point ({px},{py}) is on monitor {mon}")
            return mon
    log.warning(f"Point ({px},{py}) not on any reported monitor; falling back to first: {monitors[0]}")
    return monitors[0]  # fallback if cursor is somehow off every monitor


def get_virtual_screen_bounds(monitors):
    """
    Bounding box spanning ALL monitors combined - i.e. the full extended desktop.
    """
    min_x = min(m[1] for m in monitors)
    min_y = min(m[2] for m in monitors)
    max_x = max(m[1] + m[3] for m in monitors)
    max_y = max(m[2] + m[4] for m in monitors)
    bounds = (min_x, min_y, max_x - min_x, max_y - min_y)
    log.debug(f"Virtual screen bounds across {len(monitors)} monitor(s): {bounds}")
    return bounds


def capture_monitor_x11(x, y, w, h, path):
    run_logged(["maim", "-m", "1", "-g", f"{w}x{h}+{x}+{y}", "-f", "png", str(path)], check=True)


def capture_monitor_wayland(x, y, w, h, path):
    run_logged(["grim", "-l", "1", "-g", f"{x},{y} {w}x{h}", str(path)], check=True)


def capture_monitor(x, y, w, h, path):
    if BACKEND == "wayland":
        capture_monitor_wayland(x, y, w, h, path)
    else:
        capture_monitor_x11(x, y, w, h, path)


def capture_full_screen_wayland(path):
    run_logged(["grim", "-l", "1", str(path)], check=True)


def capture_full_screen(path):
    if BACKEND == "wayland":
        capture_full_screen_wayland(path)
    else:
        capture_full_screen_x11(path)


def select_window_id():
    """
    X11 only - see pick_region_interactive_wayland for the Wayland
    equivalent.
    """
    result = run_logged(["xdotool", "selectwindow"], capture_output=True, text=True, check=True)
    win_id = result.stdout.strip()
    log.debug(f"Selected window id: {win_id}")
    return win_id


def get_window_geometry(window_id):
    out = run_logged(
        ["xdotool", "getwindowgeometry", "--shell", window_id],
        capture_output=True, text=True, check=True,
    ).stdout
    vals = dict(line.split("=") for line in out.strip().splitlines())
    geom = int(vals["X"]), int(vals["Y"]), int(vals["WIDTH"]), int(vals["HEIGHT"])
    log.debug(f"Window {window_id} geometry: {geom}")
    return geom


def capture_window(window_id, path):
    """
    X11 only - Wayland's --window mode uses pick_region_interactive_wayland
    + capture_monitor_wayland instead (no window-id concept).
    """
    run_logged(["maim", "-m", "1", "-i", window_id, "-f", "png", str(path)], check=True)


def pick_region_interactive_wayland():
    """
    Runs slurp for a manual interactive region drag - used for --window
    mode under Wayland. Returns (x, y, w, h).
    """
    out = run_logged(["slurp"], capture_output=True, text=True, check=True).stdout.strip()
    xy, wh = out.split(" ")
    x, y = (int(v) for v in xy.split(","))
    w, h = (int(v) for v in wh.split("x"))
    return x, y, w, h


def pick_output_interactive_wayland():
    """
    Runs `slurp -o` for an interactive click-to-pick-a-monitor - the
    Wayland substitute for --fullscreen mode's X11 "whichever monitor the
    mouse is on" behavior, since Wayland blocks querying cursor position
    directly. Returns (name, x, y, w, h).
    """
    out = run_logged(
        ["slurp", "-o", "-f", "%o %x,%y %wx%h"], capture_output=True, text=True, check=True
    ).stdout.strip()
    name, xy, wh = out.split(" ")
    x, y = (int(v) for v in xy.split(","))
    w, h = (int(v) for v in wh.split("x"))
    return name, x, y, w, h


def copy_image_to_clipboard_x11(path):
    popen_logged(
        ["xclip", "-selection", "clipboard", "-t", "image/png", "-i", str(path)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def copy_image_to_clipboard_wayland(path):
    with open(path, "rb") as f:
        data = f.read()
    p = popen_logged(
        ["wl-copy", "-t", "image/png"],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    p.stdin.write(data)
    p.stdin.close()


def copy_image_to_clipboard(path):
    if BACKEND == "wayland":
        copy_image_to_clipboard_wayland(path)
    else:
        copy_image_to_clipboard_x11(path)


def copy_text_to_clipboard_x11(text):
    p = popen_logged(
        ["xclip", "-selection", "clipboard"],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    p.stdin.write(text.encode())
    p.stdin.close()


def copy_text_to_clipboard_wayland(text):
    p = popen_logged(
        ["wl-copy"],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    p.stdin.write(text.encode())
    p.stdin.close()


def copy_text_to_clipboard(text):
    if BACKEND == "wayland":
        copy_text_to_clipboard_wayland(text)
    else:
        copy_text_to_clipboard_x11(text)


def ocr_image(path):
    result = run_logged(["tesseract", str(path), "stdout"], capture_output=True, text=True)
    text = result.stdout.strip()
    log.debug(f"OCR produced {len(text)} chars")
    return text


def ensure_dbus_env():
    """
    Global hotkey daemons often spawn commands with a stripped environment
    missing DBUS_SESSION_BUS_ADDRESS, which notify-send needs to reach the
    notification service. Without it, notify-send doesn't fail cleanly - it
    hangs trying to connect. Falls back to the standard systemd-user D-Bus socket path
    if the variable isn't already set and that socket actually exists.
    """
    if "DBUS_SESSION_BUS_ADDRESS" in os.environ:
        log.debug(f"DBUS_SESSION_BUS_ADDRESS already set: {os.environ['DBUS_SESSION_BUS_ADDRESS']}")
        return
    candidate_path = f"/run/user/{os.getuid()}/bus"
    if Path(candidate_path).exists():
        os.environ["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={candidate_path}"
        log.info(f"DBUS_SESSION_BUS_ADDRESS was unset; defaulted to unix:path={candidate_path}")
    else:
        log.warning(
            f"DBUS_SESSION_BUS_ADDRESS is unset and the default socket {candidate_path} "
            "doesn't exist either - notifications will likely keep failing silently."
        )


def show_toast_fallback(title, body):
    """
    Tiny popup used only when notify-send isn't working, so there's still
    some visual confirmation the action completed.

    IMPORTANT: never create a second independent tk.Tk() while one already
    exists in this process - doing so corrupts Tkinter's shared default-root
    state and can silently break subsequent widget/PhotoImage creation on
    the ORIGINAL root .
    Instead: if a root already exists, attach as a Toplevel of it and use
    wait_window() to block until it closes; only create a standalone Tk()
    if no root exists yet at all (e.g. after self.root.destroy() elsewhere).

    FLAG RETURN TO THIS ISSUE: Even so, the "Capture taken" notification
    (fired before SelectorApp's window is up, with an active window manager
    managing windows) proved too risky in practice - rapid creation and
    destruction of an overrideredirect+topmost toast right before that same
    root gets its OWN overrideredirect/geometry/topmost configured can race
    with the WM in ways a headless test environment can't catch. See
    notify()'s allow_toast parameter - it's set False for "Capture taken"
    specifically, skipping the toast there entirely (a log entry is enough;
    the incoming selection overlay itself is the real visual confirmation).
    """
    try:
        existing_root = getattr(tk, "_default_root", None)
        owns_root = existing_root is None
        toast = tk.Tk() if owns_root else tk.Toplevel(existing_root)
        toast.overrideredirect(True)
        toast.attributes("-topmost", True)
        try:
            monitors = get_monitors()
            _, mx, my, mw, mh = monitors[0]
        except Exception:
            mx, my, mw, mh = 0, 0, 800, 600
        w, h = 320, 90
        x = mx + mw - w - 20
        y = my + mh - h - 40
        toast.geometry(f"{w}x{h}+{x}+{y}")
        frame = tk.Frame(toast, bg="#222222", highlightbackground="#555555", highlightthickness=1)
        frame.pack(fill="both", expand=True)
        tk.Label(
            frame, text=title, fg="white", bg="#222222", font=("", 11, "bold"),
            anchor="w", justify="left",
        ).pack(fill="x", padx=10, pady=(8, 2))
        if body:
            tk.Label(
                frame, text=body, fg="#cccccc", bg="#222222",
                anchor="w", justify="left", wraplength=300,
            ).pack(fill="x", padx=10, pady=(0, 8))
        toast.after(2500, toast.destroy)
        if owns_root:
            toast.mainloop()
        else:
            toast.wait_window(toast)
        return True
    except Exception:
        log.exception("Toast fallback itself failed (non-fatal)")
        return False


def notify(title, body="", allow_toast=True):
    """
    Two channels are available: the OS notification daemon (notify-send)
    and an in-app Tk toast. NOTIFICATION_BACKEND picks which one is tried
    first; the other is used as a fallback if the primary fails. allow_toast
    still means "don't attempt the toast at all here" regardless of which
    backend is primary - it exists for the specific 'Capture taken' calls
    that fire before SelectorApp's window is set up, where creating a toast
    window risks racing with the WM (see show_toast_fallback's docstring).
    """
    if NOTIFICATION_BACKEND == "toast":
        _notify_toast_primary(title, body, allow_toast)
    else:
        _notify_system_primary(title, body, allow_toast)


def _notify_system_primary(title, body, allow_toast):
    if not allow_toast:
        popen_logged(["notify-send", title, body], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return

    proc = popen_logged(
        ["notify-send", title, body],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        out, err = proc.communicate(timeout=1.0)
        if proc.returncode != 0:
            log.warning(f"notify-send exited {proc.returncode}: {err.strip()}")
            show_toast_fallback(title, body)
        else:
            log.debug("notify-send returned success")
    except subprocess.TimeoutExpired:
        log.warning("notify-send did not return within 1s; left running detached, outcome unknown")
        show_toast_fallback(title, body)
    except FileNotFoundError:
        log.error("notify-send not found on PATH - notifications will never appear")
        show_toast_fallback(title, body)


def _notify_toast_primary(title, body, allow_toast):
    if not allow_toast:
        popen_logged(["notify-send", title, body], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    succeeded = show_toast_fallback(title, body)
    if not succeeded:
        log.warning("Toast (primary) failed - falling back to notify-send")
        popen_logged(["notify-send", title, body], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def timestamp():
    return datetime.now().strftime("%Y%m%d-%H%M%S")


OCR_UPSCALE_FACTOR = 2       # how much to enlarge crops before OCR
OCR_UPSCALE_SKIP_HEIGHT = 1000   # skip upscaling only for already-large crops


def preprocess_for_ocr(img):
    """
    Upscale (unless already large) + grayscale + autocontrast + Otsu
    binarize + auto-invert so color and background variation (multi-colored
    terminal text, translucent dark panels) can't confuse tesseract, and
    small real-world terminal font sizes get enough resolution to recognize
    reliably.

    Upscaling is based on total crop height only to skip the rare
    pathologically-large case (a whole-screen OCR).
    """
    from PIL import Image, ImageOps
    if img.height < OCR_UPSCALE_SKIP_HEIGHT:
        img = img.resize((img.width * OCR_UPSCALE_FACTOR, img.height * OCR_UPSCALE_FACTOR), Image.LANCZOS)
    gray = ImageOps.autocontrast(img.convert("L"))
    hist = gray.histogram()
    total = sum(hist)
    if total == 0:
        return img
    sum_total = sum(i * hist[i] for i in range(256))
    sum_bg = 0
    weight_bg = 0
    max_variance = -1
    threshold = 127
    for i in range(256):
        weight_bg += hist[i]
        if weight_bg == 0:
            continue
        weight_fg = total - weight_bg
        if weight_fg == 0:
            break
        sum_bg += i * hist[i]
        mean_bg = sum_bg / weight_bg
        mean_fg = (sum_total - sum_bg) / weight_fg
        variance_between = weight_bg * weight_fg * (mean_bg - mean_fg) ** 2
        if variance_between > max_variance:
            max_variance = variance_between
            threshold = i
    binary = gray.point(lambda p: 255 if p > threshold else 0)
    if binary.histogram()[0] > binary.histogram()[255]:
        binary = ImageOps.invert(binary)
    return binary.convert("RGB")


def hex_to_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


class SelectorApp:
    def __init__(self, root, image_path, mode="monitor", bounds=None, auto_select_full=False,
                 monitors=None, capture_tempdir=None, current_monitor_name=None):
        """
        mode="monitor":    bounds=(x, y, w, h) of the exact monitor captured;
                            borderless overlay pinned to that monitor exactly,
                            for interactive drag-to-select. Toolbar floats near
                            the selection.
        mode="window":     bounds=(x, y, w, h) of the monitor containing the
                            captured window, used only to center the popup.
                            Decorated window, fixed toolbar strip at the bottom.
        mode="fullscreen": same as "window" presentation-wise, but the whole
                            captured image starts pre-selected (auto_select_full),
                            so there's no drag step required. `monitors` and
                            `current_monitor_name` enable the monitor-switcher
                            dropdown for this mode.
        `capture_tempdir`: the TemporaryDirectory path from main(), so Redo
        Capture can clean it up before re-exec'ing (execv skips normal cleanup).
        """
        log.info(f"SelectorApp init: mode={mode} bounds={bounds} auto_select_full={auto_select_full}")
        self.root = root
        self.image_path = image_path
        self.owned_temp_paths = [image_path]  # cleaned up when the window is destroyed - see _cleanup_temp_files
        self._ocr_overlay = None
        self._ocr_text_widget = None
        self._ocr_copy_var = None
        self.mode = mode
        self.available_monitors = monitors or []
        self.current_monitor_name = current_monitor_name
        self.capture_tempdir = capture_tempdir

        self.img = tk.PhotoImage(file=str(image_path))
        iw, ih = self.img.width(), self.img.height()
        log.debug(f"Loaded capture image: {iw}x{ih}")

        self.toolbar_container = None

        if mode == "monitor":
            x, y, _, _ = bounds
            self.origin_x, self.origin_y = x, y
            root.overrideredirect(True)
            root.geometry(f"{iw}x{ih}+{x}+{y}")
            root.attributes("-topmost", True)
            log.debug(f"Placed borderless overlay at {iw}x{ih}+{x}+{y}")
        else:
            mx, my, mw, mh = bounds
            win_w = max(iw, MIN_WINDOW_WIDTH)
            total_h = ih + TOOLBAR_HEIGHT
            cx = mx + max((mw - win_w) // 2, 0)
            cy = my + max((mh - total_h) // 2, 0)
            root.title("snippysnappy")
            root.geometry(f"{win_w}x{total_h}+{cx}+{cy}")
            root.attributes("-topmost", True)
            self.toolbar_container = tk.Frame(root, bg="#222222", height=TOOLBAR_HEIGHT)
            self.toolbar_container.pack(side="bottom", fill="x")
            self.toolbar_container.pack_propagate(False)
            log.debug(f"Placed decorated window at {win_w}x{total_h}+{cx}+{cy} (image {iw}x{ih})")

        root.configure(cursor="crosshair")
        self.canvas = tk.Canvas(root, width=iw, height=ih, highlightthickness=0)
        self.canvas.pack(side="top", fill="both", expand=True)
        self.canvas.create_image(0, 0, anchor="nw", image=self.img)

        # selection state
        self.rect = None
        self.start_x = self.start_y = 0
        self.sel_coords = None
        self.handles = []
        self.reset_btn_items = []
        self.active_handle = None
        self.toolbar = None

        # OCR paint-mask state
        self.ocr_strokes = []
        self.ocr_current_stroke = None

        # edit (pen/highlighter/text) state
        self.edit_strokes = []
        self.edit_current_stroke = None
        self.draw_color = PALETTE[0]
        self.draw_tool = "pen"
        self.pen_width = EDIT_PEN_WIDTH
        self.highlight_width = EDIT_HIGHLIGHT_WIDTH
        self.ocr_brush_width = OCR_BRUSH_WIDTH
        self.baked_image = None
        self.baked_overlay_photo = None
        self.baked_overlay_photo_id = None
        self.text_font_size = TEXT_FONT_SIZE_DEFAULT
        self.text_bg_fill = "transparent"
        self.text_box_start = None
        self.text_current_box_id = None
        self._pending_text_widget = None
        self._pending_text_window_id = None
        self._pending_text_box = None
        self._pre_resize_snapshot = None

        self.tools_row_expanded = False
        self.dimension_label = None
        self.measure_mode_active = False
        self.measure_boxes = []          # each: {"coords", "color", "rect_id", "handle_ids", "label"}
        self.measure_active_box = None   # index of box currently being created/resized
        self.measure_active_handle = None
        self.measure_creating = False
        self.measure_start = None

        self.magnify_mode_active = False
        self.magnify_lens_size = MAGNIFIER_LENS_SIZE_DEFAULT
        self.magnify_level = MAGNIFIER_LEVEL_DEFAULT
        self.magnify_lens_item_id = None
        self.magnify_cursor_item_id = None
        self.magnify_source_image = None   # cached PIL image of the current selection, reloaded once per magnify session

        self.edit_mode_active = False
        self.ocr_paint_mode_active = False

        self.copy_on_save_var = tk.IntVar(value=int(DEFAULT_COPY_ON_SAVE))

        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        root.bind("<Escape>", self._handle_escape)
        root.bind("<Destroy>", self._cleanup_temp_files)

        # In-app keyboard shortcuts (Ctrl+letter). Guarded to only fire once a real selection exists, and context-aware for Save/Slop Save: targets the underlying image normally, or the extracted text specifically while the OCR result screen is showing.
        root.bind("<Control-c>", lambda e: self._keybind_copy())
        root.bind("<Control-s>", lambda e: self._keybind_slop_save())
        root.bind("<Control-S>", lambda e: self._keybind_normal_save())
        root.bind("<Control-e>", lambda e: self._keybind_edit())
        root.bind("<Control-t>", lambda e: self._keybind_extract_text())
        root.bind("<Control-u>", lambda e: self._keybind_undo())
        self.canvas.bind("<Button-4>", lambda e: self._on_scroll(1))
        self.canvas.bind("<Button-5>", lambda e: self._on_scroll(-1))
        self.canvas.bind("<MouseWheel>", lambda e: self._on_scroll(1 if e.delta > 0 else -1))
        root.focus_force()
        if mode == "monitor":
            root.update_idletasks()
            try:
                root.grab_set_global()
            except tk.TclError:
                log.warning("grab_set_global() failed (window not yet viewable?) - falling back to focus_force() only")

        if auto_select_full:
            self.sel_coords = (0, 0, iw, ih)
            self.draw_handles()
            self.show_main_toolbar()

    # ---- rectangle selection ----

    def _handle_escape(self, event=None):
        if self.measure_mode_active:
            self.exit_measure_mode()
            return
        log.info("Escape pressed, destroying window")
        self.root.destroy()

    def hit_handle(self, x, y):
        for hid, name in self.handles:
            hx = self.canvas.coords(hid)[0] + HANDLE_SIZE
            hy = self.canvas.coords(hid)[1] + HANDLE_SIZE
            if abs(x - hx) <= HANDLE_SIZE * 1.5 and abs(y - hy) <= HANDLE_SIZE * 1.5:
                return name
        return None

    def hit_reset_button(self, x, y):
        if not self.reset_btn_items:
            return False
        bbox = self.canvas.bbox(self.reset_btn_items[0])
        if bbox is None:
            return False
        pad = 10  #  tolerance for click register
        return (bbox[0] - pad) <= x <= (bbox[2] + pad) and (bbox[1] - pad) <= y <= (bbox[3] + pad)

    def on_press(self, event):
        if self.hit_reset_button(event.x, event.y):
            if self.rect:
                self.canvas.delete(self.rect)
                self.rect = None
            self.active_handle = None
            self.reset_selection()
            return
        handle = self.hit_handle(event.x, event.y)
        if handle:
            self.active_handle = handle
            return
        self.clear_handles()
        self.active_handle = None
        self.start_x, self.start_y = event.x, event.y
        if self.rect:
            self.canvas.delete(self.rect)
        self.rect = self.canvas.create_rectangle(
            self.start_x, self.start_y, self.start_x, self.start_y,
            outline="#00b4ff", width=2,
        )
        if self.toolbar_container is not None:
            self.show_main_toolbar(reset_tools_row=False)
        else:
            self.clear_toolbar()


    def on_drag(self, event):
        if self.active_handle:
            self.resize_with_handle(self.active_handle, event.x, event.y)
        elif self.rect:
            self.canvas.coords(self.rect, self.start_x, self.start_y, event.x, event.y)
            self.update_dimension_display(override=(abs(event.x - self.start_x), abs(event.y - self.start_y)))

    def on_release(self, event):
        if self.rect:
            x1, y1, x2, y2 = self.canvas.coords(self.rect)
            self.sel_coords = (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
            log.info(f"Selection made: {self.sel_coords}")
            if self.mode == "monitor":
                self.transition_to_review()
            else:
                self.draw_handles()
                self.show_main_toolbar()
        self.active_handle = None

    def transition_to_review(self):
        """
        Select mode only: once a region is chosen, shrink down from the
        full-screen borderless overlay into a small decorated window holding
        just the selection - frees the rest of the desktop for normal use
        while you work with Save/Copy/Edit/Extract Text, instead of a
        screen-spanning overlay staying up the whole time. Trade-off: no more
        on-the-fly resizing after this point - use Redo Capture to start over
        if you need a different region.
        """
        from PIL import Image
        log.info("Selection finalized, transitioning to compact review window")
        try:
            self.root.grab_release()
        except tk.TclError:
            pass
        x1, y1, x2, y2 = (int(v) for v in self.sel_coords)
        full = Image.open(self.image_path)
        crop = full.crop((x1, y1, x2, y2))
        new_iw, new_ih = crop.size

        fd, tmp_name = tempfile.mkstemp(suffix=".png", prefix="snippysnappy-crop-")
        os.close(fd)
        crop_path = Path(tmp_name)
        crop.save(crop_path)
        global_cx = self.origin_x + (x1 + x2) / 2
        global_cy = self.origin_y + (y1 + y2) / 2
        monitors = get_monitors()
        _, mx, my, mw, mh = monitor_at_point(monitors, global_cx, global_cy)

        self.image_path = crop_path
        self.owned_temp_paths.append(crop_path)
        self.img = tk.PhotoImage(file=str(crop_path))

        self.canvas.delete("all")
        self.canvas.config(width=new_iw, height=new_ih)
        self.canvas.create_image(0, 0, anchor="nw", image=self.img)

        self.root.withdraw()
        self.root.overrideredirect(False)
        self.root.attributes("-topmost", True)
        self.root.title("snippysnappy")
        win_w = max(new_iw, MIN_WINDOW_WIDTH)
        total_h = new_ih + TOOLBAR_HEIGHT
        cx = mx + max((mw - win_w) // 2, 0)
        cy = my + max((mh - total_h) // 2, 0)
        self.root.geometry(f"{win_w}x{total_h}+{cx}+{cy}")

        self.toolbar_container = tk.Frame(self.root, bg="#222222", height=TOOLBAR_HEIGHT)
        self.toolbar_container.pack(side="bottom", fill="x")
        self.toolbar_container.pack_propagate(False)
        self.canvas.pack_forget()
        self.canvas.pack(side="top", fill="both", expand=True)

        self.root.deiconify()

        self.mode = "window"
        self.sel_coords = (0, 0, new_iw, new_ih)
        self.baked_image = None
        self.baked_overlay_photo = None
        self.baked_overlay_photo_id = None
        self.edit_strokes = []
        self.ocr_strokes = []

        log.debug(f"Review window placed at {win_w}x{total_h}+{cx}+{cy} (image {new_iw}x{new_ih}) on monitor near ({global_cx:.0f},{global_cy:.0f})")
        self.draw_handles()
        self.show_main_toolbar()

    def _keybind_copy(self):
        if self.sel_coords is None:
            return
        if self._ocr_overlay is not None:
            self.do_copy_text(self._ocr_overlay, self._ocr_text_widget)
        else:
            self.do_copy()

    def _keybind_slop_save(self):
        if self.sel_coords is None:
            return
        if self._ocr_overlay is not None:
            self.do_slop_save_text()
        else:
            self.do_slop_save()

    def _keybind_normal_save(self):
        if self.sel_coords is None:
            return
        if self._ocr_overlay is not None:
            self.begin_normal_save_text()
        else:
            self.begin_normal_save()

    def _keybind_edit(self):
        if self.sel_coords is None or self._ocr_overlay is not None:
            return
        self.enter_edit_mode()

    def _keybind_extract_text(self):
        if self.sel_coords is None or self._ocr_overlay is not None:
            return
        self.enter_ocr_paint_mode()

    def _keybind_undo(self):
        if self.sel_coords is None:
            return
        self.undo_last_stroke()

    def _on_scroll(self, direction):
        """
        Mouse wheel over the canvas adjusts whichever parameter is
        relevant to the current mode: magnifier zoom, pen/highlighter width,
        or OCR brush width. A no-op everywhere else (main toolbar, measure
        mode, text tool - no width/zoom concept applies there).
        """
        if self.magnify_mode_active:
            self.magnify_level = max(2, min(16, self.magnify_level + direction))
            self.show_magnify_toolbar()
        elif self.edit_mode_active:
            if self.draw_tool == "pen":
                self.pen_width = max(1, min(30, self.pen_width + direction))
                self.show_edit_toolbar()
            elif self.draw_tool == "highlight":
                self.highlight_width = max(4, min(60, self.highlight_width + direction))
                self.show_edit_toolbar()
        elif self.ocr_paint_mode_active:
            self.ocr_brush_width = max(4, min(60, self.ocr_brush_width + direction))
            self.show_ocr_toolbar()

    def _cleanup_temp_files(self, event=None):
        """
        Removes incidental temp files this app created (the initial
        capture, any crop from transition_to_review, any monitor-switch
        recapture) once the window is actually destroyed - regardless of
        which action (Copy, Save, Cancel, Escape, OCR Close) triggered it.
        Never touches anything the user explicitly saved (Screenshots,
        Slop-o-graphs, sniptext) or do_copy's clipboard temp file (that one
        is deliberately excluded from owned_temp_paths - xclip needs to
        keep serving it as a detached background process after we exit).
        """
        if event is not None and event.widget != self.root:
            return
        for p in self.owned_temp_paths:
            try:
                Path(p).unlink(missing_ok=True)
            except Exception:
                log.exception(f"Failed to clean up temp file {p} (non-fatal)")

    def redo_capture(self):
        """
        Restarts the whole capture flow from scratch by re-running the
        script fresh with the same arguments - simplest robust way to support
        'redo' uniformly across select/window/fullscreen modes without complex
        window reconfiguration for each case.
        """
        log.info("Redo Capture pressed - re-executing script fresh")
        try:
            if self.capture_tempdir:
                shutil.rmtree(self.capture_tempdir, ignore_errors=True)
                log.debug(f"Cleaned up {self.capture_tempdir} before redo")
            if self.image_path and Path(self.image_path).exists():
                Path(self.image_path).unlink(missing_ok=True)
        except Exception:
            log.exception("Cleanup before redo failed (non-fatal, continuing)")
        self.root.destroy()
        os.execv(sys.executable, [sys.executable] + sys.argv)

    def clear_handles(self):
        for hid, _ in self.handles:
            self.canvas.delete(hid)
        self.handles = []
        for item in self.reset_btn_items:
            self.canvas.delete(item)
        self.reset_btn_items = []

    def draw_handles(self):
        self.clear_handles()
        if not self.sel_coords:
            return
        x1, y1, x2, y2 = self.sel_coords
        corners = {"nw": (x1, y1), "ne": (x2, y1), "sw": (x1, y2), "se": (x2, y2)}
        for name, (hx, hy) in corners.items():
            hid = self.canvas.create_rectangle(
                hx - HANDLE_SIZE, hy - HANDLE_SIZE,
                hx + HANDLE_SIZE, hy + HANDLE_SIZE,
                fill="#00b4ff", outline="white",
            )
            self.handles.append((hid, name))
        self.draw_reset_button(x1, y1, x2, y2)

    def draw_reset_button(self, x1, y1, x2, y2):
        """
        Small "x" button just inside the top edge, centered horizontally -
        resets back to the full image.

        Click handling is done via hit_reset_button() inside on_press,
        NOT via canvas.tag_bind - item-level tag_bind and the widget-level
        <ButtonPress-1> binding (on_press itself) are separate binding
        layers in Tk, and "break" from one does not stop the other from
        also firing.
        """
        bx, by = (x1 + x2) / 2, y1 + 18
        circ = self.canvas.create_oval(
            bx - 10, by - 10, bx + 10, by + 10, fill="#333333", outline="white",
        )
        txt = self.canvas.create_text(bx, by, text="\u00d7", fill="white", font=("", 11, "bold"))
        self.reset_btn_items = [circ, txt]

    def reset_selection(self, event=None):
        log.info("Selection reset to full image")
        iw, ih = self.img.width(), self.img.height()
        self.sel_coords = (0, 0, iw, ih)
        self.draw_handles()
        self.show_main_toolbar()

    def resize_with_handle(self, name, x, y):
        x1, y1, x2, y2 = self.sel_coords
        if name == "nw":
            x1, y1 = x, y
        elif name == "ne":
            x2, y1 = x, y
        elif name == "sw":
            x1, y2 = x, y
        elif name == "se":
            x2, y2 = x, y
        self.sel_coords = (x1, y1, x2, y2)
        self.canvas.coords(self.rect, x1, y1, x2, y2)
        self.draw_handles()
        self.update_dimension_display()

    def clamp_to_selection(self, x, y):
        x1, y1, x2, y2 = self.sel_coords
        return max(x1, min(x, x2)), max(y1, min(y, y2))

    # ---- toolbar plumbing ----

    def clear_toolbar(self):
        if self.toolbar_container is not None:
            for w in self.toolbar_container.winfo_children():
                w.destroy()
        elif self.toolbar:
            self.toolbar.destroy()
            self.toolbar = None

    def new_toolbar_frame(self):
        """
        Frame to pack buttons into. Caller must still call finish_toolbar_placement.
        """
        if self.toolbar_container is not None:
            frame = tk.Frame(self.toolbar_container, bg="#222222")
            frame.pack(fill="both", expand=True)
            return frame
        self.toolbar = tk.Frame(self.root, bg="#222222")
        return self.toolbar

    def finish_toolbar_placement(self, frame):
        if self.toolbar_container is None:
            x1, y1, x2, y2 = self.sel_coords
            ty = min(y2 + 8, self.canvas.winfo_height() - 40)
            frame.place(x=max(x1, 0), y=ty)
        else:
            self._ensure_window_fits_toolbar(frame)

    def _ensure_window_fits_toolbar(self, content_frame):
        """
        Grows the window if the toolbar content actually needs more room
        than the window currently has.

        IMPORTANT: measures content_frame (the inner frame built by
        new_toolbar_frame), NOT self.toolbar_container because
        it's created with pack_propagate(False) and an explicit height. The
        UNCONSTRAINED inner frame correctly reports the true required size.
        """
        if self.toolbar_container is None:
            return
        self.root.update_idletasks()
        required_w = content_frame.winfo_reqwidth()
        required_h = self.canvas.winfo_reqheight() + content_frame.winfo_reqheight()
        current_w = self.root.winfo_width()
        current_h = self.root.winfo_height()
        new_w = max(current_w, required_w + 10)
        new_h = max(current_h, required_h)
        if new_w != current_w or new_h != current_h:
            x, y = self.root.winfo_x(), self.root.winfo_y()
            self.root.geometry(f"{new_w}x{new_h}+{x}+{y}")
            log.debug(f"Resized window to fit toolbar content: {new_w}x{new_h} (was {current_w}x{current_h})")

    def show_main_toolbar(self, reset_tools_row=True):
        if reset_tools_row:
            self.tools_row_expanded = False
        self.edit_mode_active = False
        self.ocr_paint_mode_active = False
        self._ocr_overlay = None
        self._ocr_text_widget = None
        self._ocr_copy_var = None
        self._rebind_selection_handlers()
        self.clear_toolbar()
        frame = self.new_toolbar_frame()
        main_row = tk.Frame(frame, bg="#222222")
        main_row.pack(side="top", fill="x")
        tk.Button(main_row, text="Copy", command=self.do_copy).pack(side="left", padx=4, pady=4)
        tk.Button(main_row, text="Save", command=self.show_save_options).pack(side="left", padx=4, pady=4)
        tk.Button(main_row, text="Edit", command=self.enter_edit_mode).pack(side="left", padx=4, pady=4)
        tk.Button(
            main_row, text="Tools",
            relief=("sunken" if self.tools_row_expanded else "raised"),
            command=self.toggle_tools_row,
        ).pack(side="left", padx=4, pady=4)
        if self.mode == "fullscreen" and len(self.available_monitors) > 1:
            self._add_monitor_dropdown(main_row)
        else:
            tk.Button(main_row, text="Redo", command=self.redo_capture).pack(side="left", padx=4, pady=4)
        tk.Button(main_row, text="Cancel", command=lambda: (log.info("Cancel pressed"), self.root.destroy())).pack(side="left", padx=4, pady=4)
        self.dimension_label = tk.Label(main_row, text="", fg="white", bg="#222222")
        self.dimension_label.pack(side="right", padx=8)

        if self.tools_row_expanded:
            tools_row = tk.Frame(frame, bg="#222222")
            tools_row.pack(side="top", fill="x")
            tk.Button(tools_row, text="Extract Text", command=self.enter_ocr_paint_mode).pack(side="left", padx=4, pady=4)
            tk.Button(tools_row, text="Measure", command=self.enter_measure_mode).pack(side="left", padx=4, pady=4)
            tk.Button(tools_row, text="Magnify", command=self.enter_magnify_mode).pack(side="left", padx=4, pady=4)

        self.finish_toolbar_placement(frame)
        self.update_dimension_display()

    def toggle_tools_row(self):
        self.tools_row_expanded = not self.tools_row_expanded
        self.show_main_toolbar(reset_tools_row=False)

    def _rebind_selection_handlers(self):
        """
        Restores the canvas to normal selection-dragging behavior. Called
        by show_main_toolbar so every path back to the main toolbar (Done,
        Cancel, OCR Close/Copy/Save, measure exit, etc.) reliably leaves the
        canvas in the same state, rather than needing every individual exit
        path to remember to do this itself.
        """
        self.canvas.unbind("<ButtonPress-1>")
        self.canvas.unbind("<B1-Motion>")
        self.canvas.unbind("<ButtonRelease-1>")
        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)

    def update_dimension_display(self, override=None):
        """
        Keeps the small "W x H" readout at the right of the main toolbar
        current. override=(w,h) is used by measure mode to show the phantom
        box's size instead of the real selection's.
        """
        if getattr(self, "dimension_label", None) is None:
            return
        try:
            if not self.dimension_label.winfo_exists():
                return
        except tk.TclError:
            return
        if override is not None:
            w, h = override
        elif self.sel_coords:
            x1, y1, x2, y2 = self.sel_coords
            w, h = int(abs(x2 - x1)), int(abs(y2 - y1))
        else:
            return
        self.dimension_label.config(text=f"{w} x {h}")

    def _add_monitor_dropdown(self, frame):
        names = [m[0] for m in self.available_monitors]
        var = tk.StringVar(value=self.current_monitor_name or names[0])
        dropdown = tk.OptionMenu(frame, var, *names, command=self.switch_fullscreen_monitor)
        dropdown.pack(side="left", padx=4, pady=4)

    def switch_fullscreen_monitor(self, name):
        log.info(f"Switching fullscreen capture to monitor: {name}")
        mon = next((m for m in self.available_monitors if m[0] == name), None)
        if mon is None:
            log.error(f"Monitor '{name}' not found in available list: {self.available_monitors}")
            return
        _, x, y, w, h = mon

        fd, tmp_name = tempfile.mkstemp(suffix=".png", prefix="snippysnappy-full-")
        os.close(fd)
        new_path = Path(tmp_name)
        capture_monitor(x, y, w, h, new_path)

        old_path = self.image_path
        self.image_path = new_path
        self.owned_temp_paths.append(new_path)
        self.img = tk.PhotoImage(file=str(new_path))
        try:
            if old_path and Path(old_path).exists():
                Path(old_path).unlink()
            if old_path in self.owned_temp_paths:
                self.owned_temp_paths.remove(old_path)
        except Exception:
            log.exception("Could not remove previous monitor capture temp file (non-fatal)")

        self.canvas.delete("all")
        self.canvas.config(width=w, height=h)
        self.canvas.create_image(0, 0, anchor="nw", image=self.img)

        win_w = max(w, MIN_WINDOW_WIDTH)
        total_h = h + TOOLBAR_HEIGHT
        cx = x + max((w - win_w) // 2, 0)
        cy = y
        self.root.geometry(f"{win_w}x{total_h}+{cx}+{cy}")

        self.sel_coords = (0, 0, w, h)
        self.baked_image = None
        self.baked_overlay_photo = None
        self.baked_overlay_photo_id = None
        self.edit_strokes = []
        self.ocr_strokes = []
        self.current_monitor_name = name

        log.debug(f"Switched to monitor {name}: {w}x{h} at window {win_w}x{total_h}+{cx}+{cy}")
        self.draw_handles()
        self.show_main_toolbar()

    # ---- working image ----

    def crop_original(self):
        from PIL import Image
        full = Image.open(self.image_path)
        x1, y1, x2, y2 = (int(v) for v in self.sel_coords)
        return full.crop((x1, y1, x2, y2)).convert("RGB")

    def get_working_image(self):
        if self.baked_image is not None:
            return self.baked_image
        return self.crop_original()

    # ---- edit mode ----

    def enter_edit_mode(self):
        log.info("Entering edit mode")
        self.edit_mode_active = True
        self.clear_toolbar()
        self.canvas.unbind("<ButtonPress-1>")
        self.canvas.unbind("<B1-Motion>")
        self.canvas.unbind("<ButtonRelease-1>")
        self.canvas.bind("<ButtonPress-1>", self.edit_press)
        self.canvas.bind("<B1-Motion>", self.edit_drag)
        self.canvas.bind("<ButtonRelease-1>", self.edit_release)
        self.show_edit_toolbar()

    def edit_press(self, event):
        x, y = self.clamp_to_selection(event.x, event.y)
        if self.draw_tool == "text":
            self.text_box_start = (x, y)
            if self.text_current_box_id:
                self.canvas.delete(self.text_current_box_id)
            self.text_current_box_id = self.canvas.create_rectangle(
                x, y, x, y, outline="#00b4ff", width=2, dash=(4, 2),
            )
            return
        width = self.pen_width if self.draw_tool == "pen" else self.highlight_width
        self.edit_current_stroke = {
            "points": [(x, y)], "color": self.draw_color, "mode": self.draw_tool,
            "width": width, "segment_ids": [],
        }

    def _lighten(self, hex_color, factor=0.5):
        r, g, b = hex_to_rgb(hex_color)
        r = int(r + (255 - r) * factor)
        g = int(g + (255 - g) * factor)
        b = int(b + (255 - b) * factor)
        return f"#{r:02x}{g:02x}{b:02x}"

    def _draw_preview_segment(self, lx, ly, x, y, color, tool, width):
        if tool == "pen":
            return self.canvas.create_line(
                lx, ly, x, y, fill=color, width=width,
                capstyle=tk.ROUND, joinstyle=tk.ROUND,
            )
        return self.canvas.create_line(
            lx, ly, x, y, fill=color, width=width,
            capstyle=tk.ROUND, joinstyle=tk.ROUND, stipple="gray50",
        )

    def edit_drag(self, event):
        x, y = self.clamp_to_selection(event.x, event.y)
        if self.draw_tool == "text":
            if self.text_box_start and self.text_current_box_id:
                self.canvas.coords(self.text_current_box_id, self.text_box_start[0], self.text_box_start[1], x, y)
            return
        if not self.edit_current_stroke:
            return
        lx, ly = self.edit_current_stroke["points"][-1]
        if (x - lx) ** 2 + (y - ly) ** 2 < MIN_POINT_DIST ** 2:
            return
        seg_id = self._draw_preview_segment(
            lx, ly, x, y, self.edit_current_stroke["color"],
            self.edit_current_stroke["mode"], self.edit_current_stroke["width"],
        )
        self.edit_current_stroke["segment_ids"].append(seg_id)
        self.edit_current_stroke["points"].append((x, y))

    def edit_release(self, event):
        x, y = self.clamp_to_selection(event.x, event.y)
        if self.draw_tool == "text":
            if not self.text_box_start:
                return
            x1b, y1b = self.text_box_start
            self.text_box_start = None
            bx1, by1, bx2, by2 = min(x1b, x), min(y1b, y), max(x1b, x), max(y1b, y)
            if bx2 - bx1 < 10 or by2 - by1 < 10:
                # too small to be a real drag - treat as an accidental click, discard
                if self.text_current_box_id:
                    self.canvas.delete(self.text_current_box_id)
                    self.text_current_box_id = None
                return
            self._begin_text_entry(bx1, by1, bx2, by2)
            return
        if not self.edit_current_stroke:
            return
        lx, ly = self.edit_current_stroke["points"][-1]
        if (x, y) != (lx, ly):
            seg_id = self._draw_preview_segment(
                lx, ly, x, y, self.edit_current_stroke["color"],
                self.edit_current_stroke["mode"], self.edit_current_stroke["width"],
            )
            self.edit_current_stroke["segment_ids"].append(seg_id)
            self.edit_current_stroke["points"].append((x, y))

        if len(self.edit_current_stroke["points"]) > 1:
            self.edit_strokes.append(self.edit_current_stroke)
            log.debug(f"Edit stroke committed: mode={self.edit_current_stroke['mode']} color={self.edit_current_stroke['color']} width={self.edit_current_stroke['width']} points={len(self.edit_current_stroke['points'])}")
        self.edit_current_stroke = None

    def _begin_text_entry(self, x1, y1, x2, y2):
        """
        Embeds a real Tk Text widget directly on the canvas at the boxed
        area, so you type directly where the text will appear (WYSIWYG-ish)
        rather than through a separate dialog. Enter confirms; Shift+Enter
        inserts a newline (for multi-line text); Escape or clicking away
        cancels. _confirm_text_entry/_cancel_text_entry are real instance
        methods (not local closures) specifically so bake_edits (Done) and
        cancel_edit (Cancel) can call them directly - Done should apply
        whatever's currently being typed rather than leaving it dangling.
        """
        if self.text_current_box_id:
            self.canvas.delete(self.text_current_box_id)
            self.text_current_box_id = None
        text_widget = tk.Text(
            self.canvas, wrap="word", bd=1, relief="solid",
            font=("Serif", max(10, int(self.text_font_size * 0.6))),
        )
        win_id = self.canvas.create_window(
            x1, y1, anchor="nw", width=int(x2 - x1), height=int(y2 - y1), window=text_widget,
        )
        self._pending_text_widget = text_widget
        self._pending_text_window_id = win_id
        self._pending_text_box = (x1, y1, x2, y2)
        text_widget.focus_set()

        text_widget.bind("<Return>", self._confirm_text_entry)
        text_widget.bind("<Shift-Return>", lambda e: None)  # let the Text widget's own default newline-insert behavior happen
        text_widget.bind("<Escape>", self._cancel_text_entry)
        text_widget.bind("<FocusOut>", self._confirm_text_entry)

    def _confirm_text_entry(self, event=None):
        if self._pending_text_widget is None:
            return "break"
        content = self._pending_text_widget.get("1.0", "end-1c")
        x1, y1, x2, y2 = self._pending_text_box
        self.canvas.delete(self._pending_text_window_id)
        self._pending_text_widget.destroy()
        self._pending_text_widget = None
        self._pending_text_window_id = None
        self._pending_text_box = None
        if content.strip():
            stroke = {
                "mode": "text", "box": (x1, y1, x2, y2), "text": content,
                "font_size": self.text_font_size, "color": self.draw_color,
                "bg_fill": self.text_bg_fill, "segment_ids": [],
            }
            self.edit_strokes.append(stroke)
            log.debug(f"Text stroke added: {len(content)} chars, box={stroke['box']}, font_size={stroke['font_size']}, bg_fill={stroke['bg_fill']}")
            self._recompute_baked_image()
        return "break"  # stop Enter from ALSO inserting a newline via the Text widget's default class binding

    def _cancel_text_entry(self, event=None):
        if self._pending_text_widget is None:
            return "break"
        self.canvas.delete(self._pending_text_window_id)
        self._pending_text_widget.destroy()
        self._pending_text_widget = None
        self._pending_text_window_id = None
        self._pending_text_box = None
        return "break"  # critical: stop Escape from ALSO reaching the root-level "close the whole app" binding

    def pick_color(self, color):
        self.draw_color = color
        self.show_edit_toolbar()

    def pick_tool(self, tool):
        self.draw_tool = tool
        self.show_edit_toolbar()

    def set_text_font_size(self, val):
        self.text_font_size = int(float(val))

    def set_text_bg_fill(self, choice):
        self.text_bg_fill = choice
        self.show_edit_toolbar()

    def set_pen_width(self, val):
        self.pen_width = int(float(val))

    def set_highlight_width(self, val):
        self.highlight_width = int(float(val))

    def cancel_edit(self):
        log.info(f"Edit cancelled, discarding {len(self.edit_strokes)} stroke(s)")
        if self._pending_text_widget is not None:
            self._cancel_text_entry()
        if self.text_current_box_id:
            self.canvas.delete(self.text_current_box_id)
            self.text_current_box_id = None
        for stroke in self.edit_strokes:
            for seg_id in stroke["segment_ids"]:
                self.canvas.delete(seg_id)
        self.edit_strokes = []
        self.edit_current_stroke = None
        self.show_main_toolbar()

    def show_edit_toolbar(self):
        self.clear_toolbar()
        frame = self.new_toolbar_frame()
        main_row = tk.Frame(frame, bg="#222222")
        main_row.pack(side="top", fill="x")

        tk.Button(
            main_row, text="Pen",
            relief=("sunken" if self.draw_tool == "pen" else "raised"),
            command=lambda: self.pick_tool("pen"),
        ).pack(side="left", padx=2, pady=4)
        tk.Button(
            main_row, text="Highlighter",
            relief=("sunken" if self.draw_tool == "highlight" else "raised"),
            command=lambda: self.pick_tool("highlight"),
        ).pack(side="left", padx=2, pady=4)
        tk.Button(
            main_row, text="Text",
            relief=("sunken" if self.draw_tool == "text" else "raised"),
            command=lambda: self.pick_tool("text"),
        ).pack(side="left", padx=2, pady=4)

        tk.Button(main_row, text="Resize", command=self.show_resize_options).pack(side="left", padx=(8, 2), pady=4)
        tk.Button(main_row, text="Done", command=self.bake_edits).pack(side="left", padx=(10, 4), pady=4)
        tk.Button(main_row, text="Undo", command=lambda: (self.undo_last_stroke(), self.show_edit_toolbar())).pack(side="left", padx=4, pady=4)
        tk.Button(main_row, text="Cancel", command=self.cancel_edit).pack(side="left", padx=4, pady=4)

        context_row = tk.Frame(frame, bg="#222222")
        context_row.pack(side="top", fill="x")

        for c in PALETTE:
            border = "white" if c == self.draw_color else "#222222"
            tk.Button(
                context_row, bg=c, activebackground=c, width=2, height=1,
                highlightbackground=border, highlightthickness=2,
                command=lambda c=c: self.pick_color(c),
            ).pack(side="left", padx=2, pady=4)

        if self.draw_tool == "pen":
            tk.Label(context_row, text="Width", fg="white", bg="#222222").pack(side="left", padx=(10, 2))
            tk.Scale(
                context_row, from_=1, to=30, orient="horizontal", length=90, showvalue=True,
                bg="#222222", fg="white", troughcolor="#444444", highlightthickness=0,
                variable=tk.IntVar(value=self.pen_width), command=self.set_pen_width,
            ).pack(side="left", padx=(0, 8), pady=4)
        elif self.draw_tool == "highlight":
            tk.Label(context_row, text="Width", fg="white", bg="#222222").pack(side="left", padx=(10, 2))
            tk.Scale(
                context_row, from_=4, to=60, orient="horizontal", length=90, showvalue=True,
                bg="#222222", fg="white", troughcolor="#444444", highlightthickness=0,
                variable=tk.IntVar(value=self.highlight_width), command=self.set_highlight_width,
            ).pack(side="left", padx=(0, 8), pady=4)
        elif self.draw_tool == "text":
            tk.Label(context_row, text="Font size", fg="white", bg="#222222").pack(side="left", padx=(10, 2))
            tk.Scale(
                context_row, from_=10, to=72, orient="horizontal", length=90, showvalue=True,
                bg="#222222", fg="white", troughcolor="#444444", highlightthickness=0,
                variable=tk.IntVar(value=self.text_font_size), command=self.set_text_font_size,
            ).pack(side="left", padx=(0, 12), pady=4)
            tk.Label(context_row, text="Background", fg="white", bg="#222222").pack(side="left", padx=(0, 4))
            for choice, label, swatch, fg in [
                ("transparent", "None", "#666666", "white"),
                ("white", "White", "#ffffff", "black"),
                ("black", "Black", "#000000", "white"),
            ]:
                tk.Button(
                    context_row, text=label, bg=swatch, fg=fg,
                    relief=("sunken" if self.text_bg_fill == choice else "raised"),
                    command=lambda c=choice: self.set_text_bg_fill(c),
                ).pack(side="left", padx=2, pady=4)

        self.finish_toolbar_placement(frame)

    def show_resize_options(self):
        """
        Rescales the whole working image (crop or already-edited bake) by
        a chosen factor via LANCZOS resampling - e.g. blow up a small
        selection for a larger final file. Resets edit/OCR strokes since
        their pixel coordinates wouldn't correspond to the new dimensions.
        """
        self.clear_toolbar()
        frame = self.new_toolbar_frame()
        tk.Label(frame, text="Scale factor:", fg="white", bg="#222222").pack(side="left", padx=(8, 4), pady=4)
        scale_var = tk.DoubleVar(value=2.0)
        tk.Scale(
            frame, from_=1.0, to=4.0, resolution=0.5, orient="horizontal", length=140,
            bg="#222222", fg="white", troughcolor="#444444", highlightthickness=0,
            variable=scale_var,
        ).pack(side="left", padx=(0, 8), pady=4)
        tk.Button(frame, text="Apply", command=lambda: self.apply_resize(scale_var.get())).pack(side="left", padx=4, pady=4)
        tk.Button(frame, text="Cancel", command=self.show_edit_toolbar).pack(side="left", padx=4, pady=4)
        self.finish_toolbar_placement(frame)

    def apply_resize(self, scale_factor):
        from PIL import Image
        log.info(f"Applying resize: scale factor {scale_factor}")
        source = self.get_working_image()
        new_w = max(1, int(source.width * scale_factor))
        new_h = max(1, int(source.height * scale_factor))
        resized = source.resize((new_w, new_h), Image.LANCZOS)

        fd, tmp_name = tempfile.mkstemp(suffix=".png", prefix="snippysnappy-resize-")
        os.close(fd)
        new_path = Path(tmp_name)
        resized.save(new_path)

        if self._pre_resize_snapshot is not None:
            old_snapshot_path = self._pre_resize_snapshot["image_path"]
            if old_snapshot_path in self.owned_temp_paths:
                self.owned_temp_paths.remove(old_snapshot_path)
            try:
                Path(old_snapshot_path).unlink(missing_ok=True)
            except Exception:
                log.exception("Could not remove superseded resize-undo snapshot (non-fatal)")

        self._pre_resize_snapshot = {
            "image_path": self.image_path,
            "sel_coords": self.sel_coords,
            "baked_image": self.baked_image,
            "img_w": self.img.width(),
            "img_h": self.img.height(),
        }

        self.image_path = new_path
        self.owned_temp_paths.append(new_path)

        self.img = tk.PhotoImage(file=str(new_path))
        self.canvas.delete("all")
        self.canvas.config(width=new_w, height=new_h)
        self.canvas.create_image(0, 0, anchor="nw", image=self.img)

        self.sel_coords = (0, 0, new_w, new_h)
        self.baked_image = None
        self.baked_overlay_photo = None
        self.baked_overlay_photo_id = None
        self.edit_strokes = []
        self.ocr_strokes = []

        win_w = max(new_w, MIN_WINDOW_WIDTH)
        total_h = new_h + TOOLBAR_HEIGHT
        x, y = self.root.winfo_x(), self.root.winfo_y()
        self.root.geometry(f"{win_w}x{total_h}+{x}+{y}")

        log.debug(f"Resized working image to {new_w}x{new_h}")
        self.draw_handles()
        self.show_edit_toolbar()

    def _undo_resize(self):
        """
        Reverts the most recent resize. Single-level only - if another
        resize happened since, only the latest one is undoable (the older
        snapshot's temp file was already cleaned up in apply_resize).
        """
        log.info("Undoing resize, reverting to pre-resize image")
        snap = self._pre_resize_snapshot
        self._pre_resize_snapshot = None

        if self.image_path in self.owned_temp_paths:
            self.owned_temp_paths.remove(self.image_path)
        try:
            Path(self.image_path).unlink(missing_ok=True)
        except Exception:
            log.exception("Could not remove post-resize temp file being undone (non-fatal)")

        self.image_path = snap["image_path"]
        self.sel_coords = snap["sel_coords"]
        self.baked_image = snap["baked_image"]
        new_w, new_h = snap["img_w"], snap["img_h"]

        self.img = tk.PhotoImage(file=str(self.image_path))
        self.canvas.delete("all")
        self.canvas.config(width=new_w, height=new_h)
        self.canvas.create_image(0, 0, anchor="nw", image=self.img)
        self.baked_overlay_photo = None
        self.baked_overlay_photo_id = None
        if self.baked_image is not None:
            self._refresh_canvas_with_baked_image()

        win_w = max(new_w, MIN_WINDOW_WIDTH)
        total_h = new_h + TOOLBAR_HEIGHT
        x, y = self.root.winfo_x(), self.root.winfo_y()
        self.root.geometry(f"{win_w}x{total_h}+{x}+{y}")

        log.debug(f"Reverted to pre-resize image: {new_w}x{new_h}")
        self.draw_handles()

    def bake_edits(self):
        if self._pending_text_widget is not None:
            self._confirm_text_entry()
        log.info(f"Baking {len(self.edit_strokes)} edit stroke(s)")
        self._recompute_baked_image()
        self.show_main_toolbar()

    def undo_last_stroke(self):
        """
        Removes the most recent edit action and immediately refreshes the
        visual. Priority order: an open (unconfirmed) text box first, then
        the most recent stroke, then a resize if nothing else is left to
        undo. Does NOT change which toolbar is showing - the Undo button
        (while in edit mode) re-shows the edit toolbar itself afterward;
        Ctrl+U leaves whatever's currently showing alone.
        """
        if self._pending_text_widget is not None:
            log.info("Undo pressed while a text box was open - cancelling it")
            self._cancel_text_entry()
            return
        if not self.edit_strokes:
            if self._pre_resize_snapshot is not None:
                self._undo_resize()
                return
            log.info("Undo pressed with no strokes to undo")
            return
        stroke = self.edit_strokes.pop()
        for seg_id in stroke.get("segment_ids", []):
            self.canvas.delete(seg_id)
        log.info(f"Undid last stroke (mode={stroke.get('mode')}), {len(self.edit_strokes)} remaining")
        self._recompute_baked_image()

    def _draw_text_stroke(self, base_rgba, stroke, offset_x, offset_y):
        from PIL import ImageDraw
        bx1, by1, bx2, by2 = stroke["box"]
        local_box = (bx1 - offset_x, by1 - offset_y, bx2 - offset_x, by2 - offset_y)
        font = load_serif_font(stroke.get("font_size", TEXT_FONT_SIZE_DEFAULT))
        draw = ImageDraw.Draw(base_rgba)
        if stroke.get("bg_fill") in ("white", "black"):
            draw.rectangle(local_box, fill=stroke["bg_fill"])
        draw.multiline_text(
            (local_box[0] + 4, local_box[1] + 2), stroke["text"],
            fill=stroke.get("color", "#000000"), font=font,
        )

    def _recompute_baked_image(self):
        """
        Rebuilds self.baked_image from the original crop + every current
        edit_strokes entry, and refreshes the on-screen visual to match.
        Shared by bake_edits (Done) and undo_last_stroke, so undo shows its
        effect immediately rather than only after the next Done.
        """
        from PIL import Image, ImageDraw, ImageFont
        x1, y1, _, _ = (int(v) for v in self.sel_coords)
        base = self.crop_original().convert("RGBA")

        for stroke in self.edit_strokes:
            if stroke.get("mode") == "text":
                self._draw_text_stroke(base, stroke, x1, y1)
                continue
            pts = [(px - x1, py - y1) for px, py in stroke["points"]]
            width = stroke.get("width", EDIT_PEN_WIDTH if stroke["mode"] == "pen" else EDIT_HIGHLIGHT_WIDTH)
            if stroke["mode"] == "pen":
                draw = ImageDraw.Draw(base)
                if len(pts) == 1:
                    px, py = pts[0]
                    r = width / 2
                    draw.ellipse([px - r, py - r, px + r, py + r], fill=stroke["color"])
                else:
                    draw.line(pts, fill=stroke["color"], width=width, joint="curve")
            else:
                overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
                odraw = ImageDraw.Draw(overlay)
                rgba = hex_to_rgb(stroke["color"]) + (EDIT_HIGHLIGHT_ALPHA,)
                if len(pts) == 1:
                    px, py = pts[0]
                    r = width / 2
                    odraw.ellipse([px - r, py - r, px + r, py + r], fill=rgba)
                else:
                    odraw.line(pts, fill=rgba, width=width, joint="curve")
                base = Image.alpha_composite(base, overlay)

        self.baked_image = base.convert("RGB")
        log.debug("Edits baked into working image")
        self._refresh_canvas_with_baked_image()

    def _refresh_canvas_with_baked_image(self):
        """
        Update what's on screen to match the true baked (translucent) result -
        until now the canvas only ever showed the solid live-preview lines, which
        never visually updated to reflect the real blended colors after Done.
        """
        x1, y1, x2, y2 = (int(v) for v in self.sel_coords)
        for stroke in self.edit_strokes:
            for seg_id in stroke.get("segment_ids", []):
                self.canvas.delete(seg_id)
        if getattr(self, "baked_overlay_photo_id", None) is not None:
            self.canvas.delete(self.baked_overlay_photo_id)
        fd, tmp_name = tempfile.mkstemp(suffix=".png", prefix="snippysnappy-preview-")
        os.close(fd)
        preview_path = Path(tmp_name)
        self.baked_image.save(preview_path)
        self.baked_overlay_photo = tk.PhotoImage(file=str(preview_path))
        preview_path.unlink(missing_ok=True)
        self.baked_overlay_photo_id = self.canvas.create_image(
            x1, y1, anchor="nw", image=self.baked_overlay_photo,
        )

    # ---- OCR paint mode ----

    def enter_measure_mode(self):
        """
        'Dry run' selection boxes for measuring parts of the image
        without changing the real selection/crop at all. Supports multiple
        simultaneous boxes (each its own color, with corner handles to
        reposition/resize it, not just create new ones), each with its own
        live dimension readout and individual clear button. Escape (see
        _handle_escape) or Done exits back to the main toolbar; the real
        selection and everything else is completely untouched throughout.
        """
        log.info("Entering measure mode")
        self.measure_mode_active = True
        self.measure_boxes = []
        self.measure_active_box = None
        self.measure_active_handle = None
        self.measure_creating = False
        self.measure_start = None
        self.canvas.unbind("<ButtonPress-1>")
        self.canvas.unbind("<B1-Motion>")
        self.canvas.unbind("<ButtonRelease-1>")
        self.canvas.bind("<ButtonPress-1>", self._measure_press)
        self.canvas.bind("<B1-Motion>", self._measure_drag)
        self.canvas.bind("<ButtonRelease-1>", self._measure_release)
        self.show_measure_toolbar()

    def _hit_measure_handle(self, x, y):
        for i, box in enumerate(self.measure_boxes):
            for hid, name in box["handle_ids"]:
                hx = self.canvas.coords(hid)[0] + HANDLE_SIZE
                hy = self.canvas.coords(hid)[1] + HANDLE_SIZE
                if abs(x - hx) <= HANDLE_SIZE * 1.5 and abs(y - hy) <= HANDLE_SIZE * 1.5:
                    return i, name
        return None, None

    def _measure_press(self, event):
        box_idx, handle_name = self._hit_measure_handle(event.x, event.y)
        if box_idx is not None:
            self.measure_active_box = box_idx
            self.measure_active_handle = handle_name
            self.measure_creating = False
            return
        color = MEASURE_COLORS[len(self.measure_boxes) % len(MEASURE_COLORS)]
        rect_id = self.canvas.create_rectangle(
            event.x, event.y, event.x, event.y, outline=color, width=2, dash=(4, 2),
        )
        box = {"coords": (event.x, event.y, event.x, event.y), "color": color, "rect_id": rect_id, "handle_ids": [], "label": None}
        self.measure_boxes.append(box)
        self.measure_active_box = len(self.measure_boxes) - 1
        self.measure_active_handle = None
        self.measure_creating = True
        self.measure_start = (event.x, event.y)
        self.show_measure_toolbar()

    def _measure_drag(self, event):
        if self.measure_active_box is None:
            return
        box = self.measure_boxes[self.measure_active_box]
        if self.measure_creating:
            x1, y1 = self.measure_start
            x2, y2 = event.x, event.y
        else:
            x1, y1, x2, y2 = box["coords"]
            name = self.measure_active_handle
            if name == "nw":
                x1, y1 = event.x, event.y
            elif name == "ne":
                x2, y1 = event.x, event.y
            elif name == "sw":
                x1, y2 = event.x, event.y
            elif name == "se":
                x2, y2 = event.x, event.y
        box["coords"] = (x1, y1, x2, y2)
        self.canvas.coords(box["rect_id"], x1, y1, x2, y2)
        self._draw_measure_handles(self.measure_active_box)
        if box.get("label") is not None:
            w, h = int(abs(x2 - x1)), int(abs(y2 - y1))
            try:
                box["label"].config(text=f"{w} x {h}")
            except tk.TclError:
                pass

    def _draw_measure_handles(self, idx):
        box = self.measure_boxes[idx]
        for hid, _ in box["handle_ids"]:
            self.canvas.delete(hid)
        box["handle_ids"] = []
        x1, y1, x2, y2 = box["coords"]
        corners = {"nw": (x1, y1), "ne": (x2, y1), "sw": (x1, y2), "se": (x2, y2)}
        for name, (hx, hy) in corners.items():
            hid = self.canvas.create_rectangle(
                hx - HANDLE_SIZE, hy - HANDLE_SIZE, hx + HANDLE_SIZE, hy + HANDLE_SIZE,
                fill=box["color"], outline="white",
            )
            box["handle_ids"].append((hid, name))

    def _measure_release(self, event):
        self.measure_active_box = None
        self.measure_active_handle = None
        self.measure_creating = False

    def clear_measure_box(self, idx):
        if idx >= len(self.measure_boxes):
            return
        box = self.measure_boxes.pop(idx)
        self.canvas.delete(box["rect_id"])
        for hid, _ in box["handle_ids"]:
            self.canvas.delete(hid)
        self.show_measure_toolbar()

    def clear_all_measure_boxes(self):
        for box in self.measure_boxes:
            self.canvas.delete(box["rect_id"])
            for hid, _ in box["handle_ids"]:
                self.canvas.delete(hid)
        self.measure_boxes = []
        self.show_measure_toolbar()

    def exit_measure_mode(self):
        log.info("Exiting measure mode")
        self.measure_mode_active = False
        for box in self.measure_boxes:
            self.canvas.delete(box["rect_id"])
            for hid, _ in box["handle_ids"]:
                self.canvas.delete(hid)
        self.measure_boxes = []
        self.measure_active_box = None
        self.measure_active_handle = None
        self.show_main_toolbar()

    def show_measure_toolbar(self):
        self.clear_toolbar()
        frame = self.new_toolbar_frame()
        header_row = tk.Frame(frame, bg="#222222")
        header_row.pack(side="top", fill="x")
        tk.Label(
            header_row, text="Measure mode - drag to create/resize boxes, doesn't change your selection",
            fg="white", bg="#222222",
        ).pack(side="left", padx=8)
        tk.Button(header_row, text="Clear", command=self.clear_all_measure_boxes).pack(side="left", padx=4, pady=4)
        tk.Button(header_row, text="Done", command=self.exit_measure_mode).pack(side="left", padx=4, pady=4)

        if self.measure_boxes:
            boxes_row = tk.Frame(frame, bg="#222222")
            boxes_row.pack(side="top", fill="x")
            for i, box in enumerate(self.measure_boxes):
                x1, y1, x2, y2 = box["coords"]
                w, h = int(abs(x2 - x1)), int(abs(y2 - y1))
                lbl = tk.Label(boxes_row, text=f"{w} x {h}", fg=box["color"], bg="#222222")
                lbl.pack(side="left", padx=(8, 2))
                box["label"] = lbl
                tk.Button(
                    boxes_row, text="\u00d7", width=2,
                    command=lambda i=i: self.clear_measure_box(i),
                ).pack(side="left", padx=(0, 8))

        self.finish_toolbar_placement(frame)

    def enter_magnify_mode(self):
        """
        A lens that follows the cursor showing a magnified view of the
        image underneath, without altering anything. The lens is a true
        circle (via a PIL alpha mask, not just a square crop) and the real
        system cursor is hidden while over the canvas - a small translucent
        circle we draw ourselves takes its place, layered ON TOP of the lens
        (created after it) and at its true unmagnified size/position, so it
        never gets scaled by the lens itself. Reverts to the normal cursor
        the moment you leave the canvas (e.g. back to the toolbar).
        """
        log.info("Entering magnify mode")
        self.magnify_mode_active = True
        self.magnify_lens_item_id = None
        self.magnify_cursor_item_id = None
        self.magnify_source_image = self.get_working_image()
        self.canvas.unbind("<ButtonPress-1>")
        self.canvas.unbind("<B1-Motion>")
        self.canvas.unbind("<ButtonRelease-1>")
        self.canvas.bind("<Motion>", self._magnify_motion)
        self.canvas.bind("<Leave>", self._magnify_leave)
        self.canvas.configure(cursor="none")
        self.show_magnify_toolbar()

    def exit_magnify_mode(self):
        log.info("Exiting magnify mode")
        self.magnify_mode_active = False
        self._magnify_leave(None)
        self.canvas.unbind("<Motion>")
        self.canvas.unbind("<Leave>")
        self.canvas.configure(cursor="crosshair")
        self.magnify_source_image = None
        self.show_main_toolbar()

    def _magnify_leave(self, event):
        if self.magnify_lens_item_id:
            self.canvas.delete(self.magnify_lens_item_id)
            self.magnify_lens_item_id = None
        if self.magnify_cursor_item_id:
            self.canvas.delete(self.magnify_cursor_item_id)
            self.magnify_cursor_item_id = None

    def _magnify_motion(self, event):
        # Throttled regardless of backend - bounds I/O in the ImageTk-unavailable fallback path to a sane rate.
        now = time.monotonic()
        last = getattr(self, "_magnify_last_update", 0)
        if now - last < 0.03:  # ~30fps ceiling
            return
        self._magnify_last_update = now
        self._draw_magnifier(event.x, event.y)

    def _draw_magnifier(self, cx, cy):
        from PIL import Image, ImageDraw
        if self.magnify_source_image is None:
            return
        src = self.magnify_source_image
        lens_size = self.magnify_lens_size
        level = max(1, self.magnify_level)
        src_radius = max(1, int((lens_size / 2) / level))
        left = max(0, cx - src_radius)
        top = max(0, cy - src_radius)
        right = min(src.width, cx + src_radius)
        bottom = min(src.height, cy + src_radius)
        if right <= left or bottom <= top:
            return
        crop = src.crop((left, top, right, bottom))
        out_w = max(1, int((right - left) * level))
        out_h = max(1, int((bottom - top) * level))
        scaled = crop.resize((out_w, out_h), Image.NEAREST)

        mask = Image.new("L", scaled.size, 0)
        ImageDraw.Draw(mask).ellipse([0, 0, scaled.size[0] - 1, scaled.size[1] - 1], fill=255)
        rgba = scaled.convert("RGBA")
        rgba.putalpha(mask)

        photo = self._pil_to_tk(rgba)
        if photo is None:
            return
        self._magnify_current_photo = photo

        if self.magnify_lens_item_id:
            self.canvas.delete(self.magnify_lens_item_id)
        lens_x = cx - scaled.size[0] // 2
        lens_y = cy - scaled.size[1] // 2
        self.magnify_lens_item_id = self.canvas.create_image(lens_x, lens_y, anchor="nw", image=photo)

        if self.magnify_cursor_item_id:
            self.canvas.delete(self.magnify_cursor_item_id)
        r = 6
        self.magnify_cursor_item_id = self.canvas.create_oval(
            cx - r, cy - r, cx + r, cy + r, outline="white", width=2, stipple="gray50",
        )

    def _pil_to_tk(self, pil_image):
        """
        Prefers PIL.ImageTk (fast, in-memory) for the frequent updates
        this tool needs; falls back to a temp-file round-trip (tk.PhotoImage
        reads PNG, including alpha, natively) if ImageTk isn't available -
        e.g. Pillow built without the 'tk' USE flag. The fallback works but
        is slower due to real disk I/O every frame; logged once so it's
        clear which path is active and how to get the faster one.
        """
        if not hasattr(self, "_imagetk_checked"):
            self._imagetk_checked = True
            try:
                from PIL import ImageTk
                self._imagetk_available = True
            except ImportError:
                self._imagetk_available = False
                log.warning(
                    "PIL.ImageTk unavailable (Pillow likely built without the 'tk' "
                    "USE flag) - Magnify will use a slower temp-file fallback instead. "
                    "For smoother tracking on Gentoo: echo 'dev-python/pillow tk' | sudo tee "
                    "/etc/portage/package.use/pillow && sudo emerge --newuse -av dev-python/pillow"
                )
        if self._imagetk_available:
            from PIL import ImageTk
            return ImageTk.PhotoImage(pil_image)
        fd, tmp_name = tempfile.mkstemp(suffix=".png", prefix="snippysnappy-lens-")
        os.close(fd)
        tmp_path = Path(tmp_name)
        try:
            pil_image.save(tmp_path)
            return tk.PhotoImage(file=str(tmp_path))
        except Exception:
            log.exception("Magnifier lens rendering failed (non-fatal, skipping this frame)")
            return None
        finally:
            tmp_path.unlink(missing_ok=True)

    def set_magnify_lens_size(self, val):
        self.magnify_lens_size = int(float(val))

    def set_magnify_level(self, val):
        self.magnify_level = int(float(val))

    def show_magnify_toolbar(self):
        self.clear_toolbar()
        frame = self.new_toolbar_frame()
        tk.Label(frame, text="Magnify", fg="white", bg="#222222").pack(side="left", padx=8)
        tk.Label(frame, text="Lens size", fg="white", bg="#222222").pack(side="left", padx=(8, 2))
        tk.Scale(
            frame, from_=40, to=300, orient="horizontal", length=90, showvalue=False,
            bg="#222222", fg="white", troughcolor="#444444", highlightthickness=0,
            variable=tk.IntVar(value=self.magnify_lens_size), command=self.set_magnify_lens_size,
        ).pack(side="left", padx=(0, 8), pady=4)
        tk.Label(frame, text="Zoom", fg="white", bg="#222222").pack(side="left", padx=(0, 2))
        tk.Scale(
            frame, from_=2, to=16, orient="horizontal", length=90, showvalue=True,
            bg="#222222", fg="white", troughcolor="#444444", highlightthickness=0,
            variable=tk.IntVar(value=self.magnify_level), command=self.set_magnify_level,
        ).pack(side="left", padx=(0, 8), pady=4)
        tk.Button(frame, text="Done", command=self.exit_magnify_mode).pack(side="left", padx=4, pady=4)
        self.finish_toolbar_placement(frame)

    def enter_ocr_paint_mode(self):
        log.info("Entering OCR paint mode")
        self.ocr_paint_mode_active = True
        self.ocr_strokes = []
        self.ocr_current_stroke = None
        self.clear_toolbar()
        self.canvas.unbind("<ButtonPress-1>")
        self.canvas.unbind("<B1-Motion>")
        self.canvas.unbind("<ButtonRelease-1>")
        self.canvas.bind("<ButtonPress-1>", self.ocr_paint_press)
        self.canvas.bind("<B1-Motion>", self.ocr_paint_drag)
        self.canvas.bind("<ButtonRelease-1>", self.ocr_paint_release)
        self.show_ocr_toolbar()

    def ocr_paint_press(self, event):
        x, y = self.clamp_to_selection(event.x, event.y)
        log.debug(f"OCR paint press at ({x},{y})")
        self.ocr_current_stroke = {"points": [(x, y)], "width": self.ocr_brush_width, "segment_ids": []}

    def ocr_paint_drag(self, event):
        if not self.ocr_current_stroke:
            log.debug("OCR paint drag fired with no active stroke")
            return
        x, y = self.clamp_to_selection(event.x, event.y)
        lx, ly = self.ocr_current_stroke["points"][-1]
        if (x - lx) ** 2 + (y - ly) ** 2 < MIN_POINT_DIST ** 2:
            return
        seg_id = self.canvas.create_line(
            lx, ly, x, y, fill="yellow", width=self.ocr_current_stroke["width"],
            capstyle=tk.ROUND, joinstyle=tk.ROUND, stipple="gray50",
        )
        self.ocr_current_stroke["segment_ids"].append(seg_id)
        self.ocr_current_stroke["points"].append((x, y))

    def ocr_paint_release(self, event):
        if not self.ocr_current_stroke:
            return
        x, y = self.clamp_to_selection(event.x, event.y)
        lx, ly = self.ocr_current_stroke["points"][-1]
        if (x, y) != (lx, ly):
            seg_id = self.canvas.create_line(
                lx, ly, x, y, fill="yellow", width=self.ocr_current_stroke["width"],
                capstyle=tk.ROUND, joinstyle=tk.ROUND, stipple="gray50",
            )
            self.ocr_current_stroke["segment_ids"].append(seg_id)
            self.ocr_current_stroke["points"].append((x, y))

        if len(self.ocr_current_stroke["points"]) > 1:
            self.ocr_strokes.append(self.ocr_current_stroke)
            log.debug(f"OCR paint stroke committed: {len(self.ocr_current_stroke['points'])} points, width={self.ocr_current_stroke['width']}")
        self.ocr_current_stroke = None

    def set_ocr_brush_width(self, val):
        self.ocr_brush_width = int(float(val))

    def cancel_ocr_paint(self):
        log.info(f"OCR paint cancelled, discarding {len(self.ocr_strokes)} stroke(s)")
        for stroke in self.ocr_strokes:
            for seg_id in stroke["segment_ids"]:
                self.canvas.delete(seg_id)
        self.ocr_strokes = []
        self.ocr_current_stroke = None
        self.canvas.unbind("<ButtonPress-1>")
        self.canvas.unbind("<B1-Motion>")
        self.canvas.unbind("<ButtonRelease-1>")
        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.show_main_toolbar()

    def show_ocr_toolbar(self):
        self.clear_toolbar()
        frame = self.new_toolbar_frame()
        tk.Label(
            frame, text="Paint to OCR (optional)",
            fg="white", bg="#222222",
        ).pack(side="left", padx=6)
        tk.Scale(
            frame, from_=4, to=60, orient="horizontal", length=80, showvalue=False,
            bg="#222222", fg="white", troughcolor="#444444", highlightthickness=0,
            variable=tk.IntVar(value=self.ocr_brush_width), command=self.set_ocr_brush_width,
        ).pack(side="left", padx=(0, 10), pady=4)
        tk.Button(frame, text="Run OCR", command=self.run_ocr_with_mask).pack(side="left", padx=4, pady=4)
        tk.Button(frame, text="Undo", command=self.undo_last_ocr_stroke).pack(side="left", padx=4, pady=4)
        tk.Button(frame, text="Cancel", command=self.cancel_ocr_paint).pack(side="left", padx=4, pady=4)
        self.finish_toolbar_placement(frame)

    def undo_last_ocr_stroke(self):
        if not self.ocr_strokes:
            log.info("Undo pressed with no OCR paint strokes to undo")
            return
        stroke = self.ocr_strokes.pop()
        for seg_id in stroke.get("segment_ids", []):
            self.canvas.delete(seg_id)
        log.info(f"Undid last OCR stroke, {len(self.ocr_strokes)} remaining")
        self.show_ocr_toolbar()

    def run_ocr_with_mask(self):
        self.ocr_paint_mode_active = False
        log.info(f"Running OCR ({'masked' if self.ocr_strokes else 'whole selection'}, {len(self.ocr_strokes)} stroke(s))")
        from PIL import Image, ImageDraw
        x1, y1, _, _ = (int(v) for v in self.sel_coords)
        target = self.get_working_image()

        if self.ocr_strokes:
            mask = Image.new("L", target.size, 0)
            draw = ImageDraw.Draw(mask)
            for stroke in self.ocr_strokes:
                pts = [(px - x1, py - y1) for px, py in stroke["points"]]
                width = stroke.get("width", OCR_BRUSH_WIDTH)
                r = max(width / 2, OCR_MASK_MIN_PADDING)
                xs = [p[0] for p in pts]
                ys = [p[1] for p in pts]
                box = [min(xs) - r, min(ys) - r, max(xs) + r, max(ys) + r]
                draw.rectangle(box, fill=255)
            white_bg = Image.new("RGB", target.size, "white")
            target = Image.composite(target, white_bg, mask)

        target = preprocess_for_ocr(target)

        fd, tmp_name = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        tmp_path = Path(tmp_name)
        target.save(tmp_path)
        text = ocr_image(tmp_path)
        tmp_path.unlink(missing_ok=True)
        log.info(f"OCR result ready ({len(text)} chars), showing review screen")
        self.show_ocr_result(text)

    def show_ocr_result(self, text):
        """
        Review screen: shows the extracted text, lets you copy/save it, or go
        back to redo the paint/selection without losing the capture.

        Rendered as an overlay Frame inside the SAME window rather than a
        separate Toplevel.
        """
        self.clear_toolbar()
        overlay = tk.Frame(self.root, bg="#1a1a1a")
        overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._ocr_overlay = overlay

        tk.Label(
            overlay, text="Extracted Text", fg="white", bg="#1a1a1a",
            font=("", 12, "bold"),
        ).pack(anchor="w", padx=16, pady=(16, 4))

        text_widget = tk.Text(overlay, wrap="word")
        text_widget.insert("1.0", text if text else "(No text detected.)")
        text_widget.pack(fill="both", expand=True, padx=16, pady=(0, 8))
        self._ocr_text_widget = text_widget

        self._ocr_copy_var = tk.IntVar(value=int(DEFAULT_COPY_ON_SAVE))

        self._ocr_btn_frame = tk.Frame(overlay, bg="#1a1a1a")
        self._ocr_btn_frame.pack(fill="x", padx=16, pady=16)
        self._show_ocr_main_buttons()

    def _show_ocr_main_buttons(self):
        for w in self._ocr_btn_frame.winfo_children():
            w.destroy()
        tk.Button(
            self._ocr_btn_frame, text="Copy Text",
            command=lambda: self.do_copy_text(self._ocr_overlay, self._ocr_text_widget),
        ).pack(side="left", padx=4)
        tk.Button(
            self._ocr_btn_frame, text="Save Text",
            command=self.show_text_save_options,
        ).pack(side="left", padx=4)
        tk.Button(
            self._ocr_btn_frame, text="Back",
            command=lambda: (log.info("OCR result: Back pressed, returning to paint"), self._ocr_overlay.destroy(), self.show_ocr_toolbar()),
        ).pack(side="left", padx=4)
        tk.Button(
            self._ocr_btn_frame, text="Close",
            command=lambda: (log.info("OCR result: Close pressed"), self.root.destroy()),
        ).pack(side="left", padx=4)

    def show_text_save_options(self):
        """
        Mirrors show_save_options for images: Normal Save (prompts for a
        filename, into SNIPTEXT_DIR) or Slop Save (no prompt, into TEXT_DIR).
        """
        for w in self._ocr_btn_frame.winfo_children():
            w.destroy()
        check_row = tk.Frame(self._ocr_btn_frame, bg="#1a1a1a")
        check_row.pack(side="top", fill="x")
        tk.Checkbutton(
            check_row, text="Also copy", variable=self._ocr_copy_var,
            bg="#1a1a1a", fg="white", selectcolor="#444444",
            activebackground="#1a1a1a", activeforeground="white",
        ).pack(side="left")
        btn_row = tk.Frame(self._ocr_btn_frame, bg="#1a1a1a")
        btn_row.pack(side="top", fill="x", pady=(4, 0))
        tk.Button(btn_row, text="Normal Save", command=self.begin_normal_save_text).pack(side="left", padx=4)
        tk.Button(btn_row, text="Slop Save", command=self.do_slop_save_text).pack(side="left", padx=4)
        tk.Button(btn_row, text="Back", command=self._show_ocr_main_buttons).pack(side="left", padx=4)

    def begin_normal_save_text(self):
        for w in self._ocr_btn_frame.winfo_children():
            w.destroy()
        name_var = tk.StringVar(value=f"snippytext-{timestamp()}")
        entry = tk.Entry(self._ocr_btn_frame, textvariable=name_var, width=28)
        entry.pack(side="left", padx=4)
        entry.focus_set()
        tk.Button(
            self._ocr_btn_frame, text="Confirm",
            command=lambda: self.confirm_normal_save_text(name_var.get()),
        ).pack(side="left", padx=4)
        tk.Button(self._ocr_btn_frame, text="Cancel", command=self.show_text_save_options).pack(side="left", padx=4)

    def confirm_normal_save_text(self, name):
        content = self._ocr_text_widget.get("1.0", "end-1c")
        name = name.strip() or f"snippytext-{timestamp()}"
        if not name.lower().endswith(".txt"):
            name += ".txt"
        SNIPTEXT_DIR.mkdir(parents=True, exist_ok=True)
        path = SNIPTEXT_DIR / name
        path.write_text(content, encoding="utf-8")
        log.info(f"Saved OCR text -> {path} ({len(content)} chars)")
        also_copy = bool(self._ocr_copy_var.get())
        self._ocr_overlay.destroy()
        if also_copy:
            copy_text_to_clipboard(content)
            notify("Text copied to clipboard")
        notify("Text saved", f"Saved as {path.name}")
        if OCR_RETURN_TO_TOOLBAR_AFTER_ACTION:
            self.show_main_toolbar()
        else:
            self.root.destroy()

    def do_slop_save_text(self):
        content = self._ocr_text_widget.get("1.0", "end-1c")
        TEXT_DIR.mkdir(parents=True, exist_ok=True)
        path = TEXT_DIR / f"slopsnaptext-{timestamp()}.txt"
        path.write_text(content, encoding="utf-8")
        log.info(f"Slop-saved OCR text -> {path} ({len(content)} chars)")
        also_copy = bool(self._ocr_copy_var.get())
        self._ocr_overlay.destroy()
        if also_copy:
            copy_text_to_clipboard(content)
            notify("Text copied to clipboard")
        notify("Slop text saved", f"Saved as {path.name}")
        if OCR_RETURN_TO_TOOLBAR_AFTER_ACTION:
            self.show_main_toolbar()
        else:
            self.root.destroy()

    def do_copy_text(self, overlay, text_widget):
        content = text_widget.get("1.0", "end-1c")
        log.info(f"Copy Text pressed ({len(content)} chars)")
        copy_text_to_clipboard(content)
        notify("Text copied to clipboard")
        overlay.destroy()
        if OCR_RETURN_TO_TOOLBAR_AFTER_ACTION:
            self.show_main_toolbar()
        else:
            self.root.destroy()

    # ---- save ----

    def show_save_options(self):
        self.clear_toolbar()
        frame = self.new_toolbar_frame()
        check_row = tk.Frame(frame, bg="#222222")
        check_row.pack(side="top", fill="x")
        tk.Checkbutton(
            check_row, text="Also copy", variable=self.copy_on_save_var,
            bg="#222222", fg="white", selectcolor="#444444",
            activebackground="#222222", activeforeground="white",
        ).pack(side="left", padx=8, pady=(4, 0))
        btn_row = tk.Frame(frame, bg="#222222")
        btn_row.pack(side="top", fill="x")
        tk.Button(btn_row, text="Normal Save", command=self.begin_normal_save).pack(side="left", padx=4, pady=4)
        tk.Button(btn_row, text="Slop Save", command=self.do_slop_save).pack(side="left", padx=4, pady=4)
        tk.Button(btn_row, text="Back", command=self.show_main_toolbar).pack(side="left", padx=4, pady=4)
        self.finish_toolbar_placement(frame)

    def begin_normal_save(self):
        self.clear_toolbar()
        frame = self.new_toolbar_frame()
        name_var = tk.StringVar(value=f"snipsnap-{timestamp()}")
        entry = tk.Entry(frame, textvariable=name_var, width=28)
        entry.pack(side="left", padx=4, pady=4)
        entry.focus_set()
        tk.Button(
            frame, text="Confirm",
            command=lambda: self.confirm_normal_save(name_var.get()),
        ).pack(side="left", padx=4, pady=4)
        tk.Button(frame, text="Cancel", command=self.show_main_toolbar).pack(side="left", padx=4, pady=4)
        self.finish_toolbar_placement(frame)

    def confirm_normal_save(self, name):
        name = name.strip() or f"snipsnap-{timestamp()}"
        if not name.lower().endswith(".png"):
            name += ".png"
        path = SCREENSHOT_DIR / name
        log.info(f"Normal Save -> {path}")
        img = self.get_working_image()
        img.save(path)
        also_copy = bool(self.copy_on_save_var.get())
        log.debug(f"Destroying window before post-save actions (also_copy={also_copy})")
        self.root.destroy()
        if also_copy:
            copy_image_to_clipboard(path)
            notify("Capture copied to clipboard")
        notify("Screenshot saved", f"Saved as {path.name}")

    def do_slop_save(self):
        SLOP_DIR.mkdir(parents=True, exist_ok=True)
        path = SLOP_DIR / f"slopsnap-{timestamp()}.png"
        log.info(f"Slop Save -> {path}")
        img = self.get_working_image()
        img.save(path)
        also_copy = bool(self.copy_on_save_var.get())
        log.debug(f"Destroying window before post-save actions (also_copy={also_copy})")
        self.root.destroy()
        if also_copy:
            copy_image_to_clipboard(path)
            notify("Capture copied to clipboard")
        notify("Slop screenshot saved", f"Saved as {path.name}")

    # ---- copy ----

    def do_copy(self):
        log.info("Copy pressed")
        img = self.get_working_image()
        fd, tmp_name = tempfile.mkstemp(suffix=".png", prefix="snippysnappy-")
        os.close(fd)
        tmp_path = Path(tmp_name)
        img.save(tmp_path)
        log.debug("Destroying window before clipboard write")
        self.root.destroy()
        copy_image_to_clipboard(tmp_path)
        notify("Capture copied to clipboard")


def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--window", action="store_true",
        help="Capture a single window (click to select); capture adjustable",
    )
    group.add_argument(
        "--fullscreen", action="store_true",
        help="Capture the whole monitor; still adjustable via handles",
    )
    args = parser.parse_args()
    log.info(f"snippysnappy starting, args: window={args.window} fullscreen={args.fullscreen}")
    log.info(f"Backend: {BACKEND}")
    log.debug(
        "Environment: DISPLAY=%r DBUS_SESSION_BUS_ADDRESS=%r XDG_RUNTIME_DIR=%r XDG_CURRENT_DESKTOP=%r"
        % (
            os.environ.get("DISPLAY"),
            os.environ.get("DBUS_SESSION_BUS_ADDRESS"),
            os.environ.get("XDG_RUNTIME_DIR"),
            os.environ.get("XDG_CURRENT_DESKTOP"),
        )
    )
    ensure_dbus_env()

    with tempfile.TemporaryDirectory() as tmp:
        full_path = Path(tmp) / "full.png"
        root = tk.Tk()
        root.withdraw()

        def log_callback_exception(exc, val, tb):
            log.error("Unhandled exception inside a Tkinter callback (button/binding):", exc_info=(exc, val, tb))
        root.report_callback_exception = log_callback_exception

        if args.window:
            if BACKEND == "wayland":
                x, y, w, h = pick_region_interactive_wayland()
                capture_monitor(x, y, w, h, full_path)  # -g geometry capture, not window-id based
                notify("Capture taken", allow_toast=False)
                monitors = get_monitors()
                mon = monitor_at_point(monitors, x, y)
            else:
                win_id = select_window_id()
                x, y, w, h = get_window_geometry(win_id)
                capture_window(win_id, full_path)
                notify("Capture taken", allow_toast=False)
                monitors = get_monitors()
                mon = monitor_at_point(monitors, x, y)
            _, mx, my, mw, mh = mon
            root.deiconify()
            SelectorApp(
                root, full_path, mode="window", bounds=(mx, my, mw, mh), auto_select_full=True,
                monitors=monitors, capture_tempdir=tmp,
            )
        elif args.fullscreen:
            if BACKEND == "wayland":
                name, x, y, w, h = pick_output_interactive_wayland()
                capture_monitor(x, y, w, h, full_path)
                notify("Capture taken", allow_toast=False)
                monitors = get_monitors()
            else:
                mouse_x, mouse_y = get_mouse_position()
                monitors = get_monitors()
                name, x, y, w, h = monitor_at_point(monitors, mouse_x, mouse_y)
                capture_monitor(x, y, w, h, full_path)
                notify("Capture taken", allow_toast=False)
            root.deiconify()
            SelectorApp(
                root, full_path, mode="fullscreen", bounds=(x, y, w, h), auto_select_full=True,
                monitors=monitors, capture_tempdir=tmp, current_monitor_name=name,
            )
        else:
            monitors = get_monitors()
            x, y, w, h = get_virtual_screen_bounds(monitors)
            log.info(f"Select mode: spanning full virtual screen {w}x{h}+{x}+{y} across {len(monitors)} monitor(s)")
            capture_full_screen(full_path)
            notify("Capture taken", allow_toast=False)
            root.deiconify()
            SelectorApp(root, full_path, mode="monitor", bounds=(x, y, w, h), capture_tempdir=tmp)

        log.debug("Entering Tk mainloop")
        root.mainloop()
        log.info("Tk mainloop exited, script ending")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("Unhandled exception, snippysnappy is exiting")
        raise

SNIPPYSNAPPY_PYEOF

    if [[ -f "$SNIPPYSNAPPY_PY" ]]; then
        if diff -q "$tmp_new" "$SNIPPYSNAPPY_PY" &>/dev/null; then
            log "$SNIPPYSNAPPY_PY already up to date - skipping."
            record_step "SKIPPED" "$SNIPPYSNAPPY_PY (already up to date)"
            rm -f "$tmp_new"
            return 0
        fi
        if ! confirm_destructive "$SNIPPYSNAPPY_PY already exists and differs from this installer's version. Overwrite it? (a .bak copy will be kept)"; then
            warn "Leaving existing $SNIPPYSNAPPY_PY in place - it will NOT match this installer's version."
            record_step "SKIPPED" "$SNIPPYSNAPPY_PY (user declined overwrite)"
            rm -f "$tmp_new"
            return 0
        fi
        cp "$SNIPPYSNAPPY_PY" "$SNIPPYSNAPPY_PY.bak"
        log "Backed up existing file to $SNIPPYSNAPPY_PY.bak"
    fi
    mv "$tmp_new" "$SNIPPYSNAPPY_PY"
    chown "$TARGET_USER:$TARGET_USER" "$SNIPPYSNAPPY_PY"
    run_as_user "chmod +x '$SNIPPYSNAPPY_PY'"
}

run_step "Write snippysnappy.py" write_snippysnappy_py

write_wrapper() {
    local wrapper_content
    wrapper_content=$'#!/bin/bash\nexec python3 "$HOME/.local/bin/snippysnappy.d/snippysnappy.py" "$@"\n'
    if run_as_user "[ -f '$WRAPPER_PATH' ]"; then
        local existing
        existing=$(run_as_user "cat '$WRAPPER_PATH'")
        if [[ "$existing" == "${wrapper_content%$'\n'}" ]]; then
            log "$WRAPPER_PATH already up to date - skipping."
            record_step "SKIPPED" "$WRAPPER_PATH (already up to date)"
            return 0
        fi
        if ! confirm_destructive "$WRAPPER_PATH already exists and differs. Overwrite it?"; then
            warn "Leaving existing $WRAPPER_PATH in place."
            record_step "SKIPPED" "$WRAPPER_PATH (user declined overwrite)"
            return 0
        fi
    fi
    run_as_user "cat > '$WRAPPER_PATH' << 'WRAPPER_EOF'
$wrapper_content
WRAPPER_EOF" && run_as_user "chmod +x '$WRAPPER_PATH'"
}

run_step "Write wrapper script" write_wrapper

log "Verifying the wrapper actually launches..."
if run_as_user "'$WRAPPER_PATH' --help >/dev/null 2>&1"; then
    record_step "OK" "wrapper launches cleanly"
else
    warn "Could not verify the wrapper runs cleanly - check manually with: $WRAPPER_PATH"
    record_step "FAILED" "wrapper launch verification"
fi

# ---------------------------------------------------------------------------
# Final summary - what changed, and permissions of everything relevant so
# you can manually verify correctness.
# ---------------------------------------------------------------------------

print_summary

echo
log "Relevant file/directory permissions (verify these look right):"
run_as_user "ls -ld '$TARGET_HOME/.local/bin' '$SNIPPYSNAPPY_DIR' '$SNIPPYSNAPPY_DIR/sniptext' '$TARGET_HOME/Pictures/Screenshots' '$TARGET_HOME/Pictures/Slop-o-graphs' '$WRAPPER_PATH' '$SNIPPYSNAPPY_PY' 2>/dev/null"
if [[ "$PKG_MANAGER" == "portage" && -f /etc/portage/package.use/snippysnappy ]]; then
    ls -l /etc/portage/package.use/snippysnappy
fi

echo
log "Done."
echo "  - Make sure \$HOME/.local/bin is on your PATH (it usually is by default)."
echo "  - Set up a keybind to run 'snippysnappy' (plus --window/--fullscreen variants if wanted)."
echo "  - Screenshots: $TARGET_HOME/Pictures/Screenshots"
echo "  - Slop saves:  $TARGET_HOME/Pictures/Slop-o-graphs"
echo "  - Sniptext:    $SNIPPYSNAPPY_DIR/sniptext"
