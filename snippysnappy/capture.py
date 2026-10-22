# snippysnappy v1.0.0
"""Screen/monitor/window capture backends (X11 and Wayland), monitor geometry helpers, and the --delay countdown."""
from pathlib import Path
from .constants import DEFAULT_MONITOR_NAME
import os
import re
import subprocess
import sys
import time
import tempfile
import shutil

from .logging_setup import log, run_logged, popen_logged

def detect_backend():
    """
    'wayland' or 'x11'. Override with SNIPPYSNAPPY_BACKEND=wayland|x11 for
    testing. Auto-detection just checks WAYLAND_DISPLAY.
    """
    override = os.environ.get("SNIPPYSNAPPY_BACKEND")
    if override in ("x11", "wayland"):
        return override
    # patch 45: XDG_SESSION_TYPE too (some login managers set it without exporting WAYLAND_DISPLAY to
    # every child); Tk itself always talks to X - on Wayland that is XWayland (check_x_display()).
    if os.environ.get("WAYLAND_DISPLAY") or os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland":
        return "wayland"
    return "x11"


def check_x_display():
    """patch 45: Tk has no Wayland backend - fail with a clear message instead of a TclError traceback."""
    if os.environ.get("DISPLAY"):
        return
    if BACKEND == "wayland":
        msg = ("snippysnappy: $DISPLAY is not set. Tk needs XWayland on Wayland - enable it in your "
               "compositor (e.g. sway: 'xwayland enable', Hyprland: 'xwayland { enabled = true }').")
    else:
        msg = "snippysnappy: $DISPLAY is not set - no X server to show windows on."
    log.error(msg)
    print(msg, file=sys.stderr)
    sys.exit(1)


BACKEND = detect_backend()


# ---- patch 43: decoded-image cache + faster X11 capture ----
import atexit as _atexit
_DECODED = {}            # str(path) -> ((size, mtime_ns), PIL image in the file's own mode)
_DECODED_MAX = 2


_PENDING = {}            # str(path) -> thread still writing that capture's PNG


def wait_written(path=None):
    """Block until the capture PNG at path (or every pending one) is on disk."""
    keys = [str(path)] if path is not None else list(_PENDING)
    for k in keys:
        th = _PENDING.get(k)
        if th is not None:
            th.join()
            _PENDING.pop(k, None)


def cached_open(path):
    """
    The decoded image at path, shared and cached while the file is unchanged (size + mtime).
    Treat the result as READ-ONLY: crop/convert/copy it, never draw on it.
    A capture whose PNG is still being written is served from memory.
    """
    from PIL import Image
    key = str(path)
    th = _PENDING.get(key)
    if th is not None:
        if th.is_alive() and key in _DECODED:
            return _DECODED[key][1]
        th.join()
        _PENDING.pop(key, None)
    st = os.stat(key)
    sig = (st.st_size, st.st_mtime_ns)
    hit = _DECODED.get(key)
    if hit is not None and hit[0] == sig:
        return hit[1]
    with Image.open(key) as im:
        im.load()
        img = im.copy()
    _remember(key, sig, img)
    return img


def _remember(key, sig, img):
    _DECODED.pop(key, None)
    _DECODED[key] = (sig, img)
    for k in list(_DECODED):
        if len(_DECODED) <= _DECODED_MAX:
            break
        if k not in _PENDING:            # a capture still being written is only in memory
            _DECODED.pop(k)


def write_png_async(img, path):
    """
    Writes img to path as PNG on a background thread (Pillow releases the GIL while compressing),
    to path + ".part" first and then renamed into place. Until it is on disk, cached_open(path)
    serves img from memory; anything that needs the FILE calls wait_written(path) first.
    img must not be modified afterwards.
    """
    import threading
    key = str(path)
    wait_written(key)       # a second write to the same file waits for the first
    _remember(key, None, img)

    def write():
        try:
            tmp = key + ".part"
            img.save(tmp, format="PNG", compress_level=1)
            os.replace(tmp, key)
            st = os.stat(key)
            _remember(key, (st.st_size, st.st_mtime_ns), img)
        except Exception:
            log.exception(f"Could not write {key}")

    th = threading.Thread(target=write, name="png-writer", daemon=False)
    _PENDING[key] = th
    th.start()


# ---- patch 44: X11 capture in-process (Pillow XCB grab + libX11/libXfixes via ctypes), maim as fallback ----
_X11 = {}


