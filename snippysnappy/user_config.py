# snippysnappy v1.0.3
"""
The user's own, external config file - distinct from constants.py (which
lives inside the installed package and gets overwritten by any update or
reinstall). Lives at $XDG_CONFIG_HOME/snippysnappy/config.py (falling
back to ~/.config/snippysnappy/config.py), created with a starter
template + comments the first time snippysnappy runs and no such file
exists yet. Anything a user sets there that matches one of constants.py's
own names overrides it - nothing here is required; an empty or missing
file just means every constants.py default applies unchanged.
"""
import os
from pathlib import Path

from .logging_setup import log, set_log_level

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or "~/.config").expanduser() / "snippysnappy"
CONFIG_PATH = CONFIG_DIR / "config.py"

_STARTER_TEMPLATE = '''"""
snippysnappy user config - anything set here with the same name as a
constants.py default overrides it. This file is YOURS: it is never
touched by an update or reinstall (constants.py, inside the installed
package, is - so this is the right place for your own preferences, not
that one). Delete a line (or the whole file) to go back to the built-in
default for that setting. Only uncomment/set the ones you actually want
changed - everything below is commented out and has no effect as-is.
"""

# EXIT_BEHAVIOR = "background"       # "background" (default, closed windows stay alive/reachable via Merge in) or "full_close"
# WARN_ON_ACTIVE_CLOSE = True        # confirm before closing the active tab/window from the Tabs panel
# DEFAULT_COPY_ON_SAVE = False
# DEFAULT_ALSO_EXIT = False
# DEFAULT_ALSO_SAVE = False
# NIL_CANVAS_DEFAULT_WIDTH = 800
# NIL_CANVAS_DEFAULT_HEIGHT = 600
# NIL_CANVAS_DEFAULT_COLOR = "white"
# TEXT_FONT_SIZE_DEFAULT = 24
# DEFAULT_CAPTURE_DELAY_SECONDS = 5
# DEFAULT_PEN_MODE = "select"        # "select" | "pen" | "spray" | "highlight" | "eraser"
# DEFAULT_TEXT_MODE = "select"       # "select" | "write"
# DEFAULT_SHAPE_MODE = "select"      # "select" | "line" | "box" | "ellipse"
# DEFAULT_SNIP_MODE = "select"       # "select" | "box" | "ellipse" | "freeform"
# CONFIRM_LAYER_CHOICE = True        # False = never show the merge-into-current-layer / new-layer popup
# NEW_LAYER_ON_EDIT_MODE = "ask"   # "ask" | "current" | "new" - what happens when you edit with a different tool than the layer you last used: ask via popup, always add to the current layer, or always start a new layer. When set, overrides DEFAULT_NEW_LAYER_ON_EDIT, SILENCE_NEW_LAYER_POPUP and CONFIRM_LAYER_CHOICE.
# TEXT_CURSOR_COLOR = "#ffd800"
# TEXT_CURSOR_ALPHA = 235
# TEXT_CURSOR_CONTRAST_TEXT = True
# VIDEO_FILE_EXTENSIONS = (".mkv", ".mp4", ".webm", ".mov", ".avi")
# VIDEO_FUZZ_DIR = None

# ---- save folders ----
# SNIPPYSNAPS_DIR = "~/Pictures/Snippysnaps"
# SLOPPY_SNAPS_DIR = "~/Pictures/Sloppysnaps"
# SNIPPYTEXT_DIR = "~/Documents/Snippytext"
# SLOPPY_TEXT_DIR = "~/Documents/Sloppytext"
# SNIPPYFILMS_DIR = "~/Videos/Snippyfilms"
# SLOPPY_FILMS_DIR = "~/Videos/Sloppyfilms"
# SLOPPY_CLEANUP_DAYS = 2              # None or 0 disables the sloppy sweep
# SLOPPY_CLEANUP_INTERVAL_MINUTES = 60
# NEW_WINDOW_MONITOR = "inactive"     # "inactive" | "active"
# BACKUP_ORIGINAL = False          # True: keep a raw copy of every new capture/recording in the Sloppy folders (swept after SLOPPY_CLEANUP_DAYS)
# RECORD_AUDIO = "off"             # "off" | "desktop" | "mic" | "both"
# AUDIO_DESKTOP_DEVICE = None      # None = default output monitor; or a name from `pactl list short sources`
# AUDIO_MIC_DEVICE = None          # None = default input; or a name from `pactl list short sources`
# FILM_DEFAULT_VOLUME = 100        # 0-100, starting playback volume in the film viewer
# TEMP_DIR = None                  # None = ~/.cache/snippysnappy/tmp; or an absolute path
# TEMP_KEEP_HOURS = 24             # how long a closed process's temp folder is kept before cleanup
# SPRAY_DOT_WIDTH_DEFAULT = 2     # Spray Can's starting Width (dot size)
# FILM_SNIP_MODE = "copy"          # "copy" = film Snip copies a region into a movable object, no hole; "cut" = image-style, leaves a hole
# FILM_SNIP_HOLE_COLOR = "#000000" # color shown where a "cut" film snip was taken from
# SNIP_SELECT_ALL_OBJECTS = True   # False = Snip's Select mode only moves snips (not text/shapes/lifted drawings)
# ALIGN_GUIDES_ENABLED = True      # False = no pink alignment guides while moving / resizing
# ALIGN_GUIDE_TOLERANCE_PX = 6     # on-screen pixels; each edge shows only its nearest match
# FILM_RESIZE_BG_DEFAULT = "black" # film Resize > Background fill: "black" | "white"
# LOG_PATH = "~/.cache/snippysnappy/snippysnappy.log"   # log file; None = no log file (default: snippysnappy.log in the install folder)
# MAX_LOG_BYTES = 1_000_000        # rotate the log once it exceeds this size
# LOG_BACKUP_COUNT = 3             # rotated-out logs kept (snippysnappy.log.1, .2, ...)
# LOG_LEVEL = "WARNING"           # least severe level logged: "DEBUG" | "INFO" | "WARNING" | "ERROR". INFO/DEBUG for troubleshooting.
# TOOLTIPS_ENABLED = True         # False = no hover descriptions on toolbar buttons
# TOOLTIP_DELAY_MS = 500          # hover time before a tip appears
# TOOLTIP_OVERRIDES = {}          # {"Copy": "my text"} or {("film", "Copy"): "my text"} - replaces a built-in tip
# LEADER_ENABLED = True          # False = no leader key
# LEADER_KEY = "backslash"       # Tk keysym starting a leader sequence, e.g. "backslash", "semicolon", "Control-space"
# LEADER_TIMEOUT_MS = 5000       # leader popup closes after this long without a key (0 = never)
# LEADER_BINDINGS = {}           # {"image": {"esb": "mode:snip:box", "xx": None}, "film": {...}} - see leader.py
# FILM_SPACE_PLAYPAUSE = True    # Space plays/pauses in film windows (not while typing)
# FILM_R_RESTART = True          # R restarts playback in film windows (not while typing)
# FILM_SPLICE_STEP = 0.1         # Splice visual mode: seconds per h/l (arrow) step
# FILM_SPLICE_BIG_STEP = 1.0     # Splice visual mode: seconds per H/L (Shift+arrow) step
# FILM_SPLICE_VIM = True         # Splice mode: y take region, p insert taken region, P insert a copy (not while typing)
# FILM_FRAME_CACHE_MB = 128   # Memory for decoded film frames kept for fast scrubbing (per film window).
# CAPTURE_BACKEND = "auto"    # X11 screenshots: "auto"/"pillow" = in-process (Pillow XCB + libX11), "maim" = always use maim
# CAPTURE_INCLUDE_CURSOR = True   # Paint the mouse cursor into in-process screenshots (maim does by default)
# WAYLAND_CAPTURE_TOOL = "auto"   # "auto" | "grim" | "portal" | "spectacle" | "gnome-screenshot"
# WAYLAND_RECORD_TOOL = "auto"    # "auto" | "wf-recorder" | "portal" (GNOME/KDE: portal + PipeWire, needs gst-launch-1.0 + pipewiresrc)
# WAYLAND_FLOAT_WINDOWS = True    # sway/Hyprland: float snippysnappy windows instead of tiling them
# PIN_WAYLAND_OPACITY = "auto"   # "auto" | "native" | "simulated" - how the pin opacity slider works on Wayland
# PLUGINS_ENABLED = True         # False = never load plugin files (extra audio/video effects)
# PLUGIN_DIR = None              # None = ~/.config/snippysnappy/plugins (created automatically)
'''


