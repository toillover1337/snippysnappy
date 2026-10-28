# snippysnappy v1.0.3
"""Desktop notifications (notify-send and an in-app toast fallback), D-Bus env fixups, and the timestamp() helper."""
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from .constants import NOTIFICATION_BACKEND
from .logging_setup import log, popen_logged
from .capture import get_monitors, monitor_at_point

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


TOAST_DISPLAY_MS = 2500   # how long an in-app toast stays on screen before self-closing

_TOAST_SUBPROCESS_SCRIPT = r"""
import sys, tkinter as tk
title, body, x, y, w, h, duration_ms = sys.argv[1:8]
x, y, w, h, duration_ms = int(x), int(y), int(w), int(h), int(duration_ms)
root = tk.Tk()
root.withdraw()
root.overrideredirect(True)
root.attributes("-topmost", True)
root.geometry(f"{w}x{h}+{x}+{y}")
frame = tk.Frame(root, bg="#222222", highlightbackground="#555555", highlightthickness=1)
frame.pack(fill="both", expand=True)
tk.Label(frame, text=title, fg="white", bg="#222222", font=("", 11, "bold"), anchor="w", justify="left").pack(fill="x", padx=10, pady=(8, 2))
if body:
    tk.Label(frame, text=body, fg="#cccccc", bg="#222222", anchor="w", justify="left", wraplength=300).pack(fill="x", padx=10, pady=(0, 8))
root.deiconify()
root.after(duration_ms, root.destroy)
root.mainloop()
"""


def show_toast_fallback(title, body, blocking=True, anchor_window=None):
    """
    Runs as a fully separate, detached OS PROCESS (a small standalone
    Tkinter script - see _TOAST_SUBPROCESS_SCRIPT - launched the same way
    copy_image_to_clipboard_x11 detaches xclip: start_new_session=True, no
    pipe kept open).

    blocking=True waits for that process to exit before returning (used
    where the caller genuinely wants the notification to have been fully
    shown before moving on); blocking=False (the common case - see notify)
    returns as soon as the process is launched, which is near-instant,
    so a caller that then immediately does something else (like closing
    the window on "Also Exit") is never held up waiting on the toast.

    anchor_window, when given, positions the toast on whichever monitor
    THAT window is currently on, rather than always defaulting to
    whichever monitor xrandr happens to report first.
    """
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
    try:
        proc = popen_logged(
            [sys.executable, "-c", _TOAST_SUBPROCESS_SCRIPT, title, body, str(x), str(y), str(w), str(h), str(TOAST_DISPLAY_MS)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        log.exception("Toast fallback itself failed (non-fatal)")
        return False
    if blocking:
        try:
            proc.wait(timeout=TOAST_DISPLAY_MS / 1000 + 2)
        except Exception:
            pass
    return True


def notify(title, body="", allow_toast=True, blocking=True, anchor_window=None):
    """
    Two channels are available: the OS notification daemon (notify-send)
    and an in-app Tk toast. NOTIFICATION_BACKEND picks which one is tried
    first; the other is used as a fallback if the primary fails.
    blocking=False is for callers that can't afford to wait out the toast's
    display time (see show_toast_fallback) - e.g. "Capture taken" needs to
    return immediately so the review window isn't delayed from appearing.
    anchor_window is passed straight through to show_toast_fallback.
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