def _x11_libs():
    """libX11 + libXfixes through ctypes (loaded once). Raises if unavailable."""
    if _X11:
        return _X11
    import ctypes
    import ctypes.util
    x11 = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
    xf = ctypes.CDLL(ctypes.util.find_library("Xfixes") or "libXfixes.so.3")

    class CursorImage(ctypes.Structure):
        _fields_ = [("x", ctypes.c_short), ("y", ctypes.c_short),
                    ("width", ctypes.c_ushort), ("height", ctypes.c_ushort),
                    ("xhot", ctypes.c_ushort), ("yhot", ctypes.c_ushort),
                    ("cursor_serial", ctypes.c_ulong), ("pixels", ctypes.POINTER(ctypes.c_ulong)),
                    ("atom", ctypes.c_ulong), ("name", ctypes.c_char_p)]

    class WindowAttrs(ctypes.Structure):
        _fields_ = [("x", ctypes.c_int), ("y", ctypes.c_int), ("width", ctypes.c_int), ("height", ctypes.c_int),
                    ("border_width", ctypes.c_int), ("depth", ctypes.c_int), ("_rest", ctypes.c_byte * 256)]

    x11.XOpenDisplay.restype = ctypes.c_void_p
    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
    x11.XFree.argtypes = [ctypes.c_void_p]
    x11.XDefaultRootWindow.restype = ctypes.c_ulong
    x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
    x11.XGetWindowAttributes.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(WindowAttrs)]
    x11.XTranslateCoordinates.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_int, ctypes.c_int,
                                          ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_ulong)]
    xf.XFixesGetCursorImage.restype = ctypes.POINTER(CursorImage)
    xf.XFixesGetCursorImage.argtypes = [ctypes.c_void_p]
    _X11.update(ctypes=ctypes, x11=x11, xf=xf, WindowAttrs=WindowAttrs)
    return _X11


def _x11_cursor(dpy):
    """(RGBA image, screen x, screen y) of the pointer's current cursor, or None."""
    L = _x11_libs()
    try:
        ci = L["xf"].XFixesGetCursorImage(dpy)
    except Exception:
        return None
    if not ci:
        return None
    from PIL import Image
    try:
        c = ci.contents
        w, h = c.width, c.height
        raw = bytearray(w * h * 4)
        px = c.pixels
        for i in range(w * h):
            v = px[i] & 0xFFFFFFFF
            raw[4 * i] = (v >> 16) & 255
            raw[4 * i + 1] = (v >> 8) & 255
            raw[4 * i + 2] = v & 255
            raw[4 * i + 3] = v >> 24
        # composited as straight alpha - what maim does (same pixels, +-1 rounding)
        return Image.frombytes("RGBA", (w, h), bytes(raw)), c.x - c.xhot, c.y - c.yhot
    finally:
        L["x11"].XFree(ci)


def _paint_cursor(img, cursor, ox, oy):
    if cursor is None:
        return
    cimg, cx, cy = cursor
    dx0, dy0 = cx - ox, cy - oy
    x0, y0 = max(0, dx0), max(0, dy0)
    x1, y1 = min(img.width, dx0 + cimg.width), min(img.height, dy0 + cimg.height)
    if x1 > x0 and y1 > y0:
        img.alpha_composite(cimg, dest=(x0, y0), source=(x0 - dx0, y0 - dy0, x1 - dx0, y1 - dy0))


def _capture_backend():
    from . import constants as _c
    b = str(getattr(_c, "CAPTURE_BACKEND", "auto")).lower()
    return b if b in ("auto", "pillow", "maim") else "auto"


def _include_cursor():
    from . import constants as _c
    return bool(getattr(_c, "CAPTURE_INCLUDE_CURSOR", True))


def _grab_native(region=None, window_id=None):
    """
    RGBA screenshot without maim (Pillow's XCB grab): the whole screen, a root-relative
    (x, y, w, h) region, or the screen area a window covers. The cursor (libXfixes) is painted
    in when CAPTURE_INCLUDE_CURSOR, as maim does by default.
    """
    from PIL import Image, ImageGrab, features
    L = _x11_libs()
    x11 = L["x11"]
    ctypes = L["ctypes"]
    dpy = x11.XOpenDisplay(None)
    if not dpy:
        raise RuntimeError("XOpenDisplay failed")
    try:
        cursor = _x11_cursor(dpy) if _include_cursor() else None
        if window_id is not None:
            # the window's on-screen area (what is visible there), root-relative
            wid = int(str(window_id), 0)
            attrs = L["WindowAttrs"]()
            if not x11.XGetWindowAttributes(dpy, wid, ctypes.byref(attrs)):
                raise RuntimeError(f"no such window {window_id}")
            rx, ry, child = ctypes.c_int(), ctypes.c_int(), ctypes.c_ulong()
            x11.XTranslateCoordinates(dpy, wid, x11.XDefaultRootWindow(dpy), 0, 0, ctypes.byref(rx), ctypes.byref(ry), ctypes.byref(child))
            region = (rx.value, ry.value, attrs.width, attrs.height)
        if not features.check("xcb"):
            raise RuntimeError("Pillow was built without XCB support")
        if region is None:
            img = ImageGrab.grab().convert("RGBA")
            ox = oy = 0
        else:
            x, y, w, h = region
            img = ImageGrab.grab(bbox=(x, y, x + w, y + h)).convert("RGBA")
            ox, oy = x, y
        _paint_cursor(img, cursor, ox, oy)
        return img
    finally:
        x11.XCloseDisplay(dpy)