_SYNC_STATE_PATH = CONFIG_DIR / ".synced_defaults"


def _template_settings():
    # {NAME: full commented line} for every "# NAME = value" line in the starter template
    import re
    out = {}
    for line in _STARTER_TEMPLATE.splitlines():
        m = re.match(r"^#[ \t]*([A-Z][A-Z0-9_]*)[ \t]*=", line)
        if m:
            out[m.group(1)] = line
    return out


def _sync_missing_defaults():
    # Settings added to the starter template by an update get appended (commented out) to an
    # existing config once; a record of what has been offered keeps a line you delete from coming back.
    import json
    import re
    tmpl = _template_settings()
    try:
        known = set(json.loads(_SYNC_STATE_PATH.read_text(encoding="utf-8")))
    except Exception:
        known = set()
    text = CONFIG_PATH.read_text(encoding="utf-8")
    missing = [
        n for n in tmpl
        if n not in known and not re.search(rf"^[ \t]*#?[ \t]*{re.escape(n)}[ \t]*=", text, re.MULTILINE)
    ]
    if missing:
        if not text.endswith("\n"):
            text += "\n"
        text += "\n# ---- new settings added by a snippysnappy update (commented out - uncomment to change) ----\n"
        text += "\n".join(tmpl[n] for n in missing) + "\n"
        CONFIG_PATH.write_text(text, encoding="utf-8")
        log.info(f"User config: appended new default settings to {CONFIG_PATH}: {missing}")
    if set(tmpl) - known:
        _SYNC_STATE_PATH.write_text(json.dumps(sorted(known | set(tmpl))), encoding="utf-8")


