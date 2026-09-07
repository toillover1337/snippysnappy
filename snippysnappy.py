#!/usr/bin/env python3
"""
snippysnappy - lightweight screenshot capture with adjustable selection,
free-hand edit (pen/highlighter), and OCR text extraction.

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
    - Copy: straight to clipboard, no file written, closes immediately.
    - Save: "Normal Save" (prompts for a filename, default snipsnap-<timestamp>, into ~/Pictures/Screenshots) or "Slop Save" (no prompt, fixed name slopsnap-<timestamp>.png, into a separate folder for quick/incidental shots). A checkbox (on by default) also copies to clipboard on save.
    - Edit: color palette, Pen (opaque) or Highlighter (translucent), Done bakes it in.
    - Extract Text: optionally paint over just the text you want OCR'd, then Run OCR; result is copied to clipboard.

Dependencies:
    media-gfx/maim x11-misc/xclip app-text/tesseract dev-python/pillow x11-misc/xdotool x11-apps/xrandr dev-lang/python
    # tk use flag needed on python for tkinter support

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
import tkinter as tk
from datetime import datetime
from pathlib import Path

LOG_PATH = Path(__file__).resolve().parent / "snippysnappy.log"   # comment out this line to disable file logging entirely
MAX_LOG_BYTES = 1_000_000   # rotate once the log exceeds ~1MB
LOG_BACKUP_COUNT = 3        # keep this many rotated-out old logs (snippysnappy.log.1, .2, .3)

SCREENSHOT_DIR = Path.home() / "Pictures" / "Screenshots"
SLOP_DIR = Path.home() / "Pictures" / "Slop-o-graphs"   # tweak if you want it elsewhere
TEXT_DIR = Path("/tmp")   # saved OCR text lands here
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

HANDLE_SIZE = 8
OCR_BRUSH_WIDTH = 18
OCR_MASK_MIN_PADDING = 20   # px - minimum mask padding regardless of brush width
TOOLBAR_HEIGHT = 60
MIN_POINT_DIST = 3   # px - skip adding a new stroke point closer than this to the last one
MIN_WINDOW_WIDTH = 560   # decorated windows are at least this wide, so the toolbar always fits

EDIT_PEN_WIDTH = 4
EDIT_HIGHLIGHT_WIDTH = 22
EDIT_HIGHLIGHT_ALPHA = 90   # 0-255, translucency of highlighter mode
PALETTE = ["#FF0000", "#00A000", "#0000FF", "#000000", "#FFD400"]

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
    """subprocess.run wrapper that logs the command and its outcome."""
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
    """subprocess.Popen wrapper that logs the command being launched (fire-and-forget)."""
    log.debug(f"Launching (detached): {' '.join(cmd)}")
    try:
        return subprocess.Popen(cmd, **kwargs)
    except Exception:
        log.exception(f"Unexpected error launching {cmd[0]}")
        raise


def capture_full_screen(path):
    run_logged(["maim", "-f", "png", str(path)], check=True)


def get_mouse_position():
    out = run_logged(
        ["xdotool", "getmouselocation", "--shell"], capture_output=True, text=True, check=True
    ).stdout
    vals = dict(line.split("=") for line in out.strip().splitlines())
    pos = int(vals["X"]), int(vals["Y"])
    log.debug(f"Mouse position: {pos}")
    return pos


def get_monitors():
    """Returns [(name, x, y, w, h), ...] from `xrandr --listmonitors`."""
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


def monitor_at_point(monitors, px, py):
    for mon in monitors:
        _, x, y, w, h = mon
        if x <= px < x + w and y <= py < y + h:
            log.debug(f"Point ({px},{py}) is on monitor {mon}")
            return mon
    log.warning(f"Point ({px},{py}) not on any reported monitor; falling back to first: {monitors[0]}")
    return monitors[0]  # fallback if cursor is somehow off every monitor


def get_virtual_screen_bounds(monitors):
    """Bounding box spanning ALL monitors combined - i.e. the full extended
    desktop, exactly matching what a plain `maim` with no -g captures."""
    min_x = min(m[1] for m in monitors)
    min_y = min(m[2] for m in monitors)
    max_x = max(m[1] + m[3] for m in monitors)
    max_y = max(m[2] + m[4] for m in monitors)
    bounds = (min_x, min_y, max_x - min_x, max_y - min_y)
    log.debug(f"Virtual screen bounds across {len(monitors)} monitor(s): {bounds}")
    return bounds


def capture_monitor(x, y, w, h, path):
    run_logged(["maim", "-g", f"{w}x{h}+{x}+{y}", "-f", "png", str(path)], check=True)


def select_window_id():
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
    run_logged(["maim", "-i", window_id, "-f", "png", str(path)], check=True)


def copy_image_to_clipboard(path):
    popen_logged(
        ["xclip", "-selection", "clipboard", "-t", "image/png", "-i", str(path)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def copy_text_to_clipboard(text):
    p = popen_logged(
        ["xclip", "-selection", "clipboard"],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    p.stdin.write(text.encode())
    p.stdin.close()


def ocr_image(path):
    result = run_logged(["tesseract", str(path), "stdout"], capture_output=True, text=True)
    text = result.stdout.strip()
    log.debug(f"OCR produced {len(text)} chars")
    return text


def ensure_dbus_env():
    """Global hotkey daemons often spawn commands with a stripped environment
    missing DBUS_SESSION_BUS_ADDRESS, which notify-send needs to reach the
    notification service. Without it, notify-send doesn't fail cleanly - it
    hangs trying to connect, which matches 'notify-send did not return within
    Ns' in the log. Falls back to the standard systemd-user D-Bus socket path
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
    """Tiny popup used only when notify-send isn't working, so there's still
    some visual confirmation the action completed.

    IMPORTANT: never create a second independent tk.Tk() while one already
    exists in this process - doing so corrupts Tkinter's shared default-root
    state and can silently break subsequent widget/PhotoImage creation on
    the ORIGINAL root (this actually happened: notify("Capture taken") fires
    before SelectorApp is constructed, while main()'s root is still alive,
    so a naive tk.Tk() here broke capture/select/window modes entirely).
    Instead: if a root already exists, attach as a Toplevel of it and use
    wait_window() to block until it closes; only create a standalone Tk()
    if no root exists yet at all (e.g. after self.root.destroy() elsewhere).

    Even with that fix, the "Capture taken" notification (fired before
    SelectorApp's window is set up, with a real window manager actively
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
    except Exception:
        log.exception("Toast fallback itself failed (non-fatal)")


def notify(title, body="", allow_toast=True):
    if not allow_toast:
        # Fire-and-forget: we don't act on success/failure here (no toast
        # either way), so there's no reason to block waiting to find out.
        # Waiting here was the actual source of a guaranteed ~1s delay on
        # every launch when notify-send hangs, sitting right in the path
        # between the keybind and the selection overlay appearing.
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


def timestamp():
    return datetime.now().strftime("%Y%m%d-%H%M%S")


OCR_UPSCALE_FACTOR = 2       # how much to enlarge crops before OCR
OCR_UPSCALE_SKIP_HEIGHT = 1000   # skip upscaling only for already-large crops


def preprocess_for_ocr(img):
    """Upscale (unless already large) + grayscale + autocontrast + Otsu
    binarize + auto-invert so color and background variation (multi-colored
    terminal text, translucent dark panels) can't confuse tesseract, and
    small real-world terminal font sizes get enough resolution to recognize
    reliably. Validated against a real captured terminal screenshot.

    Upscaling is based on total crop height only to skip the rare
    pathologically-large case (a whole-screen OCR) - NOT to decide whether
    upscaling helps, since a multi-line crop can be tall overall while each
    individual line of text is still small. Modest upscaling is cheap enough
    (a fraction of a second even on large crops) that applying it broadly is
    simpler and more robust than trying to estimate per-line text size.
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

        # edit (pen/highlighter) state
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

        self.copy_on_save_var = tk.IntVar(value=1)

        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        root.bind("<Escape>", lambda e: (log.info("Escape pressed, destroying window"), root.destroy()))

        if auto_select_full:
            self.sel_coords = (0, 0, iw, ih)
            self.draw_handles()
            self.show_main_toolbar()

    # ---- rectangle selection ----

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
        pad = 10  # generous tolerance - a click on a ~20px button was missing
                  # its exact bbox in real (imprecise) mouse use
        return (bbox[0] - pad) <= x <= (bbox[2] + pad) and (bbox[1] - pad) <= y <= (bbox[3] + pad)

    def on_press(self, event):
        if self.hit_reset_button(event.x, event.y):
            # Clear any leftover rect from a PRIOR selection before returning.
            # Without this, self.rect stays non-None, and the same
            # press-release gesture that clicked this button still reaches
            # on_drag/on_release afterward (mouse motion between press and
            # release is normal, even for a "single click") - which then
            # silently overwrites the just-applied reset using that stale
            # rectangle's coordinates.
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
        self.clear_toolbar()
        self.clear_handles()
        self.active_handle = None
        self.start_x, self.start_y = event.x, event.y
        if self.rect:
            self.canvas.delete(self.rect)
        self.rect = self.canvas.create_rectangle(
            self.start_x, self.start_y, self.start_x, self.start_y,
            outline="#00b4ff", width=2,
        )

    def on_drag(self, event):
        if self.active_handle:
            self.resize_with_handle(self.active_handle, event.x, event.y)
        elif self.rect:
            self.canvas.coords(self.rect, self.start_x, self.start_y, event.x, event.y)

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
        """Select mode only: once a region is chosen, shrink down from the
        full-screen borderless overlay into a small decorated window holding
        just the selection - frees the rest of the desktop for normal use
        while you work with Save/Copy/Edit/Extract Text, instead of a
        screen-spanning overlay staying up the whole time. Trade-off: no more
        on-the-fly resizing after this point - use Redo Capture to start over
        if you need a different region.
        """
        from PIL import Image
        log.info("Selection finalized, transitioning to compact review window")
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
        self.img = tk.PhotoImage(file=str(crop_path))

        self.canvas.delete("all")
        self.canvas.config(width=new_iw, height=new_ih)
        self.canvas.create_image(0, 0, anchor="nw", image=self.img)

        self.root.withdraw()
        self.root.overrideredirect(False)
        self.root.attributes("-topmost", False)
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

    def redo_capture(self):
        """Restarts the whole capture flow from scratch by re-running the
        script fresh with the same arguments - simplest robust way to support
        'redo' uniformly across select/window/fullscreen modes without complex
        window reconfiguration for each case."""
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
        """Small "x" button just inside the top edge, centered horizontally -
        resets back to the full image. Deliberately NOT near a corner (was
        originally top-right, right next to the NE resize handle - real
        imprecise clicks near either one were ambiguous). Drawn as part of
        draw_handles/clear_handles rather than tracked separately:
        clear_handles already runs at the start of every new drag (hiding
        it automatically while you're actively selecting) and draw_handles
        runs right after any selection is finalized (showing it again) -
        no extra show/hide bookkeeping needed.

        Click handling is done via hit_reset_button() inside on_press,
        NOT via canvas.tag_bind - item-level tag_bind and the widget-level
        <ButtonPress-1> binding (on_press itself) are separate binding
        layers in Tk, and "break" from one does not stop the other from
        also firing. Using tag_bind here would let a click on this button
        ALSO reach on_press and start a new drag rectangle right where you
        clicked, immediately undoing the reset (confirmed via a real Tk
        event-dispatch test, not just a direct method call).
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
        """Frame to pack buttons into. Caller must still call finish_toolbar_placement."""
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

    def show_main_toolbar(self):
        self.clear_toolbar()
        frame = self.new_toolbar_frame()
        tk.Button(frame, text="Save", command=self.show_save_options).pack(side="left", padx=4, pady=4)
        tk.Button(frame, text="Copy", command=self.do_copy).pack(side="left", padx=4, pady=4)
        tk.Button(frame, text="Extract Text", command=self.enter_ocr_paint_mode).pack(side="left", padx=4, pady=4)
        tk.Button(frame, text="Edit", command=self.enter_edit_mode).pack(side="left", padx=4, pady=4)
        if self.mode == "fullscreen" and len(self.available_monitors) > 1:
            self._add_monitor_dropdown(frame)
        else:
            tk.Button(frame, text="Redo", command=self.redo_capture).pack(side="left", padx=4, pady=4)
        tk.Button(frame, text="Cancel", command=lambda: (log.info("Cancel pressed"), self.root.destroy())).pack(side="left", padx=4, pady=4)
        self.finish_toolbar_placement(frame)

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
        self.img = tk.PhotoImage(file=str(new_path))
        try:
            if old_path and Path(old_path).exists():
                Path(old_path).unlink()
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
        if not self.edit_current_stroke:
            return
        x, y = self.clamp_to_selection(event.x, event.y)
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
        if not self.edit_current_stroke:
            return
        x, y = self.clamp_to_selection(event.x, event.y)
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

    def pick_color(self, color):
        self.draw_color = color
        self.show_edit_toolbar()

    def pick_tool(self, tool):
        self.draw_tool = tool
        self.show_edit_toolbar()

    def set_pen_width(self, val):
        self.pen_width = int(float(val))

    def set_highlight_width(self, val):
        self.highlight_width = int(float(val))

    def cancel_edit(self):
        log.info(f"Edit cancelled, discarding {len(self.edit_strokes)} stroke(s)")
        for stroke in self.edit_strokes:
            for seg_id in stroke["segment_ids"]:
                self.canvas.delete(seg_id)
        self.edit_strokes = []
        self.edit_current_stroke = None
        self.canvas.unbind("<ButtonPress-1>")
        self.canvas.unbind("<B1-Motion>")
        self.canvas.unbind("<ButtonRelease-1>")
        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.show_main_toolbar()

    def show_edit_toolbar(self):
        self.clear_toolbar()
        frame = self.new_toolbar_frame()
        for c in PALETTE:
            border = "white" if c == self.draw_color else "#222222"
            tk.Button(
                frame, bg=c, activebackground=c, width=2, height=1,
                highlightbackground=border, highlightthickness=2,
                command=lambda c=c: self.pick_color(c),
            ).pack(side="left", padx=2, pady=4)
        tk.Button(
            frame, text="Pen",
            relief=("sunken" if self.draw_tool == "pen" else "raised"),
            command=lambda: self.pick_tool("pen"),
        ).pack(side="left", padx=(10, 2), pady=4)
        tk.Scale(
            frame, from_=1, to=30, orient="horizontal", length=80, showvalue=False,
            bg="#222222", fg="white", troughcolor="#444444", highlightthickness=0,
            variable=tk.IntVar(value=self.pen_width), command=self.set_pen_width,
        ).pack(side="left", padx=(0, 10), pady=4)
        tk.Button(
            frame, text="Highlighter",
            relief=("sunken" if self.draw_tool == "highlight" else "raised"),
            command=lambda: self.pick_tool("highlight"),
        ).pack(side="left", padx=2, pady=4)
        tk.Scale(
            frame, from_=4, to=60, orient="horizontal", length=80, showvalue=False,
            bg="#222222", fg="white", troughcolor="#444444", highlightthickness=0,
            variable=tk.IntVar(value=self.highlight_width), command=self.set_highlight_width,
        ).pack(side="left", padx=(0, 10), pady=4)
        tk.Button(frame, text="Done", command=self.bake_edits).pack(side="left", padx=(10, 4), pady=4)
        tk.Button(frame, text="Cancel", command=self.cancel_edit).pack(side="left", padx=4, pady=4)
        self.finish_toolbar_placement(frame)

    def bake_edits(self):
        log.info(f"Baking {len(self.edit_strokes)} edit stroke(s)")
        from PIL import Image, ImageDraw
        x1, y1, _, _ = (int(v) for v in self.sel_coords)
        base = self.crop_original().convert("RGBA")

        for stroke in self.edit_strokes:
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
        self.show_main_toolbar()

    def _refresh_canvas_with_baked_image(self):
        """Update what's on screen to match the true baked (translucent) result -
        until now the canvas only ever showed the solid live-preview lines, which
        never visually updated to reflect the real blended colors after Done."""
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

    def enter_ocr_paint_mode(self):
        log.info("Entering OCR paint mode")
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
            log.debug("OCR paint drag fired with no active stroke (press may not have registered)")
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
            frame, text="Paint over text to OCR (optional)",
            fg="white", bg="#222222",
        ).pack(side="left", padx=6)
        tk.Scale(
            frame, from_=4, to=60, orient="horizontal", length=80, showvalue=False,
            bg="#222222", fg="white", troughcolor="#444444", highlightthickness=0,
            variable=tk.IntVar(value=self.ocr_brush_width), command=self.set_ocr_brush_width,
        ).pack(side="left", padx=(0, 10), pady=4)
        tk.Button(frame, text="Run OCR", command=self.run_ocr_with_mask).pack(side="left", padx=4, pady=4)
        tk.Button(frame, text="Cancel", command=self.cancel_ocr_paint).pack(side="left", padx=4, pady=4)
        self.finish_toolbar_placement(frame)

    def run_ocr_with_mask(self):
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
        """Review screen: shows the extracted text, lets you copy/save it, or go
        back to redo the paint/selection without losing the whole capture.

        Rendered as an overlay Frame inside the SAME window rather than a
        separate Toplevel - a Toplevel could end up stacked behind the main
        window in select mode, since that window is a borderless,
        overrideredirect + always-on-top window covering the entire screen,
        and override-redirect windows can win stacking fights against their
        own child windows on some window managers. An in-window overlay can't
        have that problem since it's part of the same already-visible window.
        """
        self.clear_toolbar()
        overlay = tk.Frame(self.root, bg="#1a1a1a")
        overlay.place(relx=0, rely=0, relwidth=1, relheight=1)

        tk.Label(
            overlay, text="Extracted Text", fg="white", bg="#1a1a1a",
            font=("", 12, "bold"),
        ).pack(anchor="w", padx=16, pady=(16, 4))

        text_widget = tk.Text(overlay, wrap="word")
        text_widget.insert("1.0", text if text else "(no text detected)")
        text_widget.pack(fill="both", expand=True, padx=16, pady=(0, 8))

        text_copy_var = tk.IntVar(value=1)
        tk.Checkbutton(
            overlay, text="Also copy to clipboard when saving", variable=text_copy_var,
            bg="#1a1a1a", fg="white", selectcolor="#444444",
            activebackground="#1a1a1a", activeforeground="white",
        ).pack(anchor="w", padx=16)

        btn_frame = tk.Frame(overlay, bg="#1a1a1a")
        btn_frame.pack(fill="x", padx=16, pady=16)
        tk.Button(
            btn_frame, text="Copy Text",
            command=lambda: self.do_copy_text(overlay, text_widget),
        ).pack(side="left", padx=4)
        tk.Button(
            btn_frame, text="Save Text",
            command=lambda: self.do_save_text(overlay, text_widget, text_copy_var),
        ).pack(side="left", padx=4)
        tk.Button(
            btn_frame, text="Back",
            command=lambda: (log.info("OCR result: Back pressed, returning to paint mode"), overlay.destroy(), self.show_ocr_toolbar()),
        ).pack(side="left", padx=4)
        tk.Button(
            btn_frame, text="Close",
            command=lambda: (log.info("OCR result: Close pressed"), self.root.destroy()),
        ).pack(side="left", padx=4)

    def do_copy_text(self, overlay, text_widget):
        content = text_widget.get("1.0", "end-1c")
        log.info(f"Copy Text pressed ({len(content)} chars)")
        copy_text_to_clipboard(content)
        notify("Text copied to clipboard")
        self.root.destroy()

    def do_save_text(self, overlay, text_widget, copy_var):
        content = text_widget.get("1.0", "end-1c")
        TEXT_DIR.mkdir(parents=True, exist_ok=True)
        path = TEXT_DIR / f"snippytext-{timestamp()}.txt"
        path.write_text(content, encoding="utf-8")
        log.info(f"Saved OCR text -> {path} ({len(content)} chars)")
        also_copy = bool(copy_var.get())
        self.root.destroy()
        if also_copy:
            copy_text_to_clipboard(content)
            notify("Text copied to clipboard")
        notify("Text saved", f"Saved as {path.name}")

    # ---- save ----

    def show_save_options(self):
        self.clear_toolbar()
        frame = self.new_toolbar_frame()
        tk.Button(frame, text="Normal Save", command=self.begin_normal_save).pack(side="left", padx=4, pady=4)
        tk.Button(frame, text="Slop Save", command=self.do_slop_save).pack(side="left", padx=4, pady=4)
        tk.Checkbutton(
            frame, text="Also copy to clipboard", variable=self.copy_on_save_var,
            bg="#222222", fg="white", selectcolor="#444444",
            activebackground="#222222", activeforeground="white",
        ).pack(side="left", padx=8)
        tk.Button(frame, text="Back", command=self.show_main_toolbar).pack(side="left", padx=4, pady=4)
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
        help="Capture a single window (click to pick it) instead of a monitor",
    )
    group.add_argument(
        "--fullscreen", action="store_true",
        help="Capture the whole monitor immediately (no drag needed); still adjustable via handles",
    )
    args = parser.parse_args()
    log.info(f"snippysnappy starting, args: window={args.window} fullscreen={args.fullscreen}")
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
            win_id = select_window_id()
            wx, wy, ww, wh = get_window_geometry(win_id)
            capture_window(win_id, full_path)
            notify("Capture taken", allow_toast=False)
            monitors = get_monitors()
            mon = monitor_at_point(monitors, wx, wy)
            _, mx, my, mw, mh = mon
            root.deiconify()
            SelectorApp(
                root, full_path, mode="window", bounds=(mx, my, mw, mh), auto_select_full=True,
                monitors=monitors, capture_tempdir=tmp,
            )
        elif args.fullscreen:
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