def _capture_x11(path, region=None, window_id=None):
    """
    Captures to path: in-process first (CAPTURE_BACKEND auto/pillow), maim as the fallback.
    A single window (-w) still goes to maim first when it is installed: maim -i reads the
    window's own contents, the in-process route grabs the screen area the window covers.
    """
    import shutil
    if window_id is not None and shutil.which("maim") and _capture_backend() != "pillow":
        _maim_via_bmp(["-i", str(window_id)], path)
        return
    if _capture_backend() != "maim":
        try:
            write_png_async(_grab_native(region=region, window_id=window_id), path)
            return
        except Exception as e:
            log.warning(f"Capture: in-process grab failed ({type(e).__name__}: {e}) - using maim")
    if window_id is not None:
        extra = ["-i", str(window_id)]
    elif region is not None:
        x, y, w, h = region
        extra = ["-g", f"{w}x{h}+{x}+{y}"]
    else:
        extra = []
    _maim_via_bmp(extra, path)


def _maim_via_bmp(extra, path):
    """maim -> BMP on a pipe -> one decode -> PNG written here (fast compression) + kept in the cache.
    Same pixels and the same RGBA mode as maim's own PNG. Falls back to maim -> PNG file on any problem."""
    try:
        import io
        from PIL import Image
        r = run_logged(["maim", *extra, "-f", "bmp"], capture_output=True, check=True)
        img = Image.open(io.BytesIO(r.stdout)).convert("RGBA")
        write_png_async(img, path)
        return
    except Exception:
        log.exception("Capture: maim BMP path failed - falling back to maim PNG")
    run_logged(["maim", "-m", "1", *extra, "-f", "png", str(path)], check=True)


_atexit.register(wait_written)


