# snippysnappy v1.0.0
"""Configuration knobs: sizes, colors, defaults, file locations, feature toggles.
"""
from pathlib import Path

from ._paths import INSTALL_DIR

SNIPPYSNAPPY_VERSION = "1.0.0"   # X.0.0 = "stable" release; 0.X.0 = feature addition/major tweak; 0.0.X = bug fix/small tweak. Reaching the digit "9" should increment to "10" e.g. x.x.9 -> x.x.10.

LOG_PATH = INSTALL_DIR / "snippysnappy.log"   # Comment out this line to
                                              # disable file logging entirely.
MAX_LOG_BYTES = 1_000_000   # Rotate once the log exceeds ~1MB.
LOG_BACKUP_COUNT = 3        # Keep this many rotated-out old logs (snippysnappy.log.1, .2, .3).
LOG_LEVEL = "WARNING"       # "DEBUG" | "INFO" | "WARNING" | "ERROR" - least severe level logged (console and file).
TOOLTIPS_ENABLED = True     # False = no hover descriptions on toolbar buttons (cropped-name tips still show).
TOOLTIP_DELAY_MS = 500     # hover time before a tip appears.
TOOLTIP_OVERRIDES = {}      # {"Copy": "text"} or {("film", "Copy"): "text"} - replaces a built-in tip; see tooltips.py.
LEADER_ENABLED = True       # False = no leader key.
LEADER_KEY = "backslash"    # Tk keysym that starts a leader sequence, e.g. "backslash", "semicolon", "Control-space".
LEADER_TIMEOUT_MS = 5000    # popup closes after this long without a key (0 = never).
LEADER_BINDINGS = {}        # {"image": {"esb": "mode:snip:box", "xx": None}, "film": {...}} - see leader.py.
FILM_SPACE_PLAYPAUSE = True # Space plays/pauses in film windows (not while typing in a text box / entry).
FILM_R_RESTART = True       # R restarts playback in film windows (not while typing in a text box / entry).
FILM_SPLICE_STEP = 0.1      # Splice visual mode: seconds per h/l (arrow) step.
FILM_SPLICE_BIG_STEP = 1.0  # Splice visual mode: seconds per H/L (Shift+arrow) step.
FILM_SPLICE_VIM = True      # Splice mode: y takes the marked region, p inserts the taken region, P inserts a copy of the marked one.
FILM_FRAME_CACHE_MB = 128   # Memory for decoded film frames kept for fast scrubbing (per film window).
CAPTURE_BACKEND = "auto"    # X11 screenshots: "auto"/"pillow" = in-process (Pillow XCB + libX11), "maim" = always use maim. auto falls back to maim.
CAPTURE_INCLUDE_CURSOR = True   # Paint the mouse cursor into in-process screenshots (maim does by default).
WAYLAND_CAPTURE_TOOL = "auto"   # Wayland screenshots: "auto" (grim, then portal, spectacle, gnome-screenshot) | "grim" | "portal" | "spectacle" | "gnome-screenshot"
WAYLAND_RECORD_TOOL = "auto"    # Wayland recording: "auto" (wf-recorder, then portal+PipeWire via gst-launch-1.0) | "wf-recorder" | "portal"
WAYLAND_FLOAT_WINDOWS = True    # sway/Hyprland: add a runtime rule so snippysnappy windows float instead of being tiled
PLUGINS_ENABLED = True      # False = never load plugin files (effects plugins).
PLUGIN_DIR = None           # None -> ~/.config/snippysnappy/plugins (created if missing); every *.py in it is loaded.

SCREENSHOT_DIR = Path.home() / "Pictures" / "Screenshots"
SLOP_DIR = Path.home() / "Pictures" / "Slop-o-graphs"   # Incidental screenshot path (Sloppy Save).
TEXT_DIR = Path("/tmp")   # Incidental OCR text lands here (Sloppy Save Text).
SNIPTEXT_DIR = INSTALL_DIR / "sniptext"   # Persistent OCR text path (Save Text).

HANDLE_SIZE = 8
OCR_BRUSH_WIDTH = 18
OCR_MASK_MIN_PADDING = 20   # px - minimum mask padding regardless of brush width.
DEFAULT_COPY_ON_SAVE = True   # Default state of the "Also copy" checkbox on the Save/OCR-save screens.
DEFAULT_ALSO_EXIT = True   # Default state of the "Also exit" checkbox
DEFAULT_ALSO_SAVE = False   # Default state of the "Also save" checkbox.
NOTIFICATION_BACKEND = "toast"   # "system" (notify-send),  or
                                 # "toast" (in-app toast is primary, falls
                                 # back to notify-send if it fails).
