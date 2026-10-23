# snippysnappy v1.0.1
"""Optional titlebar/taskbar app icon loading (inert until a PNG is dropped in - see comment below)."""
import tkinter as tk

from ._paths import INSTALL_DIR
from .logging_setup import log

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
# application-launcher/menu entries).
# point Icon= at an absolute path to the same PNG (simplest, works
# regardless of icon theme), or install the PNG into the standard XDG
# hicolor icon theme directory and set Icon= snippysnappy ( no path/ext).
APP_ICON_FILENAME = "snippysnappy-icon.png"


def _load_app_icon():
    """
    Loads APP_ICON_FILENAME (from the same directory as this script) as a
    Tk PhotoImage for use with iconphoto(). Returns None - quietly, no
    error - if the file doesn't exist yet or fails to load, so the app
    runs completely normally without an icon until one is added.
    """
    icon_path = INSTALL_DIR / APP_ICON_FILENAME
    if not icon_path.exists():
        return None
    try:
        return tk.PhotoImage(file=str(icon_path))
    except Exception:
        log.warning(f"Could not load app icon from {icon_path} - Tk's PhotoImage needs GIF/PNG/PPM (a plain PNG is safest)")
        return None