def _ensure_config_file_exists():
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        try:
            from .effects import ensure_plugin_dir
            ensure_plugin_dir(CONFIG_DIR / "plugins")
        except Exception:
            log.exception("Plugins: could not prepare the plugins folder (non-fatal)")
        if not CONFIG_PATH.exists():
            CONFIG_PATH.write_text(_STARTER_TEMPLATE, encoding="utf-8")
            log.info(f"User config: created a starter config file at {CONFIG_PATH}")
            import json
            _SYNC_STATE_PATH.write_text(json.dumps(sorted(_template_settings())), encoding="utf-8")
        else:
            _sync_missing_defaults()
    except Exception:
        log.exception(f"User config: could not create/update {CONFIG_PATH} (non-fatal - built-in defaults apply)")


def _private_group(gid):
    """True for a user-private group (umask 002 setups): this user's primary group with no other members."""
    import os
    try:
        import grp
        import pwd
        g = grp.getgrgid(gid)
        me = pwd.getpwuid(os.getuid())
        return gid == me.pw_gid and all(m == me.pw_name for m in g.gr_mem) and g.gr_name == me.pw_name
    except (ImportError, KeyError):
        return False


def untrusted_reason(path):
    """
    Why a file we are about to run as Python code (config.py, plugins) is not safe to run,
    or None if it is: it and its folder must belong to this user (or root) and must not be
    writable by group/others - otherwise another account could put code in it. patch 42.
    """
    import os
    import stat
    uid = os.getuid()
    for p in (Path(path), Path(path).parent):
        try:
            st = os.stat(p)
        except OSError as e:
            return f"cannot stat {p} ({e})"
        if st.st_uid not in (uid, 0):
            return f"{p} belongs to another user (uid {st.st_uid})"
        if st.st_mode & stat.S_IWOTH or (st.st_mode & stat.S_IWGRP and not _private_group(st.st_gid)):
            return f"{p} is writable by group/others (mode {stat.filemode(st.st_mode)}) - fix with: chmod go-w {p}"
    return None


def load_user_overrides(constants_namespace):
    """
    Returns {name: value} for every top-level name in the user's
    config.py that ALSO exists in constants_namespace - constants.py
    applies this directly onto its own globals(). Matching against
    constants_namespace (rather than accepting anything) means a typo or
    an unrelated helper name in the user's file can't accidentally
    inject something constants.py never defined; anything the user
    doesn't set is simply absent here, leaving that default untouched.
    """
    _ensure_config_file_exists()
    if not CONFIG_PATH.exists():
        return {}
    why = untrusted_reason(CONFIG_PATH)
    if why:
        log.warning(f"User config: NOT loading {CONFIG_PATH}: {why}. Built-in defaults apply.")
        return {}
    try:
        user_ns = {}
        exec(compile(CONFIG_PATH.read_text(encoding="utf-8"), str(CONFIG_PATH), "exec"), user_ns)
    except Exception:
        log.exception(f"User config: {CONFIG_PATH} failed to load (syntax error?) - ignoring it, built-in defaults apply")
        return {}
    overrides = {
        name: value for name, value in user_ns.items()
        if not name.startswith("_") and name in constants_namespace
    }
    if "LOG_LEVEL" in overrides:
        set_log_level(overrides["LOG_LEVEL"])
    if overrides:
        log.info(f"User config: applied overrides from {CONFIG_PATH}: {sorted(overrides.keys())}")
    return overrides


def set_user_config_value(name, value):
    """
    Sets `name = value` in the user's config file: replaces the first
    existing line for that name (commented template lines count), or
    appends one. Used by the "Do not show again" checkboxes.
    """
    import re
    _ensure_config_file_exists()
    try:
        text = CONFIG_PATH.read_text(encoding="utf-8")
    except Exception:
        text = ""
    line = f"{name} = {value!r}"
    pattern = re.compile(rf"^[ \t]*#?[ \t]*{re.escape(name)}[ \t]*=.*$", re.MULTILINE)
    if pattern.search(text):
        text = pattern.sub(lambda m: line, text, count=1)
    else:
        if text and not text.endswith("\n"):
            text += "\n"
        text += line + "\n"
    CONFIG_PATH.write_text(text, encoding="utf-8")
    log.info(f"User config: set {line}")