OCR_RETURN_TO_TOOLBAR_AFTER_ACTION = True   # Set to False to close the app immediately instead "Close" on this screen always closes immediately either way.
TOOLBAR_HEIGHT = 118   # reserves height for the 2-row
                       #toolbar screens.
SIDE_PANEL_WIDTH_RESERVE = 210   # reserved in the initial window width
                                 # from the start (_recalculate_display).
BUTTON_PADY = 6   # Internal vertical padding.
MIN_POINT_DIST = 3   # px - skip adding a new stroke point closer than
                     # this to the last one.
MIN_WINDOW_WIDTH = 720   # Decorated windows start at least this wide.
MAX_WINDOW_WIDTH = 1450    # decorated windows are auto-sized no larger
                           # than this.
MAX_WINDOW_HEIGHT = 800    # same, for total height.
SCROLLBAR_THICKNESS = 12   # px - must match the width= given to the
                           # vertical/horizontal Scrollbars.
VIEWPORT_BORDER_FUDGE = 6  # px - small safety margin added on top of
                           # SCROLLBAR_THICKNESS.
MIN_CANVAS_DISPLAY_SIZE = 250   # px - very small captures are scaled UP
                                # on screen to at least this large
                                # (unconditional, regardless of
                                # AUTO_SCALE_TO_FIT).
AUTO_SCALE_TO_FIT = False   # False (default): oversized images are shown
                            # at their real size with thin scrollbars to
                            # pan around, plus a manual "Rescale to fit"
                            # button.
                            # True: the canvas view is always
                            # automatically rescaled to fit the current
                            # window.
DEFAULT_MONITOR_NAME = None   # Wayland can't always auto-pick "the right"
                              # monitor to center a window on. Set this to
                              # a monitor name (via `wlr-randr` or
                              # `xrandr --listmonitors`) to force a
                              # specific default instead of first reported.
NIL_CANVAS_DEFAULT_WIDTH = 800   # --nil/-n with no WxH override this wide.
NIL_CANVAS_DEFAULT_HEIGHT = 600  # --nil/-n with no WxH override this tall.
NIL_CANVAS_DEFAULT_COLOR = "white"   # "white" | "black" |
                                     # "transparent" - --nil/-n with no
                                     # color override uses this.
FUZZ_DIR = None   # None -> defaults to ~/Pictures . Set an absolute
                  # path to override. This is the "root" --open/-o resolves relative PATH arguments against
                  # (not the shell's current working directory), and the default search scope for
                  # `--open prompt`'s fuzzy finder (shown there as
                  # "$FUZZ_DIR", matching this variable's name).
OPEN_PROMPT_MAX_SCAN_FILES = 20000   # Safety cap on how many files `--
                                     # open prompt`'s fuzzy finder will
                                     # walk when searching a large parent
                                     # dir (e.g. "/") - avoids the picker
                                     # hanging indefinitely.
IMAGE_FILE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tiff", ".tif"}
JUNK_PENALTY_WEIGHT = 2   # how hard `--open prompt`'s junk_score (below)
                          # pulls a likely-junk file (app icons,
                          # cache thumbnails, etc.) down in the ranking - never excludes them, just deprioritizes.
                          # Raise this to push junk further down; 0 disables the heuristic entirely.
CTRL_SCROLL_ZOOM_STEP = 0.1   # Ctrl+wheel zoom step (10% per notch).
PAN_WHEEL_SUPPRESS_SECONDS = 0.4   # Plain wheel scroll is ignored while a
                                   # middle-mouse pan drag is held, and
                                   # for this long after.

# Per-action toggles for the Snip tool's clipboard toast notifications (see
# _snip_copy/_snip_cut/_snip_paste/_snip_delete_to_void) - independent of
# which register (snippy or system) was used. All default on; flip
# any of these to False to silence just that one action's toast (e.g. set
# NOTIFY_ON_SNIP_PASTE = False if you don't want a toast every time you
# paste but still want one for Copy/Cut).
NOTIFY_ON_SNIP_COPY = True
NOTIFY_ON_SNIP_CUT = True
NOTIFY_ON_SNIP_PASTE = True
NOTIFY_ON_SNIP_DELETE = True   # void-delete (Super+x / Ctrl+Shift+X
                               # equivalent void path).

