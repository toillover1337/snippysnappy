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
    snippysnappy --open PATH  Open an existing image file for editing/
                              annotation, same review window as a capture
                              (no screenshot is taken).
    snippysnappy --nil [ARGS]  Start with a blank canvas, no capture at all.
                              Optional args (either order): a WxH size
                              (e.g. 1200x1000) and/or a color code (w/b/t
                              for white/black/transparent). Defaults come
                              from NIL_CANVAS_DEFAULT_WIDTH/HEIGHT/COLOR
                              below. Example: --nil 1200x1000 b

Main toolbar:
    - Copy: Straight to clipboard, no file written, closes immediately.

    - Save: "Normal Save" (prompts for a filename, default snipsnap-<timestamp>, into ~/Pictures/Screenshots) or "Slop Save" (no prompt, fixed name slopsnap-<timestamp>.png, into a separate folder for quick/incidental shots). A checkbox (on by default) also copies to clipboard on save.

    - Edit: Resize (Full Capture or Background/canvas layer independently - see below), Create shapes, Pen (opaque) or Highlighter (translucent). Done bakes it in.

    - Resize (inside Edit) offers two independent layers: "Full Capture" flattens and
      resamples everything (capture + background + baked edits) together via scale
      factor, exact pixel dimensions, or dragging the corner handles directly.
      "Background" only grows/shrinks the canvas/whitespace around the capture
      (with a White/Black/Transparent color choice) without touching the captured
      pixels at all - shrinking crops it, growing brings whitespace (or previously
      hidden strokes) back losslessly. Escape while in Resize returns to the Edit
      toolbar, not the main one.

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
SNIPPYSNAPPY_VERSION = "0.12.0"   # X.0.0 = "stable"" release; 0.X.0 = feature addition/major tweak; 0.0.X = bug fix/small tweak. Reaching the digit "9" should increment to "10" e.g. x.x.9 -> x.x.10.

import argparse
import logging
import logging.handlers
import math
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path

LOG_PATH = Path(__file__).resolve().parent / "snippysnappy.log"   # Comment out this line to disable file logging entirely
MAX_LOG_BYTES = 1_000_000   # Rotate once the log exceeds ~1MB
LOG_BACKUP_COUNT = 3        # Keep this many rotated-out old logs (snippysnappy.log.1, .2, .3)

SCREENSHOT_DIR = Path.home() / "Pictures" / "Screenshots"
SLOP_DIR = Path.home() / "Pictures" / "Slop-o-graphs"   # Incidental screenshot path (Slop Save).
TEXT_DIR = Path("/tmp")   # Incidental OCR text lands here (Slop Save Text)
SNIPTEXT_DIR = Path(__file__).resolve().parent / "sniptext"   # Persistent OCR text path (Save Text)
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

HANDLE_SIZE = 8
OCR_BRUSH_WIDTH = 18
OCR_MASK_MIN_PADDING = 20   # px - minimum mask padding regardless of brush width
DEFAULT_COPY_ON_SAVE = True   # Default state of the "Also copy" checkbox on the Save/OCR-save screens
DEFAULT_ALSO_EXIT = True   # Default state of the "Also exit" checkbox (main toolbar) - True matches the original always-exit-after-Save/Copy behavior; uncheck to keep the window open for another action ("hot swapping" between captures without restarting)
NOTIFICATION_BACKEND = "toast"   # "system" (notify-send, falls back to the in-app toast if it fails) or "toast" (in-app toast is primary, falls back to notify-send if it fails)
OCR_RETURN_TO_TOOLBAR_AFTER_ACTION = True   # Set to False to close the app immediately instead "Close" on this screen always closes immediately either way.
TOOLBAR_HEIGHT = 90
BUTTON_PADY = 6   # Internal vertical padding applied to every toolbar button uniformly, so no row ever has some buttons taller than others.
MIN_POINT_DIST = 3   # px - skip adding a new stroke point closer than this to the last one
MIN_WINDOW_WIDTH = 720   # Decorated windows start at least this wide, reserving enough room for the widest toolbar row (Shape's Type buttons + Width slider + color swatches) up front so switching toolbars rarely needs to grow the window mid-session
MAX_WINDOW_WIDTH = 1250    # decorated windows are auto-sized no larger than this on a fresh capture/crop/monitor-switch (never restricts you from manually resizing afterward)
MAX_WINDOW_HEIGHT = 800    # same, for total height (toolbar height counts against this cap too); also never bigger than the monitor itself if the monitor is smaller than this
SCROLLBAR_THICKNESS = 12   # px - must match the width= given to the vertical/horizontal Scrollbars below; accounted for so an image just under the cap isn't left slightly cramped by the scrollbar strip eating into the viewport
VIEWPORT_BORDER_FUDGE = 6  # px - small safety margin added on top of SCROLLBAR_THICKNESS to also cover Tk's own frame/scrollbar border overhead (a couple px in practice), so the fit is never a hair short
MIN_CANVAS_DISPLAY_SIZE = 250   # px - very small captures are scaled UP on screen to at least this large (unconditional, regardless of AUTO_SCALE_TO_FIT)
AUTO_SCALE_TO_FIT = False   # False (default): oversized images are shown at their real size with thin scrollbars to pan around, plus a manual "Rescale to fit" button.
                            # True: the canvas view is always automatically rescaled to fit the current window - no scrollbars, no manual button; growing/shrinking the window live-rescales the view.
DEFAULT_MONITOR_NAME = None   # Wayland (and any case with no live cursor position to go by, e.g. --open) can't always auto-pick "the right" monitor to center a window on. Set this to a monitor name (via `wlr-randr` or `xrandr --listmonitors`) to force a specific default instead of first reported.
NIL_CANVAS_DEFAULT_WIDTH = 800    # --nil/-n with no WxH override starts this wide
NIL_CANVAS_DEFAULT_HEIGHT = 600   # --nil/-n with no WxH override starts this tall
NIL_CANVAS_DEFAULT_COLOR = "white"   # "white" | "black" | "transparent" - --nil/-n with no color override uses this
FUZZ_DIR = None   # None -> defaults to ~/Pictures (broadly, not just the Screenshots subfolder). Set an absolute
                  # path to override. This is the "root" --open/-o resolves relative PATH arguments against
                  # (not the shell's current working directory), and the default search scope for
                  # `--open prompt`'s fuzzy finder (shown there as "$FUZZ_DIR", matching this variable's name).
OPEN_PROMPT_MAX_SCAN_FILES = 20000   # Safety cap on how many files `--open prompt`'s fuzzy finder will walk when
                                     # searching a large parent dir (e.g. "/") - avoids the picker hanging indefinitely.
IMAGE_FILE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tiff", ".tif"}
JUNK_PENALTY_WEIGHT = 2   # how hard `--open prompt`'s junk_score (below) pulls a likely-junk file (app icons,
                          # cache thumbnails, etc.) down in the ranking - never excludes them, just deprioritizes.
                          # Raise this to push junk further down; 0 disables the heuristic entirely.
CTRL_SCROLL_ZOOM_STEP = 0.1   # Ctrl+wheel zoom step (10% per notch).
PAN_WHEEL_SUPPRESS_SECONDS = 0.4   # Plain wheel scroll (which adjusts pen/brush width, magnify zoom, or text size depending on mode) is ignored while a middle-mouse pan drag is held, and for this long after.

# Per-action toggles for the Snip tool's clipboard toast notifications (see
# _snip_copy/_snip_cut/_snip_paste/_snip_delete_to_void) - independent of
# which register (snippy or system) was used, since these are about
# whether you want feedback for that ACTION at all. All default on; flip
# any of these to False to silence just that one action's toast (e.g. set
# NOTIFY_ON_SNIP_PASTE = False if you don't want a toast every time you
# paste but still want one for Copy/Cut).
NOTIFY_ON_SNIP_COPY = True
NOTIFY_ON_SNIP_CUT = True
NOTIFY_ON_SNIP_PASTE = True
NOTIFY_ON_SNIP_DELETE = True   # void-delete (Super+x / Ctrl+Shift+X-equivalent void path)

WINDOW_TITLE_BASE = "snippy snappy :)"   # decorated windows are titled this, or "{BASE} - {submenu}" while
                                          # a submenu (Edit, Resize, Extract Text, etc.) is showing - see _set_window_title

# ---- app icon (currently INERT - no file is shipped, so this quietly no-ops until you add one) ----
#
# Used for: the decorated window titlebar icon, and (via Tk's iconphoto,
# which sets the standard _NET_WM_ICON X11 property) the DE's
# taskbar/alt-tab window switcher on any modern WM that reads it (GNOME
# Shell, KDE, XFCE, etc.) - both come from this ONE line, no separate
# wiring needed for each.
#
# TO ACTIVATE: drop a square PNG file at this path (by default, right next
# to this script) - 256x256 is the sweet spot (512x512 also fine); Tk/the
# WM scale it down cleanly for smaller contexts like the taskbar. A
# transparent background looks best. Nothing else in this file needs to
# change - _load_app_icon() below just starts finding the file.
#
# This does NOT cover the .desktop file's own "Icon=" line (used for
# application-launcher/menu entries) - that's a separate, short edit in
# whatever install.sh or .desktop file you use to install this: either
# point Icon= at an absolute path to the same PNG (simplest, works
# regardless of icon theme), or install the PNG into the standard XDG
# hicolor icon theme directory (e.g. ~/.local/share/icons/hicolor/256x256/
# apps/snippysnappy.png or the /usr/share equivalent) and set Icon=
# snippysnappy (just the name, no path/extension) so it's theme-aware.
APP_ICON_FILENAME = "snippysnappyicon256.png"


def _load_app_icon():
    """
    Loads APP_ICON_FILENAME (from the same directory as this script) as a
    Tk PhotoImage for use with iconphoto(). Returns None - quietly, no
    error - if the file doesn't exist yet or fails to load, so the app
    runs completely normally without an icon until one is added.
    """
    icon_path = Path(__file__).resolve().parent / APP_ICON_FILENAME
    if not icon_path.exists():
        return None
    try:
        return tk.PhotoImage(file=str(icon_path))
    except Exception:
        log.warning(f"Could not load app icon from {icon_path} - Tk's PhotoImage needs GIF/PNG/PPM (a plain PNG is safest)")
        return None


EDIT_PEN_WIDTH = 4
SPRAY_DOTS_PER_BURST = 8   # how many random dots land per burst while spraying
SPRAY_DOT_RADIUS_RANGE = (0.5, 1.5)   # each individual dot's radius is randomized within this range
SPRAY_MIN_BURST_DIST = 4   # px (capture-local) - minimum cursor movement between bursts during a drag.
                           # X11 can deliver far more Motion events for a given physical drag distance
                           # than there are visually-distinct "steps" along it (fast mice, event bursts,
                           # natural hand tremor while moving slowly) - without this, a slow or lingering
                           # drag racks up many more bursts (and therefore dots) than the drag distance
                           # would suggest, which is what made a stroke look sparse and grainy while
                           # actively spraying but far denser than expected once you stopped and looked
                           # at the result. Doesn't apply to the very first burst on press, which always fires.