def capture_full_screen_x11(path):
    _capture_x11(path)


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
    patch 45: sizes are LOGICAL (mode / scale, swapped for 90/270 transforms) - the coordinate
    space XWayland, slurp and `grim -s 1` all use; disabled outputs are skipped.
    """
    out = run_logged(["wlr-randr"], capture_output=True, text=True, check=True).stdout
    outs = []
    cur = None
    for line in out.splitlines():
        m = _WLR_RANDR_OUTPUT_RE.match(line)
        if m:
            cur = {"name": m.group(1), "enabled": True, "scale": 1.0, "transform": "normal"}
            outs.append(cur)
            continue
        if cur is None:
            continue
        st = line.strip()
        m = _WLR_RANDR_POS_RE.search(line)
        if m:
            cur["x"], cur["y"] = int(m.group(1)), int(m.group(2))
        elif st.startswith("Enabled:"):
            cur["enabled"] = st.split(":", 1)[1].strip() == "yes"
        elif st.startswith("Scale:"):
            try:
                cur["scale"] = float(st.split(":", 1)[1]) or 1.0
            except ValueError:
                pass
        elif st.startswith("Transform:"):
            cur["transform"] = st.split(":", 1)[1].strip()
        else:
            m = _WLR_RANDR_MODE_RE.search(line)
            if m:
                cur["w"], cur["h"] = int(m.group(1)), int(m.group(2))
    monitors = []
    for o in outs:
        if not o["enabled"] or any(k not in o for k in ("x", "y", "w", "h")):
            continue
        w, h = o["w"], o["h"]
        if o["transform"] in ("90", "270", "flipped-90", "flipped-270"):
            w, h = h, w
        monitors.append((o["name"], o["x"], o["y"], round(w / o["scale"]), round(h / o["scale"])))
    if not monitors:
        raise RuntimeError("wlr-randr produced no parseable monitor output")
    log.info(f"Detected monitors (wayland): {monitors}")
    return monitors


_monitors_cache = {"result": None, "at": 0.0}
MONITORS_CACHE_TTL = 1.0   # Seconds - avoids re-spawning xrandr/wlr-randr repeatedly within a short window.

# A pin's "Maximize" fits the image to the monitor it's currently on (see
# run_pin_viewer's toggle_maximize) - but get_monitors() reports the
# monitor's FULL pixel resolution, which usually has some strip of screen
# space (a panel/taskbar/dock) reserved on top of that, invisible to
# xrandr. This is a rough, adjustable fudge-factor may not even be
# running - see this file's OpenRC/no-DE setup) - reserved off the
# BOTTOM of the monitor, where a panel/taskbar most commonly sits.
PIN_MAXIMIZE_HEIGHT_MARGIN = 46


def get_monitors():
    now = time.monotonic()
    if _monitors_cache["result"] is not None and (now - _monitors_cache["at"]) < MONITORS_CACHE_TTL:
        return _monitors_cache["result"]
    if BACKEND == "wayland":
        try:
            result = get_monitors_wayland()
        except Exception:
            result = None
            # patch 45: XWayland's RandR reports the same logical layout (GNOME/KDE have no wlr-randr)
            try:
                result = get_monitors_x11()
                log.info("get_monitors_wayland() failed - using XWayland's RandR monitor list instead")
            except Exception:
                log.warning("get_monitors_wayland() and XWayland RandR both failed - treating the display as one monitor")
        if result is None:
            fd, tmp_name = tempfile.mkstemp(suffix=".png")
            os.close(fd)
            tmp_path = Path(tmp_name)
            try:
                _wl_grab_full(tmp_path, fit=False)
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
    _capture_x11(path, region=(x, y, w, h))


def capture_monitor_wayland(x, y, w, h, path):
    """patch 45: grim region when available, otherwise full-screen grab cropped."""
    if shutil.which("grim"):
        try:
            run_logged(["grim", "-l", "1", "-s", "1"] + (["-c"] if _include_cursor() else [])
                       + ["-g", f"{x},{y} {w}x{h}", str(path)], check=True)
            return
        except Exception:
            log.warning("grim region capture failed - trying the other Wayland screenshot tools")
    _wl_grab_full(path)
    vx, vy, _vw, _vh = get_virtual_screen_bounds(get_monitors())
    from PIL import Image
    with Image.open(path) as img:
        img.load()
        img.crop((x - vx, y - vy, x - vx + w, y - vy + h)).save(path, compress_level=1)


def capture_monitor(x, y, w, h, path):
    if BACKEND == "wayland":
        capture_monitor_wayland(x, y, w, h, path)
    else:
        capture_monitor_x11(x, y, w, h, path)


def capture_full_screen_wayland(path):
    _wl_grab_full(path)


def _wl_tool_pref(name, default="auto"):
    from . import constants as _c
    return str(getattr(_c, name, default) or default).lower()


def _wl_grab_full(path, fit=True):
    """
    patch 45/46: whole desktop, in logical pixels. auto order: grim (wlroots: Sway, Hyprland,
    river, Wayfire, labwc), the xdg-desktop-portal Screenshot API (GNOME, KDE, anything with a
    portal; GNOME asks permission the first time), spectacle (KDE), gnome-screenshot (GNOME).
    """
    cursor = _include_cursor()
    pref = _wl_tool_pref("WAYLAND_CAPTURE_TOOL")
    order = ["grim", "portal", "spectacle", "gnome-screenshot"]
    if pref in order:
        order = [pref]
    errors = []
    for tool in order:
        try:
            if tool == "grim":
                if not shutil.which("grim"):
                    continue
                run_logged(["grim", "-l", "1", "-s", "1"] + (["-c"] if cursor else []) + [str(path)], check=True)
                return
            if tool == "portal":
                from .dbus_mini import portal_screenshot
                src = Path(portal_screenshot())
                from PIL import Image
                try:
                    with Image.open(src) as img:
                        img.load()
                        img.save(path, format="PNG", compress_level=1)
                finally:
                    try:
                        src.unlink()   # the portal's own copy (e.g. ~/Pictures/Screenshot-*.png on GNOME)
                    except OSError:
                        pass
            else:
                cmd = (["spectacle", "-b", "-n", "-f"] + (["-p"] if cursor else []) + ["-o", str(path)]
                       if tool == "spectacle" else
                       ["gnome-screenshot"] + (["-p"] if cursor else []) + ["-f", str(path)])
                if not shutil.which(tool):
                    continue
                Path(path).unlink(missing_ok=True)
                run_logged(cmd, check=True, timeout=30)
                if not Path(path).exists():
                    raise RuntimeError("no file written")
            if fit:
                _fit_to_logical(path)
            return
        except Exception as e:
            log.warning(f"Wayland capture with {tool} failed: {e}")
            errors.append(f"{tool}: {e}")
    raise RuntimeError("No working Wayland screenshot tool. Install grim (Sway/Hyprland/river/Wayfire/labwc) or "
                       "xdg-desktop-portal with your desktop's backend (GNOME/KDE), or spectacle / gnome-screenshot."
                       + (" Errors: " + "; ".join(errors) if errors else ""))


def _fit_to_logical(path):
    """spectacle/gnome-screenshot save physical pixels; the overlay works in logical ones."""
    _vx, _vy, vw, vh = get_virtual_screen_bounds(get_monitors())
    from PIL import Image
    with Image.open(path) as img:
        if img.size == (vw, vh):
            return
        log.info(f"Wayland capture is {img.size}, desktop is {vw}x{vh} logical - scaling to match")
        img.load()
        img.resize((vw, vh), Image.LANCZOS).save(path, compress_level=1)


def install_wayland_window_rules():
    """
    patch 46: tiling compositors tile the review/film/pin windows and ignore their size and
    position. Add a runtime float rule for our window classes once per compositor session
    (marker file in the runtime dir). WAYLAND_FLOAT_WINDOWS = False turns this off; a permanent
    rule in the compositor config does the same job.
    """
    from . import constants as _c
    if BACKEND != "wayland" or not getattr(_c, "WAYLAND_FLOAT_WINDOWS", True):
        return
    if os.environ.get("SWAYSOCK") and shutil.which("swaymsg"):
        key, cmds = "sway-" + Path(os.environ["SWAYSOCK"]).name, [
            ["swaymsg", 'for_window [class="^[Ss]nippysnappy"] floating enable']]
    elif os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") and shutil.which("hyprctl"):
        key, cmds = "hypr-" + os.environ["HYPRLAND_INSTANCE_SIGNATURE"], [
            ["hyprctl", "keyword", "windowrulev2", "float,class:^([Ss]nippysnappy.*)$"],
            ["hyprctl", "keyword", "windowrule", "float, class:^([Ss]nippysnappy.*)$"]]
    else:
        return
    rt = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(rt) / "snippysnappy" if rt else None   # no runtime dir: no marker (rule may repeat)
    marker = base / f"float-rule-{re.sub(r'[^A-Za-z0-9_.-]', '_', key)}" if base else None
    if marker is not None and marker.exists():
        return
    for cmd in cmds:
        try:
            r = run_logged(cmd, capture_output=True, text=True, timeout=3)
            if r.returncode == 0 and "error" not in (r.stdout or "").lower() and "invalid" not in (r.stdout or "").lower():
                log.info(f"Wayland: float rule added ({' '.join(cmd[:3])}...)")
                break
        except Exception:
            log.exception("Wayland: could not add the float rule (non-fatal)")
    if marker is not None:
        try:
            base.mkdir(exist_ok=True, mode=0o700)
            marker.touch()
        except OSError:
            pass


def wayland_active_monitor(monitors):
    """
    patch 45: the Wayland stand-in for X11's "monitor under the mouse". Hyprland reports the
    pointer; sway reports the focused output. None when the compositor offers neither.
    """
    try:
        if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") and shutil.which("hyprctl"):
            out = run_logged(["hyprctl", "cursorpos"], capture_output=True, text=True, check=True, timeout=2).stdout
            px, py = (int(float(v)) for v in out.strip().split(","))
            return monitor_at_point(monitors, px, py)
        if os.environ.get("SWAYSOCK") and shutil.which("swaymsg"):
            import json
            out = run_logged(["swaymsg", "-t", "get_outputs", "-r"], capture_output=True, text=True, check=True, timeout=2).stdout
            for o in json.loads(out):
                if o.get("focused"):
                    for m in monitors:
                        if m[0] == o.get("name"):
                            return m
                    r = o.get("rect") or {}
                    return monitor_at_point(monitors, r.get("x", 0) + r.get("width", 0) / 2, r.get("y", 0) + r.get("height", 0) / 2)
    except Exception:
        log.exception("Could not ask the compositor for the active monitor (non-fatal)")
    return None


def _wayland_window_rects():
    """patch 45: visible window rectangles (sway / Hyprland) for slurp's click-a-window mode."""
    import json
    rects = []
    try:
        if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") and shutil.which("hyprctl"):
            mons = json.loads(run_logged(["hyprctl", "monitors", "-j"], capture_output=True, text=True, check=True, timeout=2).stdout)
            active = {m.get("activeWorkspace", {}).get("id") for m in mons}
            for c in json.loads(run_logged(["hyprctl", "clients", "-j"], capture_output=True, text=True, check=True, timeout=2).stdout):
                if c.get("mapped", True) and not c.get("hidden") and c.get("workspace", {}).get("id") in active:
                    (x, y), (w, h) = c["at"], c["size"]
                    rects.append((x, y, w, h, c.get("class") or ""))
        elif os.environ.get("SWAYSOCK") and shutil.which("swaymsg"):
            tree = json.loads(run_logged(["swaymsg", "-t", "get_tree", "-r"], capture_output=True, text=True, check=True, timeout=2).stdout)
            def walk(n):
                if n.get("pid") and n.get("visible"):
                    r = n["rect"]
                    rects.append((r["x"], r["y"], r["width"], r["height"], n.get("app_id") or n.get("name") or ""))
                for k in n.get("nodes", []) + n.get("floating_nodes", []):
                    walk(k)
            walk(tree)
    except Exception:
        log.exception("Could not list windows from the compositor (non-fatal - plain region drag instead)")
    return [r for r in rects if r[2] > 0 and r[3] > 0]