WINDOW_TITLE_BASE = "snippy snappy :)"   # decorated windows are titled
                                         #this, or "{BASE} - {submenu}".

EDIT_PEN_WIDTH = 4
SPRAY_DOTS_PER_BURST = 8   # how many spray dots land per burst.
SPRAY_DOT_RADIUS_RANGE = (0.5, 1.5)   # each individual dot's radius range.
SPRAY_DOT_WIDTH_DEFAULT = 2   # spray Can's starting Width (dot size).
SPRAY_MIN_BURST_DIST = 4   # px (capture-local) - minimum cursor movement
                           # between bursts during a drag.
EDIT_HIGHLIGHT_WIDTH = 22
EDIT_HIGHLIGHT_ALPHA = 90   # 0-255, translucency of highlighter mode
PALETTE = ["#FF0000", "#00A000", "#0000FF", "#000000", "#FFD400"]
FILL_PALETTE = PALETTE + ["#FFFFFF"]   # Shape tool's fill swatches.
DEFAULT_PROPORTIONAL_RESIZE = True   # Default state of the "Proportional"
                                     # checkbox (Snip/Text/Shape rows).
                                     # On: corner handles scale width and
                                     # height together (aspect ratio
                                     # locked). Off: corner handles adjust
                                     # width and height independently.
                                     # Edge handles (n/s/e/w) always
                                     # affect only their own axis.
RESIZE_DRAG_PREVIEW_ALPHA = 0.55   # 0.0-1.0 - opacity applied to a Snip/
                                   # Text/Shape element's own rendered
                                   # content while one of its RESIZE
                                   # handles.
SHAPE_HIT_TOLERANCE_PX = 6   # screen px of slack around a Shape's outline/line
                             # when deciding whether a click "grabbed" it (to
                             # move) or should start a new element.
SHAPE_HIT_UNFILLED_INTERIOR = True   # True (default): the whole interior
                                     # counts as "grab" even when a box/
                                     # ellipse has no fill.
                                     # False: an unfilled box/ellipse is
                                     # only grabbed by clicking on/near
                                     # its outline.
DEFAULT_SIDE_PANEL_MODE = "none"   # "none" | "layers" | "tabs" - which
                                   # side panel (if any) is showing when
                                   # the app first opens.
DEFAULT_CAPTURE_DELAY_SECONDS = 5.0   # -d/--delay no arg default time.
CONFIRM_LAYER_CHOICE = True   # Ask "merge into current layer or new layer?
EXIT_BEHAVIOR = "background"   # "background" | "full_close" - what
                               # closing the LAST open window (Cancel/
                               # Escape, Save/Copy's "Also Exit", OCR's
                               # Close, etc - see _close_window) does.
WARN_ON_ACTIVE_CLOSE = True
CONFIRM_DISCARD = True
PANEL_START_AT_TOP = True   # side panels (Tabs/Layers) start scrolled to
                            # the top when you switch to them. False =
                            # remember where each was scrolled.
FILM_REVIEW_MONITOR = "control"   # which monitor a new film review window
                                  # opens on: "control" = the monitor the
                                  # recording Stop control sits on,
                                  # "capture" = the monitor the recorded region was on,
                                  # "other" = any monitor that is NOT the capture's.
IMAGE_REVIEW_MONITOR = "control"  # same choices for a new screenshot review
                                  # window ("control" = the monitor the
                                  # recording control would use: the top-right
                                  # of the virtual screen). Set this and
                                  # FILM_REVIEW_MONITOR independently, e.g. film
                                  # "control" and image "other".
DEFAULT_NEW_LAYER_ON_EDIT = True   # the Layers panel's "New layer on edit"
                                   # checkbox's starting state. True: switching
                                   # to a tool that doesn't naturally belong in
                                   # whatever layer you last drew into starts a
                                   # new layer for it; False: always keeps
                                   # adding to whatever layer you last touched,
                                   # regardless of which tool, and never asks.
SILENCE_NEW_LAYER_POPUP = False   # True: never show the "new layer or current?"
                                  # popup at all.
NEW_LAYER_ON_EDIT_MODE = None   # None (default) = keep using
                                # DEFAULT_NEW_LAYER_ON_EDIT /
                                # SILENCE_NEW_LAYER_POPUP / CONFIRM_LAYER_CHOICE
                                # as before. "ask" = popup asks "new layer or
                                # current?". "current" = always add to the
                                # current layer. "new" = always start a new
                                # layer. Seeds the Layers panel checkbox; per-
                                # session changes never write back.
