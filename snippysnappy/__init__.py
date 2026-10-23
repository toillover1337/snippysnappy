# snippysnappy v1.0.1
"""
snippysnappy - a lightweight screenshot capture with adjustable selection,
free-hand edit (pen/highlighter), magnifier, measurement tool, and OCR text
extraction.

This package is a compartmentalized split of what used to be one flat
~12,600-line script file. Nothing about how the app BEHAVES changed as
part of that split - see HANDOVER.md for the reasoning, the module map,
and (importantly) the list of real bugs that are still open from before
this split and were deliberately NOT touched while doing it.

This __init__.py is a compatibility facade: it re-exports every module's
public (and several historically-"private", underscore-prefixed) names at
the TOP level of the `snippysnappy` package, so `import snippysnappy as ss`
followed by `ss.SelectorApp`, `ss.run_pin_viewer`, `ss.notify`,
`ss._coord`, etc. all keep working exactly as they did against the old
single file - this is what the existing test suite (`/home/claude/test/`)
relies on, and what any external caller of the old flat module would have
been doing too.

Module map (see HANDOVER.md for the full rationale):
    _paths            - INSTALL_DIR (where the log file / icon / sniptext folder live)
    constants          - all the tunable config knobs (sizes, colors, defaults, toggles)
    logging_setup      - the `log` logger, run_logged/popen_logged
    icon               - optional app icon loading
    text_render        - Text-tool font loading + word-wrap/rendering
    capture            - X11/Wayland screen/monitor/window capture backends
    clipboard          - system clipboard read/write (image + text)
    ocr                - tesseract invocation + OCR image preprocessing
    notify_toast       - desktop notifications (notify-send + in-app toast fallback)
    fuzzy_open         - --nil arg parsing, junk-file scoring, fuzzy file search
    prompt_windows     - the --open prompt / GUI file picker / --delay countdown windows
    coordinator        - cross-process window/pin discovery (hub/client socket protocol)
                         + the local (same-process) window/pin registries
    selector_app       - SelectorApp, the main capture/annotation window (the big one)
    pin_viewer         - run_pin_viewer, the floating Pin window
    app                - main(), the CLI entry point
"""

# Re-exported so `ss.subprocess.Popen = ...` (used by several tests to
# intercept pin-viewer subprocess launches) mutates the SAME module object
# every other file in this package imports and calls - subprocess is a
# process-wide singleton in sys.modules regardless of which file does
# `import subprocess`, so this is not a separate copy.
import subprocess

from ._paths import PACKAGE_DIR, INSTALL_DIR
from .constants import *  # noqa: F401,F403 - all the config knobs; deliberately re-exported wholesale, see constants.py
from .logging_setup import log, run_logged, popen_logged
from .icon import APP_ICON_FILENAME, _load_app_icon
from .text_render import load_text_font, load_serif_font, draw_text_runs, _font_ascent, _wrap_line_tokens
from .capture import (
    BACKEND, detect_backend,
    get_monitors, monitor_at_point, pick_fallback_monitor, get_mouse_position,
    get_virtual_screen_bounds, capture_full_screen, capture_full_screen_x11, capture_full_screen_wayland,
    capture_monitor, capture_monitor_x11, capture_monitor_wayland,
    capture_window, select_window_id, get_window_geometry, WindowSelectCancelled, _X11EscapePoller,
    pick_region_interactive_wayland, pick_output_interactive_wayland, _is_slurp_cancel,
    wait_capture_delay, PIN_MAXIMIZE_HEIGHT_MARGIN, MONITORS_CACHE_TTL, _monitors_cache,
)
from .clipboard import (
    copy_image_to_clipboard, copy_image_to_clipboard_x11, copy_image_to_clipboard_wayland,
    copy_text_to_clipboard, copy_text_to_clipboard_x11, copy_text_to_clipboard_wayland,
    get_system_clipboard_image, get_system_clipboard_text,
)
from .ocr import ocr_image, preprocess_for_ocr, hex_to_rgb
from .notify_toast import (
    notify, show_toast_fallback, ensure_dbus_env, timestamp,
    _notify_system_primary, _notify_toast_primary, TOAST_DISPLAY_MS,
)
from .fuzzy_open import (
    parse_nil_args, make_nil_placeholder, is_image_file, junk_score,
    get_default_open_dir, resolve_open_path, fuzzy_match, fuzzy_search_images,
)
from .prompt_windows import OpenPromptApp, GuiPromptApp, DelayPromptApp
from .coordinator import (
    # local (same-process) registries
    _LOCAL_WINDOWS, _register_window, _unregister_window,
    _LOCAL_PINS, _PinHandle, _register_pin, _unregister_pin,
    # cross-process coordinator
    SOCKET_DIR, SOCKET_PATH, _coord, COORD_POLL_INTERVAL_MS,
    init_coordinator, shutdown_coordinator,
    broadcast_window_roster, remote_windows, remote_pins,
    _find_local_window, _find_local_pin, focus_local_window,
    request_remote_focus, request_remote_pin_close, request_remote_merge,
    _handle_incoming_merge_message,
)
from .selector_app import SelectorApp
from .pin_viewer import run_pin_viewer
from .app import main

__all__ = [name for name in dir() if not name.startswith("__")]