EDIT_HIGHLIGHT_WIDTH = 22
EDIT_HIGHLIGHT_ALPHA = 90   # 0-255, translucency of highlighter mode
PALETTE = ["#FF0000", "#00A000", "#0000FF", "#000000", "#FFD400"]
TEXT_FONT_SIZE_DEFAULT = 24
MEASURE_COLORS = ["#FFD400", "#00FFFF", "#FF00FF", "#FF8800", "#00FF00"]
MAGNIFIER_LENS_SIZE_DEFAULT = 100   # Diameter in on-screen pixels
MAGNIFIER_LEVEL_DEFAULT = 4         # Magnification factor
SERIF_FONT_PATHS = [
    # Tried in order; falls back to PIL's built-in bitmap font (always available, ignores size).
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

_LOG_DATE_SEPARATOR_RE = re.compile(r"^===== (\d{4}-\d{2}-\d{2}) =====$", re.MULTILINE)


class _DateSeparatorRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """
    RotatingFileHandler that keeps a "===== YYYY-MM-DD =====" separator line
    marking each day's first log entry - log lines themselves stay time-only
    (HH:MM:SS) per LOG format, so without this, which day a given line
    belongs to is otherwise only guessable from file mtimes. A separator is
    written whenever the file's last one isn't today's date, including when
    there isn't one at all yet (brand-new file, or right after a rotation
    hands us a fresh empty one).
    """
    def _write_separator_if_needed(self):
        today = datetime.now().strftime("%Y-%m-%d")
        last_date = None
        try:
            if os.path.exists(self.baseFilename):
                with open(self.baseFilename, "r", encoding="utf-8", errors="replace") as f:
                    matches = _LOG_DATE_SEPARATOR_RE.findall(f.read())
                if matches:
                    last_date = matches[-1]
        except Exception:
            pass
        if last_date != today:
            try:
                with open(self.baseFilename, "a", encoding="utf-8") as f:
                    f.write(f"===== {today} =====\n")
            except Exception:
                pass

    def doRollover(self):
        super().doRollover()
        self._write_separator_if_needed()


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
            file_handler = _DateSeparatorRotatingFileHandler(
                log_path, mode="a", maxBytes=MAX_LOG_BYTES, backupCount=LOG_BACKUP_COUNT,
            )
            file_handler._write_separator_if_needed()
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
    log.info(f"Raw `xrandr --listmonitors` output:\n{out}")
    monitors = []
    for line in out.strip().splitlines()[1:]:
        parts = line.split()
        geom_token = next(p for p in parts if re.match(r"\d+/\d+x\d+/\d+\+\d+\+\d+", p))
        w, h, x, y = (int(v) for v in re.match(r"(\d+)/\d+x(\d+)/\d+\+(\d+)\+(\d+)", geom_token).groups())
        monitors.append((parts[-1], x, y, w, h))
    if not monitors:
        log.error("xrandr --listmonitors returned no monitors")
        raise RuntimeError("xrandr --listmonitors returned no monitors")
    log.info(f"Detected monitors: {monitors}")
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
    log.info(f"Detected monitors (wayland): {monitors}")
    return monitors


_monitors_cache = {"result": None, "at": 0.0}
MONITORS_CACHE_TTL = 1.0   # Seconds - avoids re-spawning xrandr/wlr-randr repeatedly within a short window.


def get_monitors():
    now = time.monotonic()
    if _monitors_cache["result"] is not None and (now - _monitors_cache["at"]) < MONITORS_CACHE_TTL:
        return _monitors_cache["result"]
    if BACKEND == "wayland":
        try:
            result = get_monitors_wayland()
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
            result = [("default", 0, 0, w, h)]
    else:
        result = get_monitors_x11()
    _monitors_cache["result"] = result
    _monitors_cache["at"] = now
    return result


def monitor_at_point(monitors, px, py):
    for mon in monitors:
        _, x, y, w, h = mon
        if x <= px < x + w and y <= py < y + h:
            log.debug(f"Point ({px},{py}) is on monitor {mon}")
            return mon
    log.warning(f"Point ({px},{py}) not on any reported monitor; falling back to first: {monitors[0]}")
    return monitors[0]  # Fallback if cursor is somehow off every monitor


def pick_fallback_monitor(monitors):
    """
    Used when a window needs to be centered on some monitor but there's no
    live cursor position to go by (e.g. --open, or an X11 cursor-position
    lookup that failed) - honors DEFAULT_MONITOR_NAME if it's set and
    actually present, otherwise just the first reported monitor.
    """
    if DEFAULT_MONITOR_NAME:
        for m in monitors:
            if m[0] == DEFAULT_MONITOR_NAME:
                return m
        log.warning(
            f"DEFAULT_MONITOR_NAME={DEFAULT_MONITOR_NAME!r} not found among detected "
            f"monitors {[m[0] for m in monitors]}; using the first one instead"
        )
    return monitors[0]


_NIL_DIMENSIONS_RE = re.compile(r"^(\d+)[xX](\d+)$")
_NIL_COLOR_CODES = {"w": "white", "white": "white", "b": "black", "black": "black", "t": "transparent", "transparent": "transparent"}


def parse_nil_args(tokens):
    """
    Parses --nil/-n's optional sub-arguments (any order): a WxH dimension
    token (e.g. "1200x1000") and/or a color code (w/white, b/black,
    t/transparent). Falls back to NIL_CANVAS_DEFAULT_WIDTH/HEIGHT/COLOR for
    whichever isn't given. Unrecognized tokens are warned about and ignored
    rather than treated as a hard error, consistent with this script
    generally preferring to continue over a full failure where reasonable.
    """
    width, height = NIL_CANVAS_DEFAULT_WIDTH, NIL_CANVAS_DEFAULT_HEIGHT
    color = NIL_CANVAS_DEFAULT_COLOR
    for token in tokens:
        m = _NIL_DIMENSIONS_RE.match(token.strip())
        if m:
            width, height = int(m.group(1)), int(m.group(2))
            continue
        code = _NIL_COLOR_CODES.get(token.strip().lower())
        if code:
            color = code
            continue
        log.warning(f"--nil: unrecognized argument {token!r} (expected WxH or w/b/t) - ignoring")
    return width, height, color


def make_nil_placeholder(path, color):
    """
    --nil takes no screenshot, so there's no real "capture" underneath the
    canvas layer at all - this is a throwaway 1x1 file purely so
    SelectorApp has something to open at construction time. main()'s --nil
    branch sets app._no_real_capture = True right after, which tells
    _build_canvas_composite to never actually paste this placeholder in -
    the canvas background alone provides the blank canvas. The color
    argument only matters for consistency/debugging, not for what's shown.
    """
    from PIL import Image
    fill = {"white": (255, 255, 255), "black": (0, 0, 0)}.get(color, (255, 255, 255))
    Image.new("RGB", (1, 1), fill).save(path)


def is_image_file(path):
    return path.suffix.lower() in IMAGE_FILE_EXTENSIONS


# Cheap (path/filename-only, no file I/O beyond what's already done for
# mtime) signals that a file is more likely an incidental system/app
# artifact - icons, cache thumbnails, bundled theme assets, etc. - than
# something the user actually took or wants to open. Each list is checked
# independently and only the strongest single hit in each counts (so one
# obviously-junky path doesn't get double-penalized further for also
# having a junky filename on top of it), keeping the penalty proportionate.
JUNK_PATH_MARKERS = [
    "/.cache/", "/.thumbnails/", "/.icons/", "/icons/", "/.local/share/icons/",
    "/.git/", "/node_modules/", "/.npm/", "/__pycache__/", "/.cargo/", "/.rustup/",
    "/site-packages/", "/.mozilla/", "/.thunderbird/", "/.var/app/", "/snap/",
    "/.steam/", "/.wine/", "/.local/share/trash/", "/usr/share/icons/",
    "/usr/share/pixmaps/", "/usr/lib/", "/usr/share/doc/",
]
JUNK_NAME_MARKERS = ["icon", "thumbnail", "thumb", "favicon"]


def junk_score(path):
    """
    0 = no junk signal at all; higher = more likely an incidental app/
    system artifact rather than a real photo/screenshot. Used only to
    DEPRIORITIZE - see JUNK_PENALTY_WEIGHT - never to exclude a file
    outright, so it's still findable by an explicit search.
    """
    s = str(path).lower()
    score = 0
    if any(marker in s for marker in JUNK_PATH_MARKERS):
        score += 3
    name = path.name.lower()
    if any(marker in name for marker in JUNK_NAME_MARKERS):
        score += 2
    if name.startswith("."):
        score += 1
    return score


def get_default_open_dir():
    return Path(FUZZ_DIR).expanduser() if FUZZ_DIR else (Path.home() / "Pictures")


def resolve_open_path(path_str, parent_dir=None):
    """
    Resolves a --open PATH argument (or `--open prompt`'s typed path):
    absolute paths are used as-is; anything else is resolved relative to
    `parent_dir` (defaults to get_default_open_dir(), i.e. FUZZ_DIR or
    ~/Pictures) - NOT the shell's current working directory.
    """
    p = Path(path_str).expanduser()
    if p.is_absolute():
        return p
    base = parent_dir if parent_dir is not None else get_default_open_dir()
    return Path(base) / p


def fuzzy_match(query, candidate):
    """
    Simple ordered-subsequence fuzzy match (fzf-style, simplified): every
    character of `query` (case-insensitive) must appear, in order,
    somewhere in `candidate`, though not necessarily contiguously. Returns
    (score, matched_indices) - higher score is a better match - or None if
    the query doesn't match at all (this is the "forgiving but not so
    forgiving it matches junk" boundary: a query that skips around too much
    still matches, but scores low enough to sort behind tight matches).
    Consecutive matched characters and matches right after a path
    separator/word boundary score higher, so tight substring-like matches
    consistently outrank scattered ones.
    """
    if not query:
        return (0, [])
    q = query.lower()
    c = candidate.lower()
    qi = 0
    matched = []
    score = 0.0
    last_match_idx = -2
    for ci, ch in enumerate(c):
        if qi < len(q) and ch == q[qi]:
            matched.append(ci)
            if ci == last_match_idx + 1:
                score += 3
            elif ci == 0 or c[ci - 1] in "/_-. ":
                score += 2
            else:
                score += 1
            last_match_idx = ci
            qi += 1
    if qi < len(q):
        return None
    score -= (len(c) - len(q)) * 0.01
    return (score, matched)


def fuzzy_search_images(root_dir, query, limit=30):
    """
    Searches every image file under root_dir (recursively, capped at
    OPEN_PROMPT_MAX_SCAN_FILES so a huge root like "/" can't hang the
    picker indefinitely) and fuzzy-matches `query` against each file,
    returning the top `limit` matches as (Path, matched_indices) sorted
    best-first (indices are positions within the full path string, for
    highlighting). With an empty query, returns the most recently modified
    images instead (a reasonable default list to browse).

    The FILENAME is matched first and given a large score bonus over a
    full-path match - matching the full path directly (the original
    approach) let a query match by stitching together characters from
    unrelated directory components (e.g. "logo" matching an 'l' from
    ".../uploads/..." plus a 'g' and 'o' from ".../background2.png" later
    in the same path), which could easily outscore a file whose actual
    NAME matches almost perfectly. Full-path matching is now only a
    fallback for files whose name alone doesn't match at all, so a
    directory-name-based search still finds something, just ranked well
    In both browse mode and search results, files with a nonzero
    junk_score (app icons, cache thumbnails, bundled theme assets, etc. -
    see JUNK_PATH_MARKERS/JUNK_NAME_MARKERS) are pushed down rather than
    excluded, so they don't dominate a fresh "browse the home dir" list or
    flood out real matches during an unrelated search, but still surface
    if you actually search for one.
    """
    root_dir = Path(root_dir)
    if not root_dir.is_dir():
        return []
    candidates = []
    scanned = 0
    try:
        for p in root_dir.rglob("*"):
            scanned += 1
            if scanned > OPEN_PROMPT_MAX_SCAN_FILES:
                log.warning(f"--open prompt: hit the {OPEN_PROMPT_MAX_SCAN_FILES}-file scan cap under {root_dir} - results may be incomplete")
                break
            if p.is_file() and is_image_file(p):
                candidates.append(p)
    except (PermissionError, OSError):
        log.warning(f"--open prompt: could not fully walk {root_dir} (permission or I/O error) - showing partial results")
    log.debug(f"--open prompt: scanned {scanned} filesystem entries under {root_dir}, found {len(candidates)} image file(s)")
    if not query.strip():
        # Junk tier first (ascending junk_score, so 0 = real files sort
        # first), newest-first within each tier - a pile of old icon/cache
        # files can never bury today's actual screenshots this way, no
        # matter how their mtimes compare.
        candidates.sort(key=lambda p: (junk_score(p), -(p.stat().st_mtime if p.exists() else 0)))
        junk_count = sum(1 for p in candidates[:limit] if junk_score(p) > 0)
        if junk_count:
            log.debug(f"--open prompt: browse list includes {junk_count} likely-junk file(s) (deprioritized, not hidden)")
        return [(p, []) for p in candidates[:limit]]
    FILENAME_MATCH_BONUS = 1000   # keeps every filename match ranked above every path-only fallback match
    scored = []
    name_matches = 0
    for p in candidates:
        full_path_str = str(p)
        name_offset = len(full_path_str) - len(p.name)   # where the filename starts within the full path string
        penalty = junk_score(p) * JUNK_PENALTY_WEIGHT
        name_match = fuzzy_match(query, p.name)
        if name_match is not None:
            score, name_idxs = name_match
            full_idxs = [i + name_offset for i in name_idxs]
            scored.append((score + FILENAME_MATCH_BONUS - penalty, p, full_idxs))
            name_matches += 1
            continue
        path_match = fuzzy_match(query, full_path_str)
        if path_match is not None:
            score, idxs = path_match
            scored.append((score - penalty, p, idxs))
    scored.sort(key=lambda t: t[0], reverse=True)
    log.debug(
        f"--open prompt: {len(scored)} of {len(candidates)} image file(s) matched query {query!r} "
        f"({name_matches} by filename, {len(scored) - name_matches} by path fallback only)"
    )
    return [(p, idxs) for score, p, idxs in scored[:limit]]


class OpenPromptApp:
    """
    The `--open prompt` picker: a small decorated window with a Parent Dir
    selector, a path entry, and a live fuzzy-matched, keyboard-navigable
    list of image files (shown as full paths, matched characters
    highlighted) - built with the same root Tk() instance main() already
    created, reused afterward for the real SelectorApp once a file is
    chosen (or the process exits cleanly on Cancel/close).
    """

    def __init__(self, root, default_dir):
        self.root = root
        self.default_dir = Path(default_dir).expanduser()
        self.parent_dir = self.default_dir
        self.result_path = None
        self.results = []
        self.selected_index = 0
        self.vim_mode = "insert"   # "insert" | "normal" | "visual" | "visual_line" - same scheme as SelectorApp's text tool
        self._vim_visual_anchor = None
        self.vim_register = None   # separate, simpler, plain-text-only register for this window
        log.info(f"--open prompt: opening picker, default parent dir = {self.default_dir}")

        root.title(f"{WINDOW_TITLE_BASE} - open")
        root.resizable(True, True)
        root.configure(bg="#1a1a1a")
        root.geometry("640x420")
        root.attributes("-topmost", True)   # matches every other snippysnappy window
        self._app_icon_image = _load_app_icon()   # keep a reference - Tk drops the icon if this gets garbage collected
        if self._app_icon_image is not None:
            root.iconphoto(True, self._app_icon_image)

        top = tk.Frame(root, bg="#1a1a1a")
        top.pack(side="top", fill="x", padx=12, pady=(12, 4))
        tk.Label(top, text="Parent Dir:", fg="white", bg="#1a1a1a").pack(side="left", padx=(0, 6))
        self._default_label = "$FUZZ_DIR (default)"
        self.parent_choices = {
            self._default_label: self.default_dir,
            "~/": Path.home(),
            "/": Path("/"),
        }
        self.parent_var = tk.StringVar(value=self._default_label)
        tk.OptionMenu(top, self.parent_var, *self.parent_choices.keys(), command=self._on_parent_changed).pack(side="left")

        path_row = tk.Frame(root, bg="#1a1a1a")
        path_row.pack(side="top", fill="x", padx=12, pady=(4, 8))
        tk.Label(path_row, text="Path:", fg="white", bg="#1a1a1a").pack(side="left", padx=(0, 6))
        self.path_var = tk.StringVar()
        self.entry = tk.Entry(path_row, textvariable=self.path_var, width=60)
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry.bind("<KeyRelease>", self._on_type)
        self.entry.bind("<Down>", self._on_arrow_down)
        self.entry.bind("<Up>", self._on_arrow_up)
        self.entry.bind("<Return>", self._on_enter)
        self.entry.bind("<Escape>", self._vim_escape)
        self.entry.bind("<Key>", self._vim_on_key)
        self.entry.focus_set()

        self.status_label = tk.Label(root, text="", fg="#aaaaaa", bg="#1a1a1a", anchor="w")
        self.status_label.pack(side="top", fill="x", padx=12)
        self.error_label = tk.Label(root, text="", fg="#ff6666", bg="#1a1a1a", anchor="w")
        self.error_label.pack(side="top", fill="x", padx=12)

        # takefocus=0 so clicking a result (or the dropdown) doesn't steal
        # keyboard focus away from the path entry - confirmed cause of
        # "typing stops updating the list" after any click into the
        # results area, since subsequent keystrokes were going to a
        # (disabled, non-editable) Text widget instead of the Entry.
        self.results_text = tk.Text(
            root, height=14, bg="#222222", fg="white", state="disabled",
            cursor="arrow", wrap="none", takefocus=0,
        )
        self.results_text.pack(side="top", fill="both", expand=True, padx=12, pady=(0, 8))
        self.results_text.tag_configure("match", foreground="#00b4ff", font=("", 10, "bold"))
        self.results_text.tag_configure("selected_row", background="#3a5a8a")
        self.results_text.bind("<Button-1>", self._on_click_result)

        btn_row = tk.Frame(root, bg="#1a1a1a")
        btn_row.pack(side="top", fill="x", padx=12, pady=(0, 12))
        tk.Button(btn_row, text="Open", pady=BUTTON_PADY, command=self._confirm_current).pack(side="left", padx=(0, 2))
        tk.Button(btn_row, text="Cancel", pady=BUTTON_PADY, command=self._cancel).pack(side="left", padx=2)

        root.protocol("WM_DELETE_WINDOW", self._cancel)
        self._refresh_results()

    def _on_parent_changed(self, choice):
        self.parent_dir = self.parent_choices[choice]
        log.debug(f"--open prompt: parent dir changed to {self.parent_dir}")
        self._refresh_results()
        self.entry.focus_set()   # the OptionMenu takes focus on selection - hand it back

    def _on_type(self, event):
        if event.keysym in ("Down", "Up", "Return", "Escape"):
            return
        self._refresh_results()

    def _refresh_results(self):
        query = self.path_var.get().strip()
        log.debug(f"--open prompt: searching {self.parent_dir!r} for query {query!r}")
        if not self.parent_dir.is_dir():
            log.warning(f"--open prompt: parent dir {self.parent_dir} does not exist or isn't a directory")
            self.status_label.config(text=f"'{self.parent_dir}' doesn't exist - pick a different Parent Dir.")
            self.results = []
            self.selected_index = 0
            self._render_results()
            return
        self.results = fuzzy_search_images(self.parent_dir, query)
        log.debug(f"--open prompt: {len(self.results)} result(s) for query {query!r} under {self.parent_dir}")
        if not self.results:
            self.status_label.config(
                text="No matching image files found." if query else f"No image files found under {self.parent_dir}."
            )
        else:
            self.status_label.config(text="")
        self.selected_index = 0
        self._render_results()

    def _render_results(self):
        self.results_text.config(state="normal")
        self.results_text.delete("1.0", "end")
        for i, (path, matched_idxs) in enumerate(self.results):
            self.results_text.insert("end", str(path) + "\n")
            for idx in matched_idxs:
                self.results_text.tag_add("match", f"{i + 1}.{idx}", f"{i + 1}.{idx + 1}")
        self._highlight_selected()
        self.results_text.config(state="disabled")

    def _highlight_selected(self):
        self.results_text.tag_remove("selected_row", "1.0", "end")
        if self.results:
            i = self.selected_index
            self.results_text.tag_add("selected_row", f"{i + 1}.0", f"{i + 1}.end+1c")
            self.results_text.see(f"{i + 1}.0")

    def _move_selection(self, delta):
        if not self.results:
            return
        self.selected_index = max(0, min(self.selected_index + delta, len(self.results) - 1))
        self.results_text.config(state="normal")
        self._highlight_selected()
        self.results_text.config(state="disabled")

    def _on_arrow_down(self, event):
        self._move_selection(1)
        return "break"

    def _on_arrow_up(self, event):
        self._move_selection(-1)
        return "break"

    def _on_click_result(self, event):
        index_str = self.results_text.index(f"@{event.x},{event.y}")
        line = int(index_str.split(".")[0]) - 1
        if 0 <= line < len(self.results):
            self.selected_index = line
            self.results_text.config(state="normal")
            self._highlight_selected()
            self.results_text.config(state="disabled")
            self._confirm_current()
        return "break"

    def _on_enter(self, event):
        self._confirm_current()
        return "break"

    def _confirm_current(self):
        self.error_label.config(text="")
        if self.results:
            chosen = self.results[self.selected_index][0]
            log.info(f"--open prompt: confirmed via results list -> {chosen}")
            self._finish(chosen)
            return
        typed = self.path_var.get().strip()
        if not typed:
            return
        candidate = resolve_open_path(typed, self.parent_dir)
        log.info(f"--open prompt: no list results, trying typed path directly -> {candidate}")
        if not candidate.exists() or not candidate.is_file():
            self.error_label.config(text=f"No such file: {candidate}")
            return
        if not is_image_file(candidate):
            self.error_label.config(text="Please select an image file.")
            return
        self._finish(candidate)

    def _finish(self, path):
        self.result_path = path
        self.root.quit()

    def _cancel(self):
        self.result_path = None
        self.root.quit()

    # ---- vim-style Insert/Normal/Visual modes for the path entry ----
    # Same conceptual state machine as SelectorApp's text tool (see that
    # class's _vim_on_key for the fuller explanation), adapted for a
    # single-line Entry: Up/Down stay bound to result-list navigation
    # (unchanged), so vim-mode cursor movement here only covers
    # Left/Right/Home/End, and there's no real "line" concept so Visual
    # Line just selects the whole field.

    _VIM_ENTRY_NAV_KEYSYMS = {"Left", "Right", "Home", "End"}

    def _vim_escape(self, event=None):
        if self.vim_mode == "insert":
            self._vim_enter_normal()
            return "break"
        self._cancel()
        return "break"

    def _vim_enter_insert(self):
        self.vim_mode = "insert"
        self.entry.config(insertwidth=2)
        self.entry.selection_clear()

    def _vim_enter_normal(self):
        self.vim_mode = "normal"
        self.entry.config(insertwidth=0)
        self._vim_update_block_cursor()

    def _vim_update_block_cursor(self):
        """
        Entry has no per-character tagging like Text does, so the "block
        cursor" is approximated by selecting just the one character under
        the cursor - visually similar, at the cost of briefly borrowing the
        widget's normal selection highlight for it.
        """
        pos = self.entry.index("insert")
        self.entry.selection_clear()
        end = len(self.entry.get())
        if pos < end:
            self.entry.selection_range(pos, pos + 1)

    def _vim_enter_visual(self, linewise):
        self.vim_mode = "visual_line" if linewise else "visual"
        self._vim_visual_anchor = self.entry.index("insert")
        if linewise:
            self.entry.selection_range(0, len(self.entry.get()))
        else:
            self._vim_update_visual_selection()

    def _vim_update_visual_selection(self):
        if self.vim_mode != "visual":
            return
        cur = self.entry.index("insert")
        lo, hi = sorted((self._vim_visual_anchor, cur))
        end = len(self.entry.get())
        self.entry.selection_range(lo, min(hi + 1, end))

    def _vim_get_selection_text(self):
        try:
            start, end = self.entry.index("sel.first"), self.entry.index("sel.last")
            return self.entry.get()[start:end]
        except tk.TclError:
            return ""

    def _vim_yank(self):
        if self.vim_mode in ("visual", "visual_line"):
            self.vim_register = self._vim_get_selection_text()
            log.debug(f"--open prompt vim yank: {len(self.vim_register)} character(s)")
            self._vim_enter_normal()
        else:
            self.vim_register = self.entry.get()
            log.debug("--open prompt vim yank (Normal mode, no selection): copied the whole field")

    def _vim_delete(self):
        if self.vim_mode in ("visual", "visual_line"):
            self.vim_register = self._vim_get_selection_text()
            try:
                self.entry.delete("sel.first", "sel.last")
            except tk.TclError:
                pass
            self._vim_enter_normal()
        else:
            self.vim_register = self.entry.get()
            self.entry.delete(0, "end")
        self._refresh_results()

    def _vim_paste(self):
        if self.vim_register is None:
            return
        pos = self.entry.index("insert")
        self.entry.insert(pos, self.vim_register)
        if self.vim_mode in ("visual", "visual_line"):
            self._vim_enter_normal()
        self._refresh_results()

    def _vim_on_key(self, event):
        if self.vim_mode == "insert":
            return None
        keysym = event.keysym
        if keysym == "i":
            self._vim_enter_insert(); return "break"
        if keysym == "v":
            self._vim_enter_visual(linewise=False); return "break"
        if keysym == "V":
            self._vim_enter_visual(linewise=True); return "break"
        if keysym == "y":
            self._vim_yank(); return "break"
        if keysym == "d":
            self._vim_delete(); return "break"
        if keysym == "p":
            self._vim_paste(); return "break"
        if keysym in self._VIM_ENTRY_NAV_KEYSYMS:
            if self.vim_mode in ("visual", "visual_line"):
                self.root.after_idle(self._vim_update_visual_selection)
            else:
                self.root.after_idle(self._vim_update_block_cursor)
            return None
        if event.char:
            return "break"
        return None


def get_virtual_screen_bounds(monitors):
    """
    Bounding box spanning ALL monitors combined - i.e. the full extended desktop.
    """
    min_x = min(m[1] for m in monitors)
    min_y = min(m[2] for m in monitors)
    max_x = max(m[1] + m[3] for m in monitors)
    max_y = max(m[2] + m[4] for m in monitors)
    bounds = (min_x, min_y, max_x - min_x, max_y - min_y)
    log.info(f"Virtual screen bounds across {len(monitors)} monitor(s): {bounds}")
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


def get_system_clipboard_image():
    """
    Returns a PIL Image if the system clipboard currently holds image data
    (from any application, not just this one), else None. Used by the
    system-register paste shortcuts (Ctrl+Shift+V / Super+p).
    """
    from PIL import Image
    import io
    cmd = ["wl-paste", "-t", "image/png"] if BACKEND == "wayland" else ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"]
    try:
        result = run_logged(cmd, capture_output=True, check=True)
        if not result.stdout:
            return None
        return Image.open(io.BytesIO(result.stdout)).convert("RGBA")
    except Exception:
        log.debug("get_system_clipboard_image: no image data on the clipboard (or the read failed)")
        return None


def get_system_clipboard_text():
    """
    Returns the system clipboard's plain-text content (from any
    application), or None if it holds no text. Used by the system-register
    paste shortcuts when the target is a text box rather than the canvas.
    """
    cmd = ["wl-paste", "-n"] if BACKEND == "wayland" else ["xclip", "-selection", "clipboard", "-o"]
    try:
        result = run_logged(cmd, capture_output=True, check=True, text=True)
        return result.stdout if result.stdout else None
    except Exception:
        log.debug("get_system_clipboard_text: no text data on the clipboard (or the read failed)")
        return None


def ocr_image(path):
    result = run_logged(["tesseract", str(path), "stdout"], capture_output=True, text=True)
    text = result.stdout.strip()
    log.debug(f"OCR produced {len(text)} chars")
    return text


def ensure_dbus_env():
    """
    Global hotkey daemons often spawn commands with a stripped environment
    missing DBUS_SESSION_BUS_ADDRESS, which notify-send needs to reach the
    notification service. Falls back to the standard systemd-user D-Bus socket path
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


def show_toast_fallback(title, body, blocking=True, anchor_window=None):
    """
    Tiny popup used only when notify-send isn't working, so there's still
    some visual confirmation the action completed.

    blocking=True (default) waits for the toast to close before returning.
    blocking=False draws it immediately via update() and returns right away,
    letting it self-destroy in the background - used for "Capture taken" so
    the toast doesn't delay the review window from appearing.

    anchor_window, when given, positions the toast on whichever monitor
    THAT window is currently on, rather than always defaulting to
    whichever monitor xrandr happens to report first - on a multi-monitor
    setup, a toast anchored to the wrong monitor is effectively invisible
    if you're not looking at that one, which reads as "notifications never
    appear" even though they technically fire every time.
    """
    try:
        existing_root = getattr(tk, "_default_root", None)
        owns_root = existing_root is None
        toast = tk.Tk() if owns_root else tk.Toplevel(existing_root)
        toast.withdraw()   # stay hidden until fully configured - see the note below
        toast.overrideredirect(True)
        toast.attributes("-topmost", True)
        try:
            monitors = get_monitors()
            mon = None
            if anchor_window is not None:
                try:
                    anchor_window.update_idletasks()
                    ax = anchor_window.winfo_rootx() + anchor_window.winfo_width() // 2
                    ay = anchor_window.winfo_rooty() + anchor_window.winfo_height() // 2
                    mon = monitor_at_point(monitors, ax, ay)
                except Exception:
                    mon = None
            if mon is None:
                mon = monitors[0]
            _, mx, my, mw, mh = mon
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
        # Only NOW show it - a freshly created Toplevel gets mapped to the
        # screen at its X11 default position (usually the top-left corner)
        # immediately on creation, with none of this configuration applied
        # yet, which is exactly what that brief white, undecorated,
        # top-left-corner box flash was: withdraw()-ing right after
        # creation and only deiconify()-ing once geometry/overrideredirect/
        # content are all already set means nothing unstyled is ever shown.
        toast.deiconify()
        toast.after(2500, toast.destroy)
        if not blocking:
            toast.update()
            return True
        if owns_root:
            toast.mainloop()
        else:
            toast.wait_window(toast)
        return True
    except Exception:
        log.exception("Toast fallback itself failed (non-fatal)")
        return False


def notify(title, body="", allow_toast=True, blocking=True, anchor_window=None):
    """
    Two channels are available: the OS notification daemon (notify-send)
    and an in-app Tk toast. NOTIFICATION_BACKEND picks which one is tried
    first; the other is used as a fallback if the primary fails.
    blocking=False is for callers that can't afford to wait out the toast's
    display time (see show_toast_fallback) - e.g. "Capture taken" needs to
    return immediately so the review window isn't delayed from appearing.
    anchor_window is passed straight through to show_toast_fallback - see
    there for why it matters on multi-monitor setups.
    """
    if NOTIFICATION_BACKEND == "toast":
        _notify_toast_primary(title, body, allow_toast, blocking, anchor_window)
    else:
        _notify_system_primary(title, body, allow_toast, blocking)


def _notify_system_primary(title, body, allow_toast, blocking=True):
    if not allow_toast or not blocking:
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


def _notify_toast_primary(title, body, allow_toast, blocking=True, anchor_window=None):
    if not allow_toast:
        popen_logged(["notify-send", title, body], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    succeeded = show_toast_fallback(title, body, blocking=blocking, anchor_window=anchor_window)
    if not succeeded:
        log.warning("Toast (primary) failed - falling back to notify-send")
        popen_logged(["notify-send", title, body], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def timestamp():
    return datetime.now().strftime("%Y%m%d-%H%M%S")


OCR_UPSCALE_FACTOR = 2       # How much to enlarge crops before OCR.
OCR_UPSCALE_SKIP_HEIGHT = 1000   # Skip upscaling only for already-large crops.


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
        self._true_root = root   # The ORIGINAL Tk() instance - mainloop() is tied to this one specifically.
                                  # transition_to_review swaps self.root to a new Toplevel for the review
                                  # window while keeping this alive (withdrawn) so mainloop() keeps running;
                                  # destroying self.root anywhere in the app cascades to destroying this too.
        self.image_path = image_path
        self.owned_temp_paths = [image_path]  # Cleaned up when the window is destroyed - see _cleanup_temp_files
        self._ocr_overlay = None
        self._ocr_text_widget = None
        self._ocr_copy_var = None
        self.mode = mode
        self.available_monitors = monitors or []
        self.current_monitor_name = current_monitor_name
        self.capture_tempdir = capture_tempdir

        from PIL import Image as _Image
        with _Image.open(image_path) as _im:
            iw, ih = _im.size
        log.debug(f"Loaded capture image: {iw}x{ih}")

        self.toolbar_container = None
        self.current_monitor_bounds = None
        self.canvas_frame = None
        self.vbar = None
        self.hbar = None
        self.scale_label = None
        self.rescale_btn = None
        self._base_display_photo = None
        self._base_photo_id = None
        self._configure_after_id = None
        self._panning = False
        self._pan_released_at = None
        self._expected_geometry = None
        self._geometry_enforcement_active = False
        self._geometry_disarm_after_id = None
        self._geometry_correction_after_id = None
        self._enforcing_geometry = False

        # ---- canvas (background) layer state ----
        # capture-local coordinate frame: (0,0) is the captured image's own
        # top-left corner and NEVER moves except on a "Full Capture" resize
        # (which resamples the capture itself and resets this frame). The
        # canvas is described as a rectangle in THIS frame - it can extend
        # past the capture on any side (whitespace/comment room) or inside
        # it (cropping) without touching the capture's own pixels at all.
        # All edit/OCR-paint strokes are stored in this same frame, which is
        # why growing the canvas back out after shrinking it correctly
        # reveals previously-clipped strokes - nothing is ever discarded.
        self.canvas_x1 = 0
        self.canvas_y1 = 0
        self.canvas_x2 = None   # set on first activation to match the capture
        self.canvas_y2 = None
        self.canvas_bg_color = "white"   # "white" | "black" | "transparent"
        self._canvas_active = False
        self._no_real_capture = False   # True for --nil sessions - see _build_canvas_composite
        self._force_show_canvas_composite = False   # True for --nil sessions - see _should_show_canvas_composite

        # ---- resize submenu state ----
        self.resize_mode_active = False
        self.resize_target_layer = None   # "capture" | "background" - defaults to "capture" on first open
        self._resize_active_handle = None
        self._resize_drag_snapshotted = False
        self._resize_preview_item_ids = []
        self._resize_preview_start_capture_dims = None
        self._resize_undo_stack = []   # walk-back-one-step-at-a-time, see _push_resize_undo_snapshot
        self._resize_redo_stack = []   # reverses Undo, see _redo_layer_resize
        self._resize_session_snapshot = None   # reverted-to-in-full by Cancel - see _snapshot_resize_session_start

        if mode == "monitor":
            x, y, _, _ = bounds
            self.origin_x, self.origin_y = x, y
            root.overrideredirect(True)
            self._set_geometry(f"{iw}x{ih}+{x}+{y}")
            root.attributes("-topmost", True)
            root.configure(cursor="crosshair", bg="#1a1a1a")
            self.canvas = tk.Canvas(root, width=iw, height=ih, highlightthickness=0, bg="#1a1a1a")
            self.canvas.pack(side="top", fill="both", expand=True)
            root.update_idletasks()
            log.info(
                f"Borderless overlay: requested {iw}x{ih}+{x}+{y}, "
                f"actual {root.winfo_geometry()} (these should match - overrideredirect "
                f"windows aren't WM-managed, so a mismatch means iw/ih/x/y above were "
                f"already wrong, not a window-manager override)"
            )
        else:
            mx, my, mw, mh = bounds
            self.current_monitor_bounds = (mx, my, mw, mh)
            root.title(WINDOW_TITLE_BASE)
            self._app_icon_image = _load_app_icon()   # keep a reference - Tk drops the icon if this gets garbage collected
            if self._app_icon_image is not None:
                root.iconphoto(True, self._app_icon_image)
            self._set_geometry(f"{max(iw, MIN_WINDOW_WIDTH)}x{ih + TOOLBAR_HEIGHT}+{mx}+{my}")
            root.attributes("-topmost", True)
            self.toolbar_container = tk.Frame(root, bg="#222222", height=TOOLBAR_HEIGHT)
            self.toolbar_container.pack(side="bottom", fill="x")
            self.toolbar_container.pack_propagate(False)
            root.configure(cursor="crosshair", bg="#1a1a1a")
            self.canvas_frame = tk.Frame(root, bg="#1a1a1a")
            self.canvas_frame.pack(side="top", fill="both", expand=True)
            if not AUTO_SCALE_TO_FIT:
                self.vbar = tk.Scrollbar(self.canvas_frame, orient="vertical", width=SCROLLBAR_THICKNESS)
                self.hbar = tk.Scrollbar(self.canvas_frame, orient="horizontal", width=SCROLLBAR_THICKNESS)
                self.vbar.pack(side="right", fill="y")
                self.hbar.pack(side="bottom", fill="x")
            self.canvas = tk.Canvas(self.canvas_frame, highlightthickness=0, bg="#1a1a1a")
            if not AUTO_SCALE_TO_FIT:
                self.canvas.configure(xscrollcommand=self.hbar.set, yscrollcommand=self.vbar.set)
                self.vbar.config(command=self.canvas.yview)
                self.hbar.config(command=self.canvas.xview)
            self.canvas.pack(side="top", fill="both", expand=True)
            if AUTO_SCALE_TO_FIT:
                root.bind("<Configure>", self._on_window_configure, add="+")
            log.debug(f"Decorated window created (image {iw}x{ih}) - final size/scale set by _recalculate_display()")

        self.base_img_w, self.base_img_h = iw, ih
        self._raw_img_w, self._raw_img_h = iw, ih   # dims of self.image_path itself - see _sync_base_dims_for_mode
        self.display_scale = 1.0
        self.sel_coords = None
        self.baked_image = None
        self._recalculate_display()

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
        self._ocr_redo_stack = []

        # edit (pen/highlighter/text/shape) state
        self.edit_strokes = []
        self.edit_current_stroke = None
        self._edit_redo_stack = []
        self.draw_color = PALETTE[0]
        self.draw_tool = "pen"
        self.pen_width = EDIT_PEN_WIDTH
        self.pen_variant = "pen"   # "pen" | "spray" - see set_pen_variant, the MS-Paint-style airbrush variant
        self.highlight_width = EDIT_HIGHLIGHT_WIDTH
        self.ocr_brush_width = OCR_BRUSH_WIDTH
        self.text_font_size = TEXT_FONT_SIZE_DEFAULT
        self.text_bg_fill = "transparent"
        self.text_box_start = None
        self.text_current_box_id = None
        self._pending_text_widget = None
        self._pending_text_window_id = None
        self._pending_text_box = None
        self.text_vim_mode = "insert"   # "insert" | "normal" | "visual" | "visual_line" - see _vim_on_key
        self._vim_visual_anchor = None
        self.shape_type = "box"
        self.shape_pending_start = None
        self.shape_preview_item_id = None
        self._pre_edit_baked_image = None
        self._pre_edit_strokes = []

        # ---- snip tool state ----
        # A "snip" is stored as another entry in self.edit_strokes (mode
        # "snip"), which is what makes it participate in the existing
        # Undo/Redo stack for free. Its own fields (see _finalize_snip):
        #   shape, orig_bounds, orig_freeform_points - the ORIGINAL cutout
        #     shape/location in capture-local coords, fixed forever, used
        #     only to punch the "hole" left behind at the source location.
        #   extracted_image - the masked RGBA pixel content, fixed at
        #     capture time (never re-cropped from the source afterward).
        #   origin, scale, rotation - the layer's CURRENT position/size/
        #     orientation, which is what moving/resizing/rotating changes.
        self.snip_shape_type = "box"   # "box" | "ellipse" | "freeform"
        self.snip_pending_start = None
        self.snip_pending_points = None   # accumulated capture-local points while drawing a freeform lasso
        self.snip_preview_item_ids = []
        self._snip_handle_ids = []
        self._snip_drag_index = None   # index of the ONE snip currently mid-drag, or None
        self._snip_drag_mode = None   # None | "move" | "nw"/"ne"/"sw"/"se" | "rotate"
        self._snip_drag_start = None
        self._snip_drag_orig_origin = None
        self._snip_drag_orig_scale = None
        self._snip_drag_orig_rotation = None
        self._snip_focused_index = None   # last snip clicked/dragged - what the clipboard keybinds act on
        self._snip_paste_position = None   # capture-local point from a click-without-drag - see _consume_snip_paste_position
        self._snip_drag_base_image = None   # cached composite (everything except the dragged snip) - see _recompute_baked_image_live_drag

        # ---- clipboard registers ----
        # "snippy" register: an in-process dict, full-fidelity (keeps the
        # actual stroke data - image, mask, current scale/rotation/origin -
        # not just a flattened picture). "system" register is the real OS
        # clipboard via xclip/wl-copy/wl-paste - necessarily flattened to a
        # plain PNG image or plain text, since that's all another
        # application could possibly understand.
        self.snippy_register = None   # {"kind": "snip_stroke"|"text_stroke"|"text_plain", ...} or None

        self.tools_row_expanded = False
        self.save_row_expanded = False
        self.dimension_label = None
        self.measure_mode_active = False
        self.measure_kind = "box"
        self.measure_boxes = []
        self.measure_active_box = None
        self.measure_active_handle = None
        self.measure_creating = False
        self.measure_start = None

        self.magnify_mode_active = False
        self.magnify_lens_size = MAGNIFIER_LENS_SIZE_DEFAULT
        self.magnify_level = MAGNIFIER_LEVEL_DEFAULT
        self.magnify_lens_item_id = None
        self.magnify_cursor_item_id = None
        self.magnify_source_image = None

        self.edit_mode_active = False
        self.ocr_paint_mode_active = False

        self.copy_on_save_var = tk.IntVar(value=int(DEFAULT_COPY_ON_SAVE))
        self.also_exit_var = tk.IntVar(value=int(DEFAULT_ALSO_EXIT))

        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self._bind_root_shortcuts(root)

        self.canvas.bind("<Button-4>", lambda e: self._on_scroll(1))
        self.canvas.bind("<Button-5>", lambda e: self._on_scroll(-1))
        self.canvas.bind("<MouseWheel>", lambda e: self._on_scroll(1 if e.delta > 0 else -1))
        self._bind_persistent_canvas_events()
        self._bind_clipboard_shortcuts()
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

    # ---- display scaling ----
    #
    # Two coordinate spaces are used throughout this class:
    #   "image space"  - real pixel coordinates in the currently-loaded working
    #                    image (self.image_path). sel_coords, edit_strokes,
    #                    ocr_strokes are ALWAYS stored in image space, so
    #                    saved/copied output is completely unaffected by
    #                    on-screen scaling.
    #   "canvas space" - where things actually get drawn, image space * display_scale.
    # measure_boxes are the one exception: kept directly in canvas space since
    # they're purely visual/ephemeral (never baked into the saved image).
    #
    # Once the canvas (background) layer is active, "image space" as used by
    # img_to_canvas/canvas_to_img means CANVAS-LOCAL coordinates (0,0 = the
    # canvas's own top-left) since that's what's actually being displayed.
    # Edit/OCR-paint strokes are stored in a separate CAPTURE-LOCAL frame
    # (see the canvas state comment in __init__) - capturelocal_to_canvas()/
    # canvas_to_capturelocal() convert between the two.

    def img_to_canvas(self, x, y):
        return x * self.display_scale, y * self.display_scale

    def canvas_to_img(self, x, y):
        return x / self.display_scale, y / self.display_scale

    def capturelocal_to_canvaslocal(self, x, y):
        return x - self.canvas_x1, y - self.canvas_y1

    def canvaslocal_to_capturelocal(self, x, y):
        return x + self.canvas_x1, y + self.canvas_y1

    def capturelocal_to_screen(self, x, y):
        """
        Stroke/handle coordinates are stored capture-local; this is the
        shortcut for placing them on the actual Tk canvas.
        """
        clx, cly = self.capturelocal_to_canvaslocal(x, y)
        return self.img_to_canvas(clx, cly)

    def _event_canvas_xy(self, event):
        """
        Converts a Tk mouse event's widget-relative position into true
        canvas-space coordinates.
        """
        return self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)

    def _event_to_capture_local(self, event):
        """
        Mouse event -> capture-local coordinates (the frame edit/OCR-paint
        strokes are stored in). Only meaningful once the canvas layer is
        active, which is guaranteed for every caller of this helper (edit,
        OCR-paint, magnify, and resize-handle mode all activate the canvas
        as their first step).
        """
        cx, cy = self._event_canvas_xy(event)
        ix, iy = self.canvas_to_img(cx, cy)
        return self.canvaslocal_to_capturelocal(ix, iy)

    def _scaled_width(self, w):
        return max(1, int(round(w * self.display_scale)))

    # ---- canvas (background layer) plumbing ----

    def _capture_layer_size(self):
        x1, y1, x2, y2 = self.sel_coords
        return int(round(x2 - x1)), int(round(y2 - y1))

    def _activate_canvas_if_needed(self):
        """
        First-touch initialization: the canvas starts out exactly matching
        the capture (offset 0,0), so nothing visually changes until the
        user actually resizes the background or draws outside the capture.
        Idempotent - safe to call at the top of every entry point that
        needs the canvas frame to exist (Edit, OCR paint, Magnify, Resize).
        """
        if self._canvas_active:
            return
        cw, ch = self._capture_layer_size()
        self.canvas_x1, self.canvas_y1 = 0, 0
        self.canvas_x2, self.canvas_y2 = cw, ch
        self.canvas_bg_color = "white"
        self._canvas_active = True
        log.debug(f"Canvas layer activated: matches capture at {cw}x{ch}")

    def _reset_canvas(self):
        """
        Called whenever a genuinely new capture/crop supersedes the old one
        (transition_to_review, monitor switch) - the old canvas frame no
        longer means anything once the underlying capture is replaced.
        """
        self.canvas_x1, self.canvas_y1 = 0, 0
        self.canvas_x2 = self.canvas_y2 = None
        self.canvas_bg_color = "white"
        self._canvas_active = False
        self._no_real_capture = False
        self._force_show_canvas_composite = False
        self.resize_target_layer = None

    def _canvas_dims(self):
        return (
            max(1, int(round(self.canvas_x2 - self.canvas_x1))),
            max(1, int(round(self.canvas_y2 - self.canvas_y1))),
        )

    def _canvas_bg_fill_rgba(self):
        if self.canvas_bg_color == "black":
            return (0, 0, 0, 255)
        if self.canvas_bg_color == "transparent":
            return (0, 0, 0, 0)
        return (255, 255, 255, 255)

    def _build_canvas_composite(self):
        """
        Fresh RGBA render of canvas-background + capture pasted at its
        current offset - the single source of truth for "what does the
        canvas layer look like right now", rebuilt on demand rather than
        cached, so background resizes never need to migrate/repad any
        pixel data (see the canvas-state comment in __init__).

        self._no_real_capture (set for --nil sessions, which have no actual
        screenshot underneath - see main()) skips the paste entirely: the
        canvas background alone is the whole picture. Without this, a
        --nil placeholder would always fully cover its own canvas (there's
        nothing "outside" it to show transparency/whitespace through).
        """
        from PIL import Image
        cw, ch = self._canvas_dims()
        canvas_img = Image.new("RGBA", (cw, ch), self._canvas_bg_fill_rgba())
        if not self._no_real_capture:
            capture = self.crop_original().convert("RGBA")
            paste_x = int(round(-self.canvas_x1))
            paste_y = int(round(-self.canvas_y1))
            canvas_img.paste(capture, (paste_x, paste_y), capture)
        return canvas_img

    def _should_show_canvas_composite(self):
        """
        The canvas (background) layer's composite is only what's actually
        displayed/interacted-with while inside Edit, Resize (a sub-screen of
        Edit), OCR paint, or Magnify - all of which need to draw/sample in
        the canvas's own coordinate frame. The plain main toolbar always
        shows the original raw capture with the normal crop-selection
        handles, exactly as before this feature existed - selection
        adjustment is independent of whatever the canvas layer has been
        resized to (per design decision, adjusting the crop should still let
        you see the rest of the original capture, same as always).

        self._force_show_canvas_composite is the one exception: a --nil
        session has no raw capture worth showing at all, so main() sets it
        immediately after construction and the composite (i.e. the blank
        canvas itself) is what the main toolbar shows from the very start.
        """
        return self._force_show_canvas_composite or (self._canvas_active and (
            self.edit_mode_active or self.resize_mode_active
            or self.ocr_paint_mode_active or self.magnify_mode_active
        ))

    def _sync_base_dims_for_mode(self):
        """
        Switches base_img_w/h (and therefore display_scale/scrollregion/
        window sizing via _recalculate_display) between the canvas layer's
        own dimensions (while actively editing) and the raw capture's own
        dimensions (plain main toolbar / crop-selection). Returns True if
        the dimensions actually changed, so callers can skip an unnecessary
        _recalculate_display() when nothing needs to move.
        """
        new_dims = self._canvas_dims() if self._should_show_canvas_composite() else (self._raw_img_w, self._raw_img_h)
        changed = new_dims != (self.base_img_w, self.base_img_h)
        self.base_img_w, self.base_img_h = new_dims
        return changed

    def clamp_to_canvas(self, x, y):
        """
        Replaces the old clamp_to_selection: once the canvas layer is
        active, drawing tools are bounded by the CANVAS (so you can draw in
        whitespace added around the capture), not by the original capture
        rectangle.
        """
        x1, y1, x2, y2 = self.canvas_x1, self.canvas_y1, self.canvas_x2, self.canvas_y2
        return max(x1, min(x, x2)), max(y1, min(y, y2))

    @staticmethod
    def _flatten_rgba_for_processing(img, bg=(255, 255, 255)):
        """
        OCR/Magnify/resize-sampling all need a flat RGB image to work with;
        this composites onto an opaque background if the working image is
        currently RGBA (transparent canvas background).
        """
        from PIL import Image
        if img.mode != "RGBA":
            return img.convert("RGB") if img.mode != "RGB" else img
        flat = Image.new("RGB", img.size, bg)
        flat.paste(img, (0, 0), img)
        return flat

    # ---- geometry enforcement ----
    #
    # Confirmed via real-world logs: toggling overrideredirect() (in either
    # direction) and then setting geometry() can get silently clobbered
    # afterward - sometimes to a shifted position, sometimes to something
    # close to full-monitor size - unpredictably, and NOT because the WM is
    # "managing" the window (overrideredirect windows explicitly aren't).
    # A single immediate re-assertion isn't reliable because the clobbering
    # can happen asynchronously, after our code has already moved on and
    # before Tk's event loop has even started running. The fix: route every
    # intentional geometry() call through _set_geometry, which remembers
    # what we asked for and, for a short window afterward, snaps back
    # anything that doesn't match via a <Configure> watcher.

    # ---- shared window-level bindings ----

    def _bind_root_shortcuts(self, target):
        """
        Escape, Ctrl+letter shortcuts, cleanup-on-destroy, and geometry
        enforcement - everything that needs to be bound to whichever window
        is currently "the" visible one. Factored out so transition_to_review
        can rebind all of this onto the new review Toplevel, since bindings
        don't carry over between separate window objects.
        """
        target.bind("<Escape>", self._handle_escape)
        target.bind("<Destroy>", self._cleanup_temp_files)
        target.bind("<Control-c>", lambda e: self._keybind_copy())
        target.bind("<Control-s>", lambda e: self._keybind_slop_save())
        target.bind("<Control-S>", lambda e: self._keybind_normal_save())
        target.bind("<Control-e>", lambda e: self._keybind_edit())
        target.bind("<Control-t>", lambda e: self._keybind_extract_text())
        target.bind("<Control-u>", lambda e: self._keybind_undo())
        target.bind("<Configure>", self._enforce_expected_geometry, add="+")

    def _last_intended_position(self):
        """
        Returns the x,y WE last explicitly requested via _set_geometry,
        rather than re-querying winfo_x()/winfo_y(). On X11, a decorated
        Toplevel's winfo_x()/winfo_y() can reflect the window manager's own
        frame/border offset rather than the exact client-area position we
        set via geometry() - reading it back and feeding it straight into
        another geometry() call (as _recalculate_display and
        _ensure_window_fits_toolbar both need to, to preserve the window's
        position across a toolbar-driven resize) then compounds that same
        offset every single time, which is exactly the "window creeps
        right/down every time you switch toolbars" bug this replaces.
        Falls back to winfo_x()/winfo_y() only if geometry has never been
        set yet (shouldn't normally happen after __init__).
        """
        if self._expected_geometry:
            try:
                _, xy = self._expected_geometry.split("+", 1)
                x_str, y_str = xy.split("+")
                return int(x_str), int(y_str)
            except Exception:
                pass
        return self.root.winfo_x(), self.root.winfo_y()

    def _set_geometry(self, geo_string):
        self.root.geometry(geo_string)
        self._expected_geometry = geo_string
        self._geometry_enforcement_active = True
        if self._geometry_disarm_after_id:
            try:
                self.root.after_cancel(self._geometry_disarm_after_id)
            except Exception:
                pass
        self._geometry_disarm_after_id = self.root.after(400, self._disarm_geometry_enforcement)

    def _disarm_geometry_enforcement(self):
        self._geometry_enforcement_active = False
        self._geometry_disarm_after_id = None

    def _enforce_expected_geometry(self, event=None):
        if not self._geometry_enforcement_active or self._enforcing_geometry:
            return
        if self.root.geometry() == self._expected_geometry:
            return
        if self._geometry_correction_after_id:
            try:
                self.root.after_cancel(self._geometry_correction_after_id)
            except Exception:
                pass
        self._geometry_correction_after_id = self.root.after(60, self._apply_geometry_correction)

    def _apply_geometry_correction(self):
        self._geometry_correction_after_id = None
        if not self._geometry_enforcement_active:
            return
        current = self.root.geometry()
        if current != self._expected_geometry:
            self._enforcing_geometry = True
            log.warning(f"Window geometry drifted to {current} (expected {self._expected_geometry}) - correcting")
            self.root.geometry(self._expected_geometry)
            self._enforcing_geometry = False

    def _update_scale_label(self):
        if self.scale_label is not None:
            try:
                self.scale_label.config(text=f"Scale: {self.display_scale:.2f}x")
            except tk.TclError:
                pass

    def _set_window_title(self, submenu=None):
        """
        WINDOW_TITLE_BASE alone on the main toolbar, or
        "{WINDOW_TITLE_BASE} - {submenu}" while a submenu (Edit, Resize,
        Extract Text, etc.) is showing - called from each show_*_toolbar
        method with that screen's own name. Only meaningful for decorated
        windows (self.root has a real titlebar) - the borderless
        select-mode overlay has no title to show at all.
        """
        if self.toolbar_container is None:
            return
        title = WINDOW_TITLE_BASE if not submenu else f"{WINDOW_TITLE_BASE} - {submenu}"
        try:
            self.root.title(title)
        except tk.TclError:
            pass

    def _refresh_base_display(self):
        """
        Rebuilds the single scaled image shown on the canvas. While
        actively editing (Edit/Resize/OCR-paint/Magnify) this is the canvas
        composite (or self.baked_image, which already IS that composite
        plus baked strokes). On the plain main toolbar it's always the raw
        capture with any baked edits overlaid at their crop position,
        exactly as before this feature existed - see
        _should_show_canvas_composite for why.
        """
        from PIL import Image
        if self._should_show_canvas_composite():
            if self.baked_image is not None:
                base = self.baked_image.copy()
            else:
                base = self._build_canvas_composite()
        else:
            try:
                base = Image.open(self.image_path).convert("RGB")
            except Exception:
                log.exception("Could not open image_path for display refresh")
                return
            if self.baked_image is not None and self.sel_coords is not None:
                x1, y1, _, _ = (int(v) for v in self.sel_coords)
                overlay = self.baked_image.convert("RGB") if self.baked_image.mode == "RGBA" else self.baked_image
                mask = self.baked_image if self.baked_image.mode == "RGBA" else None
                base.paste(overlay, (x1, y1), mask)
        scale = self.display_scale
        if abs(scale - 1.0) > 1e-6:
            disp = base.resize(
                (max(1, int(base.width * scale)), max(1, int(base.height * scale))),
                Image.LANCZOS if scale < 1 else Image.NEAREST,
            )
        else:
            disp = base
        photo = self._pil_to_tk(disp)
        if photo is None:
            return
        self._base_display_photo = photo
        if self._base_photo_id is not None:
            self.canvas.delete(self._base_photo_id)
        self._base_photo_id = self.canvas.create_image(0, 0, anchor="nw", image=photo)
        self.canvas.tag_lower(self._base_photo_id)

    def _recalculate_display(self, keep_position=False):
        """
        (Re)computes display_scale for the current working image and, in
        decorated-window mode, the window's outer size - capped to
        MAX_WINDOW_WIDTH/MAX_WINDOW_HEIGHT (and never bigger than the
        monitor itself, if smaller than that).

        Tiny captures are scaled UP to MIN_CANVAS_DISPLAY_SIZE globally.
        keep_position=True keeps the window's current top-left corner fixed
        (used after Resize/Undo) instead of re-centering on the monitor.

        This only sets the INITIAL size for a fresh capture/crop/monitor
        switch - it never restricts manually resizing the window afterward.
        """
        self._sync_base_dims_for_mode()
        iw, ih = self.base_img_w, self.base_img_h
        if self.toolbar_container is None:
            self.display_scale = 1.0
            self.canvas.configure(scrollregion=(0, 0, iw, ih))
            self._refresh_base_display()
            return

        mx, my, mw, mh = self.current_monitor_bounds
        max_w = max(MIN_WINDOW_WIDTH, min(MAX_WINDOW_WIDTH, mw))
        max_total_h = max(TOOLBAR_HEIGHT + MIN_CANVAS_DISPLAY_SIZE, min(MAX_WINDOW_HEIGHT, mh))
        max_canvas_h = max_total_h - TOOLBAR_HEIGHT

        if max(iw, ih) < MIN_CANVAS_DISPLAY_SIZE:
            scale = MIN_CANVAS_DISPLAY_SIZE / max(iw, ih)
        elif AUTO_SCALE_TO_FIT:
            scale = min(max_w / iw, max_canvas_h / ih)
        else:
            scale = 1.0
        self.display_scale = scale

        disp_w = max(1, int(iw * scale))
        disp_h = max(1, int(ih * scale))

        scrollbar_pad = 0 if AUTO_SCALE_TO_FIT else SCROLLBAR_THICKNESS + VIEWPORT_BORDER_FUDGE

        win_w = max(MIN_WINDOW_WIDTH, min(disp_w + scrollbar_pad, max_w))
        canvas_h_capped = min(disp_h + scrollbar_pad, max_canvas_h)
        total_h = canvas_h_capped + TOOLBAR_HEIGHT

        self.canvas.configure(scrollregion=(0, 0, disp_w, disp_h))

        if keep_position:
            x, y = self._last_intended_position()
        else:
            x = mx + max((mw - win_w) // 2, 0)
            y = my + max((mh - total_h) // 2, 0)
        requested_geometry = f"{win_w}x{total_h}+{x}+{y}"
        self._set_geometry(requested_geometry)
        try:
            self.root.attributes("-zoomed", False)
        except tk.TclError:
            pass
        self.root.update_idletasks()

        self._refresh_base_display()
        self._update_scale_label()
        log.debug(
            f"_recalculate_display: image {iw}x{ih}, scale={scale:.3f}, "
            f"requested {requested_geometry}, actual {self.root.winfo_geometry()}"
        )

    def manual_rescale_to_fit(self):
        """
        One-shot: fits the image to the CURRENT window/canvas size (does not
        change window size). Only relevant when AUTO_SCALE_TO_FIT is False -
        the Rescale button isn't shown otherwise.
        """
        self.root.update_idletasks()
        canvas_w = self.canvas.winfo_width()
        canvas_h = self.canvas.winfo_height()
        iw, ih = self.base_img_w, self.base_img_h
        if canvas_w <= 1 or canvas_h <= 1 or iw <= 0 or ih <= 0:
            return
        old_scale = self.display_scale
        self.display_scale = min(canvas_w / iw, canvas_h / ih)
        self.canvas.configure(scrollregion=(0, 0, int(iw * self.display_scale), int(ih * self.display_scale)))
        log.info(f"Manual rescale: {old_scale:.3f} -> {self.display_scale:.3f}")
        self._redraw_after_scale_change(old_scale)

    def _on_window_configure(self, event=None):
        if event is not None and event.widget is not self.root:
            return
        if self._configure_after_id:
            self.root.after_cancel(self._configure_after_id)
        self._configure_after_id = self.root.after(150, self._auto_fit_to_current_window)

    def _auto_fit_to_current_window(self):
        self._configure_after_id = None
        self.root.update_idletasks()
        canvas_w = self.canvas.winfo_width()
        canvas_h = self.canvas.winfo_height()
        iw, ih = self.base_img_w, self.base_img_h
        if canvas_w <= 1 or canvas_h <= 1 or iw <= 0 or ih <= 0:
            return
        new_scale = min(canvas_w / iw, canvas_h / ih)
        if abs(new_scale - self.display_scale) < 0.01:
            return
        old_scale = self.display_scale
        self.display_scale = new_scale
        self.canvas.configure(scrollregion=(0, 0, int(iw * new_scale), int(ih * new_scale)))
        self._redraw_after_scale_change(old_scale)

    def _redraw_after_scale_change(self, old_scale):
        """
        Refreshes everything drawn on the canvas after display_scale
        changes outside the normal _recalculate_display flow (manual
        Rescale button, or live auto-fit on window resize).
        """
        self._refresh_base_display()
        self.draw_handles()
        if self.resize_mode_active:
            self._draw_resize_handles()
        if self.edit_mode_active:
            self._redraw_edit_strokes_preview()
            self._draw_snip_ui()
        if self.ocr_paint_mode_active:
            self._redraw_ocr_strokes_preview()
        if self.measure_mode_active and old_scale:
            self._rescale_measure_boxes(old_scale, self.display_scale)
        self._update_scale_label()

    def _redraw_edit_strokes_preview(self):
        for stroke in self.edit_strokes:
            for seg_id in stroke.get("segment_ids", []):
                self.canvas.delete(seg_id)
            if stroke.get("mode") in ("text", "shape", "snip"):
                stroke["segment_ids"] = []
                continue
            new_ids = []
            pts = stroke["points"]
            width = self._scaled_width(stroke.get("width", EDIT_PEN_WIDTH))
            for i in range(1, len(pts)):
                lx, ly = self.capturelocal_to_screen(*pts[i - 1])
                cx, cy = self.capturelocal_to_screen(*pts[i])
                new_ids.append(self._draw_preview_segment(lx, ly, cx, cy, stroke["color"], stroke["mode"], width))
            stroke["segment_ids"] = new_ids

    def _redraw_ocr_strokes_preview(self):
        for stroke in self.ocr_strokes:
            for seg_id in stroke.get("segment_ids", []):
                self.canvas.delete(seg_id)
            new_ids = []
            pts = stroke["points"]
            width = self._scaled_width(stroke.get("width", OCR_BRUSH_WIDTH))
            for i in range(1, len(pts)):
                lx, ly = self.capturelocal_to_screen(*pts[i - 1])
                cx, cy = self.capturelocal_to_screen(*pts[i])
                new_ids.append(self.canvas.create_line(
                    lx, ly, cx, cy, fill="yellow", width=width, capstyle=tk.ROUND, joinstyle=tk.ROUND, stipple="gray50",
                ))
            stroke["segment_ids"] = new_ids

    def _rescale_measure_boxes(self, old_scale, new_scale):
        """
        Measure boxes are kept in canvas space (they're never baked into
        the saved image), so a scale change needs their canvas coordinates
        re-derived proportionally rather than reconverted through image space.
        """
        if not old_scale:
            return
        ratio = new_scale / old_scale
        for box in self.measure_boxes:
            x1, y1, x2, y2 = box["coords"]
            box["coords"] = (x1 * ratio, y1 * ratio, x2 * ratio, y2 * ratio)
            self.canvas.coords(box["rect_id"], *box["coords"])
        for idx in range(len(self.measure_boxes)):
            self._draw_measure_handles(idx)
            box = self.measure_boxes[idx]
            if box.get("label") is not None:
                try:
                    box["label"].config(text=self._measure_label_text(box))
                except tk.TclError:
                    pass

    # ---- rectangle selection ----

    def _handle_escape(self, event=None):
        if self.resize_mode_active:
            log.info("Escape pressed while in Resize - returning to the edit toolbar")
            self._exit_resize_mode()
            return
        if self.measure_mode_active:
            self.exit_measure_mode()
            return
        if self.shape_pending_start is not None:
            log.info("Escape pressed while a shape was pending its second click - cancelling it")
            self._cancel_pending_shape()
            self.show_edit_toolbar()
            return
        if self.edit_mode_active:
            self.cancel_edit()
            return
        if self.ocr_paint_mode_active:
            self.cancel_ocr_paint()
            return
        if self.magnify_mode_active:
            self.exit_magnify_mode()
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
        pad = 10  # Tolerance for click register.
        return (bbox[0] - pad) <= x <= (bbox[2] + pad) and (bbox[1] - pad) <= y <= (bbox[3] + pad)

    def on_press(self, event):
        _t0 = time.monotonic()
        cx, cy = self._event_canvas_xy(event)
        if self.hit_reset_button(cx, cy):
            if self.rect:
                self.canvas.delete(self.rect)
                self.rect = None
            self.active_handle = None
            self.reset_selection()
            self._log_if_slow("on_press(reset)", _t0)
            return
        handle = self.hit_handle(cx, cy)
        if handle:
            self.active_handle = handle
            self._log_if_slow("on_press(handle)", _t0)
            return
        self.clear_handles()
        self.active_handle = None
        self.start_x, self.start_y = cx, cy
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
        self._log_if_slow("on_press(new rect)", _t0)

    def _log_if_slow(self, label, t0, threshold=0.015):
        """
        Diagnostic only: logs a warning if a mouse-event handler takes
        longer than `threshold` seconds, so a slow/laggy interaction shows
        up directly in the log instead of needing to be guessed at.
        """
        dt = time.monotonic() - t0
        if dt > threshold:
            log.warning(f"{label} took {dt * 1000:.1f}ms (possible source of input lag)")

    def on_drag(self, event):
        _t0 = time.monotonic()
        cx, cy = self._event_canvas_xy(event)
        if self.active_handle:
            self.resize_with_handle(self.active_handle, cx, cy)
        elif self.rect:
            self.canvas.coords(self.rect, self.start_x, self.start_y, cx, cy)
            self.update_dimension_display(override=(
                abs(cx - self.start_x) / self.display_scale,
                abs(cy - self.start_y) / self.display_scale,
            ))
        self._log_if_slow("on_drag", _t0)

    def on_release(self, event):
        _t0 = time.monotonic()
        if self.rect:
            x1, y1, x2, y2 = self.canvas.coords(self.rect)
            ix1, iy1 = self.canvas_to_img(min(x1, x2), min(y1, y2))
            ix2, iy2 = self.canvas_to_img(max(x1, x2), max(y1, y2))
            self.sel_coords = (ix1, iy1, ix2, iy2)
            log.info(f"Selection made: {self.sel_coords}")
            if self.mode == "monitor":
                self.transition_to_review()
            else:
                self.draw_handles()
                self.show_main_toolbar()
        self.active_handle = None
        self._log_if_slow("on_release", _t0, threshold=0.05)

    def transition_to_review(self):
        """
        Select mode only: once a region is chosen, shrink down from the
        full-screen borderless overlay into a small decorated window holding
        just the selection - frees the rest of the desktop for normal use
        while you work with Save/Copy/Edit/Extract Text. Use Redo Capture
        if you need a different region.

        Built as a SEPARATE Toplevel rather than mutating the existing
        overlay window in place: the old overlay covers the entire virtual
        desktop, so hiding it before the replacement is ready leaves nothing
        covering the screen for however long the rebuild takes - a visible
        flash regardless of what color anything involved is set to. Instead,
        the new window is fully built, sized, and rendered while still
        hidden AND while the old fullscreen overlay is still intact and
        covering the screen; only once it's completely ready do we show the
        new one and hide the old one, back to back.
        """
        from PIL import Image
        log.info("Selection finalized, transitioning to compact review window")
        old_overlay = self.root
        try:
            old_overlay.grab_release()
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
        self.base_img_w, self.base_img_h = new_iw, new_ih
        self._raw_img_w, self._raw_img_h = new_iw, new_ih
        self._reset_canvas()

        new_win = tk.Toplevel(self._true_root, bg="#1a1a1a")
        new_win.withdraw()
        new_win.attributes("-topmost", True)
        new_win.title(WINDOW_TITLE_BASE)
        self._app_icon_image = _load_app_icon()   # keep a reference - Tk drops the icon if this gets garbage collected
        if self._app_icon_image is not None:
            new_win.iconphoto(True, self._app_icon_image)
        new_win.configure(cursor="crosshair", bg="#1a1a1a")

        self.root = new_win
        self.toolbar_container = tk.Frame(new_win, bg="#222222", height=TOOLBAR_HEIGHT)
        self.toolbar_container.pack(side="bottom", fill="x")
        self.toolbar_container.pack_propagate(False)
        self.canvas_frame = tk.Frame(new_win, bg="#1a1a1a")
        self.canvas_frame.pack(side="top", fill="both", expand=True)
        if not AUTO_SCALE_TO_FIT:
            self.vbar = tk.Scrollbar(self.canvas_frame, orient="vertical", width=SCROLLBAR_THICKNESS)
            self.hbar = tk.Scrollbar(self.canvas_frame, orient="horizontal", width=SCROLLBAR_THICKNESS)
            self.vbar.pack(side="right", fill="y")
            self.hbar.pack(side="bottom", fill="x")
        self.canvas = tk.Canvas(self.canvas_frame, highlightthickness=0, bg="#1a1a1a")
        if not AUTO_SCALE_TO_FIT:
            self.canvas.configure(xscrollcommand=self.hbar.set, yscrollcommand=self.vbar.set)
            self.vbar.config(command=self.canvas.yview)
            self.hbar.config(command=self.canvas.xview)
        self.canvas.pack(side="top", fill="both", expand=True)
        self._rebind_selection_handlers()
        self.canvas.bind("<Button-4>", lambda e: self._on_scroll(1))
        self.canvas.bind("<Button-5>", lambda e: self._on_scroll(-1))
        self.canvas.bind("<MouseWheel>", lambda e: self._on_scroll(1 if e.delta > 0 else -1))
        self._bind_persistent_canvas_events()
        self._bind_clipboard_shortcuts()
        self._bind_root_shortcuts(new_win)
        new_win.bind("<Destroy>", self._cascade_destroy_true_root, add="+")
        if AUTO_SCALE_TO_FIT:
            new_win.bind("<Configure>", self._on_window_configure, add="+")

        self.current_monitor_bounds = (mx, my, mw, mh)

        self.mode = "window"
        self.sel_coords = (0, 0, new_iw, new_ih)
        self.baked_image = None
        self.edit_strokes = []
        self.ocr_strokes = []

        self._recalculate_display()
        new_win.update_idletasks()
        old_overlay.attributes("-topmost", False)
        new_win.deiconify()
        new_win.lift()
        new_win.focus_force()
        old_overlay.withdraw()
        old_overlay.overrideredirect(False)
        notify("Capture taken", blocking=False)

        log.debug(f"Review window placed (image {new_iw}x{new_ih}) on monitor near ({global_cx:.0f},{global_cy:.0f})")
        self.draw_handles()
        self.show_main_toolbar()

    def _cascade_destroy_true_root(self, event=None):
        """
        Once the review window (a Toplevel distinct from self._true_root)
        is destroyed via any of the app's normal self.root.destroy() calls,
        this makes sure the original hidden root goes with it - otherwise
        mainloop() (which is tied to self._true_root) would keep running
        with nothing left visible.
        """
        if event is not None and event.widget is not self.root:
            return
        try:
            if self._true_root is not self.root and self._true_root.winfo_exists():
                self._true_root.destroy()
        except Exception:
            pass


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
        relevant to the current mode: magnifier zoom, pen/highlighter/shape
        width, text font size, or OCR brush width. A no-op everywhere else.

        Suppressed entirely while a middle-mouse pan drag is held, and for
        PAN_WHEEL_SUPPRESS_SECONDS afterward.
        """
        if self._panning:
            return
        if self._pan_released_at is not None and (time.monotonic() - self._pan_released_at) < PAN_WHEEL_SUPPRESS_SECONDS:
            return
        if self.magnify_mode_active:
            self.magnify_level = max(2, min(16, self.magnify_level + direction))
            self.show_magnify_toolbar()
        elif self.edit_mode_active and not self.resize_mode_active:
            if self.draw_tool == "pen":
                self.pen_width = max(1, min(30, self.pen_width + direction))
                self.show_edit_toolbar()
            elif self.draw_tool == "highlight":
                self.highlight_width = max(4, min(60, self.highlight_width + direction))
                self.show_edit_toolbar()
            elif self.draw_tool == "shape":
                self.pen_width = max(1, min(30, self.pen_width + direction))
                self.show_edit_toolbar()
            elif self.draw_tool == "text":
                self.text_font_size = max(10, min(72, self.text_font_size + direction))
                self.show_edit_toolbar()
        elif self.ocr_paint_mode_active:
            self.ocr_brush_width = max(4, min(60, self.ocr_brush_width + direction))
            self.show_ocr_toolbar()

    def _on_ctrl_scroll(self, direction):
        """
        Ctrl+wheel zoom - works in any decorated review window regardless of
        which tool/mode is active (including Magnify). A one-shot incremental
        rescale - if AUTO_SCALE_TO_FIT is on, the next window resize will
        recompute fit-to-window and override it, which is expected.
        """
        if self.toolbar_container is None:
            return
        old_scale = self.display_scale
        new_scale = max(0.05, min(20.0, old_scale * (1 + CTRL_SCROLL_ZOOM_STEP * direction)))
        if abs(new_scale - old_scale) < 1e-4:
            return
        self.display_scale = new_scale
        iw, ih = self.base_img_w, self.base_img_h
        self.canvas.configure(scrollregion=(0, 0, int(iw * new_scale), int(ih * new_scale)))
        self._redraw_after_scale_change(old_scale)

    def _pan_start(self, event):
        self._panning = True
        self.canvas.scan_mark(event.x, event.y)

    def _pan_drag(self, event):
        self.canvas.scan_dragto(event.x, event.y, gain=1)

    def _pan_end(self, event):
        self._panning = False
        self._pan_released_at = time.monotonic()

    def _bind_persistent_canvas_events(self):
        """
        Bindings that stay active across every tool/mode switch (which only
        rebind Button-1/B1-Motion/ButtonRelease-1): Ctrl+wheel zoom and
        middle-mouse-drag panning. Called once per canvas.
        """
        self.canvas.bind("<Control-Button-4>", lambda e: self._on_ctrl_scroll(1))
        self.canvas.bind("<Control-Button-5>", lambda e: self._on_ctrl_scroll(-1))
        self.canvas.bind("<Control-MouseWheel>", lambda e: self._on_ctrl_scroll(1 if e.delta > 0 else -1))
        self.canvas.bind("<ButtonPress-2>", self._pan_start)
        self.canvas.bind("<B2-Motion>", self._pan_drag)
        self.canvas.bind("<ButtonRelease-2>", self._pan_end)

    def _cleanup_temp_files(self, event=None):
        """
        Removes incidental temp files this app created (the initial
        capture, any crop from transition_to_review, any monitor-switch
        recapture) once the window is actually destroyed - regardless of
        which action (Copy, Save, Cancel, Escape, OCR Close) triggered it.
        Never touches anything explicitly saved (Screenshots,
        Slop-o-graphs, sniptext) or do_copy's clipboard temp file.
        """
        if event is not None and event.widget != self.root:
            return
        if self._geometry_disarm_after_id:
            try:
                self.root.after_cancel(self._geometry_disarm_after_id)
            except Exception:
                pass
            self._geometry_disarm_after_id = None
        if self._geometry_correction_after_id:
            try:
                self.root.after_cancel(self._geometry_correction_after_id)
            except Exception:
                pass
            self._geometry_correction_after_id = None
        if self._configure_after_id:
            try:
                self.root.after_cancel(self._configure_after_id)
            except Exception:
                pass
            self._configure_after_id = None
        for p in self.owned_temp_paths:
            try:
                Path(p).unlink(missing_ok=True)
            except Exception:
                log.exception(f"Failed to clean up temp file {p} (non-fatal)")

    def redo_capture(self):
        """
        Restarts the whole capture flow from scratch by re-running the
        script fresh with the same arguments.
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
        """
        The normal crop-selection handles. These work independently of the
        canvas (background) layer's own state - resizing/repositioning the
        canvas never disables or repositions these; only the currently
        active mode (Edit/Resize/OCR-paint/Magnify, which show the canvas
        composite instead of the raw capture) hides them, via not being
        called from those modes' own toolbars.
        """
        self.clear_handles()
        if self.resize_mode_active:
            # Resize draws its own handles via _draw_resize_handles.
            return
        if not self.sel_coords:
            return
        x1, y1, x2, y2 = self.sel_coords
        cx1, cy1 = self.img_to_canvas(x1, y1)
        cx2, cy2 = self.img_to_canvas(x2, y2)
        corners = {"nw": (cx1, cy1), "ne": (cx2, cy1), "sw": (cx1, cy2), "se": (cx2, cy2)}
        for name, (hx, hy) in corners.items():
            hid = self.canvas.create_rectangle(
                hx - HANDLE_SIZE, hy - HANDLE_SIZE,
                hx + HANDLE_SIZE, hy + HANDLE_SIZE,
                fill="#00b4ff", outline="white",
            )
            self.handles.append((hid, name))
        self.draw_reset_button(cx1, cy1, cx2, cy2)

    def draw_reset_button(self, x1, y1, x2, y2):
        """
        Small "x" button just inside the top edge, centered horizontally -
        resets back to the full image.

        Click handling is done via hit_reset_button() inside on_press,
        NOT via canvas.tag_bind - item-level tag_bind and the widget-level
        <ButtonPress-1> binding (on_press itself) are separate binding
        layers in Tk, and "break" from one does not stop the other from
        """
        bx, by = (x1 + x2) / 2, y1 + 18
        circ = self.canvas.create_oval(
            bx - 10, by - 10, bx + 10, by + 10, fill="#333333", outline="white",
        )
        txt = self.canvas.create_text(bx, by, text="\u00d7", fill="white", font=("", 11, "bold"))
        self.reset_btn_items = [circ, txt]

    def reset_selection(self, event=None):
        log.info("Selection reset to full image")
        iw, ih = self.base_img_w, self.base_img_h
        self.sel_coords = (0, 0, iw, ih)
        self.draw_handles()
        self.show_main_toolbar()

    def resize_with_handle(self, name, canvas_x, canvas_y):
        x, y = self.canvas_to_img(canvas_x, canvas_y)
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
        cx1, cy1 = self.img_to_canvas(x1, y1)
        cx2, cy2 = self.img_to_canvas(x2, y2)
        self.canvas.coords(self.rect, cx1, cy1, cx2, cy2)
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
            x, y = self._last_intended_position()
            self._set_geometry(f"{new_w}x{new_h}+{x}+{y}")
            log.debug(f"Resized window to fit toolbar content: {new_w}x{new_h} (was {current_w}x{current_h})")

    def show_main_toolbar(self, reset_tools_row=True, reset_save_row=True):
        if reset_tools_row:
            self.tools_row_expanded = False
        if reset_save_row:
            self.save_row_expanded = False
        self.edit_mode_active = False
        self.ocr_paint_mode_active = False
        self.resize_mode_active = False
        self._ocr_overlay = None
        self._ocr_text_widget = None
        self._ocr_copy_var = None
        self._hide_snip_ui()   # the main toolbar never shows snip handles/borders - see _should_show_canvas_composite
        if self._sync_base_dims_for_mode():
            self._recalculate_display(keep_position=True)
        else:
            self._refresh_base_display()
        self._rebind_selection_handlers()
        self.clear_toolbar()
        frame = self.new_toolbar_frame()
        main_row = tk.Frame(frame, bg="#222222")
        main_row.pack(side="top", fill="x")
        tk.Button(main_row, text="Copy", pady=BUTTON_PADY, command=self.do_copy).pack(side="left", padx=4, pady=4)
        tk.Button(
            main_row, text="Save", pady=BUTTON_PADY,
            relief=("sunken" if self.save_row_expanded else "raised"),
            command=self.toggle_save_row,
        ).pack(side="left", padx=4, pady=4)
        tk.Button(main_row, text="Edit", pady=BUTTON_PADY, command=self.enter_edit_mode).pack(side="left", padx=4, pady=4)
        tk.Button(
            main_row, text="Tools", pady=BUTTON_PADY,
            relief=("sunken" if self.tools_row_expanded else "raised"),
            command=self.toggle_tools_row,
        ).pack(side="left", padx=4, pady=4)
        if self.mode == "fullscreen" and len(self.available_monitors) > 1:
            self._add_monitor_dropdown(main_row)
        else:
            tk.Button(main_row, text="Redo", pady=BUTTON_PADY, command=self.redo_capture).pack(side="left", padx=4, pady=4)
        tk.Button(main_row, text="Cancel", pady=BUTTON_PADY, command=lambda: (log.info("Cancel pressed"), self.root.destroy())).pack(side="left", padx=4, pady=4)

        checkbox_frame = tk.Frame(main_row, bg="#222222")
        checkbox_frame.pack(side="left", padx=(8, 4))
        tk.Checkbutton(
            checkbox_frame, text="Also copy", variable=self.copy_on_save_var,
            bg="#222222", fg="white", selectcolor="#444444",
            activebackground="#222222", activeforeground="white",
        ).pack(side="left")
        tk.Checkbutton(
            checkbox_frame, text="Also exit", variable=self.also_exit_var,
            bg="#222222", fg="white", selectcolor="#444444",
            activebackground="#222222", activeforeground="white",
        ).pack(side="left", padx=(8, 0))

        info_frame = tk.Frame(main_row, bg="#222222")
        info_frame.pack(side="right", padx=8)
        self.dimension_label = tk.Label(info_frame, text="", fg="white", bg="#222222")
        self.dimension_label.pack(side="top", anchor="e")
        if self.toolbar_container is not None:
            self.scale_label = tk.Label(info_frame, text="", fg="#aaaaaa", bg="#222222")
            self.scale_label.pack(side="top", anchor="e")
            if not AUTO_SCALE_TO_FIT:
                self.rescale_btn = tk.Button(self.toolbar_container, text="Re-scale", pady=BUTTON_PADY, command=self.manual_rescale_to_fit)
                self.rescale_btn.place(relx=1.0, rely=1.0, x=-6, y=-4, anchor="se")
            else:
                self.rescale_btn = None
        else:
            self.scale_label = None
            self.rescale_btn = None
        self._update_scale_label()

        if self.save_row_expanded:
            save_row = tk.Frame(frame, bg="#222222")
            save_row.pack(side="top", fill="x")
            tk.Button(save_row, text="Save", pady=BUTTON_PADY, command=self.begin_normal_save).pack(side="left", padx=4, pady=4)
            tk.Button(save_row, text="Slop Save", pady=BUTTON_PADY, command=self.do_slop_save).pack(side="left", padx=4, pady=4)
        elif self.tools_row_expanded:
            tools_row = tk.Frame(frame, bg="#222222")
            tools_row.pack(side="top", fill="x")
            tk.Button(tools_row, text="Extract Text", pady=BUTTON_PADY, command=self.enter_ocr_paint_mode).pack(side="left", padx=4, pady=4)
            tk.Button(tools_row, text="Measure", pady=BUTTON_PADY, command=self.enter_measure_mode).pack(side="left", padx=4, pady=4)
            tk.Button(tools_row, text="Magnify", pady=BUTTON_PADY, command=self.enter_magnify_mode).pack(side="left", padx=4, pady=4)

        self.finish_toolbar_placement(frame)
        self.update_dimension_display()
        if self.tools_row_expanded:
            self._set_window_title("tools")
        elif self.save_row_expanded:
            self._set_window_title("save")
        else:
            self._set_window_title()

    def toggle_tools_row(self):
        self.tools_row_expanded = not self.tools_row_expanded
        if self.tools_row_expanded:
            self.save_row_expanded = False
        self.show_main_toolbar(reset_tools_row=False, reset_save_row=False)

    def toggle_save_row(self):
        self.save_row_expanded = not self.save_row_expanded
        if self.save_row_expanded:
            self.tools_row_expanded = False
        self.show_main_toolbar(reset_tools_row=False, reset_save_row=False)

    def _rebind_selection_handlers(self):
        """
        Restores the canvas to normal selection-dragging behavior. Called
        by show_main_toolbar so every path back to the main toolbar (Done,
        Cancel, OCR Close/Copy/Save, measure exit, etc.).
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
            w, h = int(override[0]), int(override[1])
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
        dropdown.config(pady=BUTTON_PADY)
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
        try:
            if old_path and Path(old_path).exists():
                Path(old_path).unlink()
            if old_path in self.owned_temp_paths:
                self.owned_temp_paths.remove(old_path)
        except Exception:
            log.exception("Could not remove previous monitor capture temp file (non-fatal)")

        self.base_img_w, self.base_img_h = w, h
        self._raw_img_w, self._raw_img_h = w, h
        self.current_monitor_bounds = (x, y, w, h)
        self.sel_coords = (0, 0, w, h)
        self.baked_image = None
        self.edit_strokes = []
        self.ocr_strokes = []
        self.current_monitor_name = name
        self._reset_canvas()

        self._recalculate_display()

        log.debug(f"Switched to monitor {name}: {w}x{h}")
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
        if self._canvas_active:
            return self._build_canvas_composite()
        return self.crop_original()

    # ---- edit mode ----

    def enter_edit_mode(self):
        log.info("Entering edit mode")
        self._activate_canvas_if_needed()
        self.edit_mode_active = True
        if self._sync_base_dims_for_mode():
            self._recalculate_display(keep_position=True)
        else:
            self._refresh_base_display()
        self._pre_edit_baked_image = self.baked_image
        self._pre_edit_strokes = list(self.edit_strokes)
        self._edit_redo_stack = []
        self._snip_drag_index = None
        self._snip_drag_mode = None
        self._snip_drag_base_image = None
        self._snip_paste_position = None
        self._snip_focused_index = None
        self._clear_snip_handles()
        self.snip_pending_start = None
        self.snip_pending_points = None
        self.clear_toolbar()
        self.clear_handles()
        self.canvas.unbind("<ButtonPress-1>")
        self.canvas.unbind("<B1-Motion>")
        self.canvas.unbind("<ButtonRelease-1>")
        self.canvas.bind("<ButtonPress-1>", self.edit_press)
        self.canvas.bind("<B1-Motion>", self.edit_drag)
        self.canvas.bind("<ButtonRelease-1>", self.edit_release)
        self.show_edit_toolbar()

    def edit_press(self, event):
        if self.resize_mode_active:
            return
        # Toolbar buttons (Snip, Pen, Box, etc.) grab Tk keyboard focus when
        # clicked, and nothing ever hands it back to the canvas on its own -
        # without this, every canvas-level keybind (clipboard shortcuts,
        # etc.) silently never fires after you've touched any button,
        # even though the click itself still updates focused/dragged state
        # correctly (mouse events don't need keyboard focus). Reclaim it on
        # every canvas press so keyboard shortcuts work right after you've
        # clicked/dragged something, which is exactly when you'd use them.
        self.canvas.focus_set()
        if self.draw_tool != "snip":
            # A non-snip drawing gesture is starting - hide every snip's UI
            # for its duration (edit_release brings them all back).
            self._hide_snip_ui()
        if self.draw_tool == "snip":
            screen_x, screen_y = self._event_canvas_xy(event)
            x, y = self._event_to_capture_local(event)
            x, y = self.clamp_to_canvas(x, y)
            self._snip_press(x, y, screen_x, screen_y)
            return
        x, y = self._event_to_capture_local(event)
        x, y = self.clamp_to_canvas(x, y)
        if self.draw_tool == "text":
            self.text_box_start = (x, y)
            if self.text_current_box_id:
                self.canvas.delete(self.text_current_box_id)
            cx, cy = self.capturelocal_to_screen(x, y)
            self.text_current_box_id = self.canvas.create_rectangle(
                cx, cy, cx, cy, outline="#00b4ff", width=2, dash=(4, 2),
            )
            return
        if self.draw_tool == "shape":
            self.shape_pending_start = (x, y)
            self._draw_shape_preview((x, y), (x, y))
            return
        if self.draw_tool == "pen" and self.pen_variant == "spray":
            self.edit_current_stroke = {"mode": "spray", "dots": [], "color": self.draw_color, "segment_ids": []}
            self._spray_burst(x, y)
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

    def _spray_burst(self, cx, cy):
        """
        Scatters SPRAY_DOTS_PER_BURST random dots within self.pen_width (the
        "Spread" slider, while in spray mode) of the given capture-local
        point, into self.edit_current_stroke["dots"] - the MS-Paint-style
        airbrush variant of the Draw tool (see set_pen_variant). Called once
        on press and again as the drag moves - throttled to at most once
        per SPRAY_MIN_BURST_DIST of actual cursor movement (see there for
        why), so a slow or event-heavy drag doesn't rack up far more bursts
        than the physical drag distance would suggest, while still scaling
        up naturally the further/longer you actually drag.

        Dot positions/sizes are generated ONCE here and stored directly in
        the stroke (not re-randomized on every bake), since
        _recompute_baked_image rebuilds from scratch on nearly every
        action - re-rolling the random pattern each time would make the
        spray visibly jitter/change every time anything else on the canvas
        was touched.
        """
        spread = max(2, self.pen_width)
        stroke = self.edit_current_stroke
        stroke["last_burst_pos"] = (cx, cy)
        for _ in range(SPRAY_DOTS_PER_BURST):
            angle = random.uniform(0, 2 * math.pi)
            dist = random.uniform(0, spread)
            dot_x = cx + math.cos(angle) * dist
            dot_y = cy + math.sin(angle) * dist
            dot_r = random.uniform(*SPRAY_DOT_RADIUS_RANGE)
            stroke["dots"].append((dot_x, dot_y, dot_r))
            sx, sy = self.capturelocal_to_screen(dot_x, dot_y)
            r_screen = max(0.5, dot_r * self.display_scale)
            seg_id = self.canvas.create_oval(
                sx - r_screen, sy - r_screen, sx + r_screen, sy + r_screen,
                fill=stroke["color"], outline="",
            )
            stroke["segment_ids"].append(seg_id)

    SPRAY_SUPERSAMPLE = 4   # renders spray dots this many times larger, then downsamples with LANCZOS

    def _bake_spray_stroke(self, base, stroke, ox, oy):
        """
        Spray dots are tiny (SPRAY_DOT_RADIUS_RANGE is well under 2px), and
        PIL's ImageDraw.ellipse has no antialiasing at all - drawing them
        directly at that size produces harsh, jagged, hard-edged blobs that
        look noticeably denser and "fuzzier" (aliasing artifacts, not actual
        blur) than the smoothly-rendered Tk canvas ovals shown live while
        spraying. Tk's canvas item rendering IS antialiased on most modern
        builds, which is exactly the mismatch: same dots, very different
        edge quality once baked.

        Fixed via supersample-then-LANCZOS-downsample, same as before, but
        PER DOT rather than once for the whole stroke. An earlier version
        rendered every dot in the stroke onto one shared oversized patch
        and downsampled that single patch once at the end - which does
        smooth each dot's own edge, but ALSO runs every dot's antialiasing
        kernel across its overlapping neighbors at the same time, so any
        two dots close enough to touch (extremely common - a single burst
        scatters SPRAY_DOTS_PER_BURST dots within one `spread`-sized
        radius, and a slow or lingering drag adds many overlapping bursts
        on top of each other) got smeared together into one soft blob
        instead of staying visually distinct, which is what made a real
        spray session balloon into something looking like a solid painted
        line once baked, even though the same dots looked fine individually
        while actively drawing. Downsampling each dot in its own small,
        private patch keeps that blur local to a single dot's edge - later
        dots are then pasted on top of earlier ones in the same order they
        were sprayed (each paste using that dot's own alpha as its mask),
        matching exactly how the live canvas preview stacks them as
        separate items, so the baked result keeps the same grainy,
        speckled texture instead of flattening dense areas into a blob.
        """
        from PIL import Image, ImageDraw
        dots = stroke["dots"]
        if not dots:
            return
        ss = self.SPRAY_SUPERSAMPLE
        for dot_x, dot_y, r in dots:
            pad = r + 1
            min_x, max_x = dot_x - pad, dot_x + pad
            min_y, max_y = dot_y - pad, dot_y + pad
            w = max(1, int(math.ceil(max_x - min_x)))
            h = max(1, int(math.ceil(max_y - min_y)))
            patch = Image.new("RGBA", (w * ss, h * ss), (0, 0, 0, 0))
            draw = ImageDraw.Draw(patch)
            px = (dot_x - min_x) * ss
            py = (dot_y - min_y) * ss
            pr = r * ss
            draw.ellipse([px - pr, py - pr, px + pr, py + pr], fill=stroke["color"])
            patch = patch.resize((w, h), Image.LANCZOS)
            paste_x = int(round(min_x - ox))
            paste_y = int(round(min_y - oy))
            base.paste(patch, (paste_x, paste_y), patch)

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
        if self.resize_mode_active:
            return
        if self.draw_tool == "snip":
            x, y = self._event_to_capture_local(event)
            x, y = self.clamp_to_canvas(x, y)
            self._snip_drag(x, y)
            return
        x, y = self._event_to_capture_local(event)
        x, y = self.clamp_to_canvas(x, y)
        if self.draw_tool == "text":
            if self.text_box_start and self.text_current_box_id:
                cx0, cy0 = self.capturelocal_to_screen(*self.text_box_start)
                cx, cy = self.capturelocal_to_screen(x, y)
                self.canvas.coords(self.text_current_box_id, cx0, cy0, cx, cy)
            return
        if self.draw_tool == "shape":
            if self.shape_pending_start is not None:
                self._draw_shape_preview(self.shape_pending_start, (x, y))
            return
        if not self.edit_current_stroke:
            return
        if self.edit_current_stroke.get("mode") == "spray":
            lbx, lby = self.edit_current_stroke.get("last_burst_pos", (x, y))
            if (x - lbx) ** 2 + (y - lby) ** 2 < (SPRAY_MIN_BURST_DIST / self.display_scale) ** 2:
                return
            self._spray_burst(x, y)
            return
        lx, ly = self.edit_current_stroke["points"][-1]
        if (x - lx) ** 2 + (y - ly) ** 2 < (MIN_POINT_DIST / self.display_scale) ** 2:
            return
        clx, cly = self.capturelocal_to_screen(lx, ly)
        ccx, ccy = self.capturelocal_to_screen(x, y)
        seg_id = self._draw_preview_segment(
            clx, cly, ccx, ccy, self.edit_current_stroke["color"],
            self.edit_current_stroke["mode"], self._scaled_width(self.edit_current_stroke["width"]),
        )
        self.edit_current_stroke["segment_ids"].append(seg_id)
        self.edit_current_stroke["points"].append((x, y))

    def edit_release(self, event):
        if self.resize_mode_active:
            return
        if self.draw_tool == "snip":
            x, y = self._event_to_capture_local(event)
            x, y = self.clamp_to_canvas(x, y)
            self._snip_release(x, y)
            return
        self._edit_release_non_snip(event)
        self._draw_snip_ui()   # the drawing gesture (whatever it was) has ended - show everyone again

    def _edit_release_non_snip(self, event):
        x, y = self._event_to_capture_local(event)
        x, y = self.clamp_to_canvas(x, y)
        if self.draw_tool == "text":
            if not self.text_box_start:
                return
            x1b, y1b = self.text_box_start
            self.text_box_start = None
            bx1, by1, bx2, by2 = min(x1b, x), min(y1b, y), max(x1b, x), max(y1b, y)
            ccx1, ccy1 = self.capturelocal_to_screen(bx1, by1)
            ccx2, ccy2 = self.capturelocal_to_screen(bx2, by2)
            if ccx2 - ccx1 < 10 or ccy2 - ccy1 < 10:
                if self.text_current_box_id:
                    self.canvas.delete(self.text_current_box_id)
                    self.text_current_box_id = None
                return
            self._begin_text_entry(bx1, by1, bx2, by2)
            return
        if self.draw_tool == "shape":
            if self.shape_pending_start is not None:
                self._finalize_shape(x, y)
            return
        if not self.edit_current_stroke:
            return
        if self.edit_current_stroke.get("mode") == "spray":
            if self.edit_current_stroke["dots"]:
                self.edit_strokes.append(self.edit_current_stroke)
                self._edit_redo_stack = []
                log.debug(f"Spray stroke committed: {len(self.edit_current_stroke['dots'])} dots")
            self.edit_current_stroke = None
            return
        lx, ly = self.edit_current_stroke["points"][-1]
        if (x, y) != (lx, ly):
            clx, cly = self.capturelocal_to_screen(lx, ly)
            ccx, ccy = self.capturelocal_to_screen(x, y)
            seg_id = self._draw_preview_segment(
                clx, cly, ccx, ccy, self.edit_current_stroke["color"],
                self.edit_current_stroke["mode"], self._scaled_width(self.edit_current_stroke["width"]),
            )
            self.edit_current_stroke["segment_ids"].append(seg_id)
            self.edit_current_stroke["points"].append((x, y))

        if len(self.edit_current_stroke["points"]) > 1:
            self.edit_strokes.append(self.edit_current_stroke)
            self._edit_redo_stack = []
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

        Starts in vim-style Insert mode (blinking bar cursor, normal
        typing) - see _vim_on_key/_vim_escape for Normal/Visual/Visual
        Line mode, entered via Escape.
        """
        if self.text_current_box_id:
            self.canvas.delete(self.text_current_box_id)
            self.text_current_box_id = None
        cx1, cy1 = self.capturelocal_to_screen(x1, y1)
        cx2, cy2 = self.capturelocal_to_screen(x2, y2)
        text_widget = tk.Text(
            self.canvas, wrap="word", bd=1, relief="solid",
            font=("Serif", max(10, int(self.text_font_size * 0.6 * self.display_scale))),
            fg=self.draw_color, insertbackground=self.draw_color,
        )
        win_id = self.canvas.create_window(
            cx1, cy1, anchor="nw", width=int(cx2 - cx1), height=int(cy2 - cy1), window=text_widget,
        )
        self._pending_text_widget = text_widget
        self._pending_text_window_id = win_id
        self._pending_text_box = (x1, y1, x2, y2)
        text_widget.focus_set()

        self.text_vim_mode = "insert"
        self._vim_visual_anchor = None
        text_widget.tag_configure("vim_block_cursor", background="#cccccc", foreground="black")

        text_widget.bind("<Return>", self._confirm_text_entry)
        text_widget.bind("<Shift-Return>", lambda e: None)
        text_widget.bind("<Escape>", self._vim_escape)
        text_widget.bind("<FocusOut>", self._confirm_text_entry)
        text_widget.bind("<Key>", self._vim_on_key)

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
            self._edit_redo_stack = []
            log.debug(f"Text stroke added: {len(content)} chars, box={stroke['box']}, font_size={stroke['font_size']}, bg_fill={stroke['bg_fill']}")
            self._recompute_baked_image()
        return "break"

    def _cancel_text_entry(self, event=None):
        if self._pending_text_widget is None:
            return "break"
        self.canvas.delete(self._pending_text_window_id)
        self._pending_text_widget.destroy()
        self._pending_text_widget = None
        self._pending_text_window_id = None
        self._pending_text_box = None
        return "break"

    # ---- vim-style Insert/Normal/Visual modes for text entry ----
    #
    # A deliberately minimal vim emulation (no motions beyond the arrow
    # keys) layered onto the embedded Text widget: Insert (default,
    # blinking bar cursor, types normally) <-> Normal (Escape, solid block
    # cursor simulated via a tag on the character under the cursor, since
    # Tk's Text widget has no native block-cursor mode) <-> Visual/Visual
    # Line (v/V from Normal, extend with arrow keys, y/d/p act on the
    # selection). In Normal mode with NO selection, y/d/p act on the WHOLE
    # text box as one element instead (copy/delete/paste-over), which is
    # what makes "copy a text box element, not the text inside it" work.
    # The same state machine (adapted for a single-line Entry) also drives
    # OpenPromptApp's path field - see that class's _vim_* methods.

    _VIM_NAV_KEYSYMS = {"Left", "Right", "Up", "Down", "Home", "End", "Next", "Prior"}

    def _vim_escape(self, event=None):
        if self.text_vim_mode == "insert":
            self._vim_enter_normal()
            return "break"
        return self._cancel_text_entry(event)

    def _vim_enter_insert(self):
        self.text_vim_mode = "insert"
        tw = self._pending_text_widget
        if tw is None:
            return
        tw.tag_remove("vim_block_cursor", "1.0", "end")
        tw.tag_remove("sel", "1.0", "end")
        tw.config(insertwidth=2)

    def _vim_enter_normal(self):
        self.text_vim_mode = "normal"
        tw = self._pending_text_widget
        if tw is None:
            return
        tw.tag_remove("sel", "1.0", "end")
        tw.config(insertwidth=0)
        self._vim_update_block_cursor()

    def _vim_enter_visual(self, linewise):
        tw = self._pending_text_widget
        if tw is None:
            return
        self.text_vim_mode = "visual_line" if linewise else "visual"
        tw.tag_remove("vim_block_cursor", "1.0", "end")
        self._vim_visual_anchor = tw.index("insert")
        self._vim_update_visual_selection()

    def _vim_update_block_cursor(self):
        tw = self._pending_text_widget
        if tw is None:
            return
        tw.tag_remove("vim_block_cursor", "1.0", "end")
        pos = tw.index("insert")
        tw.tag_add("vim_block_cursor", pos, f"{pos}+1c")

    def _vim_update_visual_selection(self):
        tw = self._pending_text_widget
        if tw is None or self._vim_visual_anchor is None:
            return
        tw.tag_remove("sel", "1.0", "end")
        cur = tw.index("insert")
        anchor = self._vim_visual_anchor
        if self.text_vim_mode == "visual_line":
            a_line, c_line = int(anchor.split(".")[0]), int(cur.split(".")[0])
            lo, hi = sorted((a_line, c_line))
            tw.tag_add("sel", f"{lo}.0", f"{hi}.end+1c")
        elif tw.compare(anchor, "<=", cur):
            tw.tag_add("sel", anchor, f"{cur}+1c")
        else:
            tw.tag_add("sel", cur, f"{anchor}+1c")

    def _vim_yank(self):
        tw = self._pending_text_widget
        if tw is None:
            return
        if self.text_vim_mode in ("visual", "visual_line"):
            try:
                selected = tw.get("sel.first", "sel.last")
            except tk.TclError:
                selected = ""
            self.snippy_register = {"kind": "text_plain", "text": selected}
            log.info(f"Vim yank: {len(selected)} character(s) -> snippy register")
            self._vim_enter_normal()
        else:
            # No selection in Normal mode: yank the WHOLE text box as a
            # pastable element, matching a snip's copy/paste semantics.
            self._yank_whole_text_box_element()

    def _vim_delete(self):
        tw = self._pending_text_widget
        if tw is None:
            return
        if self.text_vim_mode in ("visual", "visual_line"):
            try:
                selected = tw.get("sel.first", "sel.last")
                self.snippy_register = {"kind": "text_plain", "text": selected}
                tw.delete("sel.first", "sel.last")
                log.info(f"Vim delete: removed {len(selected)} character(s), copied to snippy register")
            except tk.TclError:
                pass
            self._vim_enter_normal()
        else:
            # No selection in Normal mode: 'd' discards the whole text box,
            # same as the pre-vim Escape-cancels behavior.
            self._cancel_text_entry()

    def _vim_paste(self):
        tw = self._pending_text_widget
        if tw is None:
            return
        reg = self.snippy_register
        if reg is None or reg.get("kind") != "text_plain":
            self._paste_error("Nothing text-like in the snippy register to paste here.")
            return
        tw.insert("insert", reg["text"])
        log.info(f"Vim paste: inserted {len(reg['text'])} character(s) from the snippy register")
        if self.text_vim_mode in ("visual", "visual_line"):
            self._vim_enter_normal()

    def _yank_whole_text_box_element(self):
        """
        Treats the text box currently being typed (not yet a committed
        stroke) as a pastable element in its own right - stores its
        current content/styling/position as a "text_stroke" register
        entry, exactly like copying an already-committed text stroke would
        (see _paste_text_stroke_element).
        """
        tw = self._pending_text_widget
        if tw is None:
            return
        content = tw.get("1.0", "end-1c")
        box = self._pending_text_box
        stroke = {
            "mode": "text", "box": box, "text": content,
            "font_size": self.text_font_size, "color": self.draw_color,
            "bg_fill": self.text_bg_fill, "segment_ids": [],
        }
        self.snippy_register = {"kind": "text_stroke", "stroke": stroke}
        log.info("Vim yank (Normal mode, no selection): copied the whole text box as an element")

    def _vim_on_key(self, event):
        """
        Bound to <Key> on the embedded text widget. Insert mode: always
        lets the keypress through unchanged (normal typing). Normal mode:
        intercepts the vim command letters (i/v/V/y/d/p), lets navigation
        keys through (also nudging the block-cursor/selection highlight
        to follow), and swallows every other printable character so it
        can't be typed as literal text - that's the entire point of a
        command mode.
        """
        if self.text_vim_mode == "insert":
            return None
        keysym = event.keysym
        if keysym == "i":
            self._vim_enter_insert()
            return "break"
        if keysym == "v":
            self._vim_enter_visual(linewise=False)
            return "break"
        if keysym == "V":
            self._vim_enter_visual(linewise=True)
            return "break"
        if keysym == "y":
            self._vim_yank()
            return "break"
        if keysym == "d":
            self._vim_delete()
            return "break"
        if keysym == "p":
            self._vim_paste()
            return "break"
        if keysym in self._VIM_NAV_KEYSYMS:
            if self.text_vim_mode in ("visual", "visual_line"):
                self.root.after_idle(self._vim_update_visual_selection)
            else:
                self.root.after_idle(self._vim_update_block_cursor)
            return None
        if event.char:
            return "break"   # swallow other printable input while in a command mode
        return None

    def _draw_shape_preview(self, start, end):
        if self.shape_preview_item_id:
            self.canvas.delete(self.shape_preview_item_id)
            self.shape_preview_item_id = None
        cx1, cy1 = self.capturelocal_to_screen(*start)
        cx2, cy2 = self.capturelocal_to_screen(*end)
        width = self._scaled_width(self.pen_width)
        if self.shape_type == "line":
            self.shape_preview_item_id = self.canvas.create_line(
                cx1, cy1, cx2, cy2, fill=self.draw_color, width=width, capstyle=tk.ROUND,
            )
        elif self.shape_type == "ellipse":
            self.shape_preview_item_id = self.canvas.create_oval(
                cx1, cy1, cx2, cy2, outline=self.draw_color, width=width,
            )
        else:
            self.shape_preview_item_id = self.canvas.create_rectangle(
                cx1, cy1, cx2, cy2, outline=self.draw_color, width=width,
            )

    def _cancel_pending_shape(self):
        """
        Discards an in-progress shape drag - called when switching tools or
        cancelling, so nothing is left dangling if that happens mid-drag.
        """
        if self.shape_preview_item_id:
            self.canvas.delete(self.shape_preview_item_id)
            self.shape_preview_item_id = None
        self.shape_pending_start = None

    def _finalize_shape(self, x, y):
        start = self.shape_pending_start
        end = (x, y)
        if self.shape_preview_item_id:
            self.canvas.delete(self.shape_preview_item_id)
            self.shape_preview_item_id = None
        self.shape_pending_start = None

        cx1, cy1 = self.capturelocal_to_screen(*start)
        cx2, cy2 = self.capturelocal_to_screen(*end)
        if self.shape_type != "line" and abs(cx2 - cx1) < 4 and abs(cy2 - cy1) < 4:
            log.info("Shape finalize: too small (accidental click?) - discarding")
            return
        if self.shape_type == "line" and start == end:
            return

        stroke = {
            "mode": "shape", "shape_type": self.shape_type,
            "start": start, "end": end, "color": self.draw_color,
            "width": self.pen_width, "segment_ids": [],
        }
        self.edit_strokes.append(stroke)
        self._edit_redo_stack = []
        log.debug(f"Shape stroke added: {self.shape_type} {start}->{end} color={self.draw_color} width={self.pen_width}")
        self._recompute_baked_image()

    def pick_color(self, color):
        self.draw_color = color
        if self._pending_text_widget is not None:
            # Reflect a color change immediately in whatever's currently
            # being typed, not just on the NEXT text box - matches how the
            # color already shows live from the moment typing starts.
            try:
                self._pending_text_widget.config(fg=color, insertbackground=color)
            except tk.TclError:
                pass
        self.show_edit_toolbar()

    def pick_tool(self, tool):
        self._cancel_pending_shape()
        self._cancel_pending_snip_shape()
        self.draw_tool = tool
        self.show_edit_toolbar()

    def pick_shape_type(self, shape_type):
        self._cancel_pending_shape()
        self.shape_type = shape_type
        self.show_edit_toolbar()

    # ---- snip tool ----
    #
    # A snip cuts a shaped region out of everything rendered so far (the
    # capture, canvas background, and any earlier pen/text/shape ink) and
    # turns it into an independently movable/resizable/rotatable layer. The
    # region it was cut FROM is punched out down to the canvas background
    # (transparent/white/black, whatever the canvas layer's own color is -
    # there's no other "layer underneath" in this app, so that's what
    # "expose whatever's underneath" resolves to here) permanently; the
    # snip itself keeps its cut content fixed and just gets moved/scaled/
    # rotated on redraw, never re-sampled from its original source location.
    #
    # Stored as a "snip"-mode entry in self.edit_strokes, so it rides along
    # with the existing Undo/Redo stack for free.
    #
    # Every snip's dashed border + handles stay visible simultaneously
    # (regardless of which tool is currently selected) for as long as
    # you're in Edit and haven't hit Done/Cancel yet - there's no single
    # "active" snip. Click-drag directly on any snip's body or handles to
    # move/resize/rotate it (topmost-created snip wins if they overlap);
    # clicking empty space with the Snip tool selected starts a new one
    # instead. While any drag is in progress - moving/resizing/rotating a
    # snip, OR drawing with Pen/Highlighter/Text/Shape/a new Snip shape -
    # every OTHER snip's UI is hidden until the drag ends, to keep the
    # canvas readable; only the one actually being manipulated (if any)
    # stays visible mid-drag. This same show/hide convention is meant to be
    # reused if shape/text ever gain their own handle-based geometry editing
    # later.
    #
    # Handle positions properly track rotation (and resize, and move) -
    # they're derived from the same rotation-matrix transform used to
    # render the layer itself (empirically matched against PIL's exact
    # rotate() call - see git history/session notes), not the old unrotated
    # bounding box. Resize is anchored at the layer's CENTER rather than the
    # opposite corner specifically so it stays correct under rotation
    # without needing to reason about a rotated anchor point - a deliberate
    # trade-off (existing corner-anchored resizes elsewhere in the app -
    # selection, canvas, Full Capture - are all axis-aligned and don't have
    # this problem).

    def _pick_snip_shape(self, choice):
        self._cancel_pending_snip_shape()
        self.snip_shape_type = choice
        self.show_edit_toolbar()

    def _cancel_pending_snip_shape(self):
        self._clear_snip_preview()
        self.snip_pending_start = None
        self.snip_pending_points = None
        self._snip_drag_mode = None
        self._snip_drag_index = None

    def _clear_snip_preview(self):
        for item_id in self.snip_preview_item_ids:
            try:
                self.canvas.delete(item_id)
            except tk.TclError:
                pass
        self.snip_preview_item_ids = []

    def _clear_snip_handles(self):
        for item_id in self._snip_handle_ids:
            try:
                self.canvas.delete(item_id)
            except tk.TclError:
                pass
        self._snip_handle_ids = []

    def _hide_snip_ui(self):
        self._clear_snip_handles()

    def _draw_snip_shape_preview(self, start, end, points=None):
        self._clear_snip_preview()
        if points is not None:
            flat = []
            for px, py in points:
                sx, sy = self.capturelocal_to_screen(px, py)
                flat.extend([sx, sy])
            if len(flat) >= 4:
                item = self.canvas.create_line(*flat, fill="#00b4ff", width=2, dash=(4, 2))
                self.snip_preview_item_ids = [item]
            return
        cx1, cy1 = self.capturelocal_to_screen(*start)
        cx2, cy2 = self.capturelocal_to_screen(*end)
        if self.snip_shape_type == "ellipse":
            item = self.canvas.create_oval(cx1, cy1, cx2, cy2, outline="#00b4ff", width=2, dash=(4, 2))
        else:
            item = self.canvas.create_rectangle(cx1, cy1, cx2, cy2, outline="#00b4ff", width=2, dash=(4, 2))
        self.snip_preview_item_ids = [item]

    def _snip_corner_offsets(self, stroke):
        """
        Offsets of the 4 corners + the rotate handle from the layer's
        CENTER, in its own unrotated local frame.
        """
        w = stroke["base_w"] * stroke["scale"]
        h = stroke["base_h"] * stroke["scale"]
        rotate_gap = 24 / max(self.display_scale, 0.01)
        return {
            "nw": (-w / 2, -h / 2), "ne": (w / 2, -h / 2),
            "sw": (-w / 2, h / 2), "se": (w / 2, h / 2),
            "rotate": (0, -h / 2 - rotate_gap),
        }

    def _snip_handle_positions(self, stroke):
        """
        Capture-local positions of the 4 resize corners plus the rotate
        handle, for the layer's CURRENT origin/scale/rotation. The rotation
        transform here (dx*cos-dy*sin, dx*sin+dy*cos with theta taken
        directly from stroke["rotation"]) was empirically matched to
        exactly reproduce where img.rotate(-rotation, expand=True) (used in
        _draw_snip_layer) actually puts each corner, so the handles and
        dashed border track the rendered rotation precisely rather than
        approximately.
        """
        ox, oy = stroke["origin"]
        w = stroke["base_w"] * stroke["scale"]
        h = stroke["base_h"] * stroke["scale"]
        cx, cy = ox + w / 2, oy + h / 2
        theta = math.radians(stroke.get("rotation", 0.0))
        cos_t, sin_t = math.cos(theta), math.sin(theta)
        result = {}
        for name, (dx, dy) in self._snip_corner_offsets(stroke).items():
            rdx = dx * cos_t - dy * sin_t
            rdy = dx * sin_t + dy * cos_t
            result[name] = (cx + rdx, cy + rdy)
        return result

    def _hit_snip_handle(self, stroke, screen_x, screen_y):
        for name, (px, py) in self._snip_handle_positions(stroke).items():
            hx, hy = self.capturelocal_to_screen(px, py)
            if abs(screen_x - hx) <= HANDLE_SIZE * 1.5 and abs(screen_y - hy) <= HANDLE_SIZE * 1.5:
                return name
        return None

    def _point_in_snip_bounds(self, stroke, x, y):
        """
        Hit-test for "click inside the body to move" - checked against the
        UNROTATED bounding box (a v1 simplification: a rotated snip's
        actual visible silhouette is a rotated rectangle, so corners of
        that true silhouette that stick outside this axis-aligned box are
        currently unclickable-for-move, while the reverse - the axis-aligned
        box's own corners once rotated away from the silhouette - are
        currently clickable when they shouldn't be. Good enough to be
        usable; exact rotated hit-testing would need a point-in-rotated-
        rectangle test instead).
        """
        ox, oy = stroke["origin"]
        w = stroke["base_w"] * stroke["scale"]
        h = stroke["base_h"] * stroke["scale"]
        return ox <= x <= ox + w and oy <= y <= oy + h

    def _draw_single_snip_ui(self, stroke, is_focused=False):
        positions = self._snip_handle_positions(stroke)
        corner_pts = []
        for name in ("nw", "ne", "se", "sw"):
            sx, sy = self.capturelocal_to_screen(*positions[name])
            corner_pts.extend([sx, sy])
        # The focused snip (what Ctrl+C/X/V and y/d/p act on) gets a
        # brighter border so it's clear which one the clipboard shortcuts
        # target when several snips are visible at once.
        border_color = "#33ff33" if is_focused else "#00b4ff"
        border = self.canvas.create_polygon(corner_pts, outline=border_color, fill="", width=2, dash=(5, 3))
        self._snip_handle_ids.append(border)
        top_mid_x = (positions["nw"][0] + positions["ne"][0]) / 2
        top_mid_y = (positions["nw"][1] + positions["ne"][1]) / 2
        tmx, tmy = self.capturelocal_to_screen(top_mid_x, top_mid_y)
        rx, ry = self.capturelocal_to_screen(*positions["rotate"])
        self._snip_handle_ids.append(self.canvas.create_line(tmx, tmy, rx, ry, fill="#33cc33", width=1))
        for name, (px, py) in positions.items():
            hx, hy = self.capturelocal_to_screen(px, py)
            color = "#33cc33" if name == "rotate" else "#ffaa00"
            hid = self.canvas.create_oval(
                hx - HANDLE_SIZE, hy - HANDLE_SIZE, hx + HANDLE_SIZE, hy + HANDLE_SIZE,
                fill=color, outline="white",
            )
            self._snip_handle_ids.append(hid)

    def _draw_snip_ui(self, only_index=None):
        """
        Redraws every snip's border+handles (or, mid-drag, only the one
        given by only_index - see the class comment above). Only ever
        shows anything while the Snip tool itself is selected, gated here
        centrally rather than via scattered per-gesture hide/show calls -
        the old per-gesture approach (hide on press, show again on
        release) left snip UI flickering back on between every individual
        stroke while continuously drawing with Pen/Highlighter/etc, which
        reads as "isn't actually hidden". Switching away from Snip now
        hides it for as long as that other tool stays selected, full stop;
        switching back re-shows it immediately.
        """
        self._clear_snip_handles()
        if self.draw_tool != "snip" or self.resize_mode_active:
            return
        for i, stroke in enumerate(self.edit_strokes):
            if stroke.get("mode") != "snip" or stroke.get("removed"):
                continue
            if only_index is not None and i != only_index:
                continue
            self._draw_single_snip_ui(stroke, is_focused=(i == self._snip_focused_index))

    def _start_snip_drag(self, index, mode, x, y):
        stroke = self.edit_strokes[index]
        self._snip_drag_index = index
        self._snip_focused_index = index   # clipboard shortcuts (Ctrl+C/X/V, y/d/p) act on this one
        self._snip_drag_mode = mode
        self._snip_drag_start = (x, y)
        self._snip_drag_orig_origin = stroke["origin"]
        self._snip_drag_orig_scale = stroke["scale"]
        self._snip_drag_orig_rotation = stroke["rotation"]
        # Cached once here, reused for every intermediate frame of this
        # drag (see _recompute_baked_image_live_drag) - avoids re-decoding
        # the capture from disk and redrawing every other stroke on every
        # single mouse-move, which was the main source of drag lag.
        self._snip_drag_base_image = self._build_composite_excluding_snip(index)
        self._draw_snip_ui(only_index=index)   # hide every other snip's UI for the duration of this drag

    def _snip_press(self, x, y, screen_x, screen_y):
        # Topmost (most recently created) snip first, so overlapping snips
        # grab in the same order they're drawn on top of each other.
        for i in range(len(self.edit_strokes) - 1, -1, -1):
            stroke = self.edit_strokes[i]
            if stroke.get("mode") != "snip" or stroke.get("removed"):
                continue
            handle = self._hit_snip_handle(stroke, screen_x, screen_y)
            if handle:
                self._start_snip_drag(i, handle, x, y)
                return
            if self._point_in_snip_bounds(stroke, x, y):
                self._start_snip_drag(i, "move", x, y)
                return
        # Nothing existing was hit - start drawing a brand new snip shape.
        self._snip_drag_mode = None
        self._snip_drag_index = None
        self._snip_focused_index = None
        self.snip_pending_start = (x, y)
        if self.snip_shape_type == "freeform":
            self.snip_pending_points = [(x, y)]
            self._draw_snip_shape_preview(None, None, points=self.snip_pending_points)
        else:
            self.snip_pending_points = None
            self._draw_snip_shape_preview((x, y), (x, y))

    def _snip_drag(self, x, y):
        if self._snip_drag_mode is not None and self._snip_drag_index is not None:
            stroke = self.edit_strokes[self._snip_drag_index]
            mode = self._snip_drag_mode
            sx, sy = self._snip_drag_start
            if mode == "move":
                ox0, oy0 = self._snip_drag_orig_origin
                stroke["origin"] = (ox0 + (x - sx), oy0 + (y - sy))
            elif mode == "rotate":
                cx = stroke["origin"][0] + stroke["base_w"] * stroke["scale"] / 2
                cy = stroke["origin"][1] + stroke["base_h"] * stroke["scale"] / 2
                start_ang = math.degrees(math.atan2(sy - cy, sx - cx))
                cur_ang = math.degrees(math.atan2(y - cy, x - cx))
                stroke["rotation"] = self._snip_drag_orig_rotation + (cur_ang - start_ang)
            else:
                self._apply_snip_corner_resize(stroke, mode, x, y)
            self._recompute_baked_image_live_drag()
            self._draw_snip_ui(only_index=self._snip_drag_index)
            return
        if self.snip_pending_start is not None:
            if self.snip_shape_type == "freeform":
                self.snip_pending_points.append((x, y))
                self._draw_snip_shape_preview(None, None, points=self.snip_pending_points)
            else:
                self._draw_snip_shape_preview(self.snip_pending_start, (x, y))

    def _apply_snip_corner_resize(self, stroke, corner, x, y):
        """
        Uniform (aspect-preserving) scale from a corner drag, anchored at
        the layer's CENTER (not the opposite corner - see the class comment
        above for why) - the new scale is just the ratio of the mouse's
        current distance from center to its distance at drag-start, which
        is rotation-invariant so this works identically regardless of the
        snip's current orientation.
        """
        ox0, oy0 = self._snip_drag_orig_origin
        cx = ox0 + stroke["base_w"] * self._snip_drag_orig_scale / 2
        cy = oy0 + stroke["base_h"] * self._snip_drag_orig_scale / 2
        sx, sy = self._snip_drag_start
        start_dist = math.hypot(sx - cx, sy - cy)
        cur_dist = math.hypot(x - cx, y - cy)
        if start_dist < 1e-6:
            return
        new_scale = max(0.05, self._snip_drag_orig_scale * (cur_dist / start_dist))
        stroke["scale"] = new_scale
        new_w = stroke["base_w"] * new_scale
        new_h = stroke["base_h"] * new_scale
        stroke["origin"] = (cx - new_w / 2, cy - new_h / 2)

    def _snip_release(self, x, y):
        if self._snip_drag_mode is not None or self._snip_drag_index is not None:
            self._snip_drag_mode = None
            self._snip_drag_index = None
            self._snip_drag_base_image = None
            self._recompute_baked_image()   # one full, correct, full-quality rebuild now that the drag has settled
            self._draw_snip_ui()   # show everyone again now that the drag has ended
            return
        if self.snip_pending_start is None:
            return
        if self.snip_shape_type == "freeform":
            points = self.snip_pending_points
            self.snip_pending_start = None
            self.snip_pending_points = None
            self._clear_snip_preview()
            if not points or len(points) < 3:
                self._draw_snip_ui()
                return
            xs = [p[0] for p in points]
            ys = [p[1] for p in points]
            bounds = (min(xs), min(ys), max(xs), max(ys))
            if bounds[2] - bounds[0] < 4 or bounds[3] - bounds[1] < 4:
                self._set_paste_position_from_click(points[0])
                self._draw_snip_ui()
                return
            self._finalize_snip("freeform", bounds, freeform_points=points)
        else:
            x1, y1 = self.snip_pending_start
            self.snip_pending_start = None
            self._clear_snip_preview()
            bounds = (min(x1, x), min(y1, y), max(x1, x), max(y1, y))
            if bounds[2] - bounds[0] < 4 or bounds[3] - bounds[1] < 4:
                log.info("Snip finalize: too small (accidental click?) - using this spot as the next paste position instead")
                self._set_paste_position_from_click((x1, y1))
                self._draw_snip_ui()
                return
            self._finalize_snip(self.snip_shape_type, bounds)

    def _finalize_snip(self, shape_type, capture_local_bounds, freeform_points=None):
        """
        Extracts the masked pixel content from everything rendered so far
        (re-baking first so the extraction sees any prior strokes/snips),
        then adds a new "snip" stroke - the hole at its original location
        gets punched on the NEXT bake, in _recompute_baked_image.
        """
        from PIL import Image, ImageDraw, ImageChops
        self._recompute_baked_image()
        source = self.baked_image if self.baked_image is not None else self._build_canvas_composite()
        ox, oy = self.canvas_x1, self.canvas_y1
        x1, y1, x2, y2 = capture_local_bounds
        bw = max(1, int(round(x2 - x1)))
        bh = max(1, int(round(y2 - y1)))
        crop_box = (int(round(x1 - ox)), int(round(y1 - oy)), int(round(x1 - ox)) + bw, int(round(y1 - oy)) + bh)
        cropped = source.crop(crop_box).convert("RGBA")

        mask = Image.new("L", (bw, bh), 0)
        draw = ImageDraw.Draw(mask)
        if shape_type == "ellipse":
            draw.ellipse([0, 0, bw - 1, bh - 1], fill=255)
        elif shape_type == "freeform":
            local_pts = [(px - x1, py - y1) for px, py in freeform_points]
            draw.polygon(local_pts, fill=255)
        else:
            draw.rectangle([0, 0, bw - 1, bh - 1], fill=255)
        cropped.putalpha(ImageChops.multiply(cropped.split()[3], mask))

        stroke = {
            "mode": "snip", "shape": shape_type,
            "orig_bounds": (x1, y1, x2, y2),
            "orig_freeform_points": list(freeform_points) if freeform_points else None,
            "extracted_image": cropped,
            "base_w": bw, "base_h": bh,
            "origin": (x1, y1), "scale": 1.0, "rotation": 0.0,
            "segment_ids": [],
        }
        self.edit_strokes.append(stroke)
        self._edit_redo_stack = []
        self._snip_focused_index = len(self.edit_strokes) - 1
        log.info(f"Snip created: {shape_type} {bw}x{bh} at capture-local ({x1:.0f},{y1:.0f})")
        self._recompute_baked_image()
        self._draw_snip_ui()
        self.show_edit_toolbar()


    def _punch_snip_hole(self, base, stroke, canvas_offset_x, canvas_offset_y):
        """
        Restores `base` to the canvas background fill wherever this snip
        was ORIGINALLY cut from - independent of where it's since been
        moved to - so the source location shows through to whatever's
        behind the capture (the canvas background layer). Strokes with
        orig_bounds is None never came from this capture at all (pasted in
        from an external image via the system clipboard) - there's no
        "hole" to punch for those; returns `base` unchanged in that case.

        This used to only zero the alpha channel at the hole's location,
        leaving whatever RGB values were already there untouched underneath
        - which worked fine as long as the only thing ever sitting there
        was opaque capture pixels (nothing downstream ever needed to look
        at those stale RGB values, since the hole was drawn UNDER any other
        ink in the old two-pass punch-then-draw pipeline). But once ANY
        other ink (a text box, pen stroke, etc.) had been drawn at that
        spot before the snip cut it out, zeroing alpha alone left that
        ink's actual color data sitting right there, and it could leak
        back into view - via a plain RGB flatten/convert (which drops alpha
        without compositing against anything) - even though alpha said
        "invisible".

        Actually painting the real background fill over the region closes
        that gap regardless of what was there before. Uses Image.composite
        against an explicit mask (rather than base.paste(overlay, ...,
        overlay) with the fill as its own mask) specifically so a
        "transparent" canvas_bg_color still works: pasting a fill image
        that's ITSELF fully transparent using itself as the paste mask is a
        no-op (mask alpha 0 means "don't touch the destination"), which
        would silently fail to erase anything for that one fill color.
        Image.composite instead uses an INDEPENDENT mask marking the hole
        shape, so the fill's own alpha (0, 255, whatever) is applied
        correctly either way. Returns the new image - Image.composite
        always produces a new object rather than mutating in place.

        For a box/ellipse snip, the rectangle here is rebuilt from the
        rounded top-left corner plus the stroke's own base_w/base_h -
        exactly how _finalize_snip derived crop_box for the ORIGINAL cut -
        rather than independently rounding x2/y2 fresh each time. Those
        two would usually agree, but rounding x1 and x2 separately can
        land on a different width than rounding (x2 - x1) once and adding
        it to a separately-rounded x1 (e.g. x1=10.5, x2=50.5: width is
        exactly 40, but naive per-corner rounding could still give 39 or
        41 depending on which way each endpoint happens to round) - and
        when that happens the hole ends up a pixel narrower or wider than
        what was actually cropped, leaving a thin sliver of whatever was
        underneath (often a stray antialiased edge pixel from text or a
        photo) sitting right at the boundary of an otherwise clean hole.
        """
        if stroke.get("orig_bounds") is None:
            return base
        from PIL import Image, ImageDraw
        x1, y1, x2, y2 = stroke["orig_bounds"]
        mask = Image.new("L", base.size, 0)
        draw = ImageDraw.Draw(mask)
        shape = stroke.get("shape")
        if shape == "freeform":
            pts = [(px - canvas_offset_x, py - canvas_offset_y) for px, py in stroke.get("orig_freeform_points") or []]
            if len(pts) >= 3:
                draw.polygon(pts, fill=255)
        else:
            cx1 = int(round(x1 - canvas_offset_x))
            cy1 = int(round(y1 - canvas_offset_y))
            cx2 = cx1 + stroke.get("base_w", int(round(x2 - x1)))
            cy2 = cy1 + stroke.get("base_h", int(round(y2 - y1)))
            if shape == "ellipse":
                draw.ellipse([cx1, cy1, cx2, cy2], fill=255)
            else:
                draw.rectangle([cx1, cy1, cx2, cy2], fill=255)
        fill_img = Image.new("RGBA", base.size, self._canvas_bg_fill_rgba())
        return Image.composite(fill_img, base, mask)

    def _draw_snip_layer(self, base, stroke, canvas_offset_x, canvas_offset_y, fast=False):
        """
        Renders this snip's fixed extracted content at its CURRENT origin/
        scale/rotation - the master extracted_image itself never changes.
        fast=True uses NEAREST resampling for both the resize and rotate
        steps (used only for the live per-frame update during an active
        drag - see _recompute_baked_image_live_drag); the settled/final
        render always uses full quality.
        """
        from PIL import Image
        img = stroke["extracted_image"]
        scale = stroke.get("scale", 1.0)
        rotation = stroke.get("rotation", 0.0)
        w = max(1, int(round(stroke["base_w"] * scale)))
        h = max(1, int(round(stroke["base_h"] * scale)))
        resize_resample = Image.NEAREST if fast else (Image.LANCZOS if scale <= 1 else Image.NEAREST)
        rendered = img.resize((w, h), resize_resample)
        if abs(rotation) > 0.01:
            rotate_resample = Image.NEAREST if fast else Image.BICUBIC
            rendered = rendered.rotate(-rotation, expand=True, resample=rotate_resample)
        ox, oy = stroke["origin"]
        center_x = ox + w / 2
        center_y = oy + h / 2
        paste_x = int(round(center_x - rendered.width / 2 - canvas_offset_x))
        paste_y = int(round(center_y - rendered.height / 2 - canvas_offset_y))
        base.paste(rendered, (paste_x, paste_y), rendered)

    # ---- clipboard: snippy register + system register ----
    #
    # Two independent clipboards, wired to two independent sets of
    # keybinds (see _bind_clipboard_shortcuts): the "snippy" register is a
    # full-fidelity in-process copy (the actual stroke dict - mask, current
    # scale/rotation, etc.), while the "system" register round-trips
    # through the real OS clipboard via xclip/wl-copy, so it necessarily
    # flattens to a plain PNG image or plain text - that's all another
    # application could possibly exchange with us. Both act on
    # self._snip_focused_index (whichever snip you last clicked/dragged).

    def _deep_copy_snip_stroke(self, stroke):
        new_stroke = dict(stroke)
        new_stroke["extracted_image"] = stroke["extracted_image"].copy()
        new_stroke["orig_freeform_points"] = list(stroke["orig_freeform_points"]) if stroke.get("orig_freeform_points") else None
        new_stroke["segment_ids"] = []
        new_stroke.pop("removed", None)   # a copy taken from the register is never itself "removed"
        return new_stroke

    def _paste_error(self, message):
        """
        Every failed-safeguard paste ends up here: logged AND surfaced as a
        toast, per the requirement that a blocked paste always explains
        itself rather than silently doing nothing.
        """
        log.error(f"Paste blocked: {message}")
        notify("Paste failed", message, blocking=False, anchor_window=self.root)

    def _is_text_widget_focused(self):
        return self._pending_text_widget is not None

    def _fit_snip_to_canvas(self, stroke, max_fraction=0.9):
        """
        Scales a newly-created/pasted snip down (keeping its center fixed)
        if it's larger than max_fraction of the current canvas in either
        dimension - applies to both a fresh paste from the system clipboard
        and a snippy-register paste, so an oversized image never arrives
        larger than the canvas itself.
        """
        cw, ch = self._canvas_dims()
        max_w, max_h = cw * max_fraction, ch * max_fraction
        cur_w = stroke["base_w"] * stroke["scale"]
        cur_h = stroke["base_h"] * stroke["scale"]
        if cur_w <= max_w and cur_h <= max_h:
            return
        scale_factor = min(max_w / stroke["base_w"], max_h / stroke["base_h"], stroke["scale"])
        cx = stroke["origin"][0] + cur_w / 2
        cy = stroke["origin"][1] + cur_h / 2
        stroke["scale"] = max(0.001, scale_factor)   # a floor only against a literal zero/negative, never large enough to override the real shrink-to-fit factor
        new_w = stroke["base_w"] * stroke["scale"]
        new_h = stroke["base_h"] * stroke["scale"]
        stroke["origin"] = (cx - new_w / 2, cy - new_h / 2)
        log.info(f"Pasted content was larger than the canvas - scaled down to fit (scale={stroke['scale']:.3f})")

    def _set_paste_position_from_click(self, point):
        """
        A click in Snip mode that didn't turn into a drag (too small to be
        a real new shape) is repurposed as "paste here" - the next paste
        lands centered on this point instead of stacking on/near whatever
        was last copied. Consumed (and cleared) by the very next paste;
        after that, pasting reverts to the normal default positioning.
        """
        self._snip_paste_position = point
        log.debug(f"Snip paste position set from click at capture-local {point}")

    def _consume_snip_paste_position(self, default_center):
        if self._snip_paste_position is not None:
            pos = self._snip_paste_position
            self._snip_paste_position = None
            return pos
        return default_center

    def _snip_copy(self, target):
        index = self._snip_focused_index
        if index is None or index >= len(self.edit_strokes) or self.edit_strokes[index].get("mode") != "snip":
            log.info(f"Copy ({target}) pressed with no snip focused - nothing to do")
            return
        stroke = self.edit_strokes[index]
        self._copy_snip_to_register(stroke, target)
        if NOTIFY_ON_SNIP_COPY:
            notify("Snip copied", "Copied to snippy register" if target == "snippy" else "Copied to system clipboard", blocking=False, anchor_window=self.root)

    def _copy_snip_to_register(self, stroke, target):
        """
        The actual register-populating logic, shared by _snip_copy (which
        also fires the Copy toast) and _snip_cut (which does NOT want a
        redundant "Copied" toast on top of its own "Cut" one - it was
        showing both before, since Cut calls this same logic internally).
        """
        if target == "snippy":
            self.snippy_register = {"kind": "snip_stroke", "stroke": self._deep_copy_snip_stroke(stroke)}
            log.info("Snip copied to the snippy register")
        else:
            self._copy_snip_to_system_clipboard(stroke)
            log.info("Snip copied to the system clipboard (flattened to PNG)")

    def _snip_cut(self, target):
        index = self._snip_focused_index
        if index is None or index >= len(self.edit_strokes) or self.edit_strokes[index].get("mode") != "snip":
            log.info(f"Cut ({target}) pressed with no snip focused - nothing to do")
            return
        self._copy_snip_to_register(self.edit_strokes[index], target)
        self._remove_snip_at(index)
        log.info(f"Snip cut (target={target})")
        if NOTIFY_ON_SNIP_CUT:
            notify("Snip cut", f"Cut to {target} register" if target == "snippy" else "Cut to system clipboard", blocking=False, anchor_window=self.root)

    def _snip_delete_to_void(self):
        """Super+x: removes the focused snip without touching either register."""
        index = self._snip_focused_index
        if index is None or index >= len(self.edit_strokes) or self.edit_strokes[index].get("mode") != "snip":
            log.info("Delete-to-void pressed with no snip focused - nothing to do")
            return
        self._remove_snip_at(index)
        log.info("Snip deleted to void (not copied to either register)")
        if NOTIFY_ON_SNIP_DELETE:
            notify("Snip deleted", "Not copied to either register", blocking=False, anchor_window=self.root)

    def _remove_snip_at(self, index):
        """
        Marks a snip "removed" from view/interaction (used by both Cut and
        void-delete) WITHOUT deleting it from edit_strokes - the hole it
        punched at its ORIGINAL location must persist forever regardless of
        what happens to the snip afterward (pasted elsewhere, still sitting
        in a register, or discarded outright): once a piece has been cut
        out, that spot stays exposed to the background. Actually deleting
        the stroke would stop _punch_snip_hole from ever running for it
        again, and the original capture content would silently reappear at
        the cut-out location as if nothing had happened - that was the bug.

        Deliberately NOT routed through the Undo/Redo stack (that stack
        only supports popping/pushing the END of the list), so a cut/
        void-delete on anything but the very last stroke isn't Undo-able.
        A cut can always be recovered manually by pasting back from
        whichever register it went to; a void-delete cannot, by definition.
        """
        self.edit_strokes[index]["removed"] = True
        if self._snip_focused_index == index:
            self._snip_focused_index = None
        self._recompute_baked_image()
        self._draw_snip_ui()

    def _copy_snip_to_system_clipboard(self, stroke):
        """
        Renders the snip exactly as it currently looks (post scale/
        rotation - see _draw_snip_layer) onto a transparent canvas just
        large enough to hold it, and puts THAT PNG on the real OS
        clipboard, since another application has no notion of our stroke
        data at all.

        copy_image_to_clipboard launches xclip -i (X11) as a detached
        background process that needs to keep running - and keep the
        source file readable - for as long as it's serving the clipboard;
        it does NOT wait for xclip to finish reading. Deleting the temp
        file immediately after (as this used to do) raced against that
        background read and could leave the clipboard empty/corrupt,
        which is exactly what made "paste from system register" silently
        fail. The existing do_copy() (the main toolbar's Copy button)
        already gets this right by simply never deleting its own temp
        file; this follows the same pattern instead, tracked in
        owned_temp_paths so it's still cleaned up on window close rather
        than leaking indefinitely.
        """
        from PIL import Image
        img = stroke["extracted_image"]
        scale = stroke.get("scale", 1.0)
        rotation = stroke.get("rotation", 0.0)
        w = max(1, int(round(stroke["base_w"] * scale)))
        h = max(1, int(round(stroke["base_h"] * scale)))
        rendered = img.resize((w, h), Image.LANCZOS if scale <= 1 else Image.NEAREST)
        if abs(rotation) > 0.01:
            rendered = rendered.rotate(-rotation, expand=True, resample=Image.BICUBIC)
        fd, tmp_name = tempfile.mkstemp(suffix=".png", prefix="snippysnappy-clip-")
        os.close(fd)
        tmp_path = Path(tmp_name)
        rendered.save(tmp_path)
        copy_image_to_clipboard(tmp_path)
        self.owned_temp_paths.append(tmp_path)

    def _snip_paste(self, target):
        if target == "snippy":
            reg = self.snippy_register
            if reg is None:
                self._paste_error("The snippy register is empty.")
                return
            kind = reg["kind"]
            if kind == "snip_stroke":
                if self._is_text_widget_focused():
                    self._paste_error("Can't paste a snip into a text box.")
                    return
                self._paste_snip_stroke_copy(reg["stroke"])
                self._notify_snip_pasted("Snip pasted", "Pasted from snippy register")
            elif kind == "text_stroke":
                self._paste_text_stroke_element(reg["stroke"])
                self._notify_snip_pasted("Text box pasted", "Pasted from snippy register")
            elif kind == "text_plain":
                if not self._is_text_widget_focused():
                    self._paste_error("Paste text into a text box first.")
                    return
                self._paste_text_plain(reg["text"])
                self._notify_snip_pasted("Text pasted", "Pasted from snippy register")
            else:
                self._paste_error("Unsupported clipboard content.")
            return

        # System register: read the real OS clipboard and figure out what
        # kind of content it actually holds before doing anything with it -
        # image and text are handled completely differently, and neither
        # is allowed into the wrong context (image -> text box, or text
        # with nowhere to receive it).
        text_focused = self._is_text_widget_focused()
        img = get_system_clipboard_image()
        if img is not None:
            if text_focused:
                self._paste_error("Can't paste an image into a text box.")
                return
            self._paste_image_as_snip(img)
            self._notify_snip_pasted("Snip pasted", "Pasted from system clipboard")
            return
        text = get_system_clipboard_text()
        if text:
            if not text_focused:
                self._paste_error("Paste text into a text box first.")
                return
            self._paste_text_plain(text)
            self._notify_snip_pasted("Text pasted", "Pasted from system clipboard")
            return
        self._paste_error("The system clipboard has no image or text content.")

    def _notify_snip_pasted(self, title, body):
        if NOTIFY_ON_SNIP_PASTE:
            notify(title, body, blocking=False, anchor_window=self.root)

    def _paste_snip_stroke_copy(self, stroke):
        new_stroke = self._deep_copy_snip_stroke(stroke)
        w = new_stroke["base_w"] * new_stroke["scale"]
        h = new_stroke["base_h"] * new_stroke["scale"]
        default_center = (stroke["origin"][0] + w / 2 + 20, stroke["origin"][1] + h / 2 + 20)
        cx, cy = self._consume_snip_paste_position(default_center)
        new_stroke["origin"] = (cx - w / 2, cy - h / 2)
        self._fit_snip_to_canvas(new_stroke)
        self.edit_strokes.append(new_stroke)
        self._edit_redo_stack = []
        self._snip_focused_index = len(self.edit_strokes) - 1
        self._recompute_baked_image()
        self._draw_snip_ui()
        log.info("Snip pasted from the snippy register")

    def _paste_image_as_snip(self, img):
        """
        Wraps an image from the system clipboard (from any application) as
        a brand-new snip layer - centered on the canvas by default, or at
        wherever a "paste here" click was last set (see
        _set_paste_position_from_click) - scaled down to fit if needed, and
        immediately focused so its resize/rotate/move handles are active
        for further adjustment. orig_bounds is None - this content never
        came from the current capture, so there's no "hole" to punch
        anywhere for it (see _punch_snip_hole).
        """
        img = img.convert("RGBA")
        bw, bh = img.size
        default_center = ((self.canvas_x1 + self.canvas_x2) / 2, (self.canvas_y1 + self.canvas_y2) / 2)
        cx, cy = self._consume_snip_paste_position(default_center)
        stroke = {
            "mode": "snip", "shape": "box",
            "orig_bounds": None, "orig_freeform_points": None,
            "extracted_image": img, "base_w": bw, "base_h": bh,
            "origin": (cx - bw / 2, cy - bh / 2), "scale": 1.0, "rotation": 0.0,
            "segment_ids": [],
        }
        self._fit_snip_to_canvas(stroke)
        self.edit_strokes.append(stroke)
        self._edit_redo_stack = []
        self._snip_focused_index = len(self.edit_strokes) - 1
        self._recompute_baked_image()
        self._draw_snip_ui()
        log.info(f"Pasted an external image from the system clipboard as a new snip ({bw}x{bh})")

    def _paste_text_stroke_element(self, stroke):
        """
        Recreates a whole copied/cut TEXT BOX as a new, already-filled-in
        text stroke, positioned with a small offset - this is the "copy the
        textbox itself, not just the text inside it" path (see the vim-mode
        y/d in Normal mode with no selection).
        """
        new_stroke = dict(stroke)
        x1, y1, x2, y2 = stroke["box"]
        new_stroke["box"] = (x1 + 20, y1 + 20, x2 + 20, y2 + 20)
        new_stroke["segment_ids"] = []
        self.edit_strokes.append(new_stroke)
        self._edit_redo_stack = []
        self._recompute_baked_image()
        log.info("Pasted a text-box element from the register")

    def _paste_text_plain(self, text):
        if self._pending_text_widget is not None:
            self._pending_text_widget.insert("insert", text)
            log.info(f"Pasted {len(text)} character(s) of plain text into the active text box")
        else:
            self._paste_error("No text box is focused.")

    # ---- clipboard keybind dispatch ----
    #
    # Bound on self.canvas (not the root window) so a focused Entry/Text
    # widget elsewhere in the app naturally takes priority for the SAME
    # physical keys, without any special-casing - Tk routes key events to
    # whichever widget currently has focus. Ctrl+C specifically only
    # consumes the event (returns "break") when it actually acted on a
    # focused snip; otherwise it falls through to the existing root-level
    # "copy the whole capture" shortcut, so nothing already working breaks.

    def _handle_copy_shortcut(self, target, event=None):
        if self.draw_tool == "snip" and self._snip_focused_index is not None:
            self._snip_copy(target)
            return "break"
        return None

    def _handle_cut_shortcut(self, target, event=None):
        if self.draw_tool == "snip" and self._snip_focused_index is not None:
            self._snip_cut(target)
            return "break"
        return None

    def _handle_paste_shortcut(self, target, event=None):
        if self.draw_tool == "snip" or self._is_text_widget_focused():
            self._snip_paste(target)
            return "break"
        return None

    def _handle_delete_to_void(self, event=None):
        if self.draw_tool == "snip" and self._snip_focused_index is not None:
            self._snip_delete_to_void()
            return "break"
        return None

    _MOD4_SUPER_BIT = 0x40   # Mod4, the near-universal Linux/X11 default mapping for the Super/Windows key

    def _on_vi_key(self, action, event):
        """
        Handles the bare y/d/p/x vi-style alt-binds, disambiguating snippy
        vs system register (and void-delete) by checking the RAW event.state
        modifier bitmask directly for Mod4/Super, rather than relying on a
        separate "<Super-...>"-style Tk binding.

        That separate-binding approach is what shipped originally, and it's
        why Super never worked: Tk's "Super" binding name is only an ALIAS
        for whichever X11 modifier slot (Mod1-Mod5) the current system
        happens to map the physical Super key to, and that mapping isn't
        guaranteed - if Tk's alias resolution doesn't match your system's
        actual xmodmap, "<Super-y>" simply never fires. Meanwhile the plain
        "<y>" binding matches "y" regardless of any OTHER modifiers also
        being held (that's Tk's normal behavior for an unmodified pattern),
        so Super+y silently fell through to it and always looked like a
        plain snippy-register keypress - exactly the reported symptom.
        Checking event.state's Mod4 bit (0x40) directly is more reliable
        since it reads the actual state X11 reported for this event,
        bypassing Tk's alias layer entirely.
        """
        is_super = bool(event.state & self._MOD4_SUPER_BIT)
        if action == "copy":
            return self._handle_copy_shortcut("system" if is_super else "snippy", event)
        if action == "cut":
            return self._handle_cut_shortcut("system" if is_super else "snippy", event)
        if action == "paste":
            return self._handle_paste_shortcut("system" if is_super else "snippy", event)
        if action == "void":
            return self._handle_delete_to_void(event) if is_super else None
        return None

    def _bind_clipboard_shortcuts(self):
        """
        Wires up both registers' keybinds per the app's dual-register
        scheme: standard Ctrl+C/X/V for the snippy register, Ctrl+Shift+
        C/X/V for the system register, and vi-style alt-binds (bare y/d/p/x,
        disambiguated by whether Super is ALSO held - see _on_vi_key). Bound
        once on the canvas (persists across tool switches - the handlers
        themselves check self.draw_tool/focus state and no-op otherwise)
        rather than rebound per tool, since the SAME keys are also
        meaningful to vim-mode text editing bound separately on the text
        widgets themselves.

        Standard Ctrl+C/X/V map to the SNIPPY register; Ctrl+Shift+C/X/V to
        the SYSTEM register. Tk reports Shift+c as keysym "C" (uppercase)
        regardless of whether Control is also held, so "<Control-C>" (the
        capital-letter form) IS "Ctrl+Shift+c" - there's deliberately no
        separate "<Control-Shift-C>" binding alongside it. Having both was
        a real bug: they describe the identical physical keystroke, so Tk
        fired the handler for it TWICE per keypress (each bind() call adds
        an independent binding; more than one can match and fire for the
        same widget/event) - for Paste specifically, that meant pasting
        twice per keypress, which looked like the registers were somehow
        corrupting/overwriting each other.
        """
        bindings = [
            ("<Control-c>", lambda e: self._handle_copy_shortcut("snippy", e)),
            ("<Control-x>", lambda e: self._handle_cut_shortcut("snippy", e)),
            ("<Control-v>", lambda e: self._handle_paste_shortcut("snippy", e)),
            ("<Control-C>", lambda e: self._handle_copy_shortcut("system", e)),
            ("<Control-X>", lambda e: self._handle_cut_shortcut("system", e)),
            ("<Control-V>", lambda e: self._handle_paste_shortcut("system", e)),
            ("<y>", lambda e: self._on_vi_key("copy", e)),
            ("<d>", lambda e: self._on_vi_key("cut", e)),
            ("<p>", lambda e: self._on_vi_key("paste", e)),
            ("<x>", lambda e: self._on_vi_key("void", e)),
        ]
        for seq, cb in bindings:
            try:
                self.canvas.bind(seq, cb)
            except tk.TclError:
                log.warning(f"Could not bind clipboard shortcut {seq!r} on this system - skipping")

    def set_text_font_size(self, val):
        self.text_font_size = int(float(val))

    def set_text_bg_fill(self, choice):
        self.text_bg_fill = choice
        self.show_edit_toolbar()

    def set_pen_width(self, val):
        self.pen_width = int(float(val))

    def set_pen_variant(self, variant):
        self.pen_variant = variant
        self.show_edit_toolbar()   # rebuild so the slider label ("Width"/"Spread") and button states update

    def set_highlight_width(self, val):
        self.highlight_width = int(float(val))

    def cancel_edit(self):
        log.info(f"Edit cancelled, reverting {len(self.edit_strokes) - len(self._pre_edit_strokes)} stroke(s) added this session")
        if self._pending_text_widget is not None:
            self._cancel_text_entry()
        if self.text_current_box_id:
            self.canvas.delete(self.text_current_box_id)
            self.text_current_box_id = None
        self._cancel_pending_shape()
        self._cancel_pending_snip_shape()
        self._hide_snip_ui()
        for stroke in self.edit_strokes:
            for seg_id in stroke.get("segment_ids", []):
                self.canvas.delete(seg_id)
        self.edit_strokes = self._pre_edit_strokes
        self.baked_image = self._pre_edit_baked_image
        self.edit_current_stroke = None
        self._refresh_base_display()
        self.show_main_toolbar()

    def _pack_labeled_slider(self, parent, label_text, variable, from_, to_, command, resolution=None, slider_length=70, value_width=4, anchor="center"):
        """
        Shared layout for every width/size/zoom/scale slider across the
        toolbars: a Label, then the Scale itself with showvalue=False, then
        a second Label sharing the SAME Tk variable to display the current
        value. Tk's default horizontal Scale (showvalue=True) draws its
        live value readout ABOVE the trough, which makes the widget's own
        bounding box taller than a Button/Label and pulls the visible
        trough down off-center once everything on the row gets vertically
        centered against that taller box. Splitting the value out into its
        own plain Label sidesteps that entirely - every widget on the row
        ends up close enough in height to actually look aligned.
        """
        tk.Label(parent, text=label_text, fg="white", bg="#222222").pack(side="left", anchor=anchor, padx=2, pady=4)
        scale_kwargs = {"resolution": resolution} if resolution is not None else {}
        tk.Scale(
            parent, from_=from_, to=to_, orient="horizontal", length=slider_length, showvalue=False,
            bg="#222222", fg="white", troughcolor="#444444", highlightthickness=0,
            variable=variable, command=command, **scale_kwargs,
        ).pack(side="left", anchor=anchor, padx=2, pady=4)
        tk.Label(parent, textvariable=variable, fg="white", bg="#222222", width=value_width, anchor="w").pack(side="left", anchor=anchor, padx=(0, 8), pady=4)

    def show_edit_toolbar(self):
        self.clear_toolbar()
        frame = self.new_toolbar_frame()
        main_row = tk.Frame(frame, bg="#222222")
        main_row.pack(side="top", fill="x")

        tk.Button(
            main_row, text="Snip", pady=BUTTON_PADY,
            relief=("sunken" if self.draw_tool == "snip" else "raised"),
            command=lambda: self.pick_tool("snip"),
        ).pack(side="left", padx=2, pady=4)
        tk.Button(
            main_row, text="Draw", pady=BUTTON_PADY,
            relief=("sunken" if self.draw_tool == "pen" else "raised"),
            command=lambda: self.pick_tool("pen"),
        ).pack(side="left", padx=2, pady=4)
        tk.Button(
            main_row, text="Highlight", pady=BUTTON_PADY,
            relief=("sunken" if self.draw_tool == "highlight" else "raised"),
            command=lambda: self.pick_tool("highlight"),
        ).pack(side="left", padx=2, pady=4)
        tk.Button(
            main_row, text="Text", pady=BUTTON_PADY,
            relief=("sunken" if self.draw_tool == "text" else "raised"),
            command=lambda: self.pick_tool("text"),
        ).pack(side="left", padx=2, pady=4)
        tk.Button(
            main_row, text="Shape", pady=BUTTON_PADY,
            relief=("sunken" if self.draw_tool == "shape" else "raised"),
            command=lambda: self.pick_tool("shape"),
        ).pack(side="left", padx=2, pady=4)

        tk.Button(main_row, text="Resize", pady=BUTTON_PADY, command=self.show_resize_options).pack(side="left", padx=2, pady=4)
        tk.Button(main_row, text="Undo", pady=BUTTON_PADY, command=lambda: (self.undo_last_stroke(), self.show_edit_toolbar())).pack(side="left", padx=2, pady=4)
        tk.Button(main_row, text="Redo", pady=BUTTON_PADY, command=lambda: (self.redo_last_stroke(), self.show_edit_toolbar())).pack(side="left", padx=2, pady=4)
        tk.Button(main_row, text="Done", pady=BUTTON_PADY, command=self.bake_edits).pack(side="left", padx=2, pady=4)
        tk.Button(main_row, text="Cancel", pady=BUTTON_PADY, command=self.cancel_edit).pack(side="left", padx=2, pady=4)

        context_row = tk.Frame(frame, bg="#222222")
        context_row.pack(side="top", fill="x")

        if self.draw_tool != "snip":
            for c in PALETTE:
                border = "white" if c == self.draw_color else "#222222"
                tk.Button(
                    context_row, bg=c, activebackground=c, width=2, height=1,
                    highlightbackground=border, highlightthickness=2,
                    command=lambda c=c: self.pick_color(c),
                ).pack(side="left", padx=2, pady=4)

        if self.draw_tool == "snip":
            tk.Label(context_row, text="Shape", fg="white", bg="#222222").pack(side="left", padx=2)
            for choice, label in [("box", "Box"), ("ellipse", "Ellipse"), ("freeform", "Free-form")]:
                tk.Button(
                    context_row, text=label, pady=BUTTON_PADY,
                    relief=("sunken" if self.snip_shape_type == choice else "raised"),
                    command=lambda c=choice: self._pick_snip_shape(c),
                ).pack(side="left", padx=2, pady=4)
            if any(s.get("mode") == "snip" for s in self.edit_strokes):
                tk.Label(
                    context_row, text="(drag inside to move, corners to resize, top handle to rotate)",
                    fg="#aaaaaa", bg="#222222",
                ).pack(side="left", padx=(10, 0))
        elif self.draw_tool == "pen":
            tk.Label(context_row, text="Type", fg="white", bg="#222222").pack(side="left", padx=2)
            for choice, label in [("pen", "Pen"), ("spray", "Spray Can")]:
                tk.Button(
                    context_row, text=label, pady=BUTTON_PADY,
                    relief=("sunken" if self.pen_variant == choice else "raised"),
                    command=lambda c=choice: self.set_pen_variant(c),
                ).pack(side="left", padx=2, pady=4)
            width_label = "Spread" if self.pen_variant == "spray" else "Width"
            self._pack_labeled_slider(context_row, width_label, tk.IntVar(value=self.pen_width), 1, 30, self.set_pen_width)
        elif self.draw_tool == "highlight":
            self._pack_labeled_slider(context_row, "Width", tk.IntVar(value=self.highlight_width), 4, 60, self.set_highlight_width)
        elif self.draw_tool == "text":
            self._pack_labeled_slider(context_row, "Font size", tk.IntVar(value=self.text_font_size), 10, 72, self.set_text_font_size)
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
        elif self.draw_tool == "shape":
            tk.Label(context_row, text="Type", fg="white", bg="#222222").pack(side="left", padx=2)
            for choice, label in [("box", "Box"), ("line", "Line"), ("ellipse", "Ellipse")]:
                tk.Button(
                    context_row, text=label, pady=BUTTON_PADY,
                    relief=("sunken" if self.shape_type == choice else "raised"),
                    command=lambda c=choice: self.pick_shape_type(c),
                ).pack(side="left", padx=2, pady=4)
            self._pack_labeled_slider(context_row, "Width", tk.IntVar(value=self.pen_width), 1, 30, self.set_pen_width)

        self.finish_toolbar_placement(frame)
        self._draw_snip_ui()
        self._set_window_title("edit")

    # ---- resize submenu ----
    #
    # Two layers can be resized independently:
    #   "capture"    - resamples the actual captured pixels (and, per
    #                  design, flattens/resamples the WHOLE current canvas
    #                  composite including baked edits as one image - if
    #                  you want something excluded, Undo it first). Canvas
    #                  bounds reset to exactly match afterward.
    #   "background" - only changes canvas_x1/y1/x2/y2 (and color). The
    #                  capture's own pixels are never touched - shrinking
    #                  just means less of it is visible/saved, growing
    #                  brings whitespace (or previously-clipped strokes)
    #                  back, all recomputed fresh each time (see
    #                  _build_canvas_composite).
    #
    # While any resize screen is showing, edit-tool bindings (pen/text/
    # shape/highlight) are suppressed and the corner handles are repurposed
    # to drag the selected layer's bounds directly. Escape always returns
    # to the plain edit toolbar (never the main toolbar) - see
    # _handle_escape.

    def show_resize_options(self):
        """
        Entry point (the "Resize" button in the edit toolbar). Matches the
        same two-row shape as the other toolbars: top row picks which layer
        you're resizing plus the exit actions, second row is that layer's
        controls (scale / exact dimensions / color) - no separate "which
        layer?" question screen.
        """
        self._activate_canvas_if_needed()
        self.resize_mode_active = True
        self._cancel_pending_snip_shape()
        self._hide_snip_ui()
        if self.resize_target_layer is None:
            self.resize_target_layer = "capture"
        self._snapshot_resize_session_start()
        self._cancel_pending_shape()
        if self._pending_text_widget is not None:
            self._cancel_text_entry()
        # Bug fix: pen/highlighter/text/shape drawing must not still respond
        # while Resize is open. edit_press/drag/release already no-op when
        # resize_mode_active, but unbinding too means no stray drag can
        # leave a visual artifact, and gets the resize handlers bound.
        self.canvas.unbind("<ButtonPress-1>")
        self.canvas.unbind("<B1-Motion>")
        self.canvas.unbind("<ButtonRelease-1>")
        self.canvas.bind("<ButtonPress-1>", self._resize_press)
        self.canvas.bind("<B1-Motion>", self._resize_drag)
        self.canvas.bind("<ButtonRelease-1>", self._resize_release)
        self.show_resize_toolbar()

    def _select_resize_layer(self, layer):
        self.resize_target_layer = layer
        self.show_resize_toolbar()

    def _exit_resize_mode(self):
        self.resize_mode_active = False
        self._resize_active_handle = None
        self._resize_session_snapshot = None
        self._clear_resize_preview()
        self.clear_handles()
        self.canvas.unbind("<ButtonPress-1>")
        self.canvas.unbind("<B1-Motion>")
        self.canvas.unbind("<ButtonRelease-1>")
        self.canvas.bind("<ButtonPress-1>", self.edit_press)
        self.canvas.bind("<B1-Motion>", self.edit_drag)
        self.canvas.bind("<ButtonRelease-1>", self.edit_release)
        self.show_edit_toolbar()

    def show_resize_toolbar(self):
        self.clear_toolbar()
        frame = self.new_toolbar_frame()
        layer = self.resize_target_layer
        if layer == "capture":
            cur_w, cur_h = self._capture_layer_composite_dims()
        else:
            cur_w, cur_h = self._canvas_dims()

        main_row = tk.Frame(frame, bg="#222222")
        main_row.pack(side="top", fill="x")
        tk.Button(
            main_row, text="Full Capture", pady=BUTTON_PADY,
            relief=("sunken" if layer == "capture" else "raised"),
            command=lambda: self._select_resize_layer("capture"),
        ).pack(side="left", padx=2, pady=4)
        tk.Button(
            main_row, text="Background", pady=BUTTON_PADY,
            relief=("sunken" if layer == "background" else "raised"),
            command=lambda: self._select_resize_layer("background"),
        ).pack(side="left", padx=2, pady=4)
        tk.Button(main_row, text="Undo", pady=BUTTON_PADY, command=self._undo_layer_resize).pack(side="left", padx=2, pady=4)
        tk.Button(main_row, text="Redo", pady=BUTTON_PADY, command=self._redo_layer_resize).pack(side="left", padx=2, pady=4)
        tk.Button(main_row, text="Done", pady=BUTTON_PADY, command=self._exit_resize_mode).pack(side="left", padx=2, pady=4)
        tk.Button(main_row, text="Cancel", pady=BUTTON_PADY, command=self._cancel_resize_session).pack(side="left", padx=2, pady=4)

        context_row = tk.Frame(frame, bg="#222222")
        context_row.pack(side="top", fill="x")
        scale_var = tk.DoubleVar(value=1.0)
        self._pack_labeled_slider(context_row, "Scale", scale_var, 0.1, 4.0, None, resolution=0.1, value_width=4)
        tk.Button(context_row, text="Apply Scale", pady=BUTTON_PADY, command=lambda: self._apply_resize_scale(scale_var.get())).pack(side="left", padx=2, pady=4)

        tk.Label(context_row, text="W", fg="white", bg="#222222").pack(side="left", padx=2)
        w_var = tk.StringVar(value=str(cur_w))
        tk.Entry(context_row, textvariable=w_var, width=6).pack(side="left", padx=2)
        tk.Label(context_row, text="H", fg="white", bg="#222222").pack(side="left", padx=2)
        h_var = tk.StringVar(value=str(cur_h))
        tk.Entry(context_row, textvariable=h_var, width=6).pack(side="left", padx=2)
        tk.Button(context_row, text="Apply Dimensions", pady=BUTTON_PADY, command=lambda: self._apply_resize_pixels(w_var.get(), h_var.get())).pack(side="left", padx=2, pady=4)

        if layer == "background":
            tk.Label(context_row, text="Color", fg="white", bg="#222222").pack(side="left", padx=2)
            for choice, clabel, swatch, swatch_fg in [
                ("white", "White", "#ffffff", "black"),
                ("black", "Black", "#000000", "white"),
                ("transparent", "Transparent", "#666666", "white"),
            ]:
                tk.Button(
                    context_row, text=clabel, bg=swatch, fg=swatch_fg,
                    activebackground=swatch, activeforeground=swatch_fg, pady=BUTTON_PADY,
                    relief=("sunken" if self.canvas_bg_color == choice else "raised"),
                    highlightbackground=("#00b4ff" if self.canvas_bg_color == choice else "#222222"),
                    highlightthickness=2,
                    command=lambda c=choice: self._set_canvas_bg_color(c),
                ).pack(side="left", padx=2, pady=4)

        tk.Label(context_row, text="(or drag a corner handle)", fg="#aaaaaa", bg="#222222").pack(side="left", padx=(10, 0))

        self.finish_toolbar_placement(frame)
        self._set_window_title("resize")
        self._draw_resize_handles()

    def _capture_resize_state_dict(self):
        """
        A full snapshot of everything a resize action can touch. Shared by
        the undo/redo stacks (one entry per action) and the session-start
        snapshot Cancel reverts to (one entry, taken once).
        """
        return {
            "image_path": self.image_path,
            "sel_coords": self.sel_coords,
            "baked_image": self.baked_image,
            "edit_strokes": list(self.edit_strokes),
            "canvas_x1": self.canvas_x1, "canvas_y1": self.canvas_y1,
            "canvas_x2": self.canvas_x2, "canvas_y2": self.canvas_y2,
            "canvas_bg_color": self.canvas_bg_color,
            "no_real_capture": self._no_real_capture,
        }

    def _restore_resize_state_dict(self, snap):
        self.image_path = snap["image_path"]
        self.sel_coords = snap["sel_coords"]
        self.baked_image = snap["baked_image"]
        self.edit_strokes = snap["edit_strokes"]
        self.canvas_x1, self.canvas_y1 = snap["canvas_x1"], snap["canvas_y1"]
        self.canvas_x2, self.canvas_y2 = snap["canvas_x2"], snap["canvas_y2"]
        self.canvas_bg_color = snap["canvas_bg_color"]
        self._no_real_capture = snap["no_real_capture"]

    def _push_resize_undo_snapshot(self):
        """
        Called once per resize action (numeric apply, or once per
        handle-drag gesture - never per drag frame) - pushes the state from
        BEFORE that action onto the undo stack, so Undo can walk back
        through any number of resizes one step at a time, all the way to
        the state Cancel would also revert to. A new action clears the
        redo stack, same as any standard undo/redo history.

        Superseded temp image files are deliberately NOT deleted here -
        unlike _cancel_resize_session (where the whole history is discarded
        at once and nothing else could still reference them), a temp file
        made obsolete by this action might still be needed by an entry
        elsewhere in the undo/redo stacks. They're cleaned up later by the
        normal _cleanup_temp_files sweep on window close instead.
        """
        self._resize_undo_stack.append(self._capture_resize_state_dict())
        self._resize_redo_stack = []

    def _snapshot_resize_session_start(self):
        """
        Taken once, the moment Resize is opened from the edit toolbar (not
        on every individual resize action - that's _push_resize_undo_snapshot
        above). Cancel reverts everything back to exactly this, discarding
        every resize done since - as distinct from Undo/Redo, which step
        through that history one action at a time without leaving Resize.
        """
        self._resize_session_snapshot = self._capture_resize_state_dict()
        self._resize_session_snapshot["owned_temp_paths_len"] = len(self.owned_temp_paths)
        self._resize_undo_stack = []
        self._resize_redo_stack = []

    def _undo_layer_resize(self):
        if not self._resize_undo_stack:
            log.info("Undo Resize pressed with nothing to undo")
            return
        self._resize_redo_stack.append(self._capture_resize_state_dict())
        snap = self._resize_undo_stack.pop()
        self._restore_resize_state_dict(snap)
        if self._sync_base_dims_for_mode():
            self._recalculate_display(keep_position=True)
        else:
            self._refresh_base_display()
        log.info(f"Resize undone, {len(self._resize_undo_stack)} more step(s) available")
        self.show_resize_toolbar()

    def _redo_layer_resize(self):
        """
        Reverses the most recent Undo, recreating the exact resize that was
        stepped back from. No-op if nothing's been undone (or a new resize
        action has been taken since, which clears this stack).
        """
        if not self._resize_redo_stack:
            log.info("Redo Resize pressed with nothing to redo")
            return
        self._resize_undo_stack.append(self._capture_resize_state_dict())
        snap = self._resize_redo_stack.pop()
        self._restore_resize_state_dict(snap)
        if self._sync_base_dims_for_mode():
            self._recalculate_display(keep_position=True)
        else:
            self._refresh_base_display()
        log.info(f"Resize redone, {len(self._resize_redo_stack)} more step(s) available")
        self.show_resize_toolbar()

    def _cancel_resize_session(self):
        """
        Discards every resize action (numeric, handle-drag, Full Capture or
        Background, any number of them, regardless of how many were
        individually Undone/Redone) made since Resize was opened this time,
        reverting to exactly the state _snapshot_resize_session_start
        captured, then exits back to the edit toolbar - distinct from Done,
        which keeps whatever's currently applied.
        """
        snap = self._resize_session_snapshot
        if snap is not None:
            idx = snap["owned_temp_paths_len"]
            for p in self.owned_temp_paths[idx:]:
                if p != snap["image_path"]:
                    try:
                        Path(p).unlink(missing_ok=True)
                    except Exception:
                        log.exception("Could not remove temp file from a cancelled resize session (non-fatal)")
            self.owned_temp_paths = self.owned_temp_paths[:idx]
            self._restore_resize_state_dict(snap)
            log.info("Resize cancelled - reverted to state before Resize was opened")
        self._resize_session_snapshot = None
        self._resize_undo_stack = []
        self._resize_redo_stack = []
        if self._sync_base_dims_for_mode():
            self._recalculate_display(keep_position=True)
        else:
            self._refresh_base_display()
        self._exit_resize_mode()

    def _apply_resize_scale(self, factor):
        layer = self.resize_target_layer
        if layer == "capture":
            cw, ch = self._capture_layer_composite_dims()
        else:
            cw, ch = self._canvas_dims()
        new_w = max(1, int(round(cw * factor)))
        new_h = max(1, int(round(ch * factor)))
        self._dispatch_layer_resize(new_w, new_h)

    def _apply_resize_pixels(self, w_str, h_str):
        try:
            new_w = max(1, int(float(w_str)))
            new_h = max(1, int(float(h_str)))
        except ValueError:
            log.warning(f"Resize: invalid pixel dimensions entered (W={w_str!r} H={h_str!r}) - ignoring")
            return
        self._dispatch_layer_resize(new_w, new_h)

    def _dispatch_layer_resize(self, new_w, new_h):
        if self.resize_target_layer == "capture":
            self._apply_full_capture_resize(new_w, new_h)
        else:
            self._apply_background_resize_anchored(new_w, new_h)
        self.show_resize_toolbar()

    def _capture_layer_composite_dims(self):
        """
        "Full Capture" resize dims are based on the CURRENT canvas
        composite (capture + background + any baked edits) since the whole
        thing gets flattened and resampled together.
        """
        return self._canvas_dims()

    def _set_canvas_bg_color(self, choice):
        self.canvas_bg_color = choice
        if self.baked_image is not None or self.edit_strokes:
            self._recompute_baked_image()
        else:
            self._refresh_base_display()
        self.show_resize_toolbar()

    def _apply_full_capture_resize(self, new_w, new_h):
        """
        Flattens the current canvas composite (capture + background +
        baked edits) into one bitmap and resamples. Existing strokes/
        whitespace get refactored along with the resize rather than
        discarded - if a specific element should be excluded, Undo it
        before resizing. Canvas resets to exactly match the new capture.
        """
        from PIL import Image
        self._push_resize_undo_snapshot()
        source = self.get_working_image()
        source_flat = self._flatten_rgba_for_processing(source) if self.canvas_bg_color != "transparent" else source
        resized = source_flat.resize((new_w, new_h), Image.LANCZOS)

        fd, tmp_name = tempfile.mkstemp(suffix=".png", prefix="snippysnappy-resize-")
        os.close(fd)
        new_path = Path(tmp_name)
        resized.save(new_path)

        self.image_path = new_path
        self.owned_temp_paths.append(new_path)

        self.sel_coords = (0, 0, new_w, new_h)
        self.canvas_x1, self.canvas_y1 = 0, 0
        self.canvas_x2, self.canvas_y2 = new_w, new_h
        self._raw_img_w, self._raw_img_h = new_w, new_h
        self._no_real_capture = False   # the resampled bitmap IS real content now (relevant for --nil sessions)
        self.baked_image = None
        self.edit_strokes = []
        self.ocr_strokes = []

        if self._sync_base_dims_for_mode():
            self._recalculate_display(keep_position=True)
        else:
            self._refresh_base_display()
        log.info(f"Full Capture resize applied: -> {new_w}x{new_h}")

    def _apply_background_resize_anchored(self, new_w, new_h):
        """
        Numeric (scale/pixel) background resize: keeps the current
        top-left corner fixed and grows/shrinks toward bottom-right. Handle
        drags (see _resize_drag) allow anchoring from any corner instead.
        """
        self._push_resize_undo_snapshot()
        self.canvas_x2 = self.canvas_x1 + new_w
        self.canvas_y2 = self.canvas_y1 + new_h
        self._finish_background_resize()
        log.info(f"Background resize applied: -> {new_w}x{new_h} (color={self.canvas_bg_color})")

    def _finish_background_resize(self):
        if self._sync_base_dims_for_mode():
            self._recalculate_display(keep_position=True)
        if self.baked_image is not None or self.edit_strokes:
            self._recompute_baked_image()
        else:
            self._refresh_base_display()

    # ---- resize handle dragging ----

    def _draw_resize_handles(self):
        """
        Repurposes the standard 4-corner handle widgets to control whichever
        layer's bounds are currently selected in the Resize submenu, in
        CANVAS-LOCAL screen coordinates (canvas-local for background: its
        own (0,0)-(canvas_w,canvas_h); capture-local shifted into
        canvas-local for the capture layer, via capturelocal_to_canvaslocal).
        """
        self.clear_handles()
        if not self.resize_mode_active or self.resize_target_layer is None:
            return
        if self.resize_target_layer == "capture":
            cw, ch = self._capture_layer_size()
            x1, y1 = self.capturelocal_to_canvaslocal(0, 0)
            x2, y2 = self.capturelocal_to_canvaslocal(cw, ch)
        else:
            x1, y1 = 0, 0
            x2, y2 = self._canvas_dims()
        cx1, cy1 = self.img_to_canvas(x1, y1)
        cx2, cy2 = self.img_to_canvas(x2, y2)
        corners = {"nw": (cx1, cy1), "ne": (cx2, cy1), "sw": (cx1, cy2), "se": (cx2, cy2)}
        for name, (hx, hy) in corners.items():
            hid = self.canvas.create_rectangle(
                hx - HANDLE_SIZE, hy - HANDLE_SIZE, hx + HANDLE_SIZE, hy + HANDLE_SIZE,
                fill="#ffaa00", outline="white",
            )
            self.handles.append((hid, name))

    def _clear_resize_preview(self):
        for item_id in self._resize_preview_item_ids:
            try:
                self.canvas.delete(item_id)
            except tk.TclError:
                pass
        self._resize_preview_item_ids = []

    def _resize_press(self, event):
        cx, cy = self._event_canvas_xy(event)
        name = self.hit_handle(cx, cy)
        self._resize_active_handle = name
        self._resize_drag_snapshotted = False
        if name and self.resize_target_layer == "capture":
            self._resize_preview_start_capture_dims = self._capture_layer_size()

    def _resize_drag(self, event):
        name = self._resize_active_handle
        if not name:
            return
        cx, cy = self._event_canvas_xy(event)
        canvas_local_x, canvas_local_y = self.canvas_to_img(cx, cy)
        capture_local_x, capture_local_y = self.canvaslocal_to_capturelocal(canvas_local_x, canvas_local_y)
        if self.resize_target_layer == "background":
            self._live_update_background_handle(name, capture_local_x, capture_local_y)
        else:
            self._live_preview_capture_handle(name, capture_local_x, capture_local_y)

    def _resize_release(self, event):
        name = self._resize_active_handle
        self._resize_active_handle = None
        if not name:
            return
        if self.resize_target_layer == "capture":
            self._finalize_capture_handle_resize()
        else:
            # Background handle drags apply live (cheap, no resampling) -
            # settle into the final state once here: resize the window to
            # match, and do the one real re-bake (was skipped per-frame
            # during the drag for performance).
            self._finish_background_resize()
        self.show_resize_toolbar()

    def _live_update_background_handle(self, name, mx, my):
        """
        Background-only: dragging a corner moves that corner of the canvas
        rectangle (in the fixed capture-local frame) while the opposite
        corner stays put - identical in spirit to the app's existing
        selection-resize handles. Pushes exactly one undo entry per drag
        gesture (guarded by _resize_drag_snapshotted, reset in
        _resize_press), not one per mouse-move frame.
        """
        if not self._resize_drag_snapshotted:
            self._push_resize_undo_snapshot()
            self._resize_drag_snapshotted = True
        x1, y1, x2, y2 = self.canvas_x1, self.canvas_y1, self.canvas_x2, self.canvas_y2
        if name == "nw":
            x1, y1 = mx, my
        elif name == "ne":
            x2, y1 = mx, my
        elif name == "sw":
            x1, y2 = mx, my
        elif name == "se":
            x2, y2 = mx, my
        if x2 - x1 < 4:
            x2 = x1 + 4
        if y2 - y1 < 4:
            y2 = y1 + 4
        self.canvas_x1, self.canvas_y1, self.canvas_x2, self.canvas_y2 = x1, y1, x2, y2
        self._sync_base_dims_for_mode()
        cw, ch = self._canvas_dims()
        self.canvas.configure(scrollregion=(0, 0, max(1, int(cw * self.display_scale)), max(1, int(ch * self.display_scale))))
        if self.baked_image is not None:
            # Cheap live preview during drag: just show the un-rebaked
            # composite; strokes are correctly re-baked once on release.
            self._refresh_base_display_without_rebake_preview()
        else:
            self._refresh_base_display()
        self._draw_resize_handles()

    def _refresh_base_display_without_rebake_preview(self):
        """
        During a live background-handle drag, rebuilding the fully-baked
        composite (which re-renders every stroke) on every mouse-move is
        wasteful; show the plain canvas+capture composite instead and defer
        the real re-bake to _resize_release/_finish_background_resize.
        """
        base = self._build_canvas_composite()
        scale = self.display_scale
        if abs(scale - 1.0) > 1e-6:
            disp = base.resize(
                (max(1, int(base.width * scale)), max(1, int(base.height * scale))),
                __import__("PIL.Image", fromlist=["Image"]).LANCZOS if scale < 1 else __import__("PIL.Image", fromlist=["Image"]).NEAREST,
            )
        else:
            disp = base
        photo = self._pil_to_tk(disp)
        if photo is None:
            return
        self._base_display_photo = photo
        if self._base_photo_id is not None:
            self.canvas.delete(self._base_photo_id)
        self._base_photo_id = self.canvas.create_image(0, 0, anchor="nw", image=photo)
        self.canvas.tag_lower(self._base_photo_id)

    def _live_preview_capture_handle(self, name, mx, my):
        """
        Capture-layer resize is a real resample, too expensive to run on
        every mouse-move - show a dashed preview rectangle of the target
        bounds instead and do the actual resample once on release.
        """
        self._clear_resize_preview()
        cw, ch = self._resize_preview_start_capture_dims or self._capture_layer_size()
        x1, y1, x2, y2 = 0, 0, cw, ch
        if name == "nw":
            x1, y1 = mx, my
        elif name == "ne":
            x2, y1 = mx, my
        elif name == "sw":
            x1, y2 = mx, my
        elif name == "se":
            x2, y2 = mx, my
        self._resize_pending_capture_bounds = (x1, y1, x2, y2)
        cx1, cy1 = self.capturelocal_to_screen(x1, y1)
        cx2, cy2 = self.capturelocal_to_screen(x2, y2)
        item = self.canvas.create_rectangle(cx1, cy1, cx2, cy2, outline="#ffaa00", width=2, dash=(5, 3))
        self._resize_preview_item_ids = [item]

    def _finalize_capture_handle_resize(self):
        bounds = getattr(self, "_resize_pending_capture_bounds", None)
        self._clear_resize_preview()
        self._resize_pending_capture_bounds = None
        if bounds is None:
            return
        x1, y1, x2, y2 = bounds
        new_w = max(1, int(round(abs(x2 - x1))))
        new_h = max(1, int(round(abs(y2 - y1))))
        self._apply_full_capture_resize(new_w, new_h)

    # ---- undo (strokes) ----

    def undo_last_stroke(self):
        """
        Removes the most recent edit action and immediately refreshes the
        visual. Priority order: an open (unconfirmed) text box first, then
        a pending shape/snip that hasn't been finalized, then the most
        recent stroke. Each undone stroke goes onto a redo stack (see
        redo_last_stroke) - a new stroke drawn afterward clears it, as
        usual for undo/redo.
        """
        if self._pending_text_widget is not None:
            log.info("Undo pressed while a text box was open - cancelling it")
            self._cancel_text_entry()
            return
        if self.shape_pending_start is not None:
            log.info("Undo pressed while a shape was pending its second click - cancelling it")
            self._cancel_pending_shape()
            return
        if self.snip_pending_start is not None:
            log.info("Undo pressed while a snip shape was being drawn - cancelling it")
            self._cancel_pending_snip_shape()
            return
        if not self.edit_strokes:
            log.info("Undo pressed with no strokes to undo")
            return
        stroke = self.edit_strokes.pop()
        for seg_id in stroke.get("segment_ids", []):
            self.canvas.delete(seg_id)
        self._edit_redo_stack.append(stroke)
        log.info(f"Undid last stroke (mode={stroke.get('mode')}), {len(self.edit_strokes)} remaining, {len(self._edit_redo_stack)} redoable")
        self._recompute_baked_image()
        self._draw_snip_ui()

    def redo_last_stroke(self):
        """
        Reverses the most recent Undo, recreating the exact stroke that was
        removed. No-op if nothing's been undone (or a new stroke has been
        drawn since, which clears this stack).
        """
        if not self._edit_redo_stack:
            log.info("Redo pressed with no strokes to redo")
            return
        stroke = self._edit_redo_stack.pop()
        self.edit_strokes.append(stroke)
        log.info(f"Redid stroke (mode={stroke.get('mode')}), {len(self.edit_strokes)} total")
        self._recompute_baked_image()
        self._draw_snip_ui()

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

    def _draw_shape_stroke(self, base_rgba, stroke, offset_x, offset_y):
        from PIL import ImageDraw
        sx = stroke["start"][0] - offset_x
        sy = stroke["start"][1] - offset_y
        ex = stroke["end"][0] - offset_x
        ey = stroke["end"][1] - offset_y
        width = max(1, int(stroke.get("width", EDIT_PEN_WIDTH)))
        color = stroke.get("color", "#000000")
        draw = ImageDraw.Draw(base_rgba)
        shape_type = stroke.get("shape_type", "box")
        if shape_type == "line":
            draw.line([sx, sy, ex, ey], fill=color, width=width)
        elif shape_type == "ellipse":
            x0, x1p = sorted((sx, ex))
            y0, y1p = sorted((sy, ey))
            draw.ellipse([x0, y0, x1p, y1p], outline=color, width=width)
        else:
            x0, x1p = sorted((sx, ex))
            y0, y1p = sorted((sy, ey))
            draw.rectangle([x0, y0, x1p, y1p], outline=color, width=width)

    def bake_edits(self):
        if self._pending_text_widget is not None:
            self._confirm_text_entry()
        self._cancel_pending_shape()
        log.info(f"Baking {len(self.edit_strokes)} edit stroke(s)")
        self._recompute_baked_image()
        self.show_main_toolbar()

    def _draw_one_stroke(self, base, stroke, ox, oy):
        """
        Draws a single pen/highlight/text/shape/spray stroke onto `base`
        (never a snip - those punch a hole or draw a layer, handled by the
        caller). Returns the resulting image, since the highlighter
        branch's alpha_composite produces a NEW image object rather than
        mutating in place.
        """
        from PIL import Image, ImageDraw
        mode = stroke.get("mode")
        if mode == "text":
            self._draw_text_stroke(base, stroke, ox, oy)
            return base
        if mode == "shape":
            self._draw_shape_stroke(base, stroke, ox, oy)
            return base
        if mode == "spray":
            self._bake_spray_stroke(base, stroke, ox, oy)
            return base
        pts = [(px - ox, py - oy) for px, py in stroke["points"]]
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
        return base

    def _recompute_baked_image(self):
        """
        Rebuilds self.baked_image from a fresh canvas composite (canvas
        background + capture pasted at its offset) plus every current
        edit_strokes entry, translated from capture-local into canvas-local
        coordinates for drawing. Rebuilding from scratch each time (rather
        than patching a cached bitmap) is what lets background resizes
        losslessly reveal/re-clip strokes without any special-case bitmap
        surgery.

        Everything - snip holes, snip LAYERS, and non-snip ink - is drawn
        in a SINGLE pass, IN CREATION ORDER, so the final image respects
        the actual order things were added: a stroke drawn after a snip
        renders on top of it, a stroke drawn before a snip gets correctly
        erased where the snip cuts over it, and a snip's own layer renders
        on top of everything that existed before it but underneath
        anything added later - exactly like a normal layer stack.

        (Earlier versions of this pipeline got progressively less wrong
        here: the very first version punched every hole, then drew every
        stroke, in two separate passes - which meant a snip's hole only
        ever erased the underlying CAPTURE, never any pen/text/shape/spray
        ink already sitting at that spot, so ink drawn before a snip kept
        redrawing right back through the hole it was supposedly cut out
        of. That was fixed by interleaving the hole-punch into the same
        ordered pass as the ink - but snip LAYERS themselves were still
        drawn in a separate final pass, always on top of everything
        regardless of creation order, which is a DIFFERENT bug with the
        same shape: anything drawn AFTER a snip - e.g. drawing on top of a
        just-pasted snip - would still end up rendered UNDER it once baked,
        even though the live, not-yet-baked canvas preview made it look
        like it was on top. Both the hole-punch and the layer draw now
        happen at the snip's own position in the single ordered pass, so
        z-order is respected in both directions.)

        This is the FULL, fully-correct rebuild - relatively expensive
        (re-decodes the capture from disk every call). During an active
        snip move/resize/rotate drag, _recompute_baked_image_live_drag is
        used instead for each intermediate frame (which deliberately keeps
        the actively-dragged snip on top for the duration of that drag, so
        it's easy to see what you're moving); this full version still runs
        once when a drag settles, and for everything else (new strokes,
        undo/redo, resizes, etc.) where correctness matters more than
        per-frame speed.
        """
        base = self._build_canvas_composite()
        ox, oy = self.canvas_x1, self.canvas_y1

        for stroke in self.edit_strokes:
            if stroke.get("mode") == "snip":
                base = self._punch_snip_hole(base, stroke, ox, oy)
                if not stroke.get("removed"):
                    self._draw_snip_layer(base, stroke, ox, oy)
            else:
                base = self._draw_one_stroke(base, stroke, ox, oy)

        self.baked_image = base
        log.debug("Edits baked into working image")
        self._refresh_canvas_with_baked_image()

    def _build_composite_excluding_snip(self, exclude_index):
        """
        Everything EXCEPT one snip's own layer - canvas+capture with all
        holes punched (including that snip's own hole, which is fixed at
        its ORIGINAL location forever and so is identical no matter where
        the snip is currently being dragged to), and all other ink and
        snip layers drawn in the same single ordered pass as
        _recompute_baked_image (see there for why order matters) - just
        with this one index's own layer skipped. Cached once at the start
        of a drag (see _start_snip_drag) so each intermediate frame only
        needs to re-render the ONE snip actually moving - see
        _recompute_baked_image_live_drag for why. The actively-dragged
        snip is deliberately still pasted on top of this cached base
        afterward (by the caller), rather than at its true chronological
        position, so it stays visible while you're actively moving it;
        _recompute_baked_image() re-establishes true z-order once the drag
        settles.
        """
        base = self._build_canvas_composite()
        ox, oy = self.canvas_x1, self.canvas_y1
        for i, stroke in enumerate(self.edit_strokes):
            if stroke.get("mode") == "snip":
                base = self._punch_snip_hole(base, stroke, ox, oy)
                if i != exclude_index and not stroke.get("removed"):
                    self._draw_snip_layer(base, stroke, ox, oy)
            else:
                base = self._draw_one_stroke(base, stroke, ox, oy)
        return base

    def _recompute_baked_image_live_drag(self):
        """
        Cheap per-frame update while a snip is actively being moved,
        resized, or rotated: reuses the cached _snip_drag_base_image
        (everything except this one snip - see
        _build_composite_excluding_snip) and only re-renders the dragged
        snip itself (at low-quality/fast resampling - see _draw_snip_layer)
        on top of a copy of that cache.

        This was the main source of the reported drag lag/latency,
        especially with fast mouse movement generating many motion events
        in quick succession: the full _recompute_baked_image path re-opens
        and re-decodes the capture image from disk and redraws every other
        stroke/snip on EVERY single frame, which is far more work than a
        drag actually needs, since only the one snip being manipulated is
        changing at all.
        """
        if self._snip_drag_base_image is None or self._snip_drag_index is None:
            self._recompute_baked_image()
            return
        base = self._snip_drag_base_image.copy()
        ox, oy = self.canvas_x1, self.canvas_y1
        stroke = self.edit_strokes[self._snip_drag_index]
        self._draw_snip_layer(base, stroke, ox, oy, fast=True)
        self.baked_image = base
        self._refresh_canvas_with_baked_image()

    def _refresh_canvas_with_baked_image(self):
        """
        Update what's on screen to match the true baked (translucent) result -
        clears the solid live-preview stroke lines (superseded now that the
        edits are baked in) and rebuilds the single scaled base display,
        which composites self.baked_image into the full picture itself.
        """
        for stroke in self.edit_strokes:
            for seg_id in stroke.get("segment_ids", []):
                self.canvas.delete(seg_id)
        self._refresh_base_display()

    # ---- measure mode ----

    def enter_measure_mode(self):
        """
        'Dry run' selection boxes for measuring parts of the image
        without changing the real selection/crop at all. Supports multiple
        simultaneous boxes (each its own color, with corner handles to
        reposition/resize it, not just create new ones), each with its own
        live dimension readout and individual clear button. Escape (see
        _handle_escape) or Done exits back to the main toolbar.
        """
        log.info("Entering measure mode")
        self.measure_mode_active = True
        self.measure_kind = "box"
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

    def _measure_label_text(self, box):
        x1, y1, x2, y2 = box["coords"]
        if box.get("kind") == "line":
            length = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5 / self.display_scale
            return f"{length:.0f} px"
        w, h = int(abs(x2 - x1) / self.display_scale), int(abs(y2 - y1) / self.display_scale)
        return f"{w} x {h}"

    def _measure_press(self, event):
        cx, cy = self._event_canvas_xy(event)
        box_idx, handle_name = self._hit_measure_handle(cx, cy)
        if box_idx is not None:
            self.measure_active_box = box_idx
            self.measure_active_handle = handle_name
            self.measure_creating = False
            return
        color = MEASURE_COLORS[len(self.measure_boxes) % len(MEASURE_COLORS)]
        if self.measure_kind == "line":
            rect_id = self.canvas.create_line(cx, cy, cx, cy, fill=color, width=2)
        else:
            rect_id = self.canvas.create_rectangle(
                cx, cy, cx, cy, outline=color, width=2, dash=(4, 2),
            )
        box = {
            "coords": (cx, cy, cx, cy), "color": color, "rect_id": rect_id,
            "handle_ids": [], "label": None, "kind": self.measure_kind,
        }
        self.measure_boxes.append(box)
        self.measure_active_box = len(self.measure_boxes) - 1
        self.measure_active_handle = None
        self.measure_creating = True
        self.measure_start = (cx, cy)
        self.show_measure_toolbar()

    def _measure_drag(self, event):
        if self.measure_active_box is None:
            return
        cx, cy = self._event_canvas_xy(event)
        box = self.measure_boxes[self.measure_active_box]
        if self.measure_creating:
            x1, y1 = self.measure_start
            x2, y2 = cx, cy
        else:
            x1, y1, x2, y2 = box["coords"]
            name = self.measure_active_handle
            if name == "nw":
                x1, y1 = cx, cy
            elif name == "ne":
                x2, y1 = cx, cy
            elif name == "sw":
                x1, y2 = cx, cy
            elif name == "se":
                x2, y2 = cx, cy
            elif name == "start":
                x1, y1 = cx, cy
            elif name == "end":
                x2, y2 = cx, cy
        box["coords"] = (x1, y1, x2, y2)
        self.canvas.coords(box["rect_id"], x1, y1, x2, y2)
        self._draw_measure_handles(self.measure_active_box)
        if box.get("label") is not None:
            try:
                box["label"].config(text=self._measure_label_text(box))
            except tk.TclError:
                pass

    def _draw_measure_handles(self, idx):
        box = self.measure_boxes[idx]
        for hid, _ in box["handle_ids"]:
            self.canvas.delete(hid)
        box["handle_ids"] = []
        x1, y1, x2, y2 = box["coords"]
        if box.get("kind") == "line":
            points = {"start": (x1, y1), "end": (x2, y2)}
        else:
            points = {"nw": (x1, y1), "ne": (x2, y1), "sw": (x1, y2), "se": (x2, y2)}
        for name, (hx, hy) in points.items():
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

    def set_measure_kind(self, kind):
        self.measure_kind = kind
        self.show_measure_toolbar()

    def show_measure_toolbar(self):
        self.clear_toolbar()
        frame = self.new_toolbar_frame()
        header_row = tk.Frame(frame, bg="#222222")
        header_row.pack(side="top", fill="x")
        tk.Label(
            header_row, text="Measure mode - doesn't change your selection:",
            fg="white", bg="#222222",
        ).pack(side="left", anchor="n", padx=2, pady=4)
        tk.Button(
            header_row, text="Boxes", pady=BUTTON_PADY,
            relief=("sunken" if self.measure_kind == "box" else "raised"),
            command=lambda: self.set_measure_kind("box"),
        ).pack(side="left", anchor="n", padx=2, pady=4)
        tk.Button(
            header_row, text="Lines", pady=BUTTON_PADY,
            relief=("sunken" if self.measure_kind == "line" else "raised"),
            command=lambda: self.set_measure_kind("line"),
        ).pack(side="left", anchor="n", padx=2, pady=4)
        tk.Button(header_row, text="Done", pady=BUTTON_PADY, command=self.exit_measure_mode).pack(side="right", anchor="n", padx=2, pady=4)
        tk.Button(header_row, text="Clear", pady=BUTTON_PADY, command=self.clear_all_measure_boxes).pack(side="right", anchor="n", padx=2, pady=4)

        if self.measure_boxes:
            boxes_row = tk.Frame(frame, bg="#222222")
            boxes_row.pack(side="top", fill="x")
            for i, box in enumerate(self.measure_boxes):
                lbl = tk.Label(boxes_row, text=self._measure_label_text(box), fg=box["color"], bg="#222222")
                lbl.pack(side="left", padx=(8, 2))
                box["label"] = lbl
                tk.Button(
                    boxes_row, text="\u00d7", width=2,
                    command=lambda i=i: self.clear_measure_box(i),
                ).pack(side="left", padx=(0, 8))

        self.finish_toolbar_placement(frame)
        self._set_window_title("measure")

    def enter_magnify_mode(self):
        """
        A lens that follows the cursor showing a magnified view of the
        image underneath, without altering anything. The lens is a true
        circle (via a PIL alpha mask) and the real system cursor is hidden
        while over the canvas - a small translucent circle takes its place,
        layered ON TOP of the lens and at its true unmagnified size/position.
        Reverts to the normal cursor the moment you leave the canvas (e.g. toolbar).
        """
        log.info("Entering magnify mode")
        self._activate_canvas_if_needed()
        self.magnify_mode_active = True
        if self._sync_base_dims_for_mode():
            self._recalculate_display(keep_position=True)
        else:
            self._refresh_base_display()
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
        now = time.monotonic()
        last = getattr(self, "_magnify_last_update", 0)
        if now - last < 0.03:  # ~30fps ceiling
            return
        self._magnify_last_update = now
        cx, cy = self._event_canvas_xy(event)
        self._draw_magnifier(cx, cy)

    def _draw_magnifier(self, canvas_cx, canvas_cy):
        """
        canvas_cx/canvas_cy are raw canvas-space mouse coordinates - used
        directly to place the lens/cursor overlay. Sampling happens against
        magnify_source_image, which is get_working_image() - the canvas
        composite once the canvas layer is active (canvas_to_img already
        gives coordinates directly in that same frame, no further offset
        needed), or the plain crop before that (unchanged from before).
        """
        from PIL import Image, ImageDraw
        if self.magnify_source_image is None:
            return
        src = self.magnify_source_image
        local_x, local_y = self.canvas_to_img(canvas_cx, canvas_cy)

        lens_size = self.magnify_lens_size
        level = max(1, self.magnify_level)
        src_radius = max(1, int((lens_size / 2) / (level * self.display_scale)))
        left = max(0, int(local_x) - src_radius)
        top = max(0, int(local_y) - src_radius)
        right = min(src.width, int(local_x) + src_radius)
        bottom = min(src.height, int(local_y) + src_radius)
        if right <= left or bottom <= top:
            return
        crop = src.crop((left, top, right, bottom))
        out_w = max(1, int((right - left) * level * self.display_scale))
        out_h = max(1, int((bottom - top) * level * self.display_scale))
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
        lens_x = canvas_cx - scaled.size[0] // 2
        lens_y = canvas_cy - scaled.size[1] // 2
        self.magnify_lens_item_id = self.canvas.create_image(lens_x, lens_y, anchor="nw", image=photo)

        if self.magnify_cursor_item_id:
            self.canvas.delete(self.magnify_cursor_item_id)
        r = 6
        self.magnify_cursor_item_id = self.canvas.create_oval(
            canvas_cx - r, canvas_cy - r, canvas_cx + r, canvas_cy + r, outline="white", width=2, stipple="gray50",
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
        tk.Label(frame, text="Magnify:", fg="white", bg="#222222").pack(side="left", anchor="n", padx=2, pady=4)
        lens_var = tk.IntVar(value=self.magnify_lens_size)
        self._pack_labeled_slider(frame, "Lens size", lens_var, 40, 300, self.set_magnify_lens_size, anchor="n")
        zoom_var = tk.IntVar(value=self.magnify_level)
        self._pack_labeled_slider(frame, "Zoom", zoom_var, 2, 16, self.set_magnify_level, anchor="n")
        tk.Button(frame, text="Done", pady=BUTTON_PADY, command=self.exit_magnify_mode).pack(side="right", anchor="n", padx=2, pady=4)
        self.finish_toolbar_placement(frame)
        self._set_window_title("magnify")

    # ---- OCR paint mode ----

    def enter_ocr_paint_mode(self):
        log.info("Entering OCR paint mode")
        self._activate_canvas_if_needed()
        self.ocr_paint_mode_active = True
        if self._sync_base_dims_for_mode():
            self._recalculate_display(keep_position=True)
        else:
            self._refresh_base_display()
        self.ocr_strokes = []
        self.ocr_current_stroke = None
        self._ocr_redo_stack = []
        self.clear_toolbar()
        self.canvas.unbind("<ButtonPress-1>")
        self.canvas.unbind("<B1-Motion>")
        self.canvas.unbind("<ButtonRelease-1>")
        self.canvas.bind("<ButtonPress-1>", self.ocr_paint_press)
        self.canvas.bind("<B1-Motion>", self.ocr_paint_drag)
        self.canvas.bind("<ButtonRelease-1>", self.ocr_paint_release)
        self.show_ocr_toolbar()

    def ocr_paint_press(self, event):
        x, y = self._event_to_capture_local(event)
        x, y = self.clamp_to_canvas(x, y)
        log.debug(f"OCR paint press at ({x},{y})")
        self.ocr_current_stroke = {"points": [(x, y)], "width": self.ocr_brush_width, "segment_ids": []}

    def ocr_paint_drag(self, event):
        if not self.ocr_current_stroke:
            log.debug("OCR paint drag fired with no active stroke")
            return
        x, y = self._event_to_capture_local(event)
        x, y = self.clamp_to_canvas(x, y)
        lx, ly = self.ocr_current_stroke["points"][-1]
        if (x - lx) ** 2 + (y - ly) ** 2 < (MIN_POINT_DIST / self.display_scale) ** 2:
            return
        clx, cly = self.capturelocal_to_screen(lx, ly)
        ccx, ccy = self.capturelocal_to_screen(x, y)
        seg_id = self.canvas.create_line(
            clx, cly, ccx, ccy, fill="yellow", width=self._scaled_width(self.ocr_current_stroke["width"]),
            capstyle=tk.ROUND, joinstyle=tk.ROUND, stipple="gray50",
        )
        self.ocr_current_stroke["segment_ids"].append(seg_id)
        self.ocr_current_stroke["points"].append((x, y))

    def ocr_paint_release(self, event):
        if not self.ocr_current_stroke:
            return
        x, y = self._event_to_capture_local(event)
        x, y = self.clamp_to_canvas(x, y)
        lx, ly = self.ocr_current_stroke["points"][-1]
        if (x, y) != (lx, ly):
            clx, cly = self.capturelocal_to_screen(lx, ly)
            ccx, ccy = self.capturelocal_to_screen(x, y)
            seg_id = self.canvas.create_line(
                clx, cly, ccx, ccy, fill="yellow", width=self._scaled_width(self.ocr_current_stroke["width"]),
                capstyle=tk.ROUND, joinstyle=tk.ROUND, stipple="gray50",
            )
            self.ocr_current_stroke["segment_ids"].append(seg_id)
            self.ocr_current_stroke["points"].append((x, y))

        if len(self.ocr_current_stroke["points"]) > 1:
            self.ocr_strokes.append(self.ocr_current_stroke)
            self._ocr_redo_stack = []
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
            frame, text="Paint to OCR (optional):",
            fg="white", bg="#222222",
        ).pack(side="left", anchor="n", padx=2, pady=4)
        brush_var = tk.IntVar(value=self.ocr_brush_width)
        self._pack_labeled_slider(frame, "Width", brush_var, 4, 60, self.set_ocr_brush_width, slider_length=80, anchor="n")
        # Packed in reverse so the visual left-to-right order reads
        # "Run OCR, Undo, Redo, Cancel" - side="right" packing fills inward
        # from the right edge, so the first widget packed ends up rightmost.
        tk.Button(frame, text="Cancel", pady=BUTTON_PADY, command=self.cancel_ocr_paint).pack(side="right", anchor="n", padx=2, pady=4)
        tk.Button(frame, text="Redo", pady=BUTTON_PADY, command=self.redo_last_ocr_stroke).pack(side="right", anchor="n", padx=2, pady=4)
        tk.Button(frame, text="Undo", pady=BUTTON_PADY, command=self.undo_last_ocr_stroke).pack(side="right", anchor="n", padx=2, pady=4)
        tk.Button(frame, text="Run OCR", pady=BUTTON_PADY, command=self.run_ocr_with_mask).pack(side="right", anchor="n", padx=2, pady=4)
        self.finish_toolbar_placement(frame)
        self._set_window_title("extract text")

    def undo_last_ocr_stroke(self):
        if not self.ocr_strokes:
            log.info("Undo pressed with no OCR paint strokes to undo")
            return
        stroke = self.ocr_strokes.pop()
        for seg_id in stroke.get("segment_ids", []):
            self.canvas.delete(seg_id)
        self._ocr_redo_stack.append(stroke)
        log.info(f"Undid last OCR stroke, {len(self.ocr_strokes)} remaining")
        self.show_ocr_toolbar()

    def redo_last_ocr_stroke(self):
        if not self._ocr_redo_stack:
            log.info("Redo pressed with no OCR strokes to redo")
            return
        stroke = self._ocr_redo_stack.pop()
        self.ocr_strokes.append(stroke)
        self._redraw_ocr_strokes_preview()   # re-draws the line segments Undo removed
        log.info(f"Redid OCR stroke, {len(self.ocr_strokes)} total")
        self.show_ocr_toolbar()

    def run_ocr_with_mask(self):
        self.ocr_paint_mode_active = False
        log.info(f"Running OCR ({'masked' if self.ocr_strokes else 'whole selection'}, {len(self.ocr_strokes)} stroke(s))")
        from PIL import Image, ImageDraw
        target = self.get_working_image()
        target = self._flatten_rgba_for_processing(target)

        if self.ocr_strokes:
            mask = Image.new("L", target.size, 0)
            draw = ImageDraw.Draw(mask)
            for stroke in self.ocr_strokes:
                pts = [(px - self.canvas_x1, py - self.canvas_y1) for px, py in stroke["points"]]
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
        self._set_window_title("extract text")

    def _show_ocr_main_buttons(self):
        for w in self._ocr_btn_frame.winfo_children():
            w.destroy()
        tk.Button(
            self._ocr_btn_frame, text="Copy Text", pady=BUTTON_PADY,
            command=lambda: self.do_copy_text(self._ocr_overlay, self._ocr_text_widget),
        ).pack(side="left", padx=4)
        tk.Button(
            self._ocr_btn_frame, text="Save Text", pady=BUTTON_PADY,
            command=self.show_text_save_options,
        ).pack(side="left", padx=4)
        tk.Button(
            self._ocr_btn_frame, text="Back", pady=BUTTON_PADY,
            command=lambda: (log.info("OCR result: Back pressed, returning to paint"), self._ocr_overlay.destroy(), self.show_ocr_toolbar()),
        ).pack(side="left", padx=4)
        tk.Button(
            self._ocr_btn_frame, text="Close", pady=BUTTON_PADY,
            command=lambda: (log.info("OCR result: Close pressed"), self.root.destroy()),
        ).pack(side="left", padx=4)

    def show_text_save_options(self):
        """
        Mirrors the image Save flow: Normal Save (prompts for a
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
        tk.Button(btn_row, text="Save", pady=BUTTON_PADY, command=self.begin_normal_save_text).pack(side="left", padx=4)
        tk.Button(btn_row, text="Slop Save", pady=BUTTON_PADY, command=self.do_slop_save_text).pack(side="left", padx=4)
        tk.Button(btn_row, text="Back", pady=BUTTON_PADY, command=self._show_ocr_main_buttons).pack(side="left", padx=4)

    def begin_normal_save_text(self):
        for w in self._ocr_btn_frame.winfo_children():
            w.destroy()
        name_var = tk.StringVar(value=f"snippytext-{timestamp()}")
        entry = tk.Entry(self._ocr_btn_frame, textvariable=name_var, width=28)
        entry.pack(side="left", padx=4)
        entry.focus_set()
        tk.Button(
            self._ocr_btn_frame, text="Confirm", pady=BUTTON_PADY,
            command=lambda: self.confirm_normal_save_text(name_var.get()),
        ).pack(side="left", padx=4)
        tk.Button(self._ocr_btn_frame, text="Cancel", pady=BUTTON_PADY, command=self.show_text_save_options).pack(side="left", padx=4)

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

    def begin_normal_save(self):
        self.clear_toolbar()
        frame = self.new_toolbar_frame()
        name_var = tk.StringVar(value=f"snipsnap-{timestamp()}")
        entry = tk.Entry(frame, textvariable=name_var, width=28)
        entry.pack(side="left", padx=4, pady=4)
        entry.focus_set()
        tk.Button(
            frame, text="Confirm", pady=BUTTON_PADY,
            command=lambda: self.confirm_normal_save(name_var.get()),
        ).pack(side="left", padx=4, pady=4)
        tk.Button(
            frame, text="Cancel", pady=BUTTON_PADY,
            command=lambda: self.show_main_toolbar(reset_tools_row=False, reset_save_row=False),
        ).pack(side="left", padx=4, pady=4)
        self.finish_toolbar_placement(frame)
        self._set_window_title("save")

    def confirm_normal_save(self, name):
        name = name.strip() or f"snipsnap-{timestamp()}"
        if not name.lower().endswith(".png"):
            name += ".png"
        path = SCREENSHOT_DIR / name
        log.info(f"Normal Save -> {path}")
        img = self.get_working_image()
        img.save(path)
        also_copy = bool(self.copy_on_save_var.get())
        also_exit = bool(self.also_exit_var.get())
        if also_copy:
            copy_image_to_clipboard(path)
            notify("Capture copied to clipboard")
        notify("Screenshot saved", f"Saved as {path.name}")
        if also_exit:
            log.debug(f"Destroying window after post-save notifications (also_copy={also_copy})")
            self.root.destroy()
        else:
            log.debug(f"Also Exit unchecked - staying open after save (also_copy={also_copy})")
            self.show_main_toolbar()

    def do_slop_save(self):
        SLOP_DIR.mkdir(parents=True, exist_ok=True)
        path = SLOP_DIR / f"slopsnap-{timestamp()}.png"
        log.info(f"Slop Save -> {path}")
        img = self.get_working_image()
        img.save(path)
        also_copy = bool(self.copy_on_save_var.get())
        also_exit = bool(self.also_exit_var.get())
        if also_copy:
            copy_image_to_clipboard(path)
            notify("Capture copied to clipboard")
        notify("Slop screenshot saved", f"Saved as {path.name}")
        if also_exit:
            log.debug(f"Destroying window after post-save notifications (also_copy={also_copy})")
            self.root.destroy()
        else:
            log.debug(f"Also Exit unchecked - staying open after save (also_copy={also_copy})")
            self.show_main_toolbar()

    # ---- copy ----

    def do_copy(self):
        log.info("Copy pressed")
        img = self.get_working_image()
        fd, tmp_name = tempfile.mkstemp(suffix=".png", prefix="snippysnappy-")
        os.close(fd)
        tmp_path = Path(tmp_name)
        img.save(tmp_path)
        copy_image_to_clipboard(tmp_path)
        notify("Capture copied to clipboard")
        if bool(self.also_exit_var.get()):
            log.debug("Destroying window after clipboard write + notification")
            self.root.destroy()
        else:
            log.debug("Also Exit unchecked - staying open after copy")
            self.show_main_toolbar()


def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "-w", "--window", action="store_true",
        help="Capture a single window (click to select); capture adjustable",
    )
    group.add_argument(
        "-f", "--fullscreen", action="store_true",
        help="Capture the whole monitor; still adjustable via handles",
    )
    group.add_argument(
        "-o", "--open", metavar="PATH", default=None,
        help=(
            "Open an existing image file for editing/annotation, instead of taking a new "
            "screenshot. PATH may be absolute, or relative to FUZZ_DIR (below) rather "
            "than the shell's current directory. Pass 'prompt' instead of a path to open a "
            "small fuzzy-finder picker window."
        ),
    )
    group.add_argument(
        "-n", "--nil", nargs="*", metavar="ARG", default=None,
        help=(
            "Start with a blank canvas instead of taking a screenshot (no capture at all). "
            "Optionally followed by a WxH dimension (e.g. 1200x1000) and/or a color code "
            "(w/white, b/black, t/transparent), in either order, to override the configured "
            f"defaults ({NIL_CANVAS_DEFAULT_WIDTH}x{NIL_CANVAS_DEFAULT_HEIGHT}, {NIL_CANVAS_DEFAULT_COLOR}) "
            "for this run only. Example: -n 1200x1000 b"
        ),
    )
    args = parser.parse_args()
    log.info(f"snippysnappy starting, args: window={args.window} fullscreen={args.fullscreen} open={args.open!r} nil={args.nil!r}")
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
        # className sets WM_CLASS (matches the .desktop file's
        # StartupWMClass) - this is the ONLY point in the whole app where
        # it can be set; Tk has no way to change it after construction,
        # and every window (including OpenPromptApp's, and any Toplevel
        # created later) shares this same root, so setting it once here
        # covers all of them.
        root = tk.Tk(className="snippysnappy")
        root.withdraw()

        def log_callback_exception(exc, val, tb):
            log.error("Unhandled exception inside a Tkinter callback (button/binding):", exc_info=(exc, val, tb))
        root.report_callback_exception = log_callback_exception

        if args.window:
            if BACKEND == "wayland":
                x, y, w, h = pick_region_interactive_wayland()
                capture_monitor(x, y, w, h, full_path)
                monitors = get_monitors()
                mon = monitor_at_point(monitors, x, y)
            else:
                win_id = select_window_id()
                x, y, w, h = get_window_geometry(win_id)
                capture_window(win_id, full_path)
                monitors = get_monitors()
                mon = monitor_at_point(monitors, x, y)
            _, mx, my, mw, mh = mon
            root.deiconify()
            SelectorApp(
                root, full_path, mode="window", bounds=(mx, my, mw, mh), auto_select_full=True,
                monitors=monitors, capture_tempdir=tmp,
            )
            notify("Capture taken", blocking=False)
        elif args.fullscreen:
            if BACKEND == "wayland":
                name, x, y, w, h = pick_output_interactive_wayland()
                capture_monitor(x, y, w, h, full_path)
                monitors = get_monitors()
            else:
                mouse_x, mouse_y = get_mouse_position()
                monitors = get_monitors()
                name, x, y, w, h = monitor_at_point(monitors, mouse_x, mouse_y)
                capture_monitor(x, y, w, h, full_path)
            root.deiconify()
            SelectorApp(
                root, full_path, mode="fullscreen", bounds=(x, y, w, h), auto_select_full=True,
                monitors=monitors, capture_tempdir=tmp, current_monitor_name=name,
            )
            notify("Capture taken", blocking=False)
        elif args.open:
            if args.open.strip().lower() == "prompt":
                prompt_app = OpenPromptApp(root, get_default_open_dir())
                root.deiconify()
                root.mainloop()
                if prompt_app.result_path is None:
                    log.info("--open prompt: cancelled, nothing chosen")
                    sys.exit(0)
                open_path = prompt_app.result_path
                for child in root.winfo_children():
                    child.destroy()
                root.withdraw()
            else:
                open_path = resolve_open_path(args.open)
            try:
                open_path = open_path.resolve(strict=True)
            except OSError:
                log.error(f"--open: file not found: {open_path}")
                print(f"snippysnappy: file not found: {open_path}", file=sys.stderr)
                sys.exit(1)
            if not is_image_file(open_path):
                log.error(f"--open: not an image file: {open_path}")
                print(f"snippysnappy: please select an image file (got: {open_path.suffix or '(no extension)'})", file=sys.stderr)
                sys.exit(1)
            try:
                from PIL import Image as _Image
                with _Image.open(open_path) as _img:
                    _img.verify()
            except Exception:
                log.exception(f"--open: not a readable image file: {open_path}")
                print(f"snippysnappy: not a readable image file: {open_path}", file=sys.stderr)
                sys.exit(1)

            monitors = get_monitors()
            mon = None
            if BACKEND == "x11":
                try:
                    mouse_x, mouse_y = get_mouse_position()
                    mon = monitor_at_point(monitors, mouse_x, mouse_y)
                except Exception:
                    log.warning("Could not determine mouse position for --open; using fallback monitor selection")
            if mon is None:
                mon = pick_fallback_monitor(monitors)
            _, mx, my, mw, mh = mon

            work_path = Path(tmp) / f"opened{open_path.suffix or '.png'}"
            shutil.copy(open_path, work_path)
            log.info(f"Opening existing image: {open_path}")
            root.deiconify()
            SelectorApp(
                root, work_path, mode="window", bounds=(mx, my, mw, mh), auto_select_full=True,
                monitors=monitors, capture_tempdir=tmp,
            )
        elif args.nil is not None:
            nil_w, nil_h, nil_color = parse_nil_args(args.nil)
            log.info(f"--nil: starting with a blank {nil_w}x{nil_h} canvas ({nil_color}), no screenshot taken")

            monitors = get_monitors()
            mon = None
            if BACKEND == "x11":
                try:
                    mouse_x, mouse_y = get_mouse_position()
                    mon = monitor_at_point(monitors, mouse_x, mouse_y)
                except Exception:
                    log.warning("Could not determine mouse position for --nil; using fallback monitor selection")
            if mon is None:
                mon = pick_fallback_monitor(monitors)
            _, mx, my, mw, mh = mon

            placeholder_path = Path(tmp) / "nil.png"
            make_nil_placeholder(placeholder_path, nil_color)
            root.deiconify()
            app = SelectorApp(
                root, placeholder_path, mode="window", bounds=(mx, my, mw, mh), auto_select_full=True,
                monitors=monitors, capture_tempdir=tmp,
            )
            # Activate the canvas layer immediately (rather than waiting for
            # the usual first Edit/OCR/Magnify/Resize touch) so Save/Copy
            # produce the requested size/color - including real transparency
            # - even if the user never enters Edit at all. _no_real_capture
            # skips ever pasting the 1x1 placeholder in (there's nothing
            # real to show), and _force_show_canvas_composite makes the
            # main toolbar display the blank canvas itself from the start,
            # rather than the (otherwise invisible) 1x1 placeholder file.
            app._activate_canvas_if_needed()
            app.canvas_x1, app.canvas_y1 = 0, 0
            app.canvas_x2, app.canvas_y2 = nil_w, nil_h
            app.canvas_bg_color = nil_color
            app._no_real_capture = True
            # sel_coords was set by auto_select_full to match the 1x1
            # placeholder file (0,0,1,1) - left as-is, the main toolbar's
            # crop-selection handles would cluster into a tiny box in the
            # corner instead of spanning the actual blank canvas.
            app.sel_coords = (0, 0, nil_w, nil_h)
            app._force_show_canvas_composite = True
            if app._sync_base_dims_for_mode():
                app._recalculate_display()
            # SelectorApp.__init__'s own auto_select_full handling already
            # called draw_handles() once, using the WRONG 1x1 placeholder
            # size (before any of the overrides above ran) - nothing since
            # then re-drew them, so the stale tiny handles just sat there.
            # update_dimension_display() has the exact same problem - it
            # was ALSO called once during __init__ with the wrong 1x1
            # sel_coords, and nothing since then refreshed the label text,
            # so it kept reading "1 x 1" even once the handles themselves
            # were fixed.
            app.draw_handles()
            app.update_dimension_display()
            notify("Blank canvas ready", f"{nil_w}x{nil_h}, {nil_color}", blocking=False)
        else:
            monitors = get_monitors()
            x, y, w, h = get_virtual_screen_bounds(monitors)
            log.info(f"Select mode: spanning full virtual screen {w}x{h}+{x}+{y} across {len(monitors)} monitor(s)")
            capture_full_screen(full_path)
            try:
                from PIL import Image as _DiagImage
                with _DiagImage.open(full_path) as _dimg:
                    log.info(f"Actual captured image size: {_dimg.size[0]}x{_dimg.size[1]} (compare against the virtual screen size logged above - a mismatch here means the capture tool itself, not this script, isn't spanning both monitors)")
            except Exception:
                log.exception("Could not read back captured image size for diagnostics (non-fatal)")
            root.deiconify()
            SelectorApp(root, full_path, mode="monitor", bounds=(x, y, w, h), capture_tempdir=tmp)
            notify("Ready for selection", blocking=False)

        log.debug("Entering Tk mainloop")
        root.mainloop()
        log.info("Tk mainloop exited, script ending")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("Unhandled exception, snippysnappy is exiting")
        raise