DEFAULT_OPEN_FILE_PICKER = "fuzzy"   # "fuzzy" | "gui" - which of the two in-app
                                     # file pickers opens by default, both for
                                     # `--open prompt` and the Tabs panel's "+
                                     # Open File..." button.

# Each of Snip/Draw/Shape/Text's own dropdown starts on whichever value is set
# here for that tool, independently of the other three - e.g. leave Snip/Shape/
# Text on their safe "select" default but set DEFAULT_PEN_MODE = "pen" if you
# draw with the pen far more than you select. Must be one of that tool's own
# valid values.
DEFAULT_SNIP_MODE = "select"    # "select" | "box" | "ellipse" | "freeform"
DEFAULT_PEN_MODE = "select"     # "select" | "pen" | "spray" | "highlight" | "eraser"
DEFAULT_SHAPE_MODE = "select"   # "select" | "line" | "box" | "ellipse"
DEFAULT_TEXT_MODE = "select"    # "select" | "write"
FILM_SNIP_MODE = "copy"   # Snippyfilm Snip tool: "copy" (default) = dragging a
                          # region copies it into a new movable object and never
                          # leaves a hole; "cut" = image-style snipping.
FILM_SNIP_HOLE_COLOR = "#000000"   # the color shown where a "cut" snip was
                                   # taken from (only used when FILM_SNIP_MODE =
                                   # "cut")
SNIP_SELECT_ALL_OBJECTS = True   # snip tool, Select mode: True (default) =
                                 # select can move/resize/rotate EVERY object ;
                                 # False = only snips.
ALIGN_GUIDES_ENABLED = True   # the pink alignment guide lines shown
                              # while moving / resizing an element (image
                              # edit).
ALIGN_GUIDE_TOLERANCE_PX = 6   # how close an edge must be to another
                               # element's edge for a guide to show.
FILM_RESIZE_BG_DEFAULT = "black"   # Snippyfilm Resize > Background.
LAYER_MODE_GROUPS = {
    # which auto-created default layer a brand-new element of each tool joins.
    "pen": "Draw", "highlight": "Draw", "spray": "Draw", "erase": "Draw",
    "shape": "Shapes", "snip": "Snips", "text": "Text",
}
TEXT_FONT_SIZE_DEFAULT = 24
TEXT_FONT_FAMILY_DEFAULT = "Serif"   # must be a key of
                                     # TEXT_FONT_FAMILIES below.
MEASURE_COLORS = ["#FFD400", "#00FFFF", "#FF00FF", "#FF8800", "#00FF00"]
MAGNIFIER_LENS_SIZE_DEFAULT = 100   # diameter in on-screen pixels.
MAGNIFIER_LEVEL_DEFAULT = 4         # magnification factor.
SERIF_FONT_PATHS = [
    # Tried in order; falls back to PIL's built-in bitmap font (always available, ignores size).
    "/usr/share/fonts/dejavu/DejaVuSerif.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
    "/usr/share/fonts/liberation-fonts/LiberationSerif-Regular.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
    "/usr/share/fonts/TTF/DejaVuSerif.ttf",
]
SANS_FONT_PATHS = [
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/liberation-fonts/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
]
MONO_FONT_PATHS = [
    "/usr/share/fonts/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/liberation-fonts/LiberationMono-Regular.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
]
COMIC_FONT_PATHS = [
    "/usr/share/fonts/truetype/msttcorefonts/Comic_Sans_MS.ttf",
    "/usr/share/fonts/truetype/mscorefonts/Comic_Sans_MS.ttf",
    "/usr/share/fonts/truetype/comic-neue/ComicNeue-Regular.ttf",
    "/usr/share/fonts/comic-neue/ComicNeue-Regular.ttf",
    "/usr/share/fonts/TTF/ComicNeue-Regular.ttf",
]
PAPYRUS_FONT_PATHS = [
    "/usr/share/fonts/truetype/papyrus/Papyrus.ttf",
    "/usr/share/fonts/papyrus/Papyrus.ttf",
]


def _user_font_paths(*stems):
    """Case-insensitive search of per-user font folders for font files whose name (minus extension) is one of `stems`."""
    found = []
    for base in (Path.home() / ".local" / "share" / "fonts", Path.home() / ".fonts"):
        if not base.is_dir():
            continue
        for p in base.rglob("*"):
            if p.suffix.lower() in (".ttf", ".otf") and p.stem.lower() in stems:
                found.append(str(p))
    return found