def capture_full_screen(path):
    if BACKEND == "wayland":
        capture_full_screen_wayland(path)
    else:
        capture_full_screen_x11(path)


class WindowSelectCancelled(Exception):
    """Raised by select_window_id() when Escape is pressed instead of clicking a window."""
    pass


class _X11EscapePoller:
    """
    Answers "is the Escape key held down right now?" via libX11's
    XQueryKeymap (through ctypes - no extra dependency). Polling the
    server's keyboard state directly is what makes Escape-to-cancel
    possible while `xdotool selectwindow` is running:
    that command only grabs the POINTER, and offers no cancel path of its
    own, so key events go to whichever window has focus and never reach us.
    XQueryKeymap reads global key state regardless of grabs or focus.
    """
    XK_ESCAPE = 0xFF1B

    def __init__(self):
        import ctypes
        import ctypes.util
        libname = ctypes.util.find_library("X11")
        if not libname:
            raise RuntimeError("libX11 not found")
        self._x11 = ctypes.CDLL(libname)
        self._x11.XOpenDisplay.restype = ctypes.c_void_p
        self._x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        self._x11.XKeysymToKeycode.restype = ctypes.c_ubyte
        self._x11.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        self._x11.XQueryKeymap.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        self._x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
        self._dpy = self._x11.XOpenDisplay(None)
        if not self._dpy:
            raise RuntimeError("XOpenDisplay failed")
        self._keycode = self._x11.XKeysymToKeycode(self._dpy, self.XK_ESCAPE)
        self._buf = ctypes.create_string_buffer(32)

    def escape_down(self):
        self._x11.XQueryKeymap(self._dpy, self._buf)
        return bool(self._buf.raw[self._keycode // 8] & (1 << (self._keycode % 8)))

    def close(self):
        try:
            self._x11.XCloseDisplay(self._dpy)
        except Exception:
            pass


def set_bypass_compositor_hint(window_id):
    """
    Sets the _NET_WM_BYPASS_COMPOSITOR=1 EWMH hint on an X11 window,
    asking the running compositor (xfwm4's built-in one, picom, etc.) to
    stop compositing this ONE window entirely leaving every other window's
    compositing untouched. Best-effort: not every compositor honors it,
    and nothing breaks if it's silently ignored.
    """
    try:
        subprocess.run(
            ["xprop", "-id", str(window_id), "-f", "_NET_WM_BYPASS_COMPOSITOR", "32c",
             "-set", "_NET_WM_BYPASS_COMPOSITOR", "1"],
            check=True, capture_output=True,
        )
        log.debug(f"Set _NET_WM_BYPASS_COMPOSITOR=1 on window {window_id}")
    except Exception:
        log.debug(f"Could not set _NET_WM_BYPASS_COMPOSITOR on window {window_id} (non-fatal - compositor may not support/honor it)")


class _X11FocusHelper:
    """
    Thin ctypes wrapper around XSetInputFocus/XGetInputFocus - lets a
    process take genuine X11 keyboard focus for one of its OWN windows
    directly at the X server level, and check who currently holds it,
    bypassing Tk's focus_force() entirely.
    """
    _REVERT_TO_PARENT = 2

    def __init__(self):
        import ctypes
        import ctypes.util
        libname = ctypes.util.find_library("X11")
        if not libname:
            raise RuntimeError("libX11 not found")
        self._ctypes = ctypes
        self._x11 = ctypes.CDLL(libname)
        self._x11.XOpenDisplay.restype = ctypes.c_void_p
        self._x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        self._x11.XSetInputFocus.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        self._x11.XGetInputFocus.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_int)]
        self._x11.XFlush.argtypes = [ctypes.c_void_p]
        self._x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
        self._dpy = self._x11.XOpenDisplay(None)
        if not self._dpy:
            raise RuntimeError("XOpenDisplay failed")

    def set_focus(self, window_id):
        """window_id: an X window id, e.g. from Tk's winfo_id()."""
        if BACKEND == "wayland":
            # patch 46: XWayland compositors revert a client's XSetInputFocus; an EWMH activation
            # request flagged as coming from a pager/taskbar (source 2) is what they honour
            # (GNOME/KDE; sway only with `focus_on_window_activation focus`).
            self.activate(window_id)
            return
        self._x11.XSetInputFocus(self._dpy, self._ctypes.c_ulong(window_id), self._REVERT_TO_PARENT, 0)
        self._x11.XFlush(self._dpy)

    def _toplevel_of(self, window_id):
        """Tk's winfo_id() is a child of the managed wrapper window - walk up to the root's child."""
        ct = self._ctypes
        x = self._x11
        x.XQueryTree.argtypes = [ct.c_void_p, ct.c_ulong, ct.POINTER(ct.c_ulong), ct.POINTER(ct.c_ulong),
                                 ct.POINTER(ct.c_void_p), ct.POINTER(ct.c_uint)]
        x.XFree.argtypes = [ct.c_void_p]
        win = window_id
        for _ in range(8):
            root, parent, kids, n = ct.c_ulong(), ct.c_ulong(), ct.c_void_p(), ct.c_uint()
            if not x.XQueryTree(self._dpy, win, ct.byref(root), ct.byref(parent), ct.byref(kids), ct.byref(n)):
                break
            if kids.value:
                x.XFree(kids)
            if parent.value in (0, root.value):
                return win, root.value
            win = parent.value
        return win, None

    def activate(self, window_id):
        """Sends _NET_ACTIVE_WINDOW (source = pager) for the toplevel containing window_id."""
        ct = self._ctypes
        x = self._x11
        top, root = self._toplevel_of(window_id)
        if root is None:
            x.XDefaultRootWindow.restype = ct.c_ulong
            x.XDefaultRootWindow.argtypes = [ct.c_void_p]
            root = x.XDefaultRootWindow(self._dpy)
        x.XInternAtom.restype = ct.c_ulong
        x.XInternAtom.argtypes = [ct.c_void_p, ct.c_char_p, ct.c_int]

        class _CM(ct.Structure):
            _fields_ = [("type", ct.c_int), ("serial", ct.c_ulong), ("send_event", ct.c_int),
                        ("display", ct.c_void_p), ("window", ct.c_ulong), ("message_type", ct.c_ulong),
                        ("format", ct.c_int), ("data", ct.c_long * 5)]

        class _Ev(ct.Union):
            _fields_ = [("xclient", _CM), ("pad", ct.c_long * 24)]

        ev = _Ev()
        ev.xclient.type = 33                      # ClientMessage
        ev.xclient.send_event = 1
        ev.xclient.window = top
        ev.xclient.message_type = x.XInternAtom(self._dpy, b"_NET_ACTIVE_WINDOW", 0)
        ev.xclient.format = 32
        ev.xclient.data[0] = 2                    # source indication: pager
        ev.xclient.data[1] = 0                    # CurrentTime
        x.XSendEvent.argtypes = [ct.c_void_p, ct.c_ulong, ct.c_int, ct.c_long, ct.c_void_p]
        x.XSendEvent(self._dpy, root, 0, (1 << 19) | (1 << 20), ct.byref(ev))
        x.XFlush(self._dpy)

    def get_focus_window(self):
        """Returns the X window id currently holding real input focus."""
        focus = self._ctypes.c_ulong()
        revert = self._ctypes.c_int()
        self._x11.XGetInputFocus(self._dpy, self._ctypes.byref(focus), self._ctypes.byref(revert))
        return focus.value

    def close(self):
        try:
            self._x11.XCloseDisplay(self._dpy)
        except Exception:
            pass