# Per-user copies (e.g. fonts copied from a Windows install into
# ~/.local/share/fonts) are tried before the system paths.
PAPYRUS_FONT_PATHS[:0] = _user_font_paths("papyrus")
COMIC_FONT_PATHS[:0] = _user_font_paths("comic")
# Font choices offered by the Text tool.
IMPACT_FONT_PATHS = [
    "/usr/share/fonts/truetype/wintc/impact.ttf",
    __import__("os").path.expanduser("~/.local/share/fonts/windows/impact.ttf"),
    "/usr/share/fonts/truetype/msttcorefonts/Impact.ttf",
    "/usr/share/fonts/truetype/mscorefonts/Impact.ttf",
    "/usr/share/fonts/TTF/impact.ttf",
]
TEXT_FONT_FAMILIES = {
    "Serif": {"tk": "serif", "paths": SERIF_FONT_PATHS},
    "Sans": {"tk": "sans-serif", "paths": SANS_FONT_PATHS},
    "Mono": {"tk": "monospace", "paths": MONO_FONT_PATHS},
    "Comic Sans": {"tk": "Comic Sans MS", "paths": COMIC_FONT_PATHS},
    "Papyrus": {"tk": "Papyrus", "paths": PAPYRUS_FONT_PATHS},
    "Impact": {"tk": "Impact", "paths": IMPACT_FONT_PATHS},
}

_FONT_CACHE = {}
_FONT_MISSING_WARNED = set()

# ---- user-config overrides ----
# Anything in ~/.config/snippysnappy/config.py with a name matching one
# of the constants above overrides it - see user_config.py.
from .user_config import load_user_overrides
TEXT_CURSOR_COLOR = "#ffd800"
TEXT_CURSOR_ALPHA = 235
TEXT_CURSOR_CONTRAST_TEXT = True

VIDEO_FILE_EXTENSIONS = (".mkv", ".mp4", ".webm", ".mov", ".avi")
VIDEO_FUZZ_DIR = None

# ---- save folders (see paths_setup.py; the old SCREENSHOT_DIR / SLOP_DIR / TEXT_DIR / SNIPTEXT_DIR names still work as overrides) ----
SNIPPYSNAPS_DIR = Path.home() / "Pictures" / "Snippysnaps"  # save.
SLOPPY_SNAPS_DIR = Path.home() / "Pictures" / "Sloppysnaps" # temp img.
SNIPPYTEXT_DIR = Path.home() / "Documents" / "Snippytext"   # save Text.
SLOPPY_TEXT_DIR = Path.home() / "Documents" / "Sloppytext"  # temp txt.
SNIPPYFILMS_DIR = Path.home() / "Videos" / "Snippyfilms"    # recordings.
SLOPPY_FILMS_DIR = Path.home() / "Videos" / "Sloppyfilms"   # temp rec.
SLOPPY_CLEANUP_DAYS = 2     # plain files in the Sloppy* folders older
                            # than this are deleted; None or 0 disables
                            # the sweep.
SLOPPY_CLEANUP_INTERVAL_MINUTES = 60     # how often the sweep re-runs while a window is open (also runs once at startup)

NEW_WINDOW_MONITOR = "inactive"   # "inactive": new decorated windows
                                  # open on a monitor other than the one
                                  # with the pointer.
                                  #"active": open where the pointer is.

BACKUP_ORIGINAL = False   # True: a raw copy of every new capture/
                          # recording is saved to the Sloppy folders as
                          # soon as its review window appears.
RECORD_AUDIO = "off"          # "off" | "desktop" | "mic" | "both" -
                              # what -r records; -ra / -r -a opens a
                              # prompt to change it for one recording.
AUDIO_DESKTOP_DEVICE = None   # None = sys default output's monitor.
AUDIO_MIC_DEVICE = None       # None = sys default input.

FILM_DEFAULT_VOLUME = 100   # starting playback volume (0-100).

TEMP_DIR = None          # None -> ~/.cache/snippysnappy/tmp
TEMP_KEEP_HOURS = 24     # a finished process's temp folder is kept this
                         # long before the next startup deletes it.

globals().update(load_user_overrides(globals()))

from .logging_setup import attach_file_logging
attach_file_logging(globals().get("LOG_PATH"), MAX_LOG_BYTES, LOG_BACKUP_COUNT)