def select_window_id():
    """
    X11 only - see pick_region_interactive_wayland for the Wayland
    equivalent. Runs `xdotool selectwindow` (click a window to pick it) and
    watches for Escape while it waits - see _X11EscapePoller for why that
    needs its own polling. Raises WindowSelectCancelled on Escape. If the
    key poller can't be set up (no libX11 via ctypes, etc.), logs a warning
    and behaves as before: click-only, no Escape cancel.
    """
    proc = popen_logged(["xdotool", "selectwindow"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    poller = None
    try:
        poller = _X11EscapePoller()
    except Exception as e:
        log.warning(f"Escape-to-cancel unavailable for window selection ({e}) - click a window to continue")
    try:
        while True:
            try:
                out, err = proc.communicate(timeout=0.05)
                break
            except subprocess.TimeoutExpired:
                if poller is not None and poller.escape_down():
                    log.info("Escape pressed during window selection - cancelling")
                    proc.terminate()
                    try:
                        proc.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    raise WindowSelectCancelled()
    finally:
        if poller is not None:
            poller.close()
    if proc.returncode != 0:
        log.error(f"xdotool selectwindow failed (rc={proc.returncode}): {err}")
        raise subprocess.CalledProcessError(proc.returncode, proc.args, output=out, stderr=err)
    win_id = out.strip()
    log.debug(f"Selected window id: {win_id}")
    return win_id


def get_window_geometry(window_id):
    """
    X11 only - Wayland's --window mode uses pick_region_interactive_wayland
    + capture_monitor_wayland instead (no window-id concept).
    """
    out = run_logged(
        ["xdotool", "getwindowgeometry", "--shell", window_id],
        capture_output=True, text=True, check=True,
    ).stdout
    vals = dict(line.split("=") for line in out.strip().splitlines())
    geom = int(vals["X"]), int(vals["Y"]), int(vals["WIDTH"]), int(vals["HEIGHT"])
    log.debug(f"Window {window_id} geometry: {geom}")
    return geom


def capture_window(window_id, path):
    if BACKEND == "wayland":
        raise RuntimeError("capture_window is X11-only (Wayland uses a region pick)")
    _capture_x11(path, window_id=window_id)


def pick_region_interactive_wayland():
    """
    --window under Wayland. Returns (x, y, w, h); raises WindowSelectCancelled on Escape.
    patch 45: on sway/Hyprland the visible windows are handed to slurp so a click picks a
    window (as on X11) and a drag still selects a free region; without slurp, the app's own
    Tk drag-to-select picker is used (works on any compositor).
    """
    if not shutil.which("slurp"):
        log.info("--window on Wayland: slurp not installed - using the built-in region picker")
        from .recorder import pick_region_interactive_x11
        return pick_region_interactive_x11()
    rects = _wayland_window_rects()
    feed = "".join(f"{x},{y} {w}x{h}\n" for x, y, w, h, _label in rects)
    try:
        out = run_logged(["slurp"], input=feed if feed else None, capture_output=True, text=True, check=True).stdout.strip()
    except subprocess.CalledProcessError as e:
        if _is_slurp_cancel(e):
            log.info("slurp selection cancelled (Escape) - cancelling")
            raise WindowSelectCancelled()
        raise
    xy, wh = out.split(" ")[:2]
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
    # patch 45: same behaviour as X11 (the active monitor, no click) when the compositor can
    # tell us which one that is; otherwise slurp -o; otherwise the fallback monitor.
    monitors = get_monitors()
    mon = wayland_active_monitor(monitors)
    if mon is not None:
        return mon
    if not shutil.which("slurp"):
        log.info("--fullscreen on Wayland: active monitor unknown and slurp not installed - using the fallback monitor")
        return pick_fallback_monitor(monitors)
    try:
        out = run_logged(
            ["slurp", "-o", "-f", "%o %x,%y %wx%h"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except subprocess.CalledProcessError as e:
        if _is_slurp_cancel(e):
            log.info("slurp output pick cancelled (Escape) - cancelling")
            raise WindowSelectCancelled()
        raise
    name, xy, wh = out.split(" ")
    x, y = (int(v) for v in xy.split(","))
    w, h = (int(v) for v in wh.split("x"))
    return name, x, y, w, h


def _is_slurp_cancel(exc):
    """
    slurp exits nonzero with "selection cancelled" on stderr when Escape is
    pressed - distinguishes that from a real failure (no compositor, etc.).
    """
    return "cancel" in (getattr(exc, "stderr", "") or "").lower()



def wait_capture_delay(delay):
    """
    The shared "count down, then proceed" step for --delay in --window and
    --fullscreen mode (select/"monitor" mode handles its own version of
    this internally on SelectorApp.
    """
    log.info(f"--delay: waiting {delay:g}s before capturing")
    from .notify_toast import notify
    notify(f"Capturing in {delay:g}s\u2026", blocking=False)
    poller = make_delay_cancel_poller(pump=True, delay=delay)
    deadline = time.monotonic() + delay
    try:
        while time.monotonic() < deadline:
            if poller is not None and poller.escape_down():
                log.info("--delay: Escape pressed during the countdown - cancelling")
                sys.exit(0)
            time.sleep(0.05)
    finally:
        if poller is not None:
            poller.close()


class _TkCancelPoller:
    """
    patch 45: Wayland has no global key query, so the --delay countdown shows a small
    always-on-top "Cancel" box instead (Escape works while it has focus, a click always).
    Same escape_down()/close() interface as _X11EscapePoller. pump=True when no Tk
    mainloop is running (wait_capture_delay) - escape_down() then services Tk itself.
    """
    def __init__(self, pump=False, delay=0.0):
        import tkinter as tk
        self._pump = pump
        self._cancel = False
        self._own_root = None
        parent = tk._default_root
        if parent is None:
            self._own_root = parent = tk.Tk(className="snippysnappy-delay")
            parent.withdraw()
        self._deadline = time.monotonic() + delay
        w = self.win = tk.Toplevel(parent, class_="snippysnappy-delay")
        w.title("snippysnappy")
        w.attributes("-topmost", True)
        w.configure(bg="#222222", padx=10, pady=8)
        self._label = tk.Label(w, text="", fg="#eeeeee", bg="#222222", font=("TkDefaultFont", 11))
        self._label.pack(side="left", padx=(0, 10))
        tk.Button(w, text="Cancel (Esc)", command=self._set).pack(side="left")
        w.bind("<Escape>", lambda e: self._set())
        w.protocol("WM_DELETE_WINDOW", self._set)
        self._update_label()
        w.update_idletasks()
        sw = w.winfo_screenwidth()
        w.geometry(f"+{max(0, sw // 2 - w.winfo_reqwidth() // 2)}+40")
        w.deiconify()
        w.lift()
        try:
            w.focus_force()
        except Exception:
            pass
        if pump:
            w.update()

    def _set(self):
        self._cancel = True

    def _update_label(self):
        left = max(0.0, self._deadline - time.monotonic())
        try:
            self._label.configure(text=f"Capturing in {left:.0f}s")
        except Exception:
            pass

    def escape_down(self):
        self._update_label()
        if self._pump:
            try:
                self.win.update()
            except Exception:
                pass
        return self._cancel

    def close(self):
        try:
            self.win.destroy()
            if self._own_root is not None:
                self._own_root.destroy()
            else:
                self.win.master.update_idletasks()
        except Exception:
            pass
        time.sleep(0.15)   # let the compositor unmap it before the screenshot


def make_delay_cancel_poller(pump=False, delay=0.0):
    """patch 45: X11 -> global Escape poll; Wayland -> visible Cancel box. None if neither works."""
    try:
        if BACKEND == "x11":
            return _X11EscapePoller()
        return _TkCancelPoller(pump=pump, delay=delay)
    except Exception:
        log.warning("Could not set up Escape-to-cancel for the --delay countdown - proceeding anyway")
        return None




# ---- patch 43: performance (capture, display, film playback, frame cache, compositing) ----
_p43_installed = True
_p44_installed = True
_p45_installed = True
_p46_installed = True
