# snippysnappy v1.0.3
"""The main capture/annotation window - by far the largest module here (SelectorApp), covering selection, all edit tools (pen/highlight/spray/eraser/shape/text/snip), resize, OCR, magnify, measure, layers, tabs/windows panel, and the Pin toolbar entry point. Kept as one class/module rather than split further for this first compartmentalization pass - see HANDOVER.md."""
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
import uuid
from datetime import datetime
from pathlib import Path

from .constants import *
from ._paths import INSTALL_DIR
from .logging_setup import log, run_logged, popen_logged
from .icon import _load_app_icon
from . import paths_setup as _paths_setup
from .capture import _X11EscapePoller, cached_open
from .capture import (BACKEND, get_monitors, monitor_at_point, capture_full_screen, capture_monitor, select_window_id)
from .clipboard import (copy_image_to_clipboard, copy_text_to_clipboard, get_system_clipboard_image, get_system_clipboard_text)
from .ocr import ocr_image, preprocess_for_ocr, hex_to_rgb
from .notify_toast import notify, timestamp
from .text_render import draw_text_runs
from .prompt_windows import OpenPromptApp, GuiPromptApp
from .fuzzy_open import get_default_open_dir, make_nil_placeholder
from .coordinator import (
    _register_window, _unregister_window, _LOCAL_WINDOWS, _LOCAL_PINS, _coord,
    broadcast_window_roster, remote_windows, remote_pins,
    request_remote_focus, request_remote_pin_close, request_remote_merge,
    focus_local_window, _find_local_pin, request_global_restart, request_remote_window_close, remote_windows_all,
)
from .pin_viewer import run_pin_viewer
from .dialogs import confirm_discard
from .side_panel import make_scrollable, add_window_row, SCROLLBAR_WIDTH, RowReorder, ordered, move_in_order, inline_rename, window_name, small_button, attach_tooltip, window_tab_labels, install_button_tooltips, style_menu_like_small_button, build_windows_rows
from .coordinator import request_remote_window_rename, broadcast_window_roster, remote_window_tab_labels, remote_window_hidden, request_remote_window_hide
from .constants import PANEL_START_AT_TOP, IMAGE_REVIEW_MONITOR

class SelectorApp:
    def __init__(self, *args, **kwargs):
        _p20_init(self, *args, **kwargs)
        try:
            if self.mode == "monitor" and self.root.overrideredirect():
                self.root.after(30, lambda: self._p22_acquire_input(0, "startup"))
                state = {"last": 0.0, "n": 0}

                def on_focus_out(_e):
                    import time as _t
                    now = _t.monotonic()
                    if state["n"] >= 10 or now - state["last"] < 0.2:
                        return
                    state["last"] = now
                    state["n"] += 1
                    self.root.after(30, lambda: self._p22_acquire_input(0, "focus lost"))
                self.root.bind("<FocusOut>", on_focus_out, add="+")
        except Exception:
            log.exception("Could not set up select-overlay input acquisition (non-fatal)")

    def _init_base(self, root, image_path, mode="monitor", bounds=None, auto_select_full=False,
                 monitors=None, capture_tempdir=None, current_monitor_name=None, capture_delay=None):
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
        `capture_delay`: seconds from --delay, mode="monitor" only (--window/
        --fullscreen handle their own delay entirely in main(), before this
        constructor ever runs, since they have no interactive step of their
        own that lives inside the Tk event loop the way drag-to-select does).
        See _begin_delayed_recapture for what actually happens with it.
        """
        log.info(f"SelectorApp init: mode={mode} bounds={bounds} auto_select_full={auto_select_full} capture_delay={capture_delay}")
        self.root = root
        self._true_root = root   # The ORIGINAL Tk() instance - mainloop() is tied to this one specifically.
                                  # transition_to_review swaps self.root to a new Toplevel for the review
                                  # window while keeping this alive (withdrawn) so mainloop() keeps running;
                                  # destroying self.root anywhere in the app cascades to destroying this too.
        self.image_path = image_path
        self._pending_capture_delay = capture_delay if mode == "monitor" else None   # see _begin_delayed_recapture
        self.owned_temp_paths = [image_path]  # Cleaned up when the window is destroyed - see _cleanup_temp_files
        self._ocr_overlay = None
        self._ocr_text_widget = None
        self._ocr_copy_var = None
        self.mode = mode
        self.available_monitors = monitors or []
        self.current_monitor_name = current_monitor_name
        self.capture_tempdir = capture_tempdir

        iw, ih = cached_open(image_path).size
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

        self.canvas_x1 = 0
        self.canvas_y1 = 0
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
        self.canvas_x2 = None   # set on first activation to match the capture
        self.canvas_y2 = None
        self.canvas_bg_color = "white"   # "white" | "black" | "transparent"
        self._canvas_active = False
        self._no_real_capture = False   # True for --nil sessions - see _build_canvas_composite
        self._force_show_canvas_composite = False   # True for --nil sessions - see _should_show_canvas_composite
        self._capture_rgba_cache = None   # (key, RGBA image, has_alpha) - see _capture_rgba

        # ---- layers ----
        # Every element (any edit_strokes entry) carries a "layer_id"
        # pointing into self.layers - an ordered list (index 0 = bottom of
        # the stack, last = top), persisted for the life of the document
        # (survives Done, leaving/re-entering Edit - only a real Save/Copy,
        # or reopening an already-saved file fresh via -o, ever flattens
        # it away). Background and Capture are always present, at the
        # bottom by default; stroke layers (Draw/Shapes/Snips/Text) are
        # created lazily on first use of their tool - see
        # _get_or_create_default_layer.
        #
        # self.isolated_layer_id, when set, both narrows what's currently
        # SHOWN (everything else transparent - see _render_layered's
        # layer_filter) and, for a stroke layer specifically, retargets
        # where brand-new elements of ANY tool land (see
        # _stroke_target_layer_id) - deliberate mixing, per design.
        # Isolating Background/Capture is purely a viewing aid; new
        # elements still go to their normal per-tool default layer.
        # Persists across Edit sessions same as the layers themselves -
        # only reset (to None, i.e. "Full") if the isolated layer itself
        # is later merged/split away out from under it.
        self._layer_id_counter = 0
        self.layers = [self._new_layer("Background", "background"), self._new_layer("Capture", "capture")]
        self.isolated_layer_id = None
        self.ghost_layer_opacity = 0.15   # how visible OTHER layers stay while one is isolated - see the Layers panel's opacity slider
        self._last_touched_layer_id = None
        self.new_layer_on_edit_default = DEFAULT_NEW_LAYER_ON_EDIT
        self._layer_popup_silenced = SILENCE_NEW_LAYER_POPUP
        from . import constants as _layer_consts
        self._layer_confirm_enabled = bool(getattr(_layer_consts, 'CONFIRM_LAYER_CHOICE', True))
        _lm = getattr(_layer_consts, 'NEW_LAYER_ON_EDIT_MODE', None)
        if _lm is not None:
            _lm = str(_lm).strip().lower()
            if _lm == 'ask':
                self.new_layer_on_edit_default = True
                self._layer_popup_silenced = False
                self._layer_confirm_enabled = True
            elif _lm == 'current':
                self.new_layer_on_edit_default = False
            elif _lm == 'new':
                self.new_layer_on_edit_default = True
                self._layer_popup_silenced = True
            else:
                log.warning(f'NEW_LAYER_ON_EDIT_MODE {_lm!r} is not ask/current/new - ignored')
        self._open_file_picker_mode = DEFAULT_OPEN_FILE_PICKER
        self.side_panel_mode = {"none": None, "layers": "layers", "tabs": "tabs"}.get(DEFAULT_SIDE_PANEL_MODE, None)   # see DEFAULT_SIDE_PANEL_MODE above; _on_side_panel_choice/_refresh_side_panel from here on
        self._layer_drag_index = None   # index into self.layers currently being dragged in the panel, or None
        self._layer_drag_start_y = None
        self._layer_drag_active = False
        self._split_mode_active = False
        self._split_start = None
        self._split_preview_item_id = None

        # ---- documents (tabs) ----
        self.window_id = str(uuid.uuid4())   # stable id for this window across processes - see the coordinator and _LOCAL_WINDOWS
        self._custom_document_label = None   # a user-set rename for the CURRENT tab, overriding the auto-derived one - see _document_label
        self.documents = []   # lazily seeded - see _ensure_documents_initialized
        self.current_document_index = 0
        self._tab_drag_index = None
        self._tab_drag_start_y = None
        self._tab_drag_active = False

        self._panel_drag_highlighted_cell = None
        self._panel_drag_preview = None
        self._panel_drag_indicator = None

        # ---- resize submenu state ----
        self.resize_mode_active = False
        self.resize_target_layer = None   # "capture" | "background" - defaults to "capture" on first open
        self._resize_active_handle = None
        self._resize_drag_snapshotted = False
        self._resize_drag_anchor_screen = None   # see _anchored_capture_local
        self._resize_drag_anchor_capture_local = None
        self._resize_preview_item_ids = []
        self._resize_preview_start_capture_dims = None
        self._resize_pending_capture_bounds = None   # (x1,y1,x2,y2) capture-local, live during a Full Capture handle drag
        self._bg_drag_preview_base = None   # (baked_image, canvas_x1, canvas_y1) at the start of a Background handle drag - see _live_update_background_handle
        self._resize_drag_orig_bounds = None   # (x1,y1,x2,y2) at the start of a background-handle drag - see _live_update_background_handle's render-offset comment
        self._bg_handle_drag_offset = (0.0, 0.0)   # temporary screen-space render offset during a background-handle drag - see _live_update_background_handle
        self._resize_undo_stack = []   # walk-back-one-step-at-a-time, see _push_resize_undo_snapshot
        self._resize_redo_stack = []   # reverses Undo, see _redo_layer_resize
        self._resize_session_snapshot = None   # reverted-to-in-full by Cancel - see _snapshot_resize_session_start

        # ---- dimension/alignment drag overlays (Office-style "smart
        # guides" plus a live width/height readout) - see
        # _show_dim_overlay/_clear_dim_overlay and
        # _show_alignment_guides/_clear_alignment_guides. Both are purely
        # visual (non-interactable canvas items), shown while actively
        # drawing/resizing/moving a shape, text box, snip, or the crop/
        # background handles, and cleared the moment the drag ends.
        self._dim_overlay_bg_id = None
        self._dim_overlay_text_id = None
        self._align_guide_ids = []

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
            root.configure(cursor="crosshair", bg="#1a1a1a")
            self._build_decorated_window_chrome(root)
            log.debug(f"Decorated window created (image {iw}x{ih}) - final size/scale set by _recalculate_display()")

        self.base_img_w, self.base_img_h = iw, ih
        self._raw_img_w, self._raw_img_h = iw, ih   # dims of self.image_path itself - see _sync_base_dims_for_mode
        self.display_scale = 1.0
        self.sel_coords = None
        self.baked_image = None
        self._recalculate_display()

        self.rect = None
        self.start_x = self.start_y = 0
        self.sel_coords = None
        self.handles = []
        self.reset_btn_items = []
        self._selection_overlay_item_ids = []   # stippled "not real content" dimming - see _update_selection_translucent_overlay
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
        self.draw_tool = "snip"   # default tool the first time Edit is entered - after that, whatever was last selected persists for the life of this window
        self.pen_width = EDIT_PEN_WIDTH
        self.pen_variant = DEFAULT_PEN_MODE   # "select" | "pen" | "spray" | "highlight" | "eraser" - see DEFAULT_PEN_MODE and the "select mode" comment block near pick_tool.
        self.spray_width = int(globals().get("SPRAY_DOT_WIDTH_DEFAULT", 2))
        self.highlight_width = EDIT_HIGHLIGHT_WIDTH
        self.ocr_brush_width = OCR_BRUSH_WIDTH
        self.text_font_size = TEXT_FONT_SIZE_DEFAULT
        self.text_font_family = TEXT_FONT_FAMILY_DEFAULT   # default font for the whole text box; a highlighted region can override it - see _apply_text_span_format
        self.text_bg_fill = "transparent"
        self.text_align = "left"   # "left" | "center" | "right" - see draw_text_runs
        self.text_mode = DEFAULT_TEXT_MODE   # "select" | "write" - Text's own Select/Write mode - see DEFAULT_TEXT_MODE and the "select mode" comment block
        self.text_box_start = None
        self.text_current_box_id = None
        self._pending_text_widget = None
        self._pending_text_window_id = None
        self._pending_text_box = None
        self._pending_text_edit_index = None   # None = a brand-new text box; an index into edit_strokes = re-editing that existing element in place (see _begin_text_edit)
        self._pending_text_tk_size = 10   # point size the live typing widget (and its per-span font tags) use - see _begin_text_entry
        self._edit_toolbar_frame = None   # the Edit toolbar's current top-level Frame (set in show_edit_toolbar)
        self.text_vim_mode = "insert"   # "insert" | "normal" | "visual" | "visual_line" - see _vim_on_key
        self._vim_visual_anchor = None
        self.shape_type = DEFAULT_SHAPE_MODE   # "select" | "line" | "box" | "ellipse" - see DEFAULT_SHAPE_MODE and the "select mode" comment block
        self.shape_fill = None   # None (no fill) or a "#rrggbb" color - independent of the line color (draw_color)
        self.shape_pending_start = None
        self.shape_preview_item_id = None
        self._pre_edit_state = None   # full snapshot taken on entering Edit, restored by cancel_edit - see enter_edit_mode / _capture_full_state

        # ---- snip tool state ----
        # A "snip" is stored as another entry in self.edit_strokes (mode
        # "snip"), which is what makes it participate in the existing
        # Undo/Redo stack for free. Its own fields (see _finalize_snip):
        #   shape, orig_bounds, orig_freeform_points - the ORIGINAL cutout
        #     shape/location in capture-local coords, fixed forever, used
        #     only to punch the "hole" left behind at the source location.
        #   extracted_image - the masked RGBA pixel content, fixed at
        #     capture time (never re-cropped from the source afterward).
        #   origin, w, h, rotation - the layer's CURRENT position/size/
        #     orientation, which is what moving/resizing/rotating changes.
        #     w/h are independent (no shared "scale") so a corner or edge
        #     handle can stretch width and height separately - see
        #     _apply_snip_corner_resize.
        self.snip_shape_type = DEFAULT_SNIP_MODE   # "select" | "box" | "ellipse" | "freeform" - see DEFAULT_SNIP_MODE and the "select mode" comment block
        self.snip_pending_start = None
        self.snip_pending_points = None   # accumulated capture-local points while drawing a freeform lasso
        self.snip_preview_item_ids = []
        self._snip_handle_ids = []
        # Crop handles: whole-canvas corner handles, styled like the
        # pre-capture selection handles (draw_handles), shown alongside
        # whichever tool's own element handles whenever that tool's own
        # mode is "Select" - see _crop_handles_enabled/_draw_crop_handles.
        # Distinct from an element's own resize handles (which resize just
        # that one Snip/Text/Shape) and from the separate Resize screen
        # (which flattens/resamples "Full Capture", or offers the same
        # background crop via a menu+numeric entry instead of direct
        # handles).
        self._crop_handle_ids = []
        self._crop_drag_active_handle = None
        self._crop_drag_anchor_screen = None   # see _anchored_capture_local
        self._crop_drag_anchor_capture_local = None
        self._crop_drag_orig_bounds = None   # see _crop_drag's proportional-lock block
        # Draw's own Select mode: a rubber-band rectangle over existing
        # pen/highlight/spray strokes groups them into one movable/
        # resizable/rotatable element.
        self._draw_select_rect_start = None
        self._draw_select_rect_item_id = None
        self._snip_drag_index = None   # index of the ONE transformable element currently mid-drag, or None
        self._snip_drag_mode = None   # None | "move" | "nw"/"ne"/"sw"/"se"/"n"/"s"/"e"/"w" | "rotate"
        self._snip_drag_start = None
        self._snip_drag_orig_origin = None
        self._snip_drag_orig_w = None
        self._snip_drag_orig_h = None
        self._snip_drag_orig_rotation = None
        self._snip_focused_index = None   # last snip clicked/dragged - what the clipboard keybinds act on (snip-only for now)
        self._snip_paste_position = None   # capture-local point from a click-without-drag - see _consume_snip_paste_position
        self._snip_drag_base_image = None   # cached composite (everything except the dragged element) - see _recompute_baked_image_live_drag
        self.proportional_resize_var = tk.IntVar(value=int(DEFAULT_PROPORTIONAL_RESIZE))   # "Proportional" checkbox (Snip/Text/Shape rows) - see _apply_snip_corner_resize
        self.pin_proportional_resize_var = tk.IntVar(value=int(DEFAULT_PROPORTIONAL_RESIZE))   # "Proportional" checkbox in the Pin toolbar - passed to each new pin via --pin-proportional (see _launch_pinned_region/run_pin_viewer); off lets that pin's OWN corner handles stretch width/height independently instead of locking aspect ratio

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

        # ---- Pin (pin a region to the desktop as its own floating,
        # always-on-top window - see enter_pin_mode/run_pin_viewer). Drag a
        # rectangle over the current image the same way Measure/Snip do;
        # release spawns a fully separate, detached snippysnappy process
        # (--pin-view) showing just that region, so it keeps floating even
        # after this window/process closes.
        self.pin_mode_active = False
        self.pin_pending_start = None
        self.pin_preview_item_id = None

        self.edit_mode_active = False
        self.ocr_paint_mode_active = False

        self.copy_on_save_var = tk.IntVar(value=int(DEFAULT_COPY_ON_SAVE))
        from . import constants as _c_also
        self.also_save_var = tk.IntVar(value=int(getattr(_c_also, "DEFAULT_ALSO_SAVE", False)))
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

        # Show whichever side panel DEFAULT_SIDE_PANEL_MODE configured for
        # a fresh start, now that every bit of state it could touch
        # (self.layers, sel_coords, etc.) actually exists - a no-op in
        # borderless select-mode (no toolbar_container yet) or when the
        # default is "none".
        if self.side_panel_mode is not None:
            self._refresh_side_panel()

        _register_window(self)

    def _build_decorated_window_chrome(self, root, *args, **kwargs):
        result = self._build_decorated_window_chrome_base(root, *args, **kwargs)
        try:
            root.protocol("WM_DELETE_WINDOW", self._close_window)
            root.bind("<Map>", self._on_mapped, add="+")
        except tk.TclError:
            pass
        return result

    def _build_decorated_window_chrome_base(self, root):
        """
        Builds the decorated-window UI shell shared by __init__ (mode
        "window"/"fullscreen") and transition_to_review (the "monitor" ->
        "window" handoff): a fixed-height toolbar strip at the bottom
        (self.toolbar_container, spanning the FULL window width - the
        Layers/Tabs toggle buttons live in there, in the persistent status
        row - see _build_status_row), and above it, self.main_area holding
        self.canvas_frame (the image viewport plus its own scrollbars,
        exactly as before this existed) side by side with
        self.side_panel_container (the vertical Layers/Tabs panel - empty
        and not packed until toggled, see _refresh_side_panel), separated
        by a thin hard line (self.side_panel_separator).
        """
        self.toolbar_container = tk.Frame(root, bg="#222222", height=TOOLBAR_HEIGHT)
        self.toolbar_container.pack(side="bottom", fill="x")
        self.toolbar_container.pack_propagate(False)

        self.main_area = tk.Frame(root, bg="#1a1a1a")
        self.main_area.pack(side="top", fill="both", expand=True)

        self.canvas_frame = tk.Frame(self.main_area, bg="#1a1a1a")
        self.canvas_frame.pack(side="left", fill="both", expand=True)
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

        self.side_panel_separator = tk.Frame(self.main_area, width=3, bg="#2a2a2a", relief="ridge", bd=2)
        self.side_panel_container = tk.Frame(self.main_area, bg="#222222")
        self._side_panel_width_applied = 0
        # Side panel (Layers, and eventually Tabs): a fixed hard-line
        # separator plus the panel content frame.

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

    def _capture_layer_size(self):
        x1, y1, x2, y2 = self.sel_coords
        return int(round(x2 - x1)), int(round(y2 - y1))

    # ---- canvas (background layer) plumbing ----

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
        if None in (self.canvas_x1, self.canvas_x2, self.canvas_y1, self.canvas_y2):
            return (max(1, int(getattr(self, "base_img_w", 1) or 1)), max(1, int(getattr(self, "base_img_h", 1) or 1)))
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

    def _capture_rgba(self):
        """
        The current crop of the raw capture as an RGBA image (alpha
        preserved - a Full Capture resize on a transparent canvas saves a
        genuinely transparent PNG, and going through RGB here used to
        silently turn every transparent pixel black), plus whether it has
        any non-opaque pixels at all. Cached, keyed on the file's path/size/
        mtime and the crop box - _build_canvas_composite runs on every
        re-bake and on every frame of a live Background-resize drag, and
        re-decoding a full-size screenshot from disk each time was a large
        share of the input lag there. Callers must treat the returned image
        as read-only (it's shared).
        """
        from PIL import Image
        x1, y1, x2, y2 = (int(v) for v in self.sel_coords)
        try:
            st = os.stat(self.image_path)
            file_sig = (st.st_size, st.st_mtime_ns)
        except OSError:
            file_sig = None
        key = (str(self.image_path), file_sig, x1, y1, x2, y2)
        cached = self._capture_rgba_cache
        if cached is not None and cached[0] == key:
            return cached[1], cached[2]
        full = cached_open(self.image_path)
        img = full.crop((x1, y1, x2, y2)).convert("RGBA")
        _ext = img.getchannel("A").getextrema()   # None for a zero-area crop (click without drag)
        has_alpha = bool(_ext) and _ext[0] < 255
        self._capture_rgba_cache = (key, img, has_alpha)
        return img, has_alpha

    def _build_canvas_composite(self):
        """
        Fresh RGBA render of JUST the Background and Capture layers
        (deliberately skipping every stroke layer, even a non-empty one -
        see the call sites. Respects the two layers' relative order in
        self.layers, same as the full render pipeline (_render_layered),
        in case they've been deliberately reordered via the Layers panel.

        self._no_real_capture (set for --nil sessions, which have no actual
        screenshot underneath - see main()) skips the paste entirely: the
        canvas background alone is the whole picture. Without this, a
        --nil placeholder would always fully cover its own canvas (there's
        nothing "outside" it to show transparency/whitespace through).
        """
        from PIL import Image
        cw, ch = self._canvas_dims()
        canvas_img = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        empty = True
        for layer in self.layers:
            if layer["kind"] == "background":
                fill = Image.new("RGBA", (cw, ch), self._canvas_bg_fill_rgba())
                # patch 43: compositing onto a fully transparent image gives exactly the fill
                canvas_img = fill if empty else Image.alpha_composite(canvas_img, fill)
                empty = False
            elif layer["kind"] == "capture" and not self._no_real_capture:
                capture, _ = self._capture_rgba()
                paste_x = int(round(-self.canvas_x1))
                paste_y = int(round(-self.canvas_y1))
                self._composite_capture(canvas_img, capture, paste_x, paste_y)
                empty = False
        return canvas_img

    @staticmethod
    def _composite_capture(base, capture, paste_x, paste_y):
        """In place, only over the overlap: same pixels as pasting capture (with its own mask)
        onto a transparent full-size layer at (paste_x, paste_y) and alpha-compositing that. patch 43."""
        from PIL import Image
        cw, ch = base.size
        x0, y0 = max(0, paste_x), max(0, paste_y)
        x1, y1 = min(cw, paste_x + capture.width), min(ch, paste_y + capture.height)
        if x1 <= x0 or y1 <= y0:
            return
        cap_layer = Image.new("RGBA", capture.size, (0, 0, 0, 0))
        cap_layer.paste(capture, (0, 0), capture)
        base.alpha_composite(cap_layer, dest=(x0, y0), source=(x0 - paste_x, y0 - paste_y, x1 - paste_x, y1 - paste_y))

    def _should_show_canvas_composite(self):
        """
        The canvas (background) layer's composite is only what's actually
        displayed/interacted-with while inside Edit, Resize (a sub-screen of
        Edit), OCR paint, or Magnify - all of which need to draw/sample in
        the canvas's own coordinate frame. The plain main toolbar always
        shows the original raw capture with the normal crop-selection
        handles, exactly as before this feature existed - selection
        adjustment is independent of whatever the canvas layer has been
        resized to.

        self._force_show_canvas_composite is the one exception: a --nil
        session has no raw capture worth showing at all, so main() sets it
        immediately after construction and the composite (i.e. the blank
        canvas itself) is what the main toolbar shows from the very start.

        An isolated layer (self.isolated_layer_id) is the other exception:
        Layers is meant to stay usable right up until you actually
        save/copy, including from the plain main toolbar, so isolating a
        layer forces the canvas composite view on regardless of mode.

        self._no_real_capture is checked directly here (not just via
        _force_show_canvas_composite).
        """
        return (
            self._force_show_canvas_composite or self._no_real_capture
            or self.isolated_layer_id is not None or (self._canvas_active and (
                self.edit_mode_active or self.resize_mode_active
                or self.ocr_paint_mode_active or self.magnify_mode_active
            ))
        )

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

    # ---- shared window-level bindings ----

    def _bind_root_shortcuts(self, target):
        target.bind("<Escape>", self._handle_escape)
        target.bind("<Destroy>", self._cleanup_temp_files)
        target.bind("<Control-c>", lambda e: self._keybind_copy())
        target.bind("<Control-s>", lambda e: self._keybind_slop_save())
        target.bind("<Control-S>", lambda e: self._keybind_normal_save())
        target.bind("<Control-e>", lambda e: self._keybind_edit())
        target.bind("<Control-t>", lambda e: self._keybind_extract_text())
        target.bind("<Control-u>", lambda e: self._keybind_undo())
        target.bind("<Configure>", self._enforce_expected_geometry, add="+")
        try:
            from .leader import install_leader
            install_leader(self, target, "image")
        except Exception:
            log.exception("Could not install the leader key (non-fatal)")

    def _last_intended_position(self):
        if not getattr(self, "_geometry_enforcement_active", False):
            pos = None
            try:
                pos = self._p14_actual_pos()
            except Exception:
                pass
            pos = pos or getattr(self, "_p14_user_pos", None)
            if pos:
                return pos
        # Returns the x,y WE last explicitly requested via _set_geometry,
        # rather than re-querying winfo_x()/winfo_y().
        #
        # Falls back to winfo_x()/winfo_y() only if geometry has never been
        # set yet (shouldn't normally happen after __init__).
        if self._expected_geometry:
            try:
                _, xy = self._expected_geometry.split("+", 1)
                x_str, y_str = xy.split("+")
                return int(x_str), int(y_str)
            except Exception:
                pass
        return self.root.winfo_x(), self.root.winfo_y()

    def _set_geometry(self, geo_string):
        try:
            m = _p33_re.match(r"^(\d+)x(\d+)\+(-?\d+)\+(-?\d+)$", str(geo_string))
            if m and self.toolbar_container is not None and self.root.winfo_ismapped():
                w, h, x, y = (int(v) for v in m.groups())
                if w > self.root.winfo_width() or h > self.root.winfo_height():
                    mons = list(self.available_monitors or []) or list(get_monitors())
                    nx, ny = _p33_clamp(mons, x, y, w, h)
                    if (nx, ny) != (x, y):
                        log.info(f"Window grew to {w}x{h}: moved from +{x}+{y} to +{nx}+{ny} to stay on its monitor")
                        geo_string = f"{w}x{h}+{nx}+{ny}"
        except Exception:
            log.exception("Keep-on-screen check failed (non-fatal)")
        try:
            import traceback as _tb
            callers = " < ".join(f.name for f in reversed(_tb.extract_stack(limit=6)[:-1]))
            actual = self.root.geometry()
            log.info(f"_set_geometry({geo_string}) actual-before={actual} enforcing={self._geometry_enforcement_active} via {callers}")
        except Exception:
            pass
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
        # Whenever the window is idle (no geometry request of ours in flight) remember where it really is,
        # so a drag to another monitor is what later resizes are anchored to.
        try:
            if not self._geometry_enforcement_active and (event is None or event.widget is self.root):
                pos = self._p14_actual_pos()
                if pos:
                    self._p14_user_pos = pos
        except Exception:
            pass
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
        self._last_submenu = submenu
        try:
            tab = self._document_label()
        except Exception:
            tab = ""
        title = f"{WINDOW_TITLE_BASE} - {tab}" if tab else WINDOW_TITLE_BASE
        if submenu:
            title += f", {submenu}"
        try:
            self.root.title(title)
        except tk.TclError:
            pass

    def _refresh_base_display(self, *args, **kwargs):
        result = self._refresh_base_display_base(*args, **kwargs)
        if self._mp_pin is not None:
            try:
                self._mp_draw()
            except tk.TclError:
                pass
        r = result
        try:
            last = getattr(self, "_p17_last", None)
            if last and abs(self.display_scale - getattr(self, "_p17_scale", 0)) > 1e-6:
                if self.resize_mode_active:
                    self._p16_draw_preview(*last)
                else:
                    self._p16_clear_preview()
        except Exception:
            pass
        return r

    def _refresh_base_display_base(self, fast=False, composite_only=False):
        """
        fast=True uses a cheaper downscale filter (live drags); composite_only=True
        ignores self.baked_image and shows the plain canvas+capture composite
        (live Background-handle drags build their own preview - see
        _live_update_background_handle).

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
            base = self._current_display_image(composite_only=composite_only)
        else:
            try:
                base = cached_open(self.image_path).convert("RGB")
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
                (Image.BILINEAR if fast else Image.LANCZOS) if scale < 1 else Image.NEAREST,
            )
        else:
            disp = base
        self._show_base_photo(disp, 0, 0)

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

        win_w = max(MIN_WINDOW_WIDTH, min(disp_w + scrollbar_pad, max_w)) + SIDE_PANEL_WIDTH_RESERVE
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
        self._p35_refit = True
        try:
            # One-shot: fits the image to the CURRENT window/canvas size (does not
            # change window size). Only relevant when AUTO_SCALE_TO_FIT is False -
            # the Rescale button isn't shown otherwise.
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
            return
        finally:
            self._p35_refit = False

    def _on_window_configure(self, event=None):
        if event is not None and event.widget is not self.root:
            return
        if self._configure_after_id:
            self.root.after_cancel(self._configure_after_id)
        self._configure_after_id = self.root.after(150, self._auto_fit_to_current_window)

    def _auto_fit_to_current_window(self):
        self._p35_refit = True
        try:
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
            return
        finally:
            self._p35_refit = False

    def _redraw_after_scale_change(self, old_scale):
        refit = getattr(self, "_p35_refit", False)
        # Refreshes everything drawn on the canvas after display_scale
        # changes outside the normal _recalculate_display flow (manual
        # Rescale button, Ctrl+wheel zoom, or live auto-fit on window resize).
        #
        # In Edit, existing pen/highlight/spray strokes are simply re-baked
        # (which also clears their live-preview canvas items.
        if self.edit_mode_active and self.edit_strokes:
            self._recompute_baked_image()
        else:
            self._refresh_base_display()
        if not (self.edit_mode_active or self.resize_mode_active or self.ocr_paint_mode_active or self.magnify_mode_active):
            self.draw_handles()
        if self.resize_mode_active:
            self._draw_resize_handles()
        if self.edit_mode_active:
            self._draw_snip_ui()
        if self.ocr_paint_mode_active:
            self._redraw_ocr_strokes_preview()
        if self.measure_mode_active and old_scale:
            self._rescale_measure_boxes(old_scale, self.display_scale)
        self._update_scale_label()
        r = None
        if refit:
            self._p35_center_view()
        return r

    def _redraw_edit_strokes_preview(self):
        for stroke in self.edit_strokes:
            for seg_id in stroke.get("segment_ids", []):
                self.canvas.delete(seg_id)
            if stroke.get("mode") in ("text", "shape", "snip", "spray"):
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
        if self.pin_mode_active:
            self.exit_pin_mode()
            return
        self.close_current_tab_or_window()

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
        pad = 10
        return (bbox[0] - pad) <= x <= (bbox[2] + pad) and (bbox[1] - pad) <= y <= (bbox[3] + pad)

    def on_press(self, event):
        self._sel_before_press = (self.sel_coords, self.mode)
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
            w = abs(cx - self.start_x) / self.display_scale
            h = abs(cy - self.start_y) / self.display_scale
            self.update_dimension_display(override=(w, h))
            self._show_dim_overlay(cx, cy, f"{round(w)} x {round(h)}")
        self._log_if_slow("on_drag", _t0)

    def on_release(self, event):
        before = getattr(self, "_sel_before_press", None)
        r = self.on_release_base(event)
        try:
            if before and before[0] is not None and before[1] == "window" and self.mode == "window" and tuple(before[0]) != tuple(self.sel_coords):
                self._sel_stack().append(tuple(before[0]))
                del self._sel_stack()[:-50]
                log.info(f"Selection undo: recorded {tuple(before[0])}")
        except Exception:
            log.exception("selection undo bookkeeping failed (non-fatal)")
        self._sel_before_press = None
        return r

    def on_release_base(self, event):
        _t0 = time.monotonic()
        self._clear_dim_overlay()
        if self.rect:
            coords = self.canvas.coords(self.rect)
            if len(coords) != 4:
                log.warning("on_release: self.rect no longer has 4 coords (stale/deleted item) - ignoring this release")
                self.active_handle = None
                self._log_if_slow("on_release", _t0, threshold=0.05)
                return
            x1, y1, x2, y2 = coords
            ix1, iy1 = self.canvas_to_img(min(x1, x2), min(y1, y2))
            ix2, iy2 = self.canvas_to_img(max(x1, x2), max(y1, y2))
            self.sel_coords = (ix1, iy1, ix2, iy2)
            log.info(f"Selection made: {self.sel_coords}")
            if self.mode == "monitor":
                if self._pending_capture_delay:
                    self._begin_delayed_recapture()
                else:
                    self.transition_to_review()
            else:
                self.draw_handles()
                self.show_main_toolbar()
        self.active_handle = None
        self._log_if_slow("on_release", _t0, threshold=0.05)

    def _begin_delayed_recapture(self):
        """
        --delay's select-mode hook: the drag-to-select step just finished
        completely normally (same as without --delay - handles, dragging,
        everything unchanged up to this point). NOW, instead of
        transitioning straight to the review window off the backdrop that
        was already captured before selection started, the overlay is
        withdrawn (revealing the live, un-frozen desktop) and the actual
        capture is deferred until the delay elapses - see
        _finish_delayed_recapture, which is what actually re-captures and
        proceeds to the review window from there.

        Cancelling mid-countdown: on X11, this reuses the SAME mechanism
        `select_window_id()` already relies on for exactly this kind of
        problem (see _X11EscapePoller) - XQueryKeymap polls the X
        server's actual keyboard state directly, which works regardless
        of which window currently has focus or whether ours is even
        mapped. Polled every 50ms via root.after(). Wayland has no
        equivalent global keyboard query, so this same gap remains.

        Runs via root.after() rather than a blocking sleep,.
        """
        delay = self._pending_capture_delay
        self._pending_capture_delay = None   # fires once only
        log.info(f"--delay: selection made, waiting {delay:g}s before the actual capture")
        self.root.withdraw()
        notify(f"Capturing in {delay:g}s\u2026", blocking=False)
        self._delay_deadline = time.monotonic() + delay
        from .capture import make_delay_cancel_poller   # patch 45: Wayland gets a Cancel box
        self._delay_escape_poller = make_delay_cancel_poller(pump=False, delay=delay)
        self._tick_delay_countdown()

    def _tick_delay_countdown(self):
        if self._delay_escape_poller is not None and self._delay_escape_poller.escape_down():
            log.info("--delay: Escape pressed during the countdown - cancelling the whole capture")
            self._delay_escape_poller.close()
            self._delay_escape_poller = None
            self.root.destroy()
            return
        if time.monotonic() >= self._delay_deadline:
            if self._delay_escape_poller is not None:
                self._delay_escape_poller.close()
                self._delay_escape_poller = None
            self._finish_delayed_recapture()
            return
        self.root.after(50, self._tick_delay_countdown)

    def _finish_delayed_recapture(self):
        log.info("--delay: elapsed - taking the actual (fresh) capture now")
        capture_full_screen(self.image_path)
        self._capture_rgba_cache = None   # the file on disk just changed - see _capture_rgba's cache key
        self.transition_to_review()

    def transition_to_review(self):
        from PIL import Image
        log.info("Selection finalized, transitioning to compact review window")
        old_overlay = self.root
        try:
            old_overlay.grab_release()
        except tk.TclError:
            pass
        x1, y1, x2, y2 = (int(v) for v in self.sel_coords)
        full = cached_open(self.image_path)
        crop = full.crop((x1, y1, x2, y2))
        new_iw, new_ih = crop.size

        fd, tmp_name = tempfile.mkstemp(suffix=".png", prefix="snippysnappy-crop-")
        os.close(fd)
        crop_path = Path(tmp_name)
        from .capture import write_png_async
        write_png_async(crop, crop_path)    # patch 43: off the click-release path; served from memory meanwhile
        global_cx = self.origin_x + (x1 + x2) / 2
        global_cy = self.origin_y + (y1 + y2) / 2
        monitors = get_monitors()
        _, mx, my, mw, mh = monitor_at_point(monitors, global_cx, global_cy)
        if IMAGE_REVIEW_MONITOR in ("control", "other"):
            try:
                # "control" = the monitor the recording Stop control uses: top-right of the virtual screen.
                vx = max(m[1] + m[3] for m in monitors) - 60
                ctrl = next((m for m in monitors if m[1] <= vx < m[1] + m[3] and m[2] <= 40 < m[2] + m[4]), monitors[-1])
                cap = (mx, my, mw, mh)
                if IMAGE_REVIEW_MONITOR == "control" or tuple(ctrl[1:]) != cap:
                    chosen = ctrl
                else:
                    chosen = next((m for m in monitors if tuple(m[1:]) != cap), ctrl)
                _, mx, my, mw, mh = chosen
                log.info(f"IMAGE_REVIEW_MONITOR={IMAGE_REVIEW_MONITOR!r}: review window goes to monitor {chosen[0]} (capture was on {cap})")
            except Exception:
                log.exception("Could not apply IMAGE_REVIEW_MONITOR (non-fatal) - using the capture's monitor")

        self.image_path = crop_path
        self.owned_temp_paths.append(crop_path)
        self.base_img_w, self.base_img_h = new_iw, new_ih
        self._raw_img_w, self._raw_img_h = new_iw, new_ih
        self._reset_canvas()

        new_win = tk.Toplevel(self._true_root, bg="#1a1a1a")
        new_win.withdraw()
        new_win.attributes("-topmost", True)
        new_win.title(WINDOW_TITLE_BASE)
        self._app_icon_image = _load_app_icon()
        if self._app_icon_image is not None:
            new_win.iconphoto(True, self._app_icon_image)
        new_win.configure(cursor="crosshair", bg="#1a1a1a")

        self.root = new_win
        self._build_decorated_window_chrome(new_win)
        self._rebind_selection_handlers()
        self.canvas.bind("<Button-4>", lambda e: self._on_scroll(1))
        self.canvas.bind("<Button-5>", lambda e: self._on_scroll(-1))
        self.canvas.bind("<MouseWheel>", lambda e: self._on_scroll(1 if e.delta > 0 else -1))
        self._bind_persistent_canvas_events()
        self._bind_clipboard_shortcuts()
        self._bind_root_shortcuts(new_win)
        new_win.bind("<Destroy>", self._cascade_destroy_true_root, add="+")

        self.current_monitor_bounds = (mx, my, mw, mh)

        self.mode = "window"
        self.sel_coords = (0, 0, new_iw, new_ih)
        self.baked_image = None
        self.edit_strokes = []
        self.ocr_strokes = []

        self._recalculate_display()
        if self.side_panel_mode is not None:
            self._refresh_side_panel()
        new_win.update_idletasks()
        old_overlay.attributes("-topmost", False)
        new_win.deiconify()
        new_win.lift()
        new_win.focus_force()
        try:
            from .coordinator import focus_local_window
            new_win.after(150, lambda: focus_local_window(self))   # _p22: also win focus back from a fullscreen game
        except Exception:
            log.exception("Could not schedule review-window focus (non-fatal)")
        def _reassert_position(w=new_win):
            # The WM may place a freshly mapped window near the pointer, ignoring the geometry set while withdrawn.
            try:
                if self._expected_geometry:
                    self._set_geometry(self._expected_geometry)   # re-arms the Configure-enforcement window
                    log.debug(f"Review window geometry re-asserted: {self._expected_geometry} (actual now {w.geometry()})")
            except Exception:
                log.exception("could not re-assert review window position (non-fatal)")
        new_win.update_idletasks()
        _reassert_position()
        new_win.after(120, _reassert_position)
        new_win.after(350, _reassert_position)
        new_win.after(700, _reassert_position)
        old_overlay.withdraw()
        old_overlay.overrideredirect(False)
        notify("Capture taken", blocking=False)

        log.debug(f"Review window placed (image {new_iw}x{new_ih}) on monitor near ({global_cx:.0f},{global_cy:.0f})")
        self.draw_handles()
        self.show_main_toolbar()
        r = None
        try:
            from .coordinator import broadcast_window_roster
            broadcast_window_roster()
            if True:   # the window registers / labels settle a moment after the transition
                self.root.after(300, broadcast_window_roster)
        except Exception:
            log.exception("roster broadcast failed (non-fatal)")
        return r

    def _cascade_destroy_true_root(self, event=None):
        if getattr(self, "_discarding", False) and any(w is not self for w in _LOCAL_WINDOWS):
            return
        if event is not None and event.widget is not self.root:
            return
        try:
            if self._true_root is not self.root and self._true_root.winfo_exists():
                self._true_root.destroy()
        except Exception:
            pass

    def _close_window(self):
        # The one path every "close this whole window now" action should go
        # through - Cancel/Escape's last-tab fallback, Save/Copy's "Also
        # Exit", OCR's Close button, etc - rather than calling
        # self.root.destroy() directly. Respects EXIT_BEHAVIOR:
        # "full_close" destroys the window as before (which, for whichever
        # window happens to be the one the process's mainloop() is actually
        # tied to, ends the whole process via _cascade_destroy_true_root);
        # "background" (the default) instead just WITHDRAWS it - the window
        # disappears from the screen, but nothing is actually torn down:
        # this SelectorApp instance, its documents/tabs, its temp files, and
        # the process itself all stay alive, still reachable as a "Merge in"
        # target from a NEW snippysnappy window's Tabs panel (a withdrawn
        # window is still a registered one - see _local_window_roster_entries)
        # exactly as if it were still visible. Applied uniformly to every
        # window, not just whichever one happens to be "last" - closing
        # several windows this way just leaves several of them backgrounded,
        # all still reachable the same way.
        if EXIT_BEHAVIOR == "full_close":
            log.info("Closing window (full_close exit behavior) - process will exit if this was the last one")
            self.root.destroy()
        else:
            log.info("Backgrounding window (background exit behavior) - process stays alive, its tabs remain reachable via Merge in")
            self.root.withdraw()
        result = None
        try:
            state = self.root.state()
        except tk.TclError:
            self._unlist_window()   # destroyed
            return result
        if state == "withdrawn":
            self._mark_hidden(True)   # backgrounded: stays listed, under Hidden
        return result

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
        st = self._sel_stack()
        if st and not self.edit_mode_active and not getattr(self, "resize_mode_active", False) and not getattr(self, "magnify_mode_active", False):
            self.sel_coords = st.pop()
            log.info(f"Selection undo: restored {self.sel_coords}")
            self.draw_handles()
            self.show_main_toolbar()
            return
        if self.sel_coords is None:
            return
        self.undo_last_stroke()

    def _on_scroll(self, direction):
        if self._panning:
            return
        if self._pan_released_at is not None and (time.monotonic() - self._pan_released_at) < PAN_WHEEL_SUPPRESS_SECONDS:
            return
        if self.magnify_mode_active:
            self.magnify_level = max(2, min(16, self.magnify_level + direction))
            self.show_magnify_toolbar()
        elif self.edit_mode_active and not self.resize_mode_active:
            if self.draw_tool == "pen" and self.pen_variant == "highlight":
                self.highlight_width = max(4, min(60, self.highlight_width + direction))
                self.show_edit_toolbar()
            elif self.draw_tool == "pen":
                self.pen_width = max(1, min(30, self.pen_width + direction))
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
        _p33_pan_clamp(self.canvas)

    def _pan_end(self, event):
        self._panning = False
        self._pan_released_at = time.monotonic()

    def _bind_persistent_canvas_events(self):
        self.canvas.bind("<Control-Button-4>", lambda e: self._on_ctrl_scroll(1))
        self.canvas.bind("<Control-Button-5>", lambda e: self._on_ctrl_scroll(-1))
        self.canvas.bind("<Control-MouseWheel>", lambda e: self._on_ctrl_scroll(1 if e.delta > 0 else -1))
        self.canvas.bind("<ButtonPress-2>", self._pan_start)
        self.canvas.bind("<B2-Motion>", self._pan_drag)
        self.canvas.bind("<ButtonRelease-2>", self._pan_end)
        try:
            self.canvas.configure(confine=False)
        except tk.TclError:
            pass

    def _cleanup_temp_files(self, event=None):
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
        from .capture import wait_written
        wait_written()          # patch 43: never delete a capture while its PNG is still being written
        for p in self.owned_temp_paths:
            try:
                Path(p).unlink(missing_ok=True)
            except Exception:
                log.exception(f"Failed to clean up temp file {p} (non-fatal)")
        _unregister_window(self)

    def redo_capture(self):
        log.info("Redo Capture pressed - re-executing script fresh")
        try:
            if self.capture_tempdir:
                shutil.rmtree(self.capture_tempdir, ignore_errors=True)
                log.debug(f"Cleaned up {self.capture_tempdir} before redo")
            if self.image_path:
                from .capture import wait_written
                wait_written(self.image_path)
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
        for item_id in self._selection_overlay_item_ids:
            try:
                self.canvas.delete(item_id)
            except tk.TclError:
                pass
        self._selection_overlay_item_ids = []

    def _update_selection_translucent_overlay(self):
        """
        Dims whatever part of the canvas WON'T show real captured
        content in the final output: the region outside the current
        selection AND any part of the selection that
        extends past the original captured image's own bounds (new
        whitespace, not real pixels). Cleared by clear_handles, called
        fresh by draw_handles every time the selection changes (drag or
        not).
        """
        if not self.sel_coords:
            return
        sx1, sy1, sx2, sy2 = self.sel_coords
        sx1, sx2 = min(sx1, sx2), max(sx1, sx2)
        sy1, sy2 = min(sy1, sy2), max(sy1, sy2)
        ox1, oy1, ox2, oy2 = 0, 0, self._raw_img_w, self._raw_img_h
        rx1, ry1 = max(sx1, ox1), max(sy1, oy1)
        rx2, ry2 = min(sx2, ox2), min(sy2, oy2)
        full_x1, full_y1 = min(sx1, ox1), min(sy1, oy1)
        full_x2, full_y2 = max(sx2, ox2), max(sy2, oy2)
        if rx1 < rx2 and ry1 < ry2:
            strips = [
                (full_x1, full_y1, full_x2, ry1),   # above the real/kept region
                (full_x1, ry2, full_x2, full_y2),   # below it
                (full_x1, ry1, rx1, ry2),           # left of it
                (rx2, ry1, full_x2, ry2),           # right of it
            ]
        else:
            strips = [(full_x1, full_y1, full_x2, full_y2)]   # no overlap at all - everything shown is "not real" yet
        for (a, b, c, d) in strips:
            if c - a <= 0 or d - b <= 0:
                continue
            ca, cb = self.img_to_canvas(a, b)
            cc, cd = self.img_to_canvas(c, d)
            item = self.canvas.create_rectangle(ca, cb, cc, cd, fill="#000000", stipple="gray50", outline="")
            self._selection_overlay_item_ids.append(item)

    def draw_handles(self):
        self.clear_handles()
        if self.resize_mode_active:
            return
        if not self.sel_coords:
            return
        self._update_selection_translucent_overlay()
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
        bx, by = (x1 + x2) / 2, y1 + 18
        circ = self.canvas.create_oval(
            bx - 10, by - 10, bx + 10, by + 10, fill="#333333", outline="white",
        )
        txt = self.canvas.create_text(bx, by, text="\u00d7", fill="white", font=("", 11, "bold"))
        self.reset_btn_items = [circ, txt]

    def reset_selection(self, event=None):
        try:
            if self.sel_coords is not None and self.mode == "window":
                self._sel_stack().append(tuple(self.sel_coords))
        except Exception:
            pass
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
        w = abs(x2 - x1)
        h = abs(y2 - y1)
        self._show_dim_overlay(canvas_x, canvas_y, f"{round(w)} x {round(h)}")

    def clamp_to_selection(self, x, y):
        x1, y1, x2, y2 = self.sel_coords
        return max(x1, min(x, x2)), max(y1, min(y, y2))

    def clear_toolbar(self):
        if self.toolbar_container is not None:
            for w in self.toolbar_container.winfo_children():
                w.destroy()
        elif self.toolbar:
            self.toolbar.destroy()
            self.toolbar = None

    def new_toolbar_frame(self):
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
        content_h = content_frame.winfo_reqheight()
        new_toolbar_h = max(TOOLBAR_HEIGHT, content_h)
        required_h = self.canvas.winfo_reqheight() + new_toolbar_h
        current_w = self.root.winfo_width()
        current_h = self.root.winfo_height()
        new_w = max(current_w, required_w + 10)
        new_h = max(current_h, required_h)
        if new_toolbar_h != self.toolbar_container.winfo_reqheight():
            self.toolbar_container.config(height=new_toolbar_h)
        if new_w != current_w or new_h != current_h:
            x, y = self._last_intended_position()
            self._set_geometry(f"{new_w}x{new_h}+{x}+{y}")
            log.debug(f"Resized window to fit toolbar content: {new_w}x{new_h} (was {current_w}x{current_h})")

    def show_main_toolbar(self, *args, **kwargs):
        result_2 = self.show_main_toolbar_base(*args, **kwargs)
        if not _paths_hook_state["done"]:
            try:
                if not self.root.overrideredirect():
                    _paths_hook_state["done"] = True
                    self.root.after(250, lambda: _paths_setup.startup(self))
            except Exception:
                log.exception("folder setup hook failed (non-fatal)")
        result = result_2
        if not getattr(self, "_og_backup_done", False):
            try:
                if not self.root.overrideredirect():
                    self._og_backup_done = True
                    src = Path(self.image_path)
                    from .capture import wait_written
                    wait_written(src)
                    if (
                        getattr(_og_consts, "BACKUP_ORIGINAL", False)
                        and not getattr(self, "_no_real_capture", False)
                        and not src.name.startswith(("opened", "nil"))
                        and src.exists()
                    ):
                        dest = _paths_setup.get_dir("sloppysnaps") / f"snippysnap-og-{timestamp()}.png"
                        _og_shutil.copy2(src, dest)
                        log.info(f"BACKUP_ORIGINAL -> {dest}")
            except Exception:
                log.exception("BACKUP_ORIGINAL failed (non-fatal)")
        r = result
        try:
            if (self.sel_coords and not self.handles and not self.edit_mode_active and not self.resize_mode_active
                    and not self.ocr_paint_mode_active and not self.magnify_mode_active):
                self.draw_handles()
        except Exception:
            log.exception("Could not redraw the selection handles on the main screen (non-fatal)")
        return r

    def show_main_toolbar_base(self, reset_tools_row=True, reset_save_row=True):
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
        self._hide_snip_ui()
        if self._sync_base_dims_for_mode():
            self._recalculate_display(keep_position=True)
        else:
            self._refresh_base_display()
        self._rebind_selection_handlers()
        self.clear_toolbar()
        frame = self.new_toolbar_frame()
        main_row = tk.Frame(frame, bg="#222222")
        main_row.pack(side="top", fill="x")
        if self.tools_row_expanded:
            self._build_tools_row(main_row, active=None, back=self.toggle_tools_row)
            self._build_status_row(main_row, frame)
            self.finish_toolbar_placement(frame)
            self.update_dimension_display()
            self._set_window_title("tools")
            return

        tk.Button(main_row, text="Copy", pady=BUTTON_PADY, command=self.do_copy).pack(side="left", padx=2, pady=4)
        tk.Button(
            main_row, text="Save", pady=BUTTON_PADY,
            relief=("sunken" if self.save_row_expanded else "raised"),
            command=self.toggle_save_row,
        ).pack(side="left", padx=2, pady=4)
        tk.Button(main_row, text="Edit", pady=BUTTON_PADY, command=self.enter_edit_mode).pack(side="left", padx=2, pady=4)
        tk.Button(
            main_row, text="Tools", pady=BUTTON_PADY,
            relief=("sunken" if self.tools_row_expanded else "raised"),
            command=self.toggle_tools_row,
        ).pack(side="left", padx=2, pady=4)
        if self.mode == "fullscreen" and len(self.available_monitors) > 1:
            self._add_monitor_dropdown(main_row)
        else:
            tk.Button(main_row, text="Redo", pady=BUTTON_PADY, command=self.redo_capture).pack(side="left", padx=(10, 2), pady=4)
        tk.Button(main_row, text="Cancel", pady=BUTTON_PADY, command=self._cancel_with_confirm).pack(side="left", padx=2, pady=4)

        checkbox_frame = tk.Frame(main_row, bg="#222222")
        checkbox_frame.pack(side="left", padx=(8, 4))
        tk.Checkbutton(
            checkbox_frame, text="Also save", variable=self.also_save_var,
            bg="#222222", fg="white", selectcolor="#444444",
            activebackground="#222222", activeforeground="white",
        ).pack(side="left", padx=(0, 8))
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

        self._build_status_row(main_row, frame)

        if self.save_row_expanded:
            save_row = tk.Frame(frame, bg="#222222")
            save_row.pack(side="top", fill="x")
            tk.Button(save_row, text="Save", pady=BUTTON_PADY, command=self.begin_normal_save).pack(side="left", padx=4, pady=4)
            tk.Button(save_row, text="Sloppy Save", pady=BUTTON_PADY, command=self.do_slop_save).pack(side="left", padx=4, pady=4)
            tk.Button(save_row, text="Save As\u2026", pady=BUTTON_PADY, command=self._save_as_image).pack(side="left", padx=4, pady=4)

        self.finish_toolbar_placement(frame)
        self.update_dimension_display()
        if self.save_row_expanded:
            self._set_window_title("save")
        else:
            self._set_window_title()

    def _build_tools_row(self, main_row, active, undo=None, redo=None, cancel=None, done=None, back=None):
        """
        Shared top row for the whole Tools submenu - the root's own bare
        Tools row (active=None, only `back` given) AND every individual
        tool screen (Extract Text/Measure/Magnify/Pin), styled like Edit
        mode's own tool-picker row.
        """
        tk.Button(
            main_row, text="Extract Text", pady=BUTTON_PADY,
            relief=("sunken" if active == "ocr" else "raised"),
            command=lambda: self._switch_tool("ocr", self.enter_ocr_paint_mode),
        ).pack(side="left", padx=2, pady=4)
        tk.Button(
            main_row, text="Measure", pady=BUTTON_PADY,
            relief=("sunken" if active == "measure" else "raised"),
            command=lambda: self._switch_tool("measure", self.enter_measure_mode),
        ).pack(side="left", padx=2, pady=4)
        tk.Button(
            main_row, text="Magnify", pady=BUTTON_PADY,
            relief=("sunken" if active == "magnify" else "raised"),
            command=lambda: self._switch_tool("magnify", self.enter_magnify_mode),
        ).pack(side="left", padx=2, pady=4)
        tk.Button(
            main_row, text="Pin", pady=BUTTON_PADY,
            relief=("sunken" if active == "pin" else "raised"),
            command=lambda: self._switch_tool("pin", self.enter_pin_mode),
        ).pack(side="left", padx=2, pady=4)
        if back is not None:
            tk.Button(main_row, text="Back", pady=BUTTON_PADY, command=back).pack(side="left", padx=(10, 2), pady=4)
        if undo is not None:
            tk.Button(main_row, text="Undo", pady=BUTTON_PADY, command=undo).pack(side="left", padx=2, pady=4)
        if redo is not None:
            tk.Button(main_row, text="Redo", pady=BUTTON_PADY, command=redo).pack(side="left", padx=2, pady=4)
        if cancel is not None:
            tk.Button(main_row, text="Cancel", pady=BUTTON_PADY, command=cancel).pack(side="left", padx=(10, 2), pady=4)
        if done is not None:
            tk.Button(main_row, text="Done", pady=BUTTON_PADY, command=done).pack(side="left", padx=((10, 2) if cancel is None else 2), pady=4)

    def _active_tool_name(self):
        """Which of the four Tools-submenu tools is currently active, or None."""
        if self.measure_mode_active:
            return "measure"
        if self.magnify_mode_active:
            return "magnify"
        if self.ocr_paint_mode_active:
            return "ocr"
        if self.pin_mode_active:
            return "pin"
        return None

    def _leave_current_tool_silently(self):
        """
        Tears down whichever tool is currently active (canvas bindings,
        overlays, mode flags) WITHOUT re-showing a toolbar - unlike
        exit_measure_mode/exit_magnify_mode/cancel_ocr_paint/exit_pin_mode,
        which also call show_main_toolbar. Used by _switch_tool.
        No-op if no tool is currently active.
        """
        name = self._active_tool_name()
        if name == "measure":
            self._leave_measure_mode()
        elif name == "magnify":
            self._leave_magnify_mode()
        elif name == "ocr":
            self._leave_ocr_paint_mode()
        elif name == "pin":
            self._leave_pin_mode()

    def _switch_tool(self, name, enter_fn):
        """
        The command every tool-picker button in _build_tools_row actually
        calls now (instead of calling enter_X_mode directly): a no-op if
        that tool is already the active one (so re-clicking, say, Measure
        while already in Measure doesn't wipe out your measurement boxes),
        otherwise cleans up whatever tool WAS active first.
        """
        if self._active_tool_name() == name:
            return
        self._leave_current_tool_silently()
        enter_fn()

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
        self.canvas.unbind("<ButtonPress-1>")
        self.canvas.unbind("<B1-Motion>")
        self.canvas.unbind("<ButtonRelease-1>")
        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)

    def update_dimension_display(self, override=None):
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

    def _build_status_row(self, button_row_parent, frame):
        """
        Returns the bottom "text_row" Frame (Dimensions/Scale's own row) so
        a caller can also pack something into its LEFT side - e.g. Magnify's
        "Done" or Measure's "Clear", which sit at the bottom-left corner of
        their toolbar rather than crowding the top button row. Returns None
        in the simple/no-container fallback case, where there's no such row.
        """
        if self.toolbar_container is None:
            self.dimension_label = tk.Label(button_row_parent, text="", fg="white", bg="#222222")
            self.dimension_label.pack(side="right", padx=8)
            self.scale_label = None
            self.rescale_btn = None
            self._update_scale_label()
            self.update_dimension_display()
            return None

        # Tabs selector packed first (rightmost), Re-scale to its left -
        # same order as the film window.
        panel_var = tk.StringVar(value=self._side_panel_mode_label())
        dropdown = tk.OptionMenu(button_row_parent, panel_var, "None", "Layers", "Tabs", command=self._on_side_panel_choice)
        dropdown.config(pady=BUTTON_PADY, width=7)
        dropdown.pack(side="right", padx=(4, 8), pady=4)
        if not AUTO_SCALE_TO_FIT:
            self.rescale_btn = tk.Button(button_row_parent, text="Re-scale", pady=BUTTON_PADY, command=self.manual_rescale_to_fit)
            self.rescale_btn.pack(side="right", padx=(4, 4), pady=4)
        else:
            self.rescale_btn = None

        text_row = tk.Frame(frame, bg="#222222")
        text_row.pack(side="top", fill="x")
        self.dimension_label = tk.Label(text_row, text="", fg="white", bg="#222222")
        self.dimension_label.pack(side="right", padx=(4, 8))
        self.scale_label = tk.Label(text_row, text="", fg="#aaaaaa", bg="#222222")
        self.scale_label.pack(side="right", padx=(0, 4))

        self._update_scale_label()
        self.update_dimension_display()
        return text_row

    def _add_monitor_dropdown(self, frame, *args, **kwargs):
        before = set(frame.winfo_children())
        result = self._add_monitor_dropdown_base(frame, *args, **kwargs)
        try:
            for w in _walk_option_menus(frame, before):
                w.pack_configure(padx=(10, 2))
        except tk.TclError:
            pass
        return result

    def _add_monitor_dropdown_base(self, frame):
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
            from .capture import wait_written
            wait_written(old_path)
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

    def crop_original(self):
        from PIL import Image
        full = cached_open(self.image_path)
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
        _sx1, _sy1, _sx2, _sy2 = self.sel_coords
        if (_sx2 - _sx1) < 2 or (_sy2 - _sy1) < 2:
            log.info("Edit: selection is empty/degenerate - using the full image instead")
            self.sel_coords = (0, 0, self.base_img_w, self.base_img_h)
        self._pre_edit_state = self._capture_full_state()
        self._activate_canvas_if_needed()
        self.edit_mode_active = True
        if self._sync_base_dims_for_mode():
            self._recalculate_display(keep_position=True)
        else:
            self._refresh_base_display()
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
        self.canvas.bind("<Double-Button-1>", self._edit_double_click)
        self.show_edit_toolbar()

    def edit_press(self, event):
        if self.draw_tool == "text" and self._pending_text_widget is not None:
            name = self._tx_hit_handle(event)
            if name:
                self._tx_rs = {"name": name, "box0": self._pending_text_box}
                return "break"
        r_2 = self.edit_press_base(event)
        try:
            item = self.text_current_box_id
            if self.draw_tool == "text" and item:
                fill = self.text_bg_fill
                if fill == "white":
                    self.canvas.itemconfigure(item, fill="white", outline="", dash=())
                elif fill == "black":
                    self.canvas.itemconfigure(item, fill="black", outline="", dash=())
                elif fill == "white_border":
                    self.canvas.itemconfigure(item, fill="white", outline="black", width=2, dash=())
                elif fill == "black_border":
                    self.canvas.itemconfigure(item, fill="black", outline="white", width=2, dash=())
        except Exception:
            log.exception("text drag preview styling failed (non-fatal)")
        r = r_2
        try:
            self._tx_style_drag()
        except Exception:
            log.exception("text drag preview styling failed (non-fatal)")
        return r

    def edit_press_base(self, event):
        if self.resize_mode_active:
            return
        self.canvas.focus_set()
        self._hide_snip_ui()
        screen_x, screen_y = self._event_canvas_xy(event)
        x, y = self._event_to_capture_local(event)
        x, y = self.clamp_to_canvas(x, y)
        if self.draw_tool == "snip":
            self._snip_press(x, y, screen_x, screen_y)
            return
        if self.draw_tool == "text":
            if self._hit_test_and_start_drag(x, y, screen_x, screen_y):
                return
            if self.text_mode == "select":
                # Select mode: only moving/resizing existing text boxes is
                # allowed here - nothing was hit above, so there is nothing
                # to do (other than maybe a crop handle - see _crop_press).
                # See the "select mode" comment block near pick_tool.
                self._crop_press(screen_x, screen_y)
                return
            self.text_box_start = (x, y)
            if self.text_current_box_id:
                self.canvas.delete(self.text_current_box_id)
            cx, cy = self.capturelocal_to_screen(x, y)
            self.text_current_box_id = self.canvas.create_rectangle(
                cx, cy, cx, cy, outline="#00b4ff", width=2, dash=(4, 2),
            )
            return
        if self.draw_tool == "shape":
            if self._hit_test_and_start_drag(x, y, screen_x, screen_y):
                return
            if self.shape_type == "select":
                self._crop_press(screen_x, screen_y)
                return
            self.shape_pending_start = (x, y)
            self._draw_shape_preview((x, y), (x, y))
            return
        if self.draw_tool == "pen" and self.pen_variant == "select":
            if self._hit_test_and_start_drag_draw_select(x, y, screen_x, screen_y):
                return
            if self._crop_press(screen_x, screen_y):
                return
            self._draw_select_rect_start = (x, y)
            self._draw_select_rect_item_id = self.canvas.create_rectangle(
                screen_x, screen_y, screen_x, screen_y, outline="#00b4ff", width=2, dash=(4, 2),
            )
            return
        if self.draw_tool == "pen" and self.pen_variant == "spray":
            self.edit_current_stroke = {"mode": "spray", "dots": [], "color": self.draw_color, "segment_ids": []}
            self._spray_burst(x, y)
            return
        if self.draw_tool == "pen" and self.pen_variant == "eraser":
            self.edit_current_stroke = {"points": [(x, y)], "color": "#ffffff", "mode": "erase", "width": self.pen_width, "segment_ids": []}
            return
        if self.draw_tool == "pen" and self.pen_variant == "highlight":
            self.edit_current_stroke = {
                "points": [(x, y)], "color": self.draw_color, "mode": "highlight",
                "width": self.highlight_width, "segment_ids": [],
            }
            return
        # plain Pen
        self.edit_current_stroke = {
            "points": [(x, y)], "color": self.draw_color, "mode": "pen",
            "width": self.pen_width, "segment_ids": [],
        }

    def _hit_test_and_start_drag(self, x, y, screen_x, screen_y):
        candidates = [
            i for i in range(len(self.edit_strokes) - 1, -1, -1)
            if self._select_hit_mode_ok(self.edit_strokes[i]) and not self.edit_strokes[i].get("removed")
            and self._stroke_visible_for_editing(self.edit_strokes[i])
        ]
        for i in candidates:
            handle = self._hit_snip_handle(self.edit_strokes[i], screen_x, screen_y)
            if handle:
                self._start_snip_drag(i, handle, x, y)
                return True
        for i in candidates:
            if self._point_hits_element(self.edit_strokes[i], x, y):
                self._start_snip_drag(i, "move", x, y)
                return True
        return False

    def _hit_test_and_start_drag_draw_select(self, x, y, screen_x, screen_y):
        """
        Same shape as _hit_test_and_start_drag, but for Draw's own Select
        mode.
        """
        candidates = [
            i for i in range(len(self.edit_strokes) - 1, -1, -1)
            if self.edit_strokes[i].get("mode") == "snip" and self.edit_strokes[i].get("lifted_from_draw")
            and not self.edit_strokes[i].get("removed") and self._stroke_visible_for_editing(self.edit_strokes[i])
        ]
        for i in candidates:
            handle = self._hit_snip_handle(self.edit_strokes[i], screen_x, screen_y)
            if handle:
                self._start_snip_drag(i, handle, x, y)
                return True
        for i in candidates:
            if self._point_hits_element(self.edit_strokes[i], x, y):
                self._start_snip_drag(i, "move", x, y)
                return True
        return False

    def _lighten(self, hex_color, factor=0.5):
        r, g, b = hex_to_rgb(hex_color)
        r = int(r + (255 - r) * factor)
        g = int(g + (255 - g) * factor)
        b = int(b + (255 - b) * factor)
        return f"#{r:02x}{g:02x}{b:02x}"

    def _spray_burst(self, cx, cy):
        spread = max(2, self.pen_width)
        stroke = self.edit_current_stroke
        stroke["last_burst_pos"] = (cx, cy)
        for _ in range(SPRAY_DOTS_PER_BURST):
            angle = random.uniform(0, 2 * math.pi)
            dist = random.uniform(0, spread)
            dot_x = cx + math.cos(angle) * dist
            dot_y = cy + math.sin(angle) * dist
            dot_r = random.uniform(*SPRAY_DOT_RADIUS_RANGE) * (max(1, getattr(self, "spray_width", 2)) / 2.0)
            stroke["dots"].append((dot_x, dot_y, dot_r))
            sx, sy = self.capturelocal_to_screen(dot_x, dot_y)
            r_screen = max(0.5, dot_r * self.display_scale)
            seg_id = self.canvas.create_oval(
                sx - r_screen, sy - r_screen, sx + r_screen, sy + r_screen,
                fill=stroke["color"], outline="",
            )
            stroke["segment_ids"].append(seg_id)

    SPRAY_SUPERSAMPLE = 4

    def _bake_spray_stroke(self, base, stroke, ox, oy):
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
        if tool == "erase":
            return self.canvas.create_line(lx, ly, x, y, state="hidden")
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
        r = self._edit_drag_tx2(event)
        try:
            if self.draw_tool == "pen" and self.pen_variant == "eraser" and self.edit_current_stroke:
                self._p36_er_preview()
        except Exception:
            log.exception("eraser preview hook failed (non-fatal)")
        return r

    def edit_drag_base(self, event):
        if self.resize_mode_active:
            return
        if self._crop_drag_active_handle is not None:
            # Passes the raw event through - _crop_drag converts it via
            # the anchor captured in _crop_press.
            self._crop_drag(event)
            return
        x_2, y_2 = self._event_to_capture_local(event)
        x_2, y_2 = self.clamp_to_canvas(x_2, y_2)
        if self._snip_drag_mode is not None and self._snip_drag_index is not None:
            self._snip_drag(x_2, y_2)
            return
        if self.draw_tool == "snip":
            self._snip_drag(x_2, y_2)
            return
        if self.draw_tool == "text":
            if self.text_box_start and self.text_current_box_id:
                cx0, cy0 = self.capturelocal_to_screen(*self.text_box_start)
                cx_2, cy_2 = self.capturelocal_to_screen(x_2, y_2)
                self.canvas.coords(self.text_current_box_id, cx0, cy0, cx_2, cy_2)
                tx1, ty1 = self.text_box_start
                bx1, by1, bx2, by2 = min(tx1, x_2), min(ty1, y_2), max(tx1, x_2), max(ty1, y_2)
                self._show_dim_overlay(cx_2, cy_2, f"{round(bx2 - bx1)} x {round(by2 - by1)}")
                self._show_alignment_guides((bx1, by1, bx2, by2))
            return
        if self.draw_tool == "shape":
            if self.shape_pending_start is not None:
                self._draw_shape_preview(self.shape_pending_start, (x_2, y_2))
            return
        if self.draw_tool == "pen" and self.pen_variant == "select":
            if self._draw_select_rect_start is not None:
                cx0, cy0 = self.capturelocal_to_screen(*self._draw_select_rect_start)
                cx_2, cy_2 = self.capturelocal_to_screen(x_2, y_2)
                self.canvas.coords(self._draw_select_rect_item_id, cx0, cy0, cx_2, cy_2)
            return
        if not self.edit_current_stroke:
            return
        if self.edit_current_stroke.get("mode") == "spray":
            lbx, lby = self.edit_current_stroke.get("last_burst_pos", (x_2, y_2))
            if (x_2 - lbx) ** 2 + (y_2 - lby) ** 2 < (SPRAY_MIN_BURST_DIST / self.display_scale) ** 2:
                return
            self._spray_burst(x_2, y_2)
            return
        lx_2, ly_2 = self.edit_current_stroke["points"][-1]
        if (x_2 - lx_2) ** 2 + (y_2 - ly_2) ** 2 < (MIN_POINT_DIST / self.display_scale) ** 2:
            return
        clx, cly = self.capturelocal_to_screen(lx_2, ly_2)
        ccx, ccy = self.capturelocal_to_screen(x_2, y_2)
        seg_id = self._draw_preview_segment(
            clx, cly, ccx, ccy, self.edit_current_stroke["color"],
            self.edit_current_stroke["mode"], self._scaled_width(self.edit_current_stroke["width"]),
        )
        self.edit_current_stroke["segment_ids"].append(seg_id)
        self.edit_current_stroke["points"].append((x_2, y_2))

    def edit_release(self, event):
        if getattr(self, "_tx_rs", None):
            self._tx_rs = None
            try:
                self._pending_text_widget.focus_set()
            except Exception:
                pass
            return "break"
        if self.resize_mode_active:
            return
        if self._crop_drag_active_handle is not None:
            self._crop_release()
            self._draw_snip_ui()
            return
        x, y = self._event_to_capture_local(event)
        x, y = self.clamp_to_canvas(x, y)
        if self._snip_drag_mode is not None or self._snip_drag_index is not None:
            self._snip_release(x, y)
            self._draw_snip_ui()
            return
        if self.draw_tool == "snip":
            self._snip_release(x, y)
            return
        self._edit_release_non_snip(event)
        self._draw_snip_ui()

    def _edit_release_non_snip(self, event):
        S = getattr(self, "_p36_er", None)
        if S is not None:
            if S.get("after") is not None:
                try:
                    self.canvas.after_cancel(S["after"])
                except Exception:
                    pass
            self._p36_er = None
        was = bool(self.edit_current_stroke) and self.edit_current_stroke.get("mode") == "erase"
        r = self._edit_release_non_snip_base(event)
        if was:
            self._er_next = 0.0
            try:
                self._recompute_baked_image()
            except Exception:
                log.exception("eraser release re-bake failed (non-fatal)")
        return r

    def _edit_release_non_snip_base(self, event):
        x, y = self._event_to_capture_local(event)
        x, y = self.clamp_to_canvas(x, y)
        if self.draw_tool == "text":
            if not self.text_box_start:
                return
            x1b, y1b = self.text_box_start
            self.text_box_start = None
            self._clear_dim_overlay()
            self._clear_alignment_guides()
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
        if self.draw_tool == "pen" and self.pen_variant == "select":
            if self._draw_select_rect_start is not None:
                self._finish_draw_select_rect(x, y)
            return
        if not self.edit_current_stroke:
            return
        if self.edit_current_stroke.get("mode") == "spray":
            if self.edit_current_stroke["dots"]:
                self._append_stroke(self.edit_current_stroke)
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
            self._append_stroke(self.edit_current_stroke)
            self._edit_redo_stack = []
            log.debug(f"Edit stroke committed: mode={self.edit_current_stroke['mode']} color={self.edit_current_stroke['color']} width={self.edit_current_stroke['width']} points={len(self.edit_current_stroke['points'])}")
        self.edit_current_stroke = None

    # ---- text entry (with per-span color/font/size) ----

    def _text_color_tag(self, color):
        tw = self._pending_text_widget
        name = "col_" + color.lstrip("#").lower()
        tw.tag_configure(name, foreground=color)
        return name

    def _logical_to_tk_size(self, size):
        """
        Converts a document/logical font size (the unit font_size is
        stored and compared in everywhere else - stroke dicts, runs,
        self.text_font_size) into the Tk point size the live typing
        widget should actually display it at, matching the live widget's
        own overall scale (see _begin_text_entry, which uses this same
        formula for the box's whole-widget default font).
        """
        return max(10, int(size * 0.6 * self.display_scale))

    def _text_font_tag(self, family, size):
        tw = self._pending_text_widget
        name = f"fnt_{family}@{size}"
        tw.tag_configure(name, font=(TEXT_FONT_FAMILIES[family]["tk"], self._logical_to_tk_size(size)))
        return name

    def _parse_fnt_tag(self, tagname):
        """(family, logical_size) encoded in a "fnt_<family>@<size>" tag name, or (None, None) if it doesn't parse."""
        body = tagname[len("fnt_"):]
        try:
            family, size_str = body.rsplit("@", 1)
            return family, int(size_str)
        except ValueError:
            return None, None

    def _selected_text_span(self):
        tw = self._pending_text_widget
        if tw is None:
            return None
        try:
            start, end = tw.index("sel.first"), tw.index("sel.last")
        except tk.TclError:
            return None
        if tw.compare(start, ">=", end):
            return None
        return start, end

    def _iter_font_runs(self, tw, start, end):
        """
        Yields (run_start, run_end, family, size) for each contiguous
        stretch of uniform (family, size) within [start, end) of the live
        widget - box defaults (self.text_font_family/text_font_size)
        wherever no "fnt_" tag currently overrides them. Used by
        _apply_font_or_size_to_span to know exactly which sub-ranges of a
        selection currently share which (family, size) pair.
        """
        family, size = self.text_font_family, self.text_font_size
        run_start = None
        run_family = run_size = None
        last_end = start
        for key, value, index in tw.dump(start, end, tag=True, text=True):
            if key == "tagon" and value.startswith("fnt_"):
                fam, sz = self._parse_fnt_tag(value)
                if fam is not None:
                    family, size = fam, sz
            elif key == "tagoff" and value.startswith("fnt_"):
                family, size = self.text_font_family, self.text_font_size
            elif key == "text":
                if run_start is None:
                    run_start, run_family, run_size = index, family, size
                elif (family, size) != (run_family, run_size):
                    yield (run_start, index, run_family, run_size)
                    run_start, run_family, run_size = index, family, size
                last_end = tw.index(f"{index}+{len(value)}c")
        if run_start is not None:
            yield (run_start, last_end, run_family, run_size)

    def _apply_font_or_size_to_span(self, kind, value, start, end):
        tw = self._pending_text_widget
        runs = list(self._iter_font_runs(tw, start, end))
        for tag in tw.tag_names():
            if tag.startswith("fnt_"):
                tw.tag_remove(tag, start, end)
        for run_start, run_end, family, size in runs:
            new_family = value if kind == "font" else family
            new_size = value if kind == "size" else size
            tw.tag_add(self._text_font_tag(new_family, new_size), run_start, run_end)
        log.debug(f"Text span {start}-{end}: {kind} -> {value} ({len(runs)} sub-run(s))")

    def _apply_text_span_format(self, kind, value):
        span = self._selected_text_span()
        if span is None:
            return False
        tw = self._pending_text_widget
        start, end = span
        if kind == "color":
            for tag in tw.tag_names():
                if tag.startswith("col_"):
                    tw.tag_remove(tag, start, end)
            new_tag = self._text_color_tag(value)
            tw.tag_add(new_tag, start, end)
            log.debug(f"Text span {start}-{end}: color -> {value}")
            return True
        self._apply_font_or_size_to_span(kind, value, start, end)   # kind in ("font", "size")
        return True

    def _extract_text_runs(self):
        tw = self._pending_text_widget
        color = None
        family = size = None
        runs = []
        for key, value, _index in tw.dump("1.0", "end-1c", text=True, tag=True):
            if key == "tagon":
                if value.startswith("col_"):
                    color = "#" + value[4:]
                elif value.startswith("fnt_"):
                    family, size = self._parse_fnt_tag(value)
            elif key == "tagoff":
                if value.startswith("col_") and color == "#" + value[4:]:
                    color = None
                elif value.startswith("fnt_"):
                    off_family, off_size = self._parse_fnt_tag(value)
                    if (family, size) == (off_family, off_size):
                        family, size = None, None
            elif key == "text":
                run_color = None if (color or "").lower() == self.draw_color.lower() else color
                run_font = None if family == self.text_font_family else family
                run_size = None if size == self.text_font_size else size
                if (runs and runs[-1]["color"] == run_color and runs[-1]["font"] == run_font
                        and runs[-1].get("size") == run_size):
                    runs[-1]["text"] += value
                else:
                    runs.append({"text": value, "color": run_color, "font": run_font, "size": run_size})
        return runs

    def _build_text_stroke(self, content, box):
        x1, y1, x2, y2 = box
        stroke = {
            "mode": "text", "origin": (x1, y1), "w": x2 - x1, "h": y2 - y1, "rotation": 0.0,
            "text": content, "font_size": self.text_font_size, "font_family": self.text_font_family,
            "color": self.draw_color, "bg_fill": self.text_bg_fill, "text_align": self.text_align, "segment_ids": [],
        }
        runs = self._extract_text_runs()
        if any(r_4["color"] or r_4["font"] or r_4.get("size") for r_4 in runs):
            stroke["runs"] = runs
        r = stroke
        r["rotation"] = getattr(self, "_tx_rot", 0.0)
        return r

    def _apply_text_fill_appearance(self, tw, fill):
        # Approximates this element's Fill setting (see set_text_bg_fill)
        # on the LIVE typing widget.
        try:
            if fill == "white":
                tw.config(bg="white", highlightthickness=0)
            elif fill == "black":
                tw.config(bg="black", highlightthickness=0)
            elif fill == "white_border":
                tw.config(bg="white", highlightthickness=2, highlightbackground="black", highlightcolor="black")
            elif fill == "black_border":
                tw.config(bg="black", highlightthickness=2, highlightbackground="white", highlightcolor="white")
            else:
                tw.config(bg=self._sample_transparent_preview_bg(), highlightthickness=0)
        except tk.TclError:
            pass
        try:
            tw.config(bd=0, relief="flat")
        except tk.TclError:
            pass

    def _sample_transparent_preview_bg(self):
        """
        Approximates a "transparent" preview by sampling the DOMINANT colour (not the mean - a mean of a
        real screenshot crop lands on a muddy grey) of the composited image at this box's location.
        Falls back to flat gray if there is nothing to sample; the reason is logged.
        """
        try:
            from PIL import Image
            if self._pending_text_box is None:
                log.debug("transparent text preview: no _pending_text_box yet - gray fallback")
                return "#dddddd"
            source = self.baked_image if self.baked_image is not None else self._build_canvas_composite()
            x1, y1, x2, y2 = self._pending_text_box
            cx1, cy1 = self.capturelocal_to_canvaslocal(x1, y1)
            cx2, cy2 = self.capturelocal_to_canvaslocal(x2, y2)
            ix1, iy1 = max(0, int(cx1)), max(0, int(cy1))
            ix2 = min(source.width, max(ix1 + 1, int(cx2)))
            iy2 = min(source.height, max(iy1 + 1, int(cy2)))
            if ix2 <= ix1 or iy2 <= iy1:
                log.debug(f"transparent text preview: box ({ix1},{iy1},{ix2},{iy2}) outside source {source.size} - gray fallback")
                return "#dddddd"
            region = source.crop((ix1, iy1, ix2, iy2)).convert("RGB")
            small = region.resize((48, 48), Image.NEAREST) if region.width > 48 or region.height > 48 else region
            bins = {}
            for (pr, pg, pb) in getattr(small, "get_flattened_data", small.getdata)():   # patch 45: Pillow 14
                k = (pr >> 4, pg >> 4, pb >> 4)
                e = bins.get(k)
                if e is None:
                    bins[k] = [1, pr, pg, pb]
                else:
                    e[0] += 1; e[1] += pr; e[2] += pg; e[3] += pb
            n, sr, sg, sb = max(bins.values(), key=lambda e: e[0])
            r, g, b = sr // n, sg // n, sb // n
            result = f"#{r:02x}{g:02x}{b:02x}"
            try:   # debug aid: what the sampler is looking at (overwritten each time)
                source.convert("RGBA").save("/tmp/snippysnappy-debug-sample-source.png")
                region.save("/tmp/snippysnappy-debug-sample-region.png")
                log.debug(f"transparent text preview: source is baked_image={self.baked_image is not None}, canvas=({self.canvas_x1},{self.canvas_y1},{self.canvas_x2},{self.canvas_y2}), box(capture-local)={self._pending_text_box}")
            except Exception:
                pass
            log.debug(f"transparent text preview: sampled {result} from ({ix1},{iy1},{ix2},{iy2}) of source {source.size}")
            return result
        except Exception:
            log.debug("transparent text preview: sampling failed - gray fallback", exc_info=True)
            return "#dddddd"
    def _begin_text_entry(self, x1, y1, x2, y2):
        self._tx_rot = 0.0
        if self.text_current_box_id:
            self.canvas.delete(self.text_current_box_id)
            self.text_current_box_id = None
        cx1, cy1 = self.capturelocal_to_screen(x1, y1)
        cx2, cy2 = self.capturelocal_to_screen(x2, y2)
        self._pending_text_tk_size = self._logical_to_tk_size(self.text_font_size)
        text_widget = tk.Text(
            self.canvas, wrap="word", bd=1, relief="solid",
            font=(TEXT_FONT_FAMILIES[self.text_font_family]["tk"], self._pending_text_tk_size),
            fg=self.draw_color, insertbackground=self.draw_color, exportselection=False,
        )
        # Set BEFORE _apply_text_fill_appearance - the "transparent" fill
        # case samples the real pixels under this box.
        self._pending_text_box = (x1, y1, x2, y2)
        self._apply_text_fill_appearance(text_widget, self.text_bg_fill)
        text_widget.tag_configure("sel", foreground="")
        text_widget.tag_configure("align", justify=self.text_align)
        text_widget.tag_add("align", "1.0", "end")
        win_id = self.canvas.create_window(
            cx1, cy1, anchor="nw", width=int(cx2 - cx1), height=int(cy2 - cy1), window=text_widget,
        )
        self._pending_text_widget = text_widget
        self._pending_text_window_id = win_id
        text_widget.focus_set()

        self.text_vim_mode = "insert"
        self._vim_visual_anchor = None
        text_widget.tag_configure("vim_block_cursor", background="#cccccc", foreground="black")

        text_widget.bind("<Return>", self._confirm_text_entry)
        text_widget.bind("<Shift-Return>", lambda e: None)
        text_widget.bind("<Escape>", self._vim_escape)
        text_widget.bind("<FocusOut>", self._on_text_widget_focus_out)
        text_widget.bind("<Key>", self._vim_on_key)
        r_7 = None
        try:
            if self.text_bg_fill == "transparent" and self._pending_text_widget is not None:
                self._tp_start()
        except Exception:
            log.exception("transparent text preview start failed (non-fatal)")
        r_6 = r_7
        try:
            self.canvas.delete("text_box_content")
            self._tx_rs = None
            self._tx_draw_ui()
        except Exception:
            log.exception("text box UI draw failed (non-fatal)")
        r_5 = r_6
        try:
            tw_2 = self._pending_text_widget
            if tw_2 is not None:
                self.canvas.after(30, lambda t=tw_2: self._tp_start() if self._pending_text_widget is t else None)
        except Exception:
            log.exception("text preview start failed (non-fatal)")
        r = r_5
        try:
            self._p36_install_forward()
            tw = self._pending_text_widget
            if tw is not None:
                def ensure(t=tw):
                    if self._pending_text_widget is t:
                        try:
                            if self.root.focus_displayof() is None:
                                self.root.focus_force()
                            t.focus_set()
                        except Exception:
                            pass
                for ms in (0, 60):
                    self.canvas.after(ms, ensure)
        except Exception:
            log.exception("text box focus setup failed (non-fatal)")
        return r

    def _on_text_widget_focus_out(self, event=None):
        self.canvas.after_idle(self._maybe_confirm_text_entry_after_focus_change)

    def _maybe_confirm_text_entry_after_focus_change(self):
        # patch 13 re-wrapped the ORIGINAL focus-out handler and so skipped the "just opened for editing" grace period
        # (which is why double-clicking a text box in Select mode opened it and instantly closed it again)
        if self._pending_text_widget is None:
            return
        if time.monotonic() < getattr(self, "_text_focus_grace_until", 0.0):
            try:
                self._pending_text_widget.focus_set()
            except Exception:
                pass
            return
        if self._pending_text_widget is None:
            return
        try:
            nf = self.root.focus_get()
        except (tk.TclError, KeyError):
            nf = None
        try:
            cls = nf.winfo_class() if nf is not None else None
        except Exception:
            cls = None
        if cls == "Menu":
            # a dropdown (Font, Fill, ...) is open: never finalize the text box underneath it, look again shortly
            self.canvas.after(150, self._maybe_confirm_text_entry_after_focus_change)
            return
        tries = getattr(self, "_text_focus_retries", 0)
        if nf is None and tries < 6:
            self._text_focus_retries = tries + 1
            self.canvas.after(150, self._maybe_confirm_text_entry_after_focus_change)
            return
        self._text_focus_retries = 0
        if self._pending_text_widget is None:
            return
        frame = self._edit_toolbar_frame
        if frame is not None:
            try:
                new_focus = self.root.focus_get()
            except (tk.TclError, KeyError):
                new_focus = None
            if new_focus is not None and self._widget_is_descendant_of(new_focus, frame):
                return
        self._confirm_text_entry()

    @staticmethod
    def _widget_is_descendant_of(widget, ancestor):
        """
        True if `widget` is `ancestor` or nested anywhere inside it, walked
        via the real Tk widget hierarchy (`.master` chain) rather than a
        pathname string prefix..
        """
        w = widget
        while w is not None:
            if w == ancestor:
                return True
            w = getattr(w, "master", None)
        return False

    def _confirm_text_entry(self, event=None):
        r_10 = self._confirm_text_entry_base(event)
        try:
            self._tp_stop(restore=False)
        except Exception:
            pass
        r_9 = r_10
        try:
            self.canvas.delete("text_ui")
            self.canvas.delete("text_box_content")
            self._tx_rs = None
        except Exception:
            pass
        r_8 = r_9
        try:
            self._tx_clear_ui()
            self._tx_rot = 0.0
        except Exception:
            pass
        r = r_8
        self._p36_text_sweep()
        for ms in (80, 400):
            try:
                self.canvas.after(ms, lambda: self._p36_text_sweep())
            except Exception:
                pass
        return r

    def _confirm_text_entry_base(self, event=None):
        tw = self._pending_text_widget
        if tw is None:
            return "break"
        content = tw.get("1.0", "end-1c")
        box = self._pending_text_box
        edit_index = self._pending_text_edit_index

        # Anything that reads out of the live widget (runs, _build_text_stroke)
        # must happen BEFORE it's cleared/destroyed below.
        if edit_index is not None:
            runs = self._extract_text_runs() if content.strip() else None
            new_stroke = None
        else:
            runs = None
            new_stroke = self._build_text_stroke(content, box) if content.strip() else None

        win_id = self._pending_text_window_id
        self._pending_text_widget = None
        self._pending_text_window_id = None
        self._pending_text_box = None
        self._pending_text_edit_index = None
        self.canvas.delete(win_id)
        tw.destroy()

        if edit_index is not None:
            # Re-editing an existing element - its own origin/w/h/rotation/
            # layer_id are left exactly as they were, only its content and
            # formatting change (see _begin_text_edit). Emptying it out
            # entirely removes it.
            stroke = self.edit_strokes[edit_index]
            if not content.strip():
                stroke["removed"] = True
                log.info(f"Text element {edit_index} emptied while re-editing - removed")
            else:
                stroke["text"] = content
                stroke["font_size"] = self.text_font_size
                stroke["font_family"] = self.text_font_family
                stroke["color"] = self.draw_color
                stroke["bg_fill"] = self.text_bg_fill
                stroke["text_align"] = self.text_align
                if any(r_11["color"] or r_11["font"] or r_11.get("size") for r_11 in runs):
                    stroke["runs"] = runs
                elif "runs" in stroke:
                    del stroke["runs"]
                log.debug(
                    f"Text element {edit_index} updated: {len(content)} chars, "
                    f"font_size={stroke['font_size']}, font={stroke['font_family']}, bg_fill={stroke['bg_fill']}, "
                    f"runs={len(stroke.get('runs', []))}"
                )
            self._edit_redo_stack = []
            self._recompute_baked_image()
            return "break"

        if new_stroke is not None:
            self._append_stroke(new_stroke)
            self._edit_redo_stack = []
            log.debug(
                f"Text stroke added: {len(content)} chars, origin={new_stroke['origin']}, w={new_stroke['w']:.0f}, h={new_stroke['h']:.0f}, "
                f"font_size={new_stroke['font_size']}, font={new_stroke['font_family']}, bg_fill={new_stroke['bg_fill']}, "
                f"runs={len(new_stroke.get('runs', []))}"
            )
            self._recompute_baked_image()
        return "break"

    def _cancel_text_entry(self, *a, **k):
        r = self._cancel_text_entry_base(*a, **k)
        self._p36_text_sweep()
        for ms in (80, 400):
            try:
                self.canvas.after(ms, lambda: self._p36_text_sweep())
            except Exception:
                pass
        return r

    def _cancel_text_entry_base(self, event=None):
        tw = self._pending_text_widget
        if tw is None:
            return "break"
        win_id = self._pending_text_window_id
        edit_index = self._pending_text_edit_index
        self._pending_text_widget = None
        self._pending_text_window_id = None
        self._pending_text_box = None
        self._pending_text_edit_index = None
        self.canvas.delete(win_id)
        tw.destroy()
        if edit_index is not None:
            # Discard whatever was typed - the element was only hidden
            # (not modified) while its widget was open, so re-bake to
            # bring its original, untouched rendering back.
            log.debug(f"Text element {edit_index}: re-edit cancelled, no changes kept")
            self._recompute_baked_image()
        return "break"

    def _begin_text_edit(self, index, *a, **k):
        self._text_focus_grace_until = time.monotonic() + 0.6
        r = self._begin_text_edit_wrap_begin_ui(index, *a, **k)
        tw = self._pending_text_widget
        if tw is not None:
            for ms in (30, 150, 350, 550):
                self.canvas.after(ms, lambda t=tw: t.focus_set() if self._pending_text_widget is t else None)
        return r

    def _insert_text_stroke_content(self, tw, stroke):
        content = stroke.get("text", "")
        tw.insert("1.0", content)
        runs = stroke.get("runs")
        if not runs:
            return
        idx = 0
        for run in runs:
            text_len = len(run.get("text", ""))
            if text_len and (run.get("color") or run.get("font") or run.get("size")):
                start = tw.index(f"1.0 + {idx} chars")
                end = tw.index(f"1.0 + {idx + text_len} chars")
                if run.get("color"):
                    tw.tag_add(self._text_color_tag(run["color"]), start, end)
                if run.get("font") or run.get("size"):
                    family = run.get("font") or self.text_font_family
                    size = run.get("size") or self.text_font_size
                    tw.tag_add(self._text_font_tag(family, size), start, end)
            idx += text_len

    def _edit_double_click(self, event):
        try:
            top = self.canvas.winfo_toplevel()
            log.debug(f"text double-click: tool={self.draw_tool} mode={self.text_mode} pending={self._pending_text_widget is not None} focus={self.canvas.focus_get()} displayof={top.focus_displayof()}")
            if top.focus_displayof() is None:
                top.focus_force()
        except Exception:
            pass
        r = self._edit_double_click_base(event)
        try:
            if self._pending_text_widget is not None:
                self._text_focus_grace_until = time.monotonic() + 1.5
                for ms in (20, 120, 300):
                    self.canvas.after(ms, lambda: self._refocus_main_v4())
        except Exception:
            pass
        return r

    def _edit_double_click_base(self, event):
        """
        Double-click, while the Text tool is in Select mode, reopens an
        existing text element for editing (see _begin_text_edit) instead
        of doing nothing extra beyond the ordinary move-drag the first
        click of the double-click already started. Gated to Select mode
        specifically (per request) so a double-click in Write mode still
        behaves like two ordinary clicks there - move, or start a new box
        - rather than unexpectedly interrupting text placement. Every
        other tool, and every other draw_tool value, ignores
        double-clicks entirely.
        """
        if self.draw_tool != "text" or self.text_mode != "select":
            return
        if self._pending_text_widget is not None:
            return   # already editing something - a stray double-click shouldn't reopen a second one
        x, y = self._event_to_capture_local(event)
        x, y = self.clamp_to_canvas(x, y)
        candidates = [
            i for i in range(len(self.edit_strokes) - 1, -1, -1)
            if self.edit_strokes[i].get("mode") == "text" and not self.edit_strokes[i].get("removed")
            and self._stroke_visible_for_editing(self.edit_strokes[i])
        ]
        for i in candidates:
            if self._point_hits_element(self.edit_strokes[i], x, y):
                self._begin_text_edit(i)
                return

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
        tw = self._pending_text_widget
        if tw is None:
            return
        stroke = self._build_text_stroke(tw.get("1.0", "end-1c"), self._pending_text_box)
        self.snippy_register = {"kind": "text_stroke", "stroke": stroke}
        log.info("Vim yank (Normal mode, no selection): copied the whole text box as an element")

    def _vim_move_hjkl(self, keysym):
        """
        vim-style h/j/k/l for the Text tool's Normal and Visual modes
        (never called in Insert mode, where these letters just type).
        h/l stay on the current line, like vim. j/k use Tk's own
        up/down routine so they follow wrapped display lines exactly like
        the arrow keys do, with a logical-line fallback if that routine
        isn't available on this Tk build.
        """
        tw = self._pending_text_widget
        if tw is None:
            return
        pos = tw.index("insert")
        line, col = (int(v) for v in pos.split("."))
        if keysym == "h":
            if col > 0:
                tw.mark_set("insert", f"{pos}-1c")
        elif keysym == "l":
            if tw.compare(pos, "<", "insert lineend"):
                tw.mark_set("insert", f"{pos}+1c")
        else:
            delta = 1 if keysym == "j" else -1
            try:
                tw.mark_set("insert", tw.tk.call("tk::TextUpDownLine", str(tw), delta))
            except tk.TclError:
                last_line = int(tw.index("end-1c").split(".")[0])
                if 1 <= line + delta <= last_line:
                    tw.mark_set("insert", f"insert {'+' if delta > 0 else '-'}1 lines")
        tw.see("insert")
        if self.text_vim_mode in ("visual", "visual_line"):
            self._vim_update_visual_selection()
        else:
            self._vim_update_block_cursor()

    def _vim_on_key(self, event):
        mode = getattr(self, "text_vim_mode", "insert")
        if mode != "insert" and event.keysym == "x" and not (event.state & 0x4):
            tw = self._pending_text_widget
            if tw is not None:
                if mode in ("visual", "visual_line"):
                    try:
                        cnt = len(tw.get("sel.first", "sel.last"))
                        tw.delete("sel.first", "sel.last")
                    except tk.TclError:
                        cnt = 0
                    log.info(f"Vim x: deleted {cnt} character(s) to void (register untouched)")
                    self._vim_enter_normal()
                else:
                    if tw.compare("insert", "<", "end-1c") and tw.get("insert") != "\n":
                        tw.delete("insert", "insert+1c")
                        log.info("Vim x: deleted 1 character to void (register untouched)")
                    self._vim_update_block_cursor()
            return "break"
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
        if keysym in ("h", "j", "k", "l"):
            self._vim_move_hjkl(keysym)
            return "break"
        if keysym in self._VIM_NAV_KEYSYMS:
            if self.text_vim_mode in ("visual", "visual_line"):
                self.root.after_idle(self._vim_update_visual_selection)
            else:
                self.root.after_idle(self._vim_update_block_cursor)
            return None
        if event.char:
            return "break"
        return None

    # ---- shapes ----

    # ---- dimension readout + alignment guides, shared by every drag that
    # changes a selection/element's size or position (shape/text/snip
    # creation, moving or resizing a placed element via its handles, and
    # the crop/background resize handles).

    ALIGN_TOLERANCE_SCREEN_PX = 6   # "snap-visible" tolerance, in ON-SCREEN pixels so it feels the same at any zoom level

    def _show_dim_overlay(self, screen_x, screen_y, text):
        # Shows (or moves/updates) a small floating label near (screen_x,
        # screen_y) - canvas-widget pixel coordinates, i.e. already run
        # through capturelocal_to_screen. Used for a live "123 x 45" (or
        # "37 deg" while rotating) readout during a drag. Two canvas items
        # (a background rectangle for legibility, then the text on top of
        # it) are created once and reused/moved on every subsequent call
        # during the same drag, rather than deleted and recreated every
        # frame.
        if self._dim_overlay_text_id is None:
            self._dim_overlay_bg_id = self.canvas.create_rectangle(
                0, 0, 0, 0, fill="#111111", outline="#00b4ff", width=1, tags=("dim_overlay",),
            )
            self._dim_overlay_text_id = self.canvas.create_text(
                0, 0, text="", fill="white", font=("", 10, "bold"), anchor="s", tags=("dim_overlay",),
            )
        # Anchored "s" (bottom-center) and nudged up a bit so the label
        # sits just above the cursor/handle rather than under it.
        label_x, label_y = screen_x, screen_y - 14
        self.canvas.coords(self._dim_overlay_text_id, label_x, label_y)
        self.canvas.itemconfigure(self._dim_overlay_text_id, text=text)
        bbox = self.canvas.bbox(self._dim_overlay_text_id)
        if bbox:
            pad = 4
            self.canvas.coords(self._dim_overlay_bg_id, bbox[0] - pad, bbox[1] - pad, bbox[2] + pad, bbox[3] + pad)
        self.canvas.tag_raise(self._dim_overlay_bg_id)
        self.canvas.tag_raise(self._dim_overlay_text_id)
        r = None
        try:
            self.canvas.tag_raise("dim_overlay")
            for _i in (getattr(self, "_dim_overlay_bg_id", None), getattr(self, "_dim_overlay_text_id", None)):
                if _i:
                    self.canvas.tag_raise(_i)
        except Exception:
            pass
        return r

    def _clear_dim_overlay(self):
        if self._dim_overlay_text_id is not None:
            self.canvas.delete(self._dim_overlay_bg_id)
            self.canvas.delete(self._dim_overlay_text_id)
            self._dim_overlay_bg_id = None
            self._dim_overlay_text_id = None

    def _clear_alignment_guides(self):
        for item_id in self._align_guide_ids:
            self.canvas.delete(item_id)
        self._align_guide_ids = []

    def _alignment_targets(self, exclude_index=None):
        """
        Every (kind, position) edge/center worth snapping/highlighting
        against, in capture-local coordinates: the current canvas/page
        bounds (left/right/h-center, top/bottom/v-center) plus the same
        six positions for every OTHER visible placed element (shape/text/
        snip - the same modes _element_bbox_capture_local understands),
        skipping whichever index is the one actually being dragged (so an
        element never "aligns with itself") and anything removed/hidden.
        Rotated elements use their unrotated bbox here, same tradeoff
        _element_bbox_capture_local already makes for Split's overlap
        test - good enough for a visual guide, not claiming pixel-perfect
        precision against a rotated edge.
        """
        targets_x = []   # (position, label) - vertical guide lines, i.e. an x-coordinate
        targets_y = []   # horizontal guide lines, i.e. a y-coordinate

        def add_bbox(bbox):
            x1, y1, x2, y2 = bbox
            targets_x.extend([x1, (x1 + x2) / 2, x2])
            targets_y.extend([y1, (y1 + y2) / 2, y2])

        add_bbox((self.canvas_x1, self.canvas_y1, self.canvas_x2, self.canvas_y2))
        for i, stroke in enumerate(self.edit_strokes):
            if i == exclude_index or stroke.get("removed"):
                continue
            if not self._stroke_visible_for_editing(stroke):
                continue
            bbox = self._element_bbox_capture_local(stroke)
            if bbox is not None:
                add_bbox(bbox)
        return targets_x, targets_y

    def _show_alignment_guides(self, moving_bbox, exclude_index=None):
        self._clear_alignment_guides()
        from . import constants as C
        if not getattr(C, "ALIGN_GUIDES_ENABLED", True):
            return
        mx1, my1, mx2, my2 = moving_bbox
        moving_xs = [mx1, (mx1 + mx2) / 2, mx2]
        moving_ys = [my1, (my1 + my2) / 2, my2]
        targets_x, targets_y = self._alignment_targets(exclude_index=exclude_index)
        tol_px = float(getattr(C, "ALIGN_GUIDE_TOLERANCE_PX", self.ALIGN_TOLERANCE_SCREEN_PX))
        tol = tol_px / max(self.display_scale, 0.01)
        vx1, vy1 = self.capturelocal_to_screen(self.canvas_x1, self.canvas_y1)
        vx2, vy2 = self.capturelocal_to_screen(self.canvas_x2, self.canvas_y2)

        def nearest(mv, targets):
            best = None
            for t in targets:
                d = abs(mv - t)
                if d <= tol and (best is None or d < best[0]):
                    best = (d, t)
            return None if best is None else round(best[1], 1)

        matched_x = {nearest(v, targets_x) for v in moving_xs} - {None}
        matched_y = {nearest(v, targets_y) for v in moving_ys} - {None}
        for tx in matched_x:
            sx, _ = self.capturelocal_to_screen(tx, 0)
            self._align_guide_ids.append(self.canvas.create_line(
                sx, min(vy1, vy2) - 40, sx, max(vy1, vy2) + 40, fill="#ff33cc", dash=(3, 3), width=1, tags=("align_guide",)))
        for ty in matched_y:
            _, sy = self.capturelocal_to_screen(0, ty)
            self._align_guide_ids.append(self.canvas.create_line(
                min(vx1, vx2) - 40, sy, max(vx1, vx2) + 40, sy, fill="#ff33cc", dash=(3, 3), width=1, tags=("align_guide",)))

    def _draw_shape_preview(self, start, end):
        if self.shape_preview_item_id:
            self.canvas.delete(self.shape_preview_item_id)
            self.shape_preview_item_id = None
        cx1_2, cy1_2 = self.capturelocal_to_screen(*start)
        cx2_2, cy2_2 = self.capturelocal_to_screen(*end)
        width = self._scaled_width(self.pen_width)
        # Preview always reflects the ACTUAL current selections.
        if self.shape_type == "line":
            self.shape_preview_item_id = self.canvas.create_line(
                cx1_2, cy1_2, cx2_2, cy2_2, fill=self.draw_color, width=width, capstyle=tk.ROUND,
            )
        elif self.shape_type == "ellipse":
            self.shape_preview_item_id = self.canvas.create_oval(
                cx1_2, cy1_2, cx2_2, cy2_2, outline=self.draw_color, width=width, fill=self.shape_fill or "",
            )
        else:
            self.shape_preview_item_id = self.canvas.create_rectangle(
                cx1_2, cy1_2, cx2_2, cy2_2, outline=self.draw_color, width=width, fill=self.shape_fill or "",
            )
        x1, y1, x2, y2 = min(start[0], end[0]), min(start[1], end[1]), max(start[0], end[0]), max(start[1], end[1])
        w, h = round(x2 - x1), round(y2 - y1)
        self._show_dim_overlay(cx2_2, cy2_2, f"{w} x {h}")
        self._show_alignment_guides((x1, y1, x2, y2))
        try:
            cx1, cy1 = self.capturelocal_to_screen(*start)
            cx2, cy2 = self.capturelocal_to_screen(*end)
            self.canvas.delete("shape_drag_ui")
            self.canvas.create_rectangle(cx1, cy1, cx2, cy2, outline="#00b4ff", width=2, dash=(5, 3), fill="", tags=("shape_drag_ui",))
        except Exception:
            log.exception("shape drag outline failed (non-fatal)")

    def _cancel_pending_shape(self):
        try:
            self.canvas.delete("shape_drag_ui")
        except Exception:
            pass
        if self.shape_preview_item_id:
            self.canvas.delete(self.shape_preview_item_id)
            self.shape_preview_item_id = None
        self.shape_pending_start = None
        self._clear_dim_overlay()
        self._clear_alignment_guides()

    def _finalize_shape(self, x, y):
        try:
            self.canvas.delete("shape_drag_ui")
        except Exception:
            pass
        start = self.shape_pending_start
        end = (x, y)
        if self.shape_preview_item_id:
            self.canvas.delete(self.shape_preview_item_id)
            self.shape_preview_item_id = None
        self.shape_pending_start = None
        self._clear_dim_overlay()
        self._clear_alignment_guides()

        cx1, cy1 = self.capturelocal_to_screen(*start)
        cx2, cy2 = self.capturelocal_to_screen(*end)
        if self.shape_type != "line" and abs(cx2 - cx1) < 4 and abs(cy2 - cy1) < 4:
            log.info("Shape finalize: too small (accidental click?) - discarding")
            return
        if self.shape_type == "line" and start == end:
            return

        x1, y1 = min(start[0], end[0]), min(start[1], end[1])
        w = max(4, abs(end[0] - start[0]))
        h = max(4, abs(end[1] - start[1]))
        start_frac = ((start[0] - x1) / w if w else 0.0, (start[1] - y1) / h if h else 0.0)
        end_frac = ((end[0] - x1) / w if w else 1.0, (end[1] - y1) / h if h else 1.0)

        stroke = {
            "mode": "shape", "shape_type": self.shape_type,
            "origin": (x1, y1), "w": w, "h": h, "rotation": 0.0,
            "start_frac": start_frac, "end_frac": end_frac,
            "color": self.draw_color, "width": self.pen_width,
            "fill": (self.shape_fill if self.shape_type != "line" else None),
            "segment_ids": [],
        }
        self._append_stroke(stroke)
        self._edit_redo_stack = []
        log.debug(f"Shape stroke added: {self.shape_type} {start}->{end} color={self.draw_color} fill={stroke['fill']} width={self.pen_width}")
        self._recompute_baked_image()

    # ---- Draw's own Select mode: rubber-band group-and-lift ----
    #
    # Pen/Highlight/Spray strokes are raw point lists with no natural
    # "grab this one thing" model of their own - Snip/Shape/Text all have
    # a rectangle-like footprint to click on, raster ink doesn't. So
    # instead of picking a single stroke, Select mode here draws a
    # rubber-band rectangle; whatever raster strokes it catches (by their
    # own bounding box - see _element_bbox_capture_local, already shared
    # with Split for exactly this kind of overlap test) get rasterized
    # together into ONE new transformable element and removed from
    # edit_strokes as individual strokes. That element is stored as an
    # ordinary mode "snip" stroke (orig_bounds=None, same as a pasted
    # image - there's nothing to punch a hole for, since these strokes
    # were never part of the original capture) marked "lifted_from_draw"
    # so _draw_snip_ui/the Draw-Select hit-test can find it - it otherwise
    # reuses every bit of Snip's own move/resize/rotate/render machinery
    # unchanged. Eraser strokes are deliberately excluded from grouping:
    # an eraser mark isn't really a standalone drawn "thing", it's a hole
    # punched in whatever ink is underneath, which has no sensible
    # interpretation as an independently movable image.

    def _finish_draw_select_rect(self, x, y):
        x0, y0 = self._draw_select_rect_start
        self._cancel_draw_select_rect()
        rect = (min(x0, x), min(y0, y), max(x0, x), max(y0, y))
        if rect[2] - rect[0] < 4 or rect[3] - rect[1] < 4:
            return   # too small to be a deliberate drag - treat as a plain click, select nothing
        raster_modes = ("pen", "highlight", "spray")
        caught = []
        for i, stroke in enumerate(self.edit_strokes):
            if stroke.get("mode") not in raster_modes or stroke.get("removed"):
                continue
            if not self._stroke_visible_for_editing(stroke):
                continue
            bbox = self._element_bbox_capture_local(stroke)
            if bbox is not None and self._rects_overlap(bbox, rect):
                caught.append(i)
        if not caught:
            log.info("Draw Select: no strokes were inside the selection - nothing to group")
            return
        self._group_strokes_into_transformable(caught)

    def _group_strokes_into_transformable(self, indices):
        from PIL import Image
        strokes = [self.edit_strokes[i] for i in indices]
        boxes = [self._element_bbox_capture_local(s) for s in strokes]
        x1 = min(b[0] for b in boxes)
        y1 = min(b[1] for b in boxes)
        x2 = max(b[2] for b in boxes)
        y2 = max(b[3] for b in boxes)
        bw = max(1, int(round(x2 - x1)))
        bh = max(1, int(round(y2 - y1)))
        patch = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
        for stroke in strokes:
            for seg_id in stroke.get("segment_ids", []):
                self.canvas.delete(seg_id)
            if stroke.get("mode") == "spray":
                self._bake_spray_stroke(patch, stroke, x1, y1)
            else:
                patch = self._draw_one_stroke(patch, stroke, x1, y1)
        layer_id = strokes[0].get("layer_id")
        for i in sorted(indices, reverse=True):
            del self.edit_strokes[i]
        new_stroke = {
            "mode": "snip", "shape": "box",
            "orig_bounds": None, "orig_freeform_points": None,
            "extracted_image": patch,
            "base_w": bw, "base_h": bh,
            "origin": (x1, y1), "w": float(bw), "h": float(bh), "rotation": 0.0,
            "hole_scope": "full_stack",
            "lifted_from_draw": True,
            "layer_id": layer_id,
            "segment_ids": [],
        }
        self.edit_strokes.append(new_stroke)
        self._edit_redo_stack = []
        self._snip_focused_index = len(self.edit_strokes) - 1
        log.info(f"Draw Select: grouped {len(indices)} stroke(s) into one movable/resizable element ({bw}x{bh})")
        self._recompute_baked_image()
        self._draw_snip_ui()
        self._refresh_layers_panel_if_open()

    def pick_color(self, color):
        if self._pending_text_widget is not None and self._apply_text_span_format("color", color):
            return
        self.draw_color = color
        if self._pending_text_widget is not None:
            try:
                self._pending_text_widget.config(fg=color, insertbackground=color)
            except tk.TclError:
                pass
        self.show_edit_toolbar()

    # ---- "select mode" for Snip / Draw / Shape / Text ----
    #
    # Each of these four tools now has an explicit "Select" state alongside
    # its own action(s), rather than always both drawing new elements AND
    # moving existing ones from the same click:
    #   - Snip:  self.snip_shape_type == "select"
    #   - Draw:  self.pen_variant == "select"
    #   - Shape: self.shape_type == "select"
    #   - Text:  self.text_mode == "select"
    # For the three transformable-element tools (Snip/Shape/Text), Select
    # still lets you move/resize/rotate an EXISTING element of that type
    # (the hit-test-and-drag path runs first regardless of mode - see
    # edit_press/_snip_press), it just refuses to start a brand-new one
    # when nothing was hit. For Draw, whose strokes are raster ink with no
    # movable "element" model, Select simply means "don't draw" - a safe,
    # deliberate default so switching tools never sketches by accident.
    # "Select" is also now the DEFAULT the first time each tool is used in
    # a given window, replacing the old always-ready-to-draw defaults.
    #
    # The former separate rows of type buttons (Snip's Box/Ellipse/
    # Free-form, Draw's Pen/Spray Can/Highlighter/Eraser, Shape's Line/Box/
    # Ellipse) are each now ONE dropdown selector with "Select" as their
    # first entry - see show_edit_toolbar's context_row for Snip/Draw/Shape,
    # and the dedicated Font/Fill dropdowns plus the Mode dropdown for Text.
    # Highlighter used to be its own separate top-level tool button; it's
    # folded into Draw's own Mode dropdown now, alongside Pen/Spray/Eraser,
    # since it's really just another raster-ink variant with no "element"
    # of its own, exactly like those three - not a fundamentally different
    # tool the way Snip/Shape/Text are.

    def pick_tool(self, tool):
        self._cancel_pending_shape()
        self._cancel_pending_snip_shape()
        self._cancel_draw_select_rect()
        self.draw_tool = tool
        self.show_edit_toolbar()

    def pick_shape_type(self, shape_type):
        self._cancel_pending_shape()
        self.shape_type = shape_type
        self.show_edit_toolbar()

    def set_shape_fill(self, color):
        self.shape_fill = color
        self.show_edit_toolbar()

    def set_text_mode(self, mode):
        self.text_mode = mode
        self.show_edit_toolbar()

    def set_text_font_family(self, family):
        if self._pending_text_widget is not None and self._apply_text_span_format("font", family):
            return
        self.text_font_family = family
        if self._pending_text_widget is not None:
            try:
                self._pending_text_widget.config(font=(TEXT_FONT_FAMILIES[family]["tk"], self._pending_text_tk_size))
            except tk.TclError:
                pass
        self.show_edit_toolbar()

    # ---- snip tool ----

    def _pick_snip_shape(self, choice):
        self._cancel_pending_snip_shape()
        self.snip_shape_type = choice
        self.show_edit_toolbar()

    def _cancel_pending_snip_shape(self):
        self._clear_snip_preview()
        self.snip_pending_start = None
        self.snip_pending_points = None
        self._clear_dim_overlay()
        self._clear_alignment_guides()
        self._snip_drag_mode = None
        self._snip_drag_index = None

    def _cancel_draw_select_rect(self):
        if self._draw_select_rect_item_id is not None:
            try:
                self.canvas.delete(self._draw_select_rect_item_id)
            except tk.TclError:
                pass
            self._draw_select_rect_item_id = None
        self._draw_select_rect_start = None

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
        self._clear_crop_handles()

    # ---- crop handles (canvas-bounds crop, available directly in any
    # tool's own Select mode - distinct from an element's own resize
    # handles and from the separate Resize screen) ----

    def _crop_handles_enabled(self):
        """
        Crop handles are shown whenever the CURRENT tool's own mode is
        "Select" - Select already means "not currently placing/drawing
        something new, just manipulating what's there", and a
        whole-canvas crop fits that same moment naturally in any tool,
        not just Snip (which is where it's guaranteed to appear, per the
        original ask - "at minimum in Snip's Select mode").
        """
        if self.resize_mode_active:
            return False
        if self.draw_tool == "snip":
            return self.snip_shape_type == "select"
        if self.draw_tool == "shape":
            return self.shape_type == "select"
        if self.draw_tool == "text":
            return self.text_mode == "select"
        if self.draw_tool == "pen":
            return self.pen_variant == "select"
        return False

    def _clear_crop_handles(self):
        for item_id, _name in self._crop_handle_ids:
            try:
                self.canvas.delete(item_id)
            except tk.TclError:
                pass
        self._crop_handle_ids = []

    def _draw_crop_handles(self):
        """
        Draws the 4 canvas-corner crop handles, styled exactly like the
        pre-capture selection handles (draw_handles) - same shape (a
        filled rectangle) and color - so they read as "the same kind of
        thing" to drag, just now cropping the canvas instead of the
        not-yet-captured selection. Deliberately a plain rectangle in
        #00b4ff, NOT the oval/orange style _draw_single_snip_ui uses for
        an element's own handles, so the two are never visually confused
        with each other even when they happen to land near the same spot.
        """
        self._clear_crop_handles()
        if not self._crop_handles_enabled():
            return
        cw, ch = self._canvas_dims()
        ox, oy = self._bg_handle_drag_offset   # see _live_update_background_handle
        cx1, cy1 = self.img_to_canvas(0, 0)
        cx2, cy2 = self.img_to_canvas(cw, ch)
        corners = {"nw": (cx1 + ox, cy1 + oy), "ne": (cx2 + ox, cy1 + oy), "sw": (cx1 + ox, cy2 + oy), "se": (cx2 + ox, cy2 + oy)}
        for name, (hx, hy) in corners.items():
            hid = self.canvas.create_rectangle(
                hx - HANDLE_SIZE, hy - HANDLE_SIZE, hx + HANDLE_SIZE, hy + HANDLE_SIZE,
                fill="#00b4ff", outline="white",
            )
            self._crop_handle_ids.append((hid, name))

    def _hit_crop_handle(self, screen_x, screen_y):
        """
        Computed directly from the canvas's own current bounds (the same
        math _draw_crop_handles uses to place them), NOT by reading back
        the coords of the drawn handle canvas items - every press handler
        (edit_press/_snip_press) calls _hide_snip_ui() right at the top,
        BEFORE any hit-testing happens, which deletes the crop handle
        items immediately. Hit-testing against those already-deleted
        items always found nothing, silently swallowing every click - the
        handles would reappear on the next redraw (making them look
        "there" again) but never actually grabbed a drag. Deriving the
        hit-test from geometry instead of from live canvas items sidesteps
        that ordering entirely, matching how element hit-testing already
        works from stored stroke geometry rather than from its own drawn
        handle items.
        """
        if not self._crop_handles_enabled():
            return None
        cw, ch = self._canvas_dims()
        cx1, cy1 = self.img_to_canvas(0, 0)
        cx2, cy2 = self.img_to_canvas(cw, ch)
        corners = {"nw": (cx1, cy1), "ne": (cx2, cy1), "sw": (cx1, cy2), "se": (cx2, cy2)}
        for name, (hx, hy) in corners.items():
            if hx - HANDLE_SIZE <= screen_x <= hx + HANDLE_SIZE and hy - HANDLE_SIZE <= screen_y <= hy + HANDLE_SIZE:
                return name
        return None

    def _crop_press(self, screen_x, screen_y):
        """
        Called as the LAST fallback from each tool's own Select-mode press
        handler (only once nothing else - an element's own body/handle -
        was hit), so an element sitting right at the canvas edge always
        wins the click over the crop handle underneath it.
        """
        if not self._crop_handles_enabled():
            return False
        name = self._hit_crop_handle(screen_x, screen_y)
        if not name:
            return False
        self._crop_drag_active_handle = name
        self._resize_drag_snapshotted = False
        self._bg_drag_preview_base = None
        self._bg_handle_drag_offset = (0.0, 0.0)
        # Anchor captured once, here, before anything about the drag can
        # have shifted canvas_x1/canvas_y1 - see _anchored_capture_local
        # for why re-deriving this fresh from canvas_x1/y1 on every
        # subsequent move event (the previous approach) caused the crop
        # handles to run away from the cursor, accelerating every frame.
        self._crop_drag_anchor_screen = (screen_x, screen_y)
        self._crop_drag_anchor_capture_local = self.canvaslocal_to_capturelocal(*self.canvas_to_img(screen_x, screen_y))
        # The canvas's own bounds AT THE START of this gesture - needed
        # only for the proportional-lock math below, to know the
        # aspect ratio being preserved and which corner is the fixed
        # anchor to scale from (see _crop_drag).
        self._crop_drag_orig_bounds = (self.canvas_x1, self.canvas_y1, self.canvas_x2, self.canvas_y2)
        log.debug(f"Crop handle grabbed: {name}")
        return True

    # Axis-sign convention for each crop handle relative to its OWN
    # opposite-corner anchor, keyed the same as
    # _live_update_background_handle's own nw/ne/sw/se branches - see
    # _crop_drag's proportional-lock block.
    _CROP_HANDLE_AXIS_SIGN = {"se": (1, 1), "ne": (1, -1), "sw": (-1, 1), "nw": (-1, -1)}

    def _crop_drag(self, event):
        """
        Uses the anchor captured in _crop_press (see
        _anchored_capture_local) rather than converting `event` through
        the CURRENT canvas_x1/canvas_y1 directly - those move mid-drag
        for a crop handle (that's the whole point), which corrupts a
        direct per-event conversion into runaway acceleration. The
        resulting capture-local position is deliberately UNCLAMPED (not
        run through clamp_to_canvas) - unlike drawing tools, a crop
        handle needs to be draggable past the CURRENT canvas bounds in
        order to grow the canvas back out again, exactly like the Resize
        screen's own handle drag already is.
        """
        name = self._crop_drag_active_handle
        if not name:
            return
        capture_local_x, capture_local_y = self._anchored_capture_local(
            self._crop_drag_anchor_screen, self._crop_drag_anchor_capture_local, event,
        )
        if self.proportional_resize_var.get() and self._crop_drag_orig_bounds is not None:
            ox1, oy1, ox2, oy2 = self._crop_drag_orig_bounds   # _p40: these were never assigned (NameError on every drag)
            w0 = max(1.0, ox2 - ox1)
            h0 = max(1.0, oy2 - oy1)
            opp_corners = {"nw": (ox2, oy2), "ne": (ox1, oy2), "sw": (ox2, oy1), "se": (ox1, oy1)}
            anchor_x, anchor_y = opp_corners[name]
            sign_x, sign_y = self._CROP_HANDLE_AXIS_SIGN[name]
            new_w = abs(capture_local_x - anchor_x)
            new_h = abs(capture_local_y - anchor_y)
            factor = max(new_w / w0, new_h / h0, 4 / min(w0, h0))
            capture_local_x = anchor_x + sign_x * w0 * factor
            capture_local_y = anchor_y + sign_y * h0 * factor
        self._live_update_background_handle(name, capture_local_x, capture_local_y, self._crop_drag_orig_bounds)
        self._draw_crop_handles()
        if self._dim_overlay_text_id is not None:
            self.canvas.tag_raise(self._dim_overlay_bg_id)
            self.canvas.tag_raise(self._dim_overlay_text_id)

    def _crop_release(self):
        name = self._crop_drag_active_handle
        self._crop_drag_active_handle = None
        if not name:
            return
        self._bg_drag_preview_base = None
        self._bg_handle_drag_offset = (0.0, 0.0)
        self._clear_dim_overlay()
        self._finish_background_resize()
        log.info(f"Cropped via handle ({name}) to {self._canvas_dims()}")

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
                xs = [p[0] for p in points]
                ys = [p[1] for p in points]
                self._show_dim_overlay(flat[-2], flat[-1], f"{round(max(xs) - min(xs))} x {round(max(ys) - min(ys))}")
            return
        cx1, cy1 = self.capturelocal_to_screen(*start)
        cx2, cy2 = self.capturelocal_to_screen(*end)
        if self.snip_shape_type == "ellipse":
            item = self.canvas.create_oval(cx1, cy1, cx2, cy2, outline="#00b4ff", width=2, dash=(4, 2))
        else:
            item = self.canvas.create_rectangle(cx1, cy1, cx2, cy2, outline="#00b4ff", width=2, dash=(4, 2))
        self.snip_preview_item_ids = [item]
        x1, y1, x2, y2 = min(start[0], end[0]), min(start[1], end[1]), max(start[0], end[0]), max(start[1], end[1])
        self._show_dim_overlay(cx2, cy2, f"{round(x2 - x1)} x {round(y2 - y1)}")
        self._show_alignment_guides((x1, y1, x2, y2))

    _HANDLE_AXES = {
        "nw": (True, True), "ne": (True, True), "sw": (True, True), "se": (True, True),
        "n": (False, True), "s": (False, True), "e": (True, False), "w": (True, False),
    }
    _CORNER_HANDLES = ("nw", "ne", "sw", "se")

    def _snip_corner_offsets(self, stroke):
        w = stroke["w"]
        h = stroke["h"]
        rotate_gap = 24 / max(self.display_scale, 0.01)
        return {
            "nw": (-w / 2, -h / 2), "ne": (w / 2, -h / 2),
            "sw": (-w / 2, h / 2), "se": (w / 2, h / 2),
            "n": (0, -h / 2), "s": (0, h / 2), "e": (w / 2, 0), "w": (-w / 2, 0),
            "rotate": (0, -h / 2 - rotate_gap),
        }

    def _snip_handle_positions(self, stroke):
        ox, oy = stroke["origin"]
        w = stroke["w"]
        h = stroke["h"]
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

    @staticmethod
    def _dist_to_segment(px, py, ax, ay, bx, by):
        dx, dy = bx - ax, by - ay
        length_sq = dx * dx + dy * dy
        if length_sq == 0:
            return math.hypot(px - ax, py - ay)
        t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_sq))
        return math.hypot(px - (ax + t * dx), py - (ay + t * dy))

    @staticmethod
    def _ellipse_radius_at(lx, ly, hw, hh):
        if hw <= 0 or hh <= 0:
            return 0.0
        if lx == 0 and ly == 0:
            return min(hw, hh)
        t = math.atan2(ly, lx)
        return 1.0 / math.hypot(math.cos(t) / hw, math.sin(t) / hh)

    def _point_hits_element(self, stroke, x, y):
        ox, oy = stroke["origin"]
        w = stroke["w"]
        h = stroke["h"]
        hw, hh = w / 2, h / 2
        theta = math.radians(stroke.get("rotation", 0.0))
        cos_t, sin_t = math.cos(theta), math.sin(theta)
        dx, dy = x - (ox + hw), y - (oy + hh)
        lx = dx * cos_t + dy * sin_t
        ly = -dx * sin_t + dy * cos_t
        tol = SHAPE_HIT_TOLERANCE_PX / max(self.display_scale, 0.01)
        mode = stroke.get("mode")
        if mode == "shape":
            return self._shape_hit_local(stroke, lx, ly, hw, hh, tol)
        if mode == "snip":
            shape = stroke.get("shape")
            if shape == "ellipse":
                return math.hypot(lx, ly) <= self._ellipse_radius_at(lx, ly, hw, hh) + tol
            if shape == "freeform":
                img = stroke.get("extracted_image")
                if img is None or w <= 0 or h <= 0:
                    return False
                u = int((lx + hw) / w * img.width)
                v = int((ly + hh) / h * img.height)
                if not (0 <= u < img.width and 0 <= v < img.height):
                    return False
                return img.getpixel((u, v))[3] > 0
        return abs(lx) <= hw and abs(ly) <= hh

    def _shape_hit_local(self, stroke, lx, ly, hw, hh, tol):
        width = max(1, int(stroke.get("width", EDIT_PEN_WIDTH)))
        shape_type = stroke.get("shape_type", "box")
        band = width / 2 + tol
        if shape_type == "line":
            fx1, fy1 = stroke.get("start_frac", (0.0, 0.0))
            fx2, fy2 = stroke.get("end_frac", (1.0, 1.0))
            ax, ay = (fx1 - 0.5) * 2 * hw, (fy1 - 0.5) * 2 * hh
            bx, by = (fx2 - 0.5) * 2 * hw, (fy2 - 0.5) * 2 * hh
            return self._dist_to_segment(lx, ly, ax, ay, bx, by) <= band
        interior_grabs = bool(stroke.get("fill")) or SHAPE_HIT_UNFILLED_INTERIOR
        if shape_type == "ellipse":
            dist = math.hypot(lx, ly)
            rho = self._ellipse_radius_at(lx, ly, hw, hh)
            if interior_grabs:
                return dist <= rho + tol
            return abs(dist - (rho - width / 2)) <= width / 2 + tol
        if not (abs(lx) <= hw + tol and abs(ly) <= hh + tol):
            return False
        if interior_grabs:
            return True
        inner_hw, inner_hh = hw - width - tol, hh - width - tol
        return not (inner_hw > 0 and inner_hh > 0 and abs(lx) < inner_hw and abs(ly) < inner_hh)

    def _draw_single_snip_ui(self, stroke, is_focused=False):
        positions = self._snip_handle_positions(stroke)
        corner_pts = []
        for name in ("nw", "ne", "se", "sw"):
            sx, sy = self.capturelocal_to_screen(*positions[name])
            corner_pts.extend([sx, sy])
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
        r = None
        try:
            self.canvas.tag_raise("dim_overlay")
            for _i in (getattr(self, "_dim_overlay_bg_id", None), getattr(self, "_dim_overlay_text_id", None)):
                if _i:
                    self.canvas.tag_raise(_i)
        except Exception:
            pass
        return r

    def _draw_snip_ui(self, *a, **k):
        r = self._draw_snip_ui_base(*a, **k)
        try:
            self.canvas.tag_raise("dim_overlay")
            for _i in (getattr(self, "_dim_overlay_bg_id", None), getattr(self, "_dim_overlay_text_id", None)):
                if _i:
                    self.canvas.tag_raise(_i)
        except Exception:
            pass
        return r

    def _draw_snip_ui_base(self, only_index=None):
        """
        Redraws the border+handles for every TRANSFORMABLE ELEMENT whose
        mode matches the currently selected tool (Snip/Text/Shape) - or,
        mid-drag, only the one given by only_index. Draw is a special case.
        """
        self._clear_snip_handles()
        if only_index is None:
            self._draw_crop_handles()
        if self.resize_mode_active:
            return
        if self.draw_tool == "pen":
            if self.pen_variant != "select":
                return
            def matches(s):
                return s.get("mode") == "snip" and s.get("lifted_from_draw")
        elif self.draw_tool == "snip":
            if self.snip_shape_type == "select" and self._snip_select_all():
                def matches(s):
                    return s.get("mode") in ("snip", "text", "shape")
            else:
                def matches(s):
                    return s.get("mode") == "snip"
        elif self.draw_tool in ("text", "shape"):
            def matches(s):
                return s.get("mode") == self.draw_tool
        else:
            return
        for i, stroke in enumerate(self.edit_strokes):
            if not matches(stroke) or stroke.get("removed"):
                continue
            if only_index is not None and i != only_index:
                continue
            if not self._stroke_visible_for_editing(stroke):
                continue
            self._draw_single_snip_ui(stroke, is_focused=(i == self._snip_focused_index))

    def _start_snip_drag(self, index, mode, x, y):
        stroke = self.edit_strokes[index]
        self._snip_drag_index = index
        self._snip_focused_index = index
        self._snip_drag_mode = mode
        self._snip_drag_start = (x, y)
        self._snip_drag_orig_origin = stroke["origin"]
        self._snip_drag_orig_w = stroke["w"]
        self._snip_drag_orig_h = stroke["h"]
        self._snip_drag_orig_rotation = stroke["rotation"]
        self._snip_drag_base_image = self._build_composite_excluding_element(index)
        self._draw_snip_ui(only_index=index)

    def _snip_press(self, x, y, screen_x, screen_y):
        if self._hit_test_and_start_drag(x, y, screen_x, screen_y):
            return
        if self.snip_shape_type == "select":
            # Select mode: nothing existing was hit, and Select never
            # starts a new snip - see the "select mode" comment block.
            # Try a crop handle before giving up entirely (this is the
            # "at minimum in Snip's Select mode" guarantee for crop).
            self._snip_drag_mode = None
            self._snip_drag_index = None
            self._snip_focused_index = None
            self._crop_press(screen_x, screen_y)
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
                cx = stroke["origin"][0] + stroke["w"] / 2
                cy = stroke["origin"][1] + stroke["h"] / 2
                start_ang = math.degrees(math.atan2(sy - cy, sx - cx))
                cur_ang = math.degrees(math.atan2(y - cy, x - cx))
                stroke["rotation"] = self._snip_drag_orig_rotation + (cur_ang - start_ang)
            else:
                self._apply_snip_corner_resize(stroke, mode, x, y)
            # Dimension readout + alignment guides for whatever handle
            # this is - move and resize both show a live "w x h".
            cur_x, cur_y = self.capturelocal_to_screen(x, y)
            if mode == "rotate":
                self._clear_alignment_guides()
                self._show_dim_overlay(cur_x, cur_y, f"{stroke['rotation']:.0f} deg")
            else:
                ox, oy = stroke["origin"]
                bbox = (ox, oy, ox + stroke["w"], oy + stroke["h"])
                self._show_dim_overlay(cur_x, cur_y, f"{round(stroke['w'])} x {round(stroke['h'])}")
                self._show_alignment_guides(bbox, exclude_index=self._snip_drag_index)
            self._recompute_baked_image_live_drag()
            self._draw_snip_ui(only_index=self._snip_drag_index)
            return
        if self.snip_pending_start is not None:
            if self.snip_shape_type == "freeform":
                self.snip_pending_points.append((x, y))
                self._draw_snip_shape_preview(None, None, points=self.snip_pending_points)
            else:
                self._draw_snip_shape_preview(self.snip_pending_start, (x, y))

    def _apply_snip_corner_resize(self, stroke, handle, x, y):
        affects_w, affects_h = self._HANDLE_AXES.get(handle, (True, True))
        ox0, oy0 = self._snip_drag_orig_origin
        w0, h0 = self._snip_drag_orig_w, self._snip_drag_orig_h
        cx = ox0 + w0 / 2
        cy = oy0 + h0 / 2
        theta = math.radians(self._snip_drag_orig_rotation)
        cos_t, sin_t = math.cos(theta), math.sin(theta)
        dx_world = x - cx
        dy_world = y - cy
        local_dx = dx_world * cos_t + dy_world * sin_t
        local_dy = -dx_world * sin_t + dy_world * cos_t
        if handle in self._CORNER_HANDLES and self.proportional_resize_var.get():
            half_diag0 = math.hypot(w0 / 2, h0 / 2)
            factor = math.hypot(local_dx, local_dy) / half_diag0 if half_diag0 > 0 else 1.0
            factor = max(factor, 4 / max(1e-6, min(w0, h0)))
            new_w, new_h = w0 * factor, h0 * factor
            ncx, ncy = cx, cy
        else:
            from .resize_math import anchored_resize
            new_w, new_h, ncx, ncy = anchored_resize(
                handle, w0, h0, cx, cy, cos_t, sin_t, local_dx, local_dy, affects_w, affects_h)
        stroke["w"], stroke["h"] = new_w, new_h
        stroke["origin"] = (ncx - new_w / 2, ncy - new_h / 2)

    def _snip_release(self, x, y):
        if self._snip_drag_mode is not None or self._snip_drag_index is not None:
            self._snip_drag_mode = None
            self._snip_drag_index = None
            self._snip_drag_base_image = None
            self._clear_dim_overlay()
            self._clear_alignment_guides()
            self._recompute_baked_image()
            self._draw_snip_ui()
            return
        if self.snip_pending_start is None:
            return
        self._clear_dim_overlay()
        self._clear_alignment_guides()
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
        from PIL import Image, ImageDraw, ImageChops
        self._recompute_baked_image()
        if self.isolated_layer_id is not None:
            source = self._render_layered(exclude_index=None, layer_filter=self.isolated_layer_id)
        else:
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

        crop_rect = (x1, y1, x2, y2)
        consumed_eids = []
        partial_clips = {}   # target eid -> the hole dict added to it, for undo/redo - see _restore_consumed_elements
        for other in self.edit_strokes:
            if other.get("removed") or not self._stroke_visible_for_editing(other):
                continue
            bbox = self._element_bbox_capture_local(other)
            if bbox is None:
                continue
            fully_contained = (
                bbox[0] >= crop_rect[0] and bbox[1] >= crop_rect[1]
                and bbox[2] <= crop_rect[2] and bbox[3] <= crop_rect[3]
            )
            if fully_contained:
                other["removed"] = True
                other.setdefault("eid", uuid.uuid4().hex)
                consumed_eids.append(other["eid"])
            elif other.get("mode") in ("text", "shape", "pen", "highlight", "spray") \
                    and abs(other.get("rotation", 0.0)) <= 0.01 and self._rects_overlap(bbox, crop_rect):
                # patch 44: ink that only partly overlaps the snip is cut too (was: text/shape only)
                other.setdefault("eid", uuid.uuid4().hex)
                hole = {
                    "shape": shape_type, "bounds": (x1, y1, x2, y2),
                    "freeform_points": list(freeform_points) if freeform_points else None,
                }
                other.setdefault("snip_holes", []).append(hole)
                partial_clips[other["eid"]] = hole
                if other.get("mode") in ("pen", "highlight", "spray"):
                    # its live vector lines sit above the picture and would hide the cut
                    for seg in other.get("segment_ids", []) or []:
                        try:
                            self.canvas.delete(seg)
                        except Exception:
                            pass
                    other["segment_ids"] = []

        stroke = {
            "mode": "snip", "shape": shape_type,
            "orig_bounds": (x1, y1, x2, y2),
            "orig_freeform_points": list(freeform_points) if freeform_points else None,
            "extracted_image": cropped,
            "base_w": bw, "base_h": bh,
            "origin": (x1, y1), "w": float(bw), "h": float(bh), "rotation": 0.0,
            "hole_scope": "own_layer" if self.isolated_layer_id is not None else "full_stack",
            "consumed_eids": consumed_eids,
            "partial_clips": partial_clips,
            "segment_ids": [],
        }
        self._append_stroke(stroke)
        self._edit_redo_stack = []
        self._snip_focused_index = len(self.edit_strokes) - 1
        log.info(f"Snip created: {shape_type} {bw}x{bh} at capture-local ({x1:.0f},{y1:.0f}), consumed {len(consumed_eids)} underlying element(s)")
        self._recompute_baked_image()
        self._draw_snip_ui()
        self.show_edit_toolbar()

    def _punch_snip_hole(self, base, stroke, canvas_offset_x, canvas_offset_y, fill_rgba=None):
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
        fill = fill_rgba if fill_rgba is not None else self._canvas_bg_fill_rgba()
        fill_img = Image.new("RGBA", base.size, fill)
        return Image.composite(fill_img, base, mask)

    def _render_element_local_patch(self, stroke, w, h, fast=False):
        from PIL import Image, ImageDraw
        mode = stroke.get("mode")
        w = max(1, w)
        h = max(1, h)
        if mode == "snip":
            img = stroke["extracted_image"]
            if fast:
                resample = Image.NEAREST
            else:
                resample = Image.LANCZOS if (w <= img.width and h <= img.height) else Image.NEAREST
            return img.resize((w, h), resample)
        if mode == "text":
            patch = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            draw = ImageDraw.Draw(patch)
            bg = stroke.get("bg_fill")
            if bg == "white":
                draw.rectangle([0, 0, w - 1, h - 1], fill="white")
            elif bg == "black":
                draw.rectangle([0, 0, w - 1, h - 1], fill="black")
            elif bg == "white_border":
                draw.rectangle([0, 0, w - 1, h - 1], fill="white", outline="black", width=2)
            elif bg == "black_border":
                draw.rectangle([0, 0, w - 1, h - 1], fill="black", outline="white", width=2)
            draw_text_runs(draw, stroke, box_w=w)
            return patch
        if mode == "shape":
            patch = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            draw = ImageDraw.Draw(patch)
            color = stroke.get("color", "#000000")
            width = max(1, int(stroke.get("width", EDIT_PEN_WIDTH)))
            shape_type = stroke.get("shape_type", "box")
            fill = stroke.get("fill") or None
            if shape_type == "line":
                fx1, fy1 = stroke.get("start_frac", (0.0, 0.0))
                fx2, fy2 = stroke.get("end_frac", (1.0, 1.0))
                draw.line([fx1 * (w - 1), fy1 * (h - 1), fx2 * (w - 1), fy2 * (h - 1)], fill=color, width=width)
            elif shape_type == "ellipse":
                draw.ellipse([0, 0, w - 1, h - 1], fill=fill, outline=color, width=width)
            else:
                draw.rectangle([0, 0, w - 1, h - 1], fill=fill, outline=color, width=width)
            return patch
        return Image.new("RGBA", (w, h), (0, 0, 0, 0))

    def _draw_transformable_element(self, base, stroke, canvas_offset_x, canvas_offset_y, fast=False, preview_alpha=None):
        from PIL import Image
        w = max(1, int(round(stroke["w"])))
        h = max(1, int(round(stroke["h"])))
        rendered = self._render_element_local_patch(stroke, w, h, fast=fast)
        rotation = stroke.get("rotation", 0.0)
        # Punched BEFORE rotation, while `rendered`'s local (0,0) still
        # lines up 1:1 with capture-local (origin).
        snip_holes = stroke.get("snip_holes")
        if snip_holes and abs(rotation) <= 0.01:
            rendered = self._punch_element_local_holes(rendered, snip_holes, *stroke["origin"])
        if abs(rotation) > 0.01:
            rotate_resample = Image.NEAREST if fast else Image.BICUBIC
            rendered = rendered.rotate(-rotation, expand=True, resample=rotate_resample)
        if preview_alpha is not None and preview_alpha < 1.0:
            rendered = self._scale_patch_alpha(rendered, preview_alpha)
        ox, oy = stroke["origin"]
        center_x = ox + w / 2
        center_y = oy + h / 2
        paste_x = int(round(center_x - rendered.width / 2 - canvas_offset_x))
        paste_y = int(round(center_y - rendered.height / 2 - canvas_offset_y))
        base.paste(rendered, (paste_x, paste_y), rendered)

    def _punch_element_local_holes(self, patch, holes, elem_ox, elem_oy):
        """
        Zeroes out the alpha of a Text/Shape element's own rendered,
        UN-ROTATED local patch wherever a snip's cut region overlapped it
        (see _finalize_snip's "partially overlapping" branch).
        """
        from PIL import Image, ImageDraw, ImageChops
        mask = Image.new("L", patch.size, 0)
        draw = ImageDraw.Draw(mask)
        for hole in holes:
            shape = hole.get("shape")
            if shape == "freeform":
                pts = [(px - elem_ox, py - elem_oy) for px, py in hole.get("freeform_points") or []]
                if len(pts) >= 3:
                    draw.polygon(pts, fill=255)
            else:
                hx1, hy1, hx2, hy2 = hole["bounds"]
                box = [hx1 - elem_ox, hy1 - elem_oy, hx2 - elem_ox, hy2 - elem_oy]
                if shape == "ellipse":
                    draw.ellipse(box, fill=255)
                else:
                    draw.rectangle(box, fill=255)
        r, g, b, a = patch.split()
        a = ImageChops.multiply(a, ImageChops.invert(mask))
        return Image.merge("RGBA", (r, g, b, a))

    def _scale_patch_alpha(self, patch, factor):
        """
        Scales just the alpha channel of an already-rendered element patch
        by `factor` (0.0-1.0) - used to show a Snip/Text/Shape element
        translucent while one of its resize handles is actively being
        dragged (see RESIZE_DRAG_PREVIEW_ALPHA and
        _recompute_baked_image_live_drag), so the fast/low-quality live
        preview visibly reads as "still previewing, not final" rather than
        being mistaken for the real baked result.
        """
        from PIL import Image
        r, g, b, a = patch.split()
        a = a.point(lambda v: int(v * factor))
        return Image.merge("RGBA", (r, g, b, a))

    # ---- clipboard: snippy register + system register ----
    #
    # Two independent clipboards, wired to two independent sets of
    # keybinds (see _bind_clipboard_shortcuts): the "snippy" register is a
    # full-fidelity in-process copy (the actual stroke dict - mask, current
    # scale/rotation, etc), while the "system" register round-trips
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
        Scales a newly-created/pasted snip down (keeping its center fixed,
        aspect ratio preserved) if it's larger than max_fraction of the
        current canvas in either dimension - applies to both a fresh paste
        from the system clipboard and a snippy-register paste, so an
        oversized image never arrives larger than the canvas itself.
        """
        cw, ch = self._canvas_dims()
        max_w, max_h = cw * max_fraction, ch * max_fraction
        cur_w, cur_h = stroke["w"], stroke["h"]
        if cur_w <= max_w and cur_h <= max_h:
            return
        factor = max(0.001, min(max_w / cur_w, max_h / cur_h, 1.0))
        cx = stroke["origin"][0] + cur_w / 2
        cy = stroke["origin"][1] + cur_h / 2
        new_w = max(4, cur_w * factor)
        new_h = max(4, cur_h * factor)
        stroke["w"], stroke["h"] = new_w, new_h
        stroke["origin"] = (cx - new_w / 2, cy - new_h / 2)
        log.info(f"Pasted content was larger than the canvas - scaled down to fit (factor={factor:.3f})")

    def _set_paste_position_from_click(self, point):
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
        index = self._snip_focused_index
        if index is None or index >= len(self.edit_strokes) or self.edit_strokes[index].get("mode") != "snip":
            log.info("Delete-to-void pressed with no snip focused - nothing to do")
            return
        self._remove_snip_at(index)
        log.info("Snip deleted to void (not copied to either register)")
        if NOTIFY_ON_SNIP_DELETE:
            notify("Snip deleted", "Not copied to either register", blocking=False, anchor_window=self.root)

    def _remove_snip_at(self, index):
        self.edit_strokes[index]["removed"] = True
        if self._snip_focused_index == index:
            self._snip_focused_index = None
        self._recompute_baked_image()
        self._draw_snip_ui()
        self._refresh_layers_panel_if_open()

    def _copy_snip_to_system_clipboard(self, stroke):
        from PIL import Image
        w = max(1, int(round(stroke["w"])))
        h = max(1, int(round(stroke["h"])))
        rendered = self._render_element_local_patch(stroke, w, h)
        rotation = stroke.get("rotation", 0.0)
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
        w, h = new_stroke["w"], new_stroke["h"]
        default_center = (stroke["origin"][0] + w / 2 + 20, stroke["origin"][1] + h / 2 + 20)
        cx, cy = self._consume_snip_paste_position(default_center)
        new_stroke["origin"] = (cx - w / 2, cy - h / 2)
        self._fit_snip_to_canvas(new_stroke)
        new_stroke.pop("layer_id", None)
        self._append_stroke(new_stroke)
        self._edit_redo_stack = []
        self._snip_focused_index = len(self.edit_strokes) - 1
        self._recompute_baked_image()
        self._draw_snip_ui()
        log.info("Snip pasted from the snippy register")

    def _paste_image_as_snip(self, img):
        img = img.convert("RGBA")
        bw, bh = img.size
        default_center = ((self.canvas_x1 + self.canvas_x2) / 2, (self.canvas_y1 + self.canvas_y2) / 2)
        cx, cy = self._consume_snip_paste_position(default_center)
        stroke = {
            "mode": "snip", "shape": "box",
            "orig_bounds": None, "orig_freeform_points": None,
            "extracted_image": img, "base_w": bw, "base_h": bh,
            "origin": (cx - bw / 2, cy - bh / 2), "w": float(bw), "h": float(bh), "rotation": 0.0,
            "segment_ids": [],
        }
        self._fit_snip_to_canvas(stroke)
        self._append_stroke(stroke)
        self._edit_redo_stack = []
        self._snip_focused_index = len(self.edit_strokes) - 1
        self._recompute_baked_image()
        self._draw_snip_ui()
        log.info(f"Pasted an external image from the system clipboard as a new snip ({bw}x{bh})")

    def _paste_text_stroke_element(self, stroke):
        new_stroke = dict(stroke)
        x1, y1 = stroke["origin"]
        new_stroke["origin"] = (x1 + 20, y1 + 20)
        new_stroke["segment_ids"] = []
        new_stroke.pop("layer_id", None)
        self._append_stroke(new_stroke)
        self._edit_redo_stack = []
        self._recompute_baked_image()
        log.info("Pasted a text-box element from the register")

    def _paste_text_plain(self, text):
        if self._pending_text_widget is not None:
            self._pending_text_widget.insert("insert", text)
            log.info(f"Pasted {len(text)} character(s) of plain text into the active text box")
        else:
            self._paste_error("No text box is focused.")

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

    # ---- clipboard keybind dispatch ----
    #
    # Bound on self.canvas (not the root window).

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
        vs system register (and void-delete) by checking the RAW event.
        """
        is_super = bool(event.state & self._MOD4_SUPER_BIT)
        if action == "copy":
            return self._handle_copy_shortcut("system" if is_super else "snippy", event)
        if action == "cut":
            return self._handle_cut_shortcut("system" if is_super else "snippy", event)
        if action == "paste":
            return self._handle_paste_shortcut("system" if is_super else "snippy", event)
        if action == "void":
            return self._handle_delete_to_void(event)
        return None

    def _bind_clipboard_shortcuts(self):
        """
        Wires up both registers' keybinds per the app's dual-register
        scheme: standard Ctrl+C/X/V for the snippy register, Ctrl+Shift+
        C/X/V for the system register, and vi-style alt-binds (bare y/d/p/x,
        disambiguated by whether Super is ALSO held - see _on_vi_key).
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
        """
        Same two-tier pattern as set_text_font_family: if there's a live
        widget with an active highlight, the new size applies ONLY to
        that highlighted span (via _apply_text_span_format, which
        preserves whatever font FAMILY each affected sub-run already had.
        """
        try:
            new_size = max(6, min(200, int(float(val))))
        except (TypeError, ValueError):
            return False
        if self._pending_text_widget is not None and self._apply_text_span_format("size", new_size):
            return True
        self.text_font_size = new_size
        if self._pending_text_widget is not None:
            self._pending_text_tk_size = self._logical_to_tk_size(new_size)
            try:
                self._pending_text_widget.config(font=(TEXT_FONT_FAMILIES[self.text_font_family]["tk"], self._pending_text_tk_size))
            except tk.TclError:
                pass
        return False

    def set_text_bg_fill(self, choice):
        self.text_bg_fill = choice
        if self._pending_text_widget is not None:
            self._apply_text_fill_appearance(self._pending_text_widget, choice)
        self.show_edit_toolbar()
        r = None
        try:
            if self._pending_text_widget is not None:
                self._tp_sig = None
                self._tp_start()
            for ms in (40, 160, 400):
                self.canvas.after(ms, lambda: self._refocus_main_v4())
        except Exception:
            log.exception("fill switch handling failed (non-fatal)")
        return r

    def set_text_align(self, choice):
        self.text_align = choice
        if self._pending_text_widget is not None:
            tw = self._pending_text_widget
            try:
                tw.tag_configure("align", justify=choice)
                tw.tag_add("align", "1.0", "end")
            except tk.TclError:
                pass
        self.show_edit_toolbar()

    def set_pen_width(self, val):
        self.pen_width = int(float(val))

    def set_pen_variant(self, variant):
        self.pen_variant = variant
        self.show_edit_toolbar()

    def set_highlight_width(self, val):
        self.highlight_width = int(float(val))

    def cancel_edit(self):
        snap = self._pre_edit_state
        log.info(f"Edit cancelled, reverting to the state from when Edit was entered ({len(self.edit_strokes)} stroke(s) now, {len(snap['edit_strokes']) if snap else '?'} then)")
        if self._pending_text_widget is not None:
            self._cancel_text_entry()
        if self.text_current_box_id:
            self.canvas.delete(self.text_current_box_id)
            self.text_current_box_id = None
        self._cancel_pending_shape()
        self._cancel_pending_snip_shape()
        self._cancel_draw_select_rect()
        self._hide_snip_ui()
        if snap is not None:
            self._restore_full_state(snap, discard_newer_temp_files=True)
        self._pre_edit_state = None
        self.edit_current_stroke = None
        self.show_main_toolbar()

    def _pack_labeled_slider(self, parent, label_text, variable, from_, to_, command, resolution=None, slider_length=70, value_width=4, anchor="center"):
        tk.Label(parent, text=label_text, fg="white", bg="#222222").pack(side="left", anchor=anchor, padx=2, pady=4)
        scale_kwargs = {"resolution": resolution} if resolution is not None else {}
        tk.Scale(
            parent, from_=from_, to=to_, orient="horizontal", length=slider_length, showvalue=False,
            bg="#222222", fg="white", troughcolor="#444444", highlightthickness=0,
            variable=variable, command=command, **scale_kwargs,
        ).pack(side="left", anchor=anchor, padx=2, pady=4)
        tk.Label(parent, textvariable=variable, fg="white", bg="#222222", width=value_width, anchor="w").pack(side="left", anchor=anchor, padx=(0, 8), pady=4)

    def _pack_proportional_checkbox(self, parent):
        tk.Checkbutton(
            parent, text="Proportional", variable=self.proportional_resize_var,
            bg="#222222", fg="white", selectcolor="#444444",
            activebackground="#222222", activeforeground="white",
        ).pack(side="right", padx=(2, 8), pady=4)

    def _pack_color_swatches(self, parent, colors, selected, command, none_button=None):
        if none_button is not None:
            label, is_selected, none_command = none_button
            tk.Button(
                parent, text=label, bg="#666666", fg="white", activebackground="#666666", activeforeground="white",
                relief=("sunken" if is_selected else "raised"), pady=BUTTON_PADY, command=none_command,
            ).pack(side="left", padx=2, pady=4)
        for c in colors:
            border = "white" if (selected is not None and c.lower() == selected.lower()) else "#222222"
            tk.Button(
                parent, bg=c, activebackground=c, width=2, height=1, padx=0, bd=1,
                highlightbackground=border, highlightthickness=2,
                command=lambda c=c: command(c),
            ).pack(side="left", padx=2, pady=4)

    def _pack_dropdown(self, parent, label_text, options, current_value, command, width=None):
        """
        Shared helper for the redesigned tool rows: a plain Label followed
        by a single tk.OptionMenu dropdown - what used to be a whole row of
        raised/sunken toggle buttons for Snip's shape, Draw's pen variant,
        Shape's shape type, and Text's font/fill/mode. `options` is a list
        of (value, display_label) pairs; the dropdown shows display_label
        but `command` is always called with the underlying value.
        """
        if label_text:
            tk.Label(parent, text=label_text, fg="white", bg="#222222").pack(side="left", padx=(2, 2))
        label_by_value = dict(options)
        value_by_label = {v: k for k, v in options}
        current_label = label_by_value.get(current_value, current_value)
        var = tk.StringVar(value=current_label)

        def _on_choice(chosen_label):
            command(value_by_label[chosen_label])

        menu = tk.OptionMenu(parent, var, *[lbl for _val, lbl in options], command=_on_choice)
        if width is not None:
            menu.config(width=width)
        menu.config(pady=BUTTON_PADY)
        menu.pack(side="left", padx=2, pady=4)
        return menu

    _FILL_SWATCH_COLORS = {
        "transparent": (None, None),
        "white": ("white", "white"),
        "black": ("black", "black"),
        "white_border": ("white", "black"),   # (center, border)
        "black_border": ("black", "white"),
    }

    def _build_fill_swatch(self, parent, value, width=70, height=20, bg="#222222"):
        canvas = tk.Canvas(parent, width=width, height=height, highlightthickness=0, bd=0, bg=bg)
        center, border = self._FILL_SWATCH_COLORS.get(value, (None, None))
        if center is None:
            canvas.create_rectangle(1, 1, width - 1, height - 1, fill="#3a3a3a", outline="#777777")
            canvas.create_text(width // 2, height // 2, text="None", fill="#bbbbbb", font=("TkDefaultFont", 8))
        elif center == border:
            canvas.create_rectangle(1, 1, width - 1, height - 1, fill=center, outline="#777777")
        else:
            canvas.create_rectangle(0, 0, width, height, fill=border, outline=border)
            pad = max(3, height // 4)
            canvas.create_rectangle(pad, pad, width - pad, height - pad, fill=center, outline=center)
        return canvas

    def _pack_fill_dropdown(self, parent, label_text, options, current_value, command):
        if label_text:
            tk.Label(parent, text=label_text, fg="white", bg="#222222").pack(side="left", padx=(2, 2))

        _probe_dropdown = tk.Menubutton(parent, text="X", pady=BUTTON_PADY, relief="raised", bd=1)
        _probe_dropdown.pack()
        _probe_dropdown.update_idletasks()
        fill_btn_h = _probe_dropdown.winfo_reqheight()
        _probe_dropdown.destroy()

        btn_frame = tk.Frame(parent, bg="#222222", bd=1, relief="raised", cursor="hand2")
        btn_frame.pack(side="left", padx=2, pady=4)
        label_by_value = dict(options)
        face_swatch = self._build_fill_swatch(btn_frame, current_value, width=54, height=18)
        face_swatch.pack(side="left", padx=(4, 1), pady=0)
        face_label = tk.Label(
            btn_frame, text=label_by_value.get(current_value, current_value) + " ▾",
            fg="white", bg="#222222",
        )
        face_label.pack(side="left", padx=(2, 6), pady=0)
        btn_frame.update_idletasks()
        extra = max(0, fill_btn_h - btn_frame.winfo_reqheight())
        if extra:
            pad_top = extra // 2
            pad_bottom = extra - pad_top
            face_swatch.pack_configure(pady=(pad_top, pad_bottom))
            face_label.pack_configure(pady=(pad_top, pad_bottom))

        popup_state = {"win": None}

        def _close_popup(*_args):
            win = popup_state.get("win")
            if win is not None:
                try:
                    win.destroy()
                except tk.TclError:
                    pass
                popup_state["win"] = None

        def _choose(value):
            _close_popup()
            command(value)

        def _open_popup(*_args):
            if popup_state.get("win") is not None:
                _close_popup()
                return
            win = tk.Toplevel(self.root)
            popup_state["win"] = win
            win.overrideredirect(True)
            win.configure(bg="#111111")
            x = btn_frame.winfo_rootx()
            y = btn_frame.winfo_rooty() + btn_frame.winfo_height()
            win.geometry(f"+{x}+{y}")
            outer = tk.Frame(win, bg="#111111", highlightthickness=1, highlightbackground="#666666")
            outer.pack()
            for value, label in options:
                is_selected = (value == current_value)
                row = tk.Frame(
                    outer, bg="#111111", highlightthickness=2,
                    highlightbackground=("white" if is_selected else "#111111"),
                )
                row.pack(side="top", fill="x", padx=2, pady=1)
                row_swatch = self._build_fill_swatch(row, value, width=54, height=18, bg="#111111")
                row_swatch.pack(side="left", padx=(2, 4), pady=2)
                row_label = tk.Label(row, text=label, fg="white", bg="#111111", anchor="w")
                row_label.pack(side="left", fill="x", expand=True, padx=(0, 8), pady=2)

                def _on_enter(_e, row=row):
                    row.configure(highlightbackground="white")

                def _on_leave(_e, row=row, is_selected=is_selected):
                    row.configure(highlightbackground="white" if is_selected else "#111111")

                for widget in (row, row_swatch, row_label):
                    widget.bind("<Enter>", _on_enter)
                    widget.bind("<Leave>", _on_leave)
                    widget.bind("<Button-1>", lambda _e, value=value: _choose(value))
            win.bind("<Escape>", _close_popup)
            win.bind("<FocusOut>", _close_popup)
            win.focus_force()

        btn_frame.bind("<Button-1>", _open_popup)
        face_swatch.bind("<Button-1>", _open_popup)
        return btn_frame

    def show_edit_toolbar(self):
        self.clear_toolbar()
        frame = self.new_toolbar_frame()
        self._edit_toolbar_frame = frame
        if self._split_mode_active:
            self._show_split_mode_toolbar(frame)
            return
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
        tk.Button(main_row, text="Done", pady=BUTTON_PADY, command=self.bake_edits).pack(side="left", padx=(10, 2), pady=4)
        tk.Button(main_row, text="Cancel", pady=BUTTON_PADY, command=self.cancel_edit).pack(side="left", padx=2, pady=4)
        self._build_status_row(main_row, frame)

        context_row = tk.Frame(frame, bg="#222222")
        context_row.pack(side="top", fill="x")

        if self.draw_tool == "snip":
            # Shape row collapsed into one "Mode" dropdown, Select first.
            snip_options = [("select", "Select"), ("box", "Box"), ("ellipse", "Ellipse"), ("freeform", "Free-form")]
            self._pack_dropdown(context_row, "Mode", snip_options, self.snip_shape_type, self._pick_snip_shape, width=9)
            self._pack_proportional_checkbox(context_row)
            if any(s.get("mode") == "snip" for s in self.edit_strokes):
                tk.Label(
                    context_row, text="(drag: move | corner: resize | top dot: rotate)",
                    fg="#aaaaaa", bg="#222222",
                ).pack(side="left", padx=(10, 0))
        elif self.draw_tool == "pen":
            # Type row collapsed into one "Mode" dropdown, Select first -
            # Select draws nothing at all (a safe default). Highlighter
            # merged in here too.
            pen_options = [("select", "Select"), ("pen", "Pen"), ("spray", "Spray Can"), ("highlight", "Highlighter"), ("eraser", "Eraser")]
            self._pack_dropdown(context_row, "Mode", pen_options, self.pen_variant, self.set_pen_variant, width=9)
            if self.pen_variant != "eraser":
                self._pack_color_swatches(context_row, PALETTE, self.draw_color, self.pick_color)
            if self.pen_variant == "highlight":
                self._pack_labeled_slider(context_row, "Width", tk.IntVar(value=self.highlight_width), 4, 60, self.set_highlight_width)
            elif self.pen_variant == "spray":
                self._pack_labeled_slider(context_row, "Width", tk.IntVar(value=self.spray_width), 1, 20, self.set_spray_width)
                self._pack_labeled_slider(context_row, "Spread", tk.IntVar(value=self.pen_width), 1, 30, self.set_pen_width)
            else:
                self._pack_labeled_slider(context_row, "Width", tk.IntVar(value=self.pen_width), 1, 30, self.set_pen_width)
        elif self.draw_tool == "text":
            self._build_text_context_row(context_row)
        elif self.draw_tool == "shape":
            # Type row collapsed into one "Mode" dropdown, Select first -
            # Select only allows moving/resizing an existing shape. See the
            # "select mode" comment block.
            shape_options = [("select", "Select"), ("line", "Line"), ("box", "Box"), ("ellipse", "Ellipse")]
            self._pack_dropdown(context_row, "Mode", shape_options, self.shape_type, self.pick_shape_type, width=9)
            tk.Label(context_row, text="Border", fg="white", bg="#222222").pack(side="left", padx=(8, 2))
            self._pack_color_swatches(context_row, PALETTE, self.draw_color, self.pick_color)
            tk.Frame(context_row, width=8, bg="#222222").pack(side="left")
            self._pack_labeled_slider(context_row, "Width", tk.IntVar(value=self.pen_width), 1, 30, self.set_pen_width)
            tk.Label(context_row, text="Fill", fg="white", bg="#222222").pack(side="left", padx=2)
            self._pack_color_swatches(
                context_row, FILL_PALETTE, self.shape_fill, self.set_shape_fill,
                none_button=("None", self.shape_fill is None, lambda: self.set_shape_fill(None)),
            )
            self._pack_proportional_checkbox(context_row)

        self.finish_toolbar_placement(frame)
        self._draw_snip_ui()
        self._set_window_title("edit")

    def _build_text_context_row(self, context_row):
        mode_options = [("select", "Select"), ("write", "Write")]
        self._pack_dropdown(context_row, "Mode", mode_options, self.text_mode, self.set_text_mode, width=7)
        self._pack_color_swatches(context_row, PALETTE, self.draw_color, self.pick_color)
        self._pack_fill_dropdown(
            context_row, "Fill",
            [("transparent", "None"), ("white", "White"), ("black", "Black"), ("white_border", "White/Black"), ("black_border", "Black/White")],
            self.text_bg_fill, self.set_text_bg_fill,
        )

        font_options = [(name, name) for name in TEXT_FONT_FAMILIES]
        font_menu = self._pack_dropdown(context_row, "Font", font_options, self.text_font_family, self.set_text_font_family, width=7)
        try:
            inner_menu = font_menu["menu"]
            for i, name in enumerate(TEXT_FONT_FAMILIES):
                inner_menu.entryconfig(i, font=(TEXT_FONT_FAMILIES[name]["tk"], 11))
        except tk.TclError:
            pass   # per-item font preview is a nicety, not load-bearing - fall back to the plain dropdown silently

        tk.Label(context_row, text="Size", fg="white", bg="#222222").pack(side="left", padx=(8, 2))
        size_var = tk.StringVar(value=str(self.text_font_size))

        def _commit_size(*_args):
            # set_text_font_size itself now handles both the per-span case
            # (highlighted text - see _apply_font_or_size_to_span) and the
            # whole-box default case.
            applied_to_span = self.set_text_font_size(size_var.get())
            if not applied_to_span:
                size_var.set(str(self.text_font_size))
            return "break"

        def _step_size(delta):
            try:
                current = int(float(size_var.get()))
            except ValueError:
                current = self.text_font_size
            size_var.set(str(max(6, min(200, current + delta))))
            _commit_size()

        _probe_normal = tk.Button(context_row, text="X", pady=BUTTON_PADY)
        _probe_normal.pack()
        _probe_normal.update_idletasks()
        stepper_h = _probe_normal.winfo_reqheight()
        _probe_normal.destroy()

        _probe_arrow = tk.Button(context_row, text="\u25b2", width=2, pady=0)
        _probe_arrow.pack()
        _probe_arrow.update_idletasks()
        stepper_w = max(12, _probe_arrow.winfo_reqwidth() // 3 + 3)
        _probe_arrow.destroy()

        entry_frame = tk.Frame(context_row, width=32, height=stepper_h, bg="#222222")
        entry_frame.pack_propagate(False)
        entry_frame.pack(side="left", padx=(2, 0), pady=4)
        size_entry = tk.Entry(entry_frame, textvariable=size_var, justify="center")
        size_entry.pack(fill="both", expand=True)
        size_entry.bind("<Return>", _commit_size)
        size_entry.bind("<FocusOut>", _commit_size)
        size_entry.bind("<Up>", lambda e: _step_size(2))
        size_entry.bind("<Down>", lambda e: _step_size(-2))

        stepper_frame = tk.Frame(context_row, width=stepper_w, height=stepper_h, bg="#222222")
        stepper_frame.pack_propagate(False)
        stepper_frame.pack(side="left", padx=(1, 2), pady=4)
        up_h = stepper_h // 2
        down_h = stepper_h - up_h
        up_frame = tk.Frame(stepper_frame, height=up_h, bg="#222222")
        up_frame.pack_propagate(False)
        up_frame.pack(side="top", fill="both")
        down_frame = tk.Frame(stepper_frame, height=down_h, bg="#222222")
        down_frame.pack_propagate(False)
        down_frame.pack(side="top", fill="both")
        tk.Button(
            up_frame, text="\u25b2", padx=0, pady=0, command=lambda: _step_size(2),
        ).pack(fill="both", expand=True)
        tk.Button(
            down_frame, text="\u25bc", padx=0, pady=0, command=lambda: _step_size(-2),
        ).pack(fill="both", expand=True)

        fill_options = [
            ("transparent", "None"), ("white", "White"), ("black", "Black"),
            ("white_border", "White/Black"), ("black_border", "Black/White"),
        ]
        # Custom popup dropdown (not a tk.OptionMenu).
        pass
        align_options = [("left", "Left"), ("center", "Center"), ("right", "Right")]
        self._pack_dropdown(context_row, "Align", align_options, self.text_align, self.set_text_align, width=6)
        self._pack_proportional_checkbox(context_row)

    def _show_split_mode_toolbar(self, frame):
        row = tk.Frame(frame, bg="#222222")
        row.pack(side="top", fill="x")
        layer = self._find_layer(self.isolated_layer_id)
        name = layer["name"] if layer else "?"
        tk.Label(
            row, text=f"Split '{name}': drag a rectangle over the elements to split out",
            fg="white", bg="#222222",
        ).pack(side="left", padx=4, pady=4)
        tk.Button(row, text="Cancel", pady=BUTTON_PADY, command=self._exit_split_mode).pack(side="right", padx=4, pady=4)
        self._build_status_row(row, frame)
        self.finish_toolbar_placement(frame)
        self._set_window_title("split")

    # ---- resize submenu ----
    #
    # Two layers can be resized independently:
    #   "capture"    - scales the WHOLE canvas coordinate frame (the raw
    #                  screenshot pixels, the canvas background's own
    #                  bounds, and every element's origin/size, or a raster
    #                  stroke's points/width) by independent x/y factors.
    #                  Every element stays fully editable afterward - see
    #                  _apply_full_capture_resize. Its handles sit at the
    #                  corners of the whole canvas.
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
    # to the plain edit toolbar (never the main one) - see _handle_escape.

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
        self._cancel_draw_select_rect()
        self._hide_snip_ui()
        if self.resize_target_layer is None:
            self.resize_target_layer = "capture"
        self._cancel_pending_shape()
        if self._pending_text_widget is not None:
            self._cancel_text_entry()
        if self.edit_strokes:
            self._recompute_baked_image()
        self._snapshot_resize_session_start()
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
        self._bg_drag_preview_base = None
        self._bg_handle_drag_offset = (0.0, 0.0)
        self._clear_resize_preview()
        self.clear_handles()
        self.canvas.unbind("<ButtonPress-1>")
        self.canvas.unbind("<B1-Motion>")
        self.canvas.unbind("<ButtonRelease-1>")
        self.canvas.bind("<ButtonPress-1>", self.edit_press)
        self.canvas.bind("<B1-Motion>", self.edit_drag)
        self.canvas.bind("<ButtonRelease-1>", self.edit_release)
        self.canvas.bind("<Double-Button-1>", self._edit_double_click)
        self.show_edit_toolbar()

    def show_resize_toolbar(self):
        self.clear_toolbar()
        self._p16_clear_preview()
        frame = self.new_toolbar_frame()
        layer = self.resize_target_layer
        if layer == "capture":
            cur_w, cur_h = self._capture_layer_composite_dims()
        else:
            cur_w, cur_h = self._canvas_dims()

        main_row = tk.Frame(frame, bg="#222222")
        main_row.pack(side="top", fill="x")
        for label, tool in (("Snip", "snip"), ("Draw", "pen"), ("Text", "text"), ("Shape", "shape")):
            tk.Button(main_row, text=label, pady=BUTTON_PADY, relief="raised",
                      command=lambda t=tool: self._p14_leave_resize_then(lambda: self.pick_tool(t))).pack(side="left", padx=2, pady=4)
        tk.Button(main_row, text="Resize", pady=BUTTON_PADY, relief="sunken").pack(side="left", padx=2, pady=4)
        tk.Button(main_row, text="Undo", pady=BUTTON_PADY, command=self._undo_layer_resize).pack(side="left", padx=2, pady=4)
        tk.Button(main_row, text="Redo", pady=BUTTON_PADY, command=self._redo_layer_resize).pack(side="left", padx=2, pady=4)
        tk.Button(main_row, text="Done", pady=BUTTON_PADY, command=self._exit_resize_mode).pack(side="left", padx=(10, 2), pady=4)
        tk.Button(main_row, text="Cancel", pady=BUTTON_PADY, command=self._cancel_resize_session).pack(side="left", padx=2, pady=4)
        self._build_status_row(main_row, frame)

        context_row = tk.Frame(frame, bg="#222222")
        context_row.pack(side="top", fill="x")
        self._pack_dropdown(context_row, "Region", [("capture", "Full Capture"), ("background", "Background")],
                            layer, self._select_resize_layer, width=12)
        if layer == "background":
            self._pack_dropdown(context_row, "Color", [("white", "White"), ("black", "Black"), ("transparent", "Transparent")],
                                self.canvas_bg_color, self._set_canvas_bg_color, width=10)
        scale_var = tk.DoubleVar(value=1.0)
        self._pack_labeled_slider(context_row, "Scale", scale_var, 0.1, 4.0, lambda v: self._p16_preview_scale(v), resolution=0.1, value_width=3)
        try:
            lbl = context_row.winfo_children()[-1]
            lbl.pack_configure(padx=(0, 2))
        except Exception:
            pass
        tk.Button(context_row, text="Apply Scale", pady=BUTTON_PADY, command=lambda: self._apply_resize_scale(scale_var.get())).pack(side="left", padx=2, pady=4)

        def _apply_dimensions(*_args):
            self._apply_resize_pixels(w_var.get(), h_var.get())
            return "break"

        tk.Label(context_row, text="W", fg="white", bg="#222222").pack(side="left", padx=(14, 2))
        w_var = tk.StringVar(value=str(cur_w))
        w_entry = tk.Entry(context_row, textvariable=w_var, width=6)
        w_entry.pack(side="left", padx=2)
        w_entry.bind("<Return>", _apply_dimensions)
        tk.Label(context_row, text="H", fg="white", bg="#222222").pack(side="left", padx=2)
        h_var = tk.StringVar(value=str(cur_h))
        _pd = lambda *_a: self._p16_preview_dims(w_var.get(), h_var.get())
        w_var.trace_add("write", _pd)
        h_var.trace_add("write", _pd)
        h_entry = tk.Entry(context_row, textvariable=h_var, width=6)
        h_entry.pack(side="left", padx=2)
        h_entry.bind("<Return>", _apply_dimensions)
        tk.Button(context_row, text="Apply Dimensions", pady=BUTTON_PADY, command=lambda: self._apply_resize_pixels(w_var.get(), h_var.get())).pack(side="left", padx=2, pady=4)

        self.finish_toolbar_placement(frame)
        self._set_window_title("resize")
        self._draw_resize_handles()

    # ---- full-state snapshots (Resize undo/redo/cancel, and Edit's Cancel) ----

    @staticmethod
    def _snapshot_strokes(strokes):
        """
        Dict-level copy of each stroke. Element geometry (origin/w/h/
        rotation/removed) is REPLACED on the dict when it changes, never
        mutated in place, so this is enough to keep a snapshot from being
        altered by later moves/resizes of the live strokes (a plain
        list(...) copy shared the same dicts, so cancelling after moving a
        pre-existing element left it moved). Heavy image data is shared,
        not copied - it's never modified in place.
        """
        return [dict(s) for s in strokes]

    def _capture_full_state(self):
        """
        Everything Resize (or Edit as a whole) can touch, as one snapshot -
        shared by the resize undo/redo stacks (one per action), the resize
        session-start snapshot Cancel reverts to, and Edit's own
        pre-session snapshot (see enter_edit_mode / cancel_edit). Includes
        the raw image dimensions - restoring image_path/sel_coords without
        them left the plain main-toolbar view sized for the resampled
        image after undoing a Full Capture resize.
        """
        from .capture import wait_written
        wait_written(self.image_path)   # patch 43: the path may be handed to another window/process
        return {
            "image_path": self.image_path,
            "sel_coords": self.sel_coords,
            "baked_image": self.baked_image,
            "edit_strokes": self._snapshot_strokes(self.edit_strokes),
            "ocr_strokes": list(self.ocr_strokes),
            "canvas_x1": self.canvas_x1, "canvas_y1": self.canvas_y1,
            "canvas_x2": self.canvas_x2, "canvas_y2": self.canvas_y2,
            "canvas_bg_color": self.canvas_bg_color,
            "canvas_active": self._canvas_active,
            "no_real_capture": self._no_real_capture,
            "raw_w": self._raw_img_w, "raw_h": self._raw_img_h,
            "layers": [dict(l) for l in self.layers],
        }

    def _restore_full_state(self, snap, discard_newer_temp_files=False):
        """
        Puts back a snapshot from _capture_full_state. Callers still need
        to re-sync/re-display afterward (_sync_base_dims_for_mode +
        _recalculate_display/_refresh_base_display) - base dims are
        derived, not stored. discard_newer_temp_files=True also deletes
        resize temp images created since (only used when the WHOLE
        history is being thrown away - Undo/Redo keep them, since another
        stack entry may still point at one).
        """
        for stroke in self.edit_strokes:
            for seg_id in stroke.get("segment_ids", []):
                self.canvas.delete(seg_id)
        if discard_newer_temp_files:
            keep = snap["image_path"]
            still_owned = []
            from .capture import wait_written
            wait_written()
            for p in self.owned_temp_paths:
                if p != keep and Path(p).name.startswith("snippysnappy-resize-"):
                    try:
                        Path(p).unlink(missing_ok=True)
                    except Exception:
                        log.exception("Could not remove a resize temp file while reverting (non-fatal)")
                else:
                    still_owned.append(p)
            self.owned_temp_paths = still_owned
        self.image_path = snap["image_path"]
        self.sel_coords = snap["sel_coords"]
        self.baked_image = snap["baked_image"]
        self.edit_strokes = self._snapshot_strokes(snap["edit_strokes"])   # copied again so the snapshot itself stays pristine for repeated restores
        self.ocr_strokes = list(snap["ocr_strokes"])
        self.canvas_x1, self.canvas_y1 = snap["canvas_x1"], snap["canvas_y1"]
        self.canvas_x2, self.canvas_y2 = snap["canvas_x2"], snap["canvas_y2"]
        self.canvas_bg_color = snap["canvas_bg_color"]
        self._canvas_active = snap["canvas_active"]
        self._no_real_capture = snap["no_real_capture"]
        self._raw_img_w, self._raw_img_h = snap["raw_w"], snap["raw_h"]
        self.layers = [dict(l) for l in snap["layers"]]
        if self.isolated_layer_id is not None and self._find_layer(self.isolated_layer_id) is None:
            log.info("Isolated layer no longer exists after restoring state - falling back to Full")
            self.isolated_layer_id = None
        self._edit_redo_stack = []
        self._snip_focused_index = None
        self._snip_drag_index = None
        self._snip_drag_mode = None
        self._snip_drag_base_image = None

    def _resync_display_after_state_change(self):
        if self._sync_base_dims_for_mode():
            self._recalculate_display(keep_position=True)
        else:
            self._refresh_base_display()

    def _push_resize_undo_snapshot(self):
        """
        Called once per resize action (numeric apply, or once per
        handle-drag gesture - never per drag frame) - pushes the state from
        BEFORE that action onto the undo stack, so Undo can walk back
        through any number of resizes one step at a time, all the way to
        the state Cancel would also revert to. A new action clears the
        redo stack, same as any standard undo/redo history.

        Superseded temp image files are deliberately NOT deleted here -
        a temp file made obsolete by this action might still be needed by
        an entry elsewhere in the undo/redo stacks. They're cleaned up by
        the normal _cleanup_temp_files sweep on window close instead.
        """
        self._resize_undo_stack.append(self._capture_full_state())
        self._resize_redo_stack = []

    def _snapshot_resize_session_start(self):
        """
        Taken once, the moment Resize is opened from the edit toolbar (not
        on every individual resize action - that's _push_resize_undo_snapshot
        above). Cancel reverts everything back to exactly this, discarding
        every resize done since - as distinct from Undo/Redo, which step
        through that history one action at a time without leaving Resize.
        """
        self._resize_session_snapshot = self._capture_full_state()
        self._resize_undo_stack = []
        self._resize_redo_stack = []

    def _undo_layer_resize(self):
        if not self._resize_undo_stack:
            log.info("Undo Resize pressed with nothing to undo")
            return
        self._resize_redo_stack.append(self._capture_full_state())
        snap = self._resize_undo_stack.pop()
        self._restore_full_state(snap)
        self._resync_display_after_state_change()
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
        self._resize_undo_stack.append(self._capture_full_state())
        snap = self._resize_redo_stack.pop()
        self._restore_full_state(snap)
        self._resync_display_after_state_change()
        log.info(f"Resize redone, {len(self._resize_redo_stack)} more step(s) available")
        self.show_resize_toolbar()

    def _cancel_resize_session(self):
        snap = self._resize_session_snapshot
        if snap is not None:
            self._restore_full_state(snap, discard_newer_temp_files=True)
            log.info("Resize cancelled - reverted to state before Resize was opened")
        self._resize_session_snapshot = None
        self._resize_undo_stack = []
        self._resize_redo_stack = []
        self._resync_display_after_state_change()
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

    def _rescale_point_list(self, points, sx, sy):
        return [(px * sx, py * sy) for px, py in points]

    def _rescale_stroke_for_full_capture(self, stroke, sx, sy):
        """
        Rescales one edit_strokes entry in place for a Full Capture resize
        (see _apply_full_capture_resize) - every stored coordinate is
        multiplied by (sx, sy) about the fixed frame origin (the capture's
        own top-left corner, which per the class-level coordinate-frame
        comment never moves except on this exact operation). A raster
        stroke's line/brush width is scaled by the AVERAGE of sx and sy,
        since a single width can't stretch non-uniformly the way a
        rectangle's w/h can. Rotation angles are left as-is.
        """
        avg = (sx + sy) / 2
        mode = stroke.get("mode")
        if mode in ("snip", "text", "shape"):
            ox, oy = stroke["origin"]
            stroke["origin"] = (ox * sx, oy * sy)
            stroke["w"] = stroke["w"] * sx
            stroke["h"] = stroke["h"] * sy
            if mode == "snip":
                if stroke.get("orig_bounds") is not None:
                    x1, y1, x2, y2 = stroke["orig_bounds"]
                    nx1, ny1, nx2, ny2 = x1 * sx, y1 * sy, x2 * sx, y2 * sy
                    stroke["orig_bounds"] = (nx1, ny1, nx2, ny2)
                    stroke["base_w"] = max(1, round(nx2 - nx1))
                    stroke["base_h"] = max(1, round(ny2 - ny1))
                if stroke.get("orig_freeform_points"):
                    stroke["orig_freeform_points"] = self._rescale_point_list(stroke["orig_freeform_points"], sx, sy)
        elif mode == "spray":
            stroke["dots"] = [(x * sx, y * sy, r * avg) for x, y, r in stroke.get("dots", [])]
        else:   # pen, highlight, erase - all a plain points+width raster stroke
            stroke["points"] = self._rescale_point_list(stroke["points"], sx, sy)
            stroke["width"] = max(1, round(stroke.get("width", EDIT_PEN_WIDTH) * avg))

    def _apply_full_capture_resize(self, new_w, new_h):
        """
        Resizes the WHOLE canvas coordinate frame (capture, canvas
        background bounds, and every edit_strokes element) by independent
        x/y scale factors derived from new_w/new_h against the current
        canvas dimensions - everything scales together, and every element
        stays fully editable afterward (move/resize/rotate, or re-color a
        span of text, etc), rather than being flattened into one bitmap.
        The scale is anchored at the capture's own top-left corner.
        """
        t0 = time.monotonic()
        if self.edit_strokes:
            self._recompute_baked_image()   # bring baked_image up to date first - see show_resize_options
        self._push_resize_undo_snapshot()

        cw, ch = self._canvas_dims()
        sx = new_w / cw if cw else 1.0
        sy = new_h / ch if ch else 1.0

        ow = self.sel_coords[2] - self.sel_coords[0]
        oh = self.sel_coords[3] - self.sel_coords[1]
        new_raw_w = max(1, round(ow * sx))
        new_raw_h = max(1, round(oh * sy))
        resampled_capture = False
        if not self._no_real_capture:
            from PIL import Image
            source = self.crop_original()   # the raw screenshot pixels alone - RGB, no alpha (see crop_original)
            resized = source.resize((new_raw_w, new_raw_h), Image.LANCZOS)
            fd, tmp_name = tempfile.mkstemp(suffix=".png", prefix="snippysnappy-resize-")
            os.close(fd)
            new_path = Path(tmp_name)
            resized.save(new_path)
            self.image_path = new_path
            self.owned_temp_paths.append(new_path)
            resampled_capture = True

        for stroke in self.edit_strokes:
            for seg_id in stroke.get("segment_ids", []):
                self.canvas.delete(seg_id)
            stroke["segment_ids"] = []
            self._rescale_stroke_for_full_capture(stroke, sx, sy)
        for stroke in self.ocr_strokes:
            for seg_id in stroke.get("segment_ids", []):
                self.canvas.delete(seg_id)
            stroke["segment_ids"] = []
            stroke["points"] = self._rescale_point_list(stroke["points"], sx, sy)
            stroke["width"] = max(1, round(stroke.get("width", OCR_BRUSH_WIDTH) * (sx + sy) / 2))

        self.sel_coords = (0, 0, new_raw_w, new_raw_h)
        self._raw_img_w, self._raw_img_h = new_raw_w, new_raw_h
        self.canvas_x1, self.canvas_y1 = self.canvas_x1 * sx, self.canvas_y1 * sy
        self.canvas_x2, self.canvas_y2 = self.canvas_x2 * sx, self.canvas_y2 * sy
        self.baked_image = None
        self._snip_focused_index = None
        self._snip_drag_base_image = None

        self._resync_display_after_state_change()
        self._recompute_baked_image()   # elements were only rescaled, not yet re-rendered at their new size
        log.info(
            f"Full Capture resize applied: canvas {cw}x{ch} -> {new_w}x{new_h} (scale x={sx:.4f} y={sy:.4f}), "
            f"capture {'resampled to ' + str((new_raw_w, new_raw_h)) if resampled_capture else 'skipped (--nil, no real capture)'}, "
            f"{len(self.edit_strokes)} element(s) rescaled and kept editable, "
            f"{(time.monotonic() - t0) * 1000:.0f}ms"
        )

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
        Repurposes the standard 4-corner handle widgets to control the
        canvas rectangle (canvas-local (0,0)-(canvas_w,canvas_h)) - the
        thing both resize layers act on: Background moves its bounds, Full
        Capture resamples the whole composite it bounds.
        """
        self.clear_handles()
        if not self.resize_mode_active or self.resize_target_layer is None:
            return
        cw, ch = self._canvas_dims()
        ox, oy = self._bg_handle_drag_offset   # see _live_update_background_handle
        cx1, cy1 = self.img_to_canvas(0, 0)
        cx2, cy2 = self.img_to_canvas(cw, ch)
        corners = {"nw": (cx1 + ox, cy1 + oy), "ne": (cx2 + ox, cy1 + oy), "sw": (cx1 + ox, cy2 + oy), "se": (cx2 + ox, cy2 + oy)}
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
        self._p16_clear_preview()
        cx, cy = self._event_canvas_xy(event)
        name = self.hit_handle(cx, cy)
        self._resize_active_handle = name
        self._resize_drag_snapshotted = False
        self._resize_pending_capture_bounds = None
        self._bg_drag_preview_base = None
        if name:
            self._resize_drag_anchor_screen = (cx, cy)
            self._resize_drag_anchor_capture_local = self.canvaslocal_to_capturelocal(*self.canvas_to_img(cx, cy))
            self._resize_drag_orig_bounds = (self.canvas_x1, self.canvas_y1, self.canvas_x2, self.canvas_y2)
            self._bg_handle_drag_offset = (0.0, 0.0)
        if name and self.resize_target_layer == "capture":
            self._resize_preview_start_capture_dims = self._canvas_dims()

    def _anchored_capture_local(self, anchor_screen, anchor_capture_local, event):
        """
        Converts a drag event to capture-local coordinates using a FIXED
        anchor captured once at press time, rather than re-deriving the
        conversion from self.canvas_x1/canvas_y1 as they stand RIGHT NOW.
        """
        cx, cy = self._event_canvas_xy(event)
        dx = (cx - anchor_screen[0]) / self.display_scale
        dy = (cy - anchor_screen[1]) / self.display_scale
        return anchor_capture_local[0] + dx, anchor_capture_local[1] + dy

    def _resize_drag(self, event):
        name = self._resize_active_handle
        if not name:
            return
        capture_local_x, capture_local_y = self._anchored_capture_local(
            self._resize_drag_anchor_screen, self._resize_drag_anchor_capture_local, event,
        )
        if self.resize_target_layer == "background":
            self._live_update_background_handle(name, capture_local_x, capture_local_y, self._resize_drag_orig_bounds)
        else:
            self._live_preview_capture_handle(name, capture_local_x, capture_local_y)

    def _resize_release(self, event):
        name = self._resize_active_handle
        self._resize_active_handle = None
        if not name:
            return
        self._clear_dim_overlay()
        if self.resize_target_layer == "capture":
            self._finalize_capture_handle_resize()
        else:
            self._bg_drag_preview_base = None
            self._bg_handle_drag_offset = (0.0, 0.0)
            self._finish_background_resize()
        self.show_resize_toolbar()

    def _show_pil_on_base(self, base, fast=False):
        """Displays an arbitrary PIL image (at the current display_scale) as the canvas's base image."""
        from PIL import Image
        scale = self.display_scale
        if abs(scale - 1.0) > 1e-6:
            disp = base.resize(
                (max(1, int(base.width * scale)), max(1, int(base.height * scale))),
                (Image.BILINEAR if fast else Image.LANCZOS) if scale < 1 else Image.NEAREST,
            )
        else:
            disp = base
        # Normally (0, 0) - _bg_handle_drag_offset is only ever nonzero
        # mid-drag on a west/north background/crop handle, to keep the
        # un-dragged corner visually fixed.
        ox, oy = self._bg_handle_drag_offset
        self._show_base_photo(disp, ox, oy)

    def _show_base_photo(self, disp, x, y):
        """
        Puts disp on the canvas as the bottom-most picture item. patch 43: when the size and
        mode are unchanged, the existing Tk image is updated in place (no new PhotoImage, no new
        canvas item) - same picture, same stacking (it is lowered to the bottom either way).
        """
        photo = self._base_display_photo
        item = self._base_photo_id
        key = (disp.width, disp.height, disp.mode)
        if (photo is not None and item is not None and getattr(self, "_base_photo_key", None) == key
                and getattr(self, "_imagetk_available", False) and self.canvas.type(item) == "image"):
            photo.paste(disp)
            self.canvas.coords(item, x, y)
            self.canvas.tag_lower(item)
            return
        photo = self._pil_to_tk(disp)
        if photo is None:
            return
        self._base_display_photo = photo
        self._base_photo_key = key
        if self._base_photo_id is not None:
            self.canvas.delete(self._base_photo_id)
        self._base_photo_id = self.canvas.create_image(x, y, anchor="nw", image=photo)
        self.canvas.tag_lower(self._base_photo_id)

    def _live_update_background_handle(self, name, mx, my, orig_bounds=None):
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
            if self.baked_image is not None:
                self._bg_drag_preview_base = (self.baked_image, self.canvas_x1, self.canvas_y1)
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
        if orig_bounds is not None:
            ox1, oy1, _ox2, _oy2 = orig_bounds
            self._bg_handle_drag_offset = (
                (self.canvas_x1 - ox1) * self.display_scale,
                (self.canvas_y1 - oy1) * self.display_scale,
            )
        cw, ch = self._canvas_dims()
        self.canvas.configure(scrollregion=(0, 0, max(1, int(cw * self.display_scale)), max(1, int(ch * self.display_scale))))
        preview = self._build_canvas_composite()
        if self._bg_drag_preview_base is not None:
            old_baked, old_x1, old_y1 = self._bg_drag_preview_base
            preview.paste(old_baked, (int(round(old_x1 - self.canvas_x1)), int(round(old_y1 - self.canvas_y1))), old_baked)
        self._show_pil_on_base(preview, fast=True)
        self._draw_resize_handles()
        # Live "W x H" readout for the canvas/page itself while dragging a
        # crop or background-resize corner - same helper the shape/text/
        # snip drags use, just without alignment guides (there's nothing
        # else for the canvas's own edges to align against here).
        handle_screen_x, handle_screen_y = self.capturelocal_to_screen(mx, my)
        self._show_dim_overlay(handle_screen_x, handle_screen_y, f"{round(cw)} x {round(ch)}")

    def _live_preview_capture_handle(self, name, mx, my):
        """
        Full Capture resize is a real resample, too expensive to run on
        every mouse-move - show a dashed preview rectangle of the target
        bounds instead and do the actual resample once on release. The
        rectangle starts from the CANVAS bounds (what actually gets
        resampled - capture, background and edits together), so the size
        it previews is the size you get.
        """
        self._clear_resize_preview()
        x1, y1, x2, y2 = self.canvas_x1, self.canvas_y1, self.canvas_x2, self.canvas_y2
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
        self._show_dim_overlay(cx2, cy2, f"{round(abs(x2 - x1))} x {round(abs(y2 - y1))}")

    def _finalize_capture_handle_resize(self):
        bounds = self._resize_pending_capture_bounds
        self._clear_resize_preview()
        self._resize_pending_capture_bounds = None
        if bounds is None:
            return
        x1, y1, x2, y2 = bounds
        new_w = max(1, int(round(abs(x2 - x1))))
        new_h = max(1, int(round(abs(y2 - y1))))
        self._apply_full_capture_resize(new_w, new_h)

    # ---- undo (strokes) ----

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
        if self._draw_select_rect_start is not None:
            log.info("Undo pressed while a Draw Select rubber-band was being drawn - cancelling it")
            self._cancel_draw_select_rect()
            return
        if not self.edit_strokes:
            log.info("Undo pressed with no strokes to undo")
            return
        stroke = self.edit_strokes.pop()
        for seg_id in stroke.get("segment_ids", []):
            self.canvas.delete(seg_id)
        self._restore_consumed_elements(stroke)
        self._edit_redo_stack.append(stroke)
        log.info(f"Undid last stroke (mode={stroke.get('mode')}), {len(self.edit_strokes)} remaining, {len(self._edit_redo_stack)} redoable")
        self._recompute_baked_image()
        self._draw_snip_ui()
        self._refresh_layers_panel_if_open()

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
        self._reconsume_elements(stroke)
        log.info(f"Redid stroke (mode={stroke.get('mode')}), {len(self.edit_strokes)} total")
        self._recompute_baked_image()
        self._draw_snip_ui()
        self._refresh_layers_panel_if_open()

    def _restore_consumed_elements(self, stroke):
        """
        Companion to _finalize_snip's "consume whatever's fully inside the
        cut region" behavior: undoing a snip must bring back whatever it
        consumed, not just the snip itself, or those elements stay
        soft-deleted (removed=True) forever even after the snip that
        removed them is gone. Matched by "eid" (see _append_stroke)
        rather than list index or dict identity - neither survives a
        full-state snapshot/restore (Resize's own undo, Cancel) intact.

        Also undoes any PARTIAL clips this same snip made (stroke's own
        "partial_clips": target eid -> the exact hole dict that was
        appended to that target's "snip_holes" - see _finalize_snip),
        removing just that one hole so the element's un-clipped portion
        (and any OTHER snip's holes on it) is unaffected.
        """
        eids = stroke.get("consumed_eids")
        if eids:
            eids = set(eids)
            for other in self.edit_strokes:
                if other.get("eid") in eids:
                    other["removed"] = False
        partial_clips = stroke.get("partial_clips")
        if partial_clips:
            for target_eid, hole in partial_clips.items():
                for other in self.edit_strokes:
                    if other.get("eid") == target_eid:
                        holes = other.get("snip_holes")
                        if holes and hole in holes:
                            holes.remove(hole)
                        break

    def _reconsume_elements(self, stroke):
        """The reverse of _restore_consumed_elements, for Redo - see there."""
        eids = stroke.get("consumed_eids")
        if eids:
            eids = set(eids)
            for other in self.edit_strokes:
                if other.get("eid") in eids:
                    other["removed"] = True
        partial_clips = stroke.get("partial_clips")
        if partial_clips:
            for target_eid, hole in partial_clips.items():
                for other in self.edit_strokes:
                    if other.get("eid") == target_eid:
                        other.setdefault("snip_holes", []).append(hole)
                        break

    def bake_edits(self):
        if self._pending_text_widget is not None:
            self._confirm_text_entry()
        self._cancel_pending_shape()
        log.info(f"Baking {len(self.edit_strokes)} edit stroke(s)")
        self._recompute_baked_image()
        self.show_main_toolbar()

    def _apply_erase_stroke(self, base, pre_ink_base, stroke, ox, oy):
        """
        "Erases" along this stroke's path by restoring `base` to
        `pre_ink_base` - the composite as it would look with NO ink
        (pen/highlight/spray) ever drawn - wherever the erase brush passed,
        so whatever was really there (capture, canvas background, or a
        Snip/Text/Shape element) shows through exactly as it should,
        regardless of its color. A flat fill color would be wrong here: it
        would paint over the actual photo/capture instead of revealing it.
        Only ink is erased - Snip/Text/Shape elements are untouched (they
        aren't part of pre_ink_base's exclusion, so they show through
        unaffected wherever they were drawn), matching "erase from the
        drawing layer" rather than erasing everything.
        """
        from PIL import Image, ImageDraw
        pts = [(px - ox, py - oy) for px, py in stroke["points"]]
        width = max(1, int(stroke.get("width", EDIT_PEN_WIDTH)))
        mask = Image.new("L", base.size, 0)
        draw = ImageDraw.Draw(mask)
        r = width / 2
        if len(pts) == 1:
            px, py = pts[0]
            draw.ellipse([px - r, py - r, px + r, py + r], fill=255)
        else:
            draw.line(pts, fill=255, width=width, joint="curve")
            for px, py in (pts[0], pts[-1]):   # round caps at both ends, matching pen's rounded strokes
                draw.ellipse([px - r, py - r, px + r, py + r], fill=255)
        return Image.composite(pre_ink_base, base, mask)

    def _draw_one_stroke(self, base, stroke, ox, oy):
        """
        Draws a single pen/highlight/spray stroke onto `base` - the only
        raster/free-hand ink left here now that text/shape/snip are all
        transformable elements handled by _draw_transformable_element
        instead (see _recompute_baked_image). Returns the resulting image,
        since the highlighter branch's alpha_composite produces a NEW
        image object rather than mutating in place.
        """
        from PIL import Image, ImageDraw
        mode = stroke.get("mode")
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

    def _render_stroke_layer_only(self, layer, exclude_index=None):
        """
        Renders ONE stroke layer's own content into its own, independent,
        fully transparent buffer - its own hole-punching and its own
        erase-reveal happen entirely within this buffer, seeing nothing
        from any other layer. This is what makes a layer genuinely
        independent: a snip's "hole" only ever removes THIS layer's own
        paint (see _punch_snip_hole - its fill is always transparent now),
        and once this buffer is composited onto the rest of the stack (see
        _render_layered), the hole naturally reveals whatever's truly
        underneath - the capture, the background, or a lower layer -
        never a flat fill color and never another layer's unrelated
        content. The same independence is what lets a Snip taken while a
        layer is isolated extract just THAT layer's own drawn pixels (see
        _finalize_snip) rather than a chunk of the full picture.
        """
        from PIL import Image
        cw, ch = self._canvas_dims()
        base = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        pre_ink_base = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        ox, oy = self.canvas_x1, self.canvas_y1
        for i, stroke in enumerate(self.edit_strokes):
            if stroke.get("layer_id") != layer["id"]:
                continue
            mode = stroke.get("mode")
            if mode == "snip":
                if stroke.get("hole_scope", "full_stack") == "own_layer":
                    base = self._punch_snip_hole(base, stroke, ox, oy, fill_rgba=(0, 0, 0, 0))
                    pre_ink_base = self._punch_snip_hole(pre_ink_base, stroke, ox, oy, fill_rgba=(0, 0, 0, 0))
                if i != exclude_index and not stroke.get("removed"):
                    self._draw_transformable_element(base, stroke, ox, oy)
                    self._draw_transformable_element(pre_ink_base, stroke, ox, oy)
            elif mode in ("text", "shape"):
                # Same "removed" check the snip branch above already has -
                # missing here, a Text/Shape element that a snip consumed
                # (soft-deleted via removed=True, see _finalize_snip) kept
                # being drawn anyway: it visibly still sat over the snip's
                # own exposed background at its ORIGINAL spot the moment
                # the snip was moved away, a duplicate "ghost" of exactly
                # the kind consuming it was supposed to prevent.
                if i != exclude_index and not stroke.get("removed"):
                    self._draw_transformable_element(base, stroke, ox, oy)
                    self._draw_transformable_element(pre_ink_base, stroke, ox, oy)
            elif mode == "erase":
                if not stroke.get("removed"):
                    base = self._apply_erase_stroke(base, pre_ink_base, stroke, ox, oy)
            else:
                if not stroke.get("removed"):
                    holes = stroke.get("snip_holes")
                    if holes:
                        # patch 44: ink cut by a snip - draw it on its own buffer, clear the holes, composite
                        ink = self._draw_one_stroke(Image.new("RGBA", (cw, ch), (0, 0, 0, 0)), stroke, ox, oy)
                        for h in holes:
                            ink = self._punch_snip_hole(ink, {"orig_bounds": h["bounds"], "shape": h["shape"],
                                                              "orig_freeform_points": h.get("freeform_points")},
                                                        ox, oy, fill_rgba=(0, 0, 0, 0))
                        base = Image.alpha_composite(base, ink)
                    else:
                        base = self._draw_one_stroke(base, stroke, ox, oy)
        return base

    def _render_layered(self, exclude_index=None, layer_filter=None):
        from PIL import Image
        cw, ch = self._canvas_dims()
        base = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        ox, oy = self.canvas_x1, self.canvas_y1
        holes_punched = False
        empty = True
        for layer in self.layers:
            if layer_filter is not None and layer["id"] != layer_filter:
                continue
            # patch 43: every composite below only touches the area a layer actually covers -
            # compositing fully transparent pixels leaves the destination exactly as it was.
            if layer["kind"] == "background":
                fill = Image.new("RGBA", (cw, ch), self._canvas_bg_fill_rgba())
                base = fill if empty else Image.alpha_composite(base, fill)
                empty = False
            elif layer["kind"] == "capture":
                if not self._no_real_capture:
                    capture, _ = self._capture_rgba()
                    self._composite_capture(base, capture, int(round(-ox)), int(round(-oy)))
                    empty = False
            else:
                if not holes_punched:
                    for stroke in self.edit_strokes:
                        if stroke.get("mode") == "snip" and stroke.get("hole_scope", "full_stack") == "full_stack" \
                                and not stroke.get("removed"):
                            base = self._punch_snip_hole(base, stroke, ox, oy)
                    holes_punched = True
                layer_img = self._render_stroke_layer_only(layer, exclude_index=exclude_index)
                bbox = layer_img.getchannel("A").getbbox()
                if bbox:
                    base.alpha_composite(layer_img, dest=(bbox[0], bbox[1]), source=bbox)
                empty = False
        return base

    def _recompute_baked_image(self, exclude_index=None):
        """
        Rebuilds self.baked_image - the single authoritative, fully-merged
        document, ALWAYS every layer regardless of what's currently
        isolated for viewing (see _render_layered). This is what
        get_working_image()/Save/Copy/Resize/snip-extraction always read.
        Rebuilding from scratch each time.
        """
        self.baked_image = self._render_layered(exclude_index=exclude_index, layer_filter=None)
        log.debug("Edits baked into working image" if exclude_index is None else f"Edits baked into working image (element {exclude_index} hidden - being re-edited live)")
        self._refresh_canvas_with_baked_image()

    def _render_display_composite(self, exclude_index=None):
        """
        The shared core behind both _build_composite_excluding_element and
        _current_display_image: when a layer is isolated, renders every
        OTHER layer at reduced opacity (self.ghost_layer_opacity - see the
        Layers panel's opacity slider) as a positioning/context reference,
        """
        if self.isolated_layer_id is None:
            return self._render_layered(exclude_index=exclude_index, layer_filter=None)
        target_img = self._render_layered(exclude_index=exclude_index, layer_filter=self.isolated_layer_id)
        opacity = self.ghost_layer_opacity
        if opacity <= 0:
            return target_img
        from PIL import Image
        full_img = self._render_layered(exclude_index=exclude_index, layer_filter=None)
        r, g, b, a = full_img.split()
        a = a.point(lambda v: int(v * opacity))
        ghost = Image.merge("RGBA", (r, g, b, a))
        return Image.alpha_composite(target_img, ghost)

    def _build_composite_excluding_element(self, exclude_index):
        return self._render_display_composite(exclude_index=exclude_index)

    def _current_display_image(self, composite_only=False):
        if self.isolated_layer_id is not None:
            return self._render_display_composite()
        if self.baked_image is not None and not composite_only:
            return self.baked_image
        return self._build_canvas_composite()

    def _recompute_baked_image_live_drag(self):
        """
        Cheap per-frame update while a transformable element (Snip, Text,
        or Shape) is actively being moved, resized, or rotated: reuses the
        cached _snip_drag_base_image (everything except this one element,
        already scoped to whatever's currently isolated - see
        _build_composite_excluding_element) and only re-renders the
        dragged element itself (at low-quality/fast resampling - see
        _draw_transformable_element) on top of a copy of that cache.

        This updates the ON-SCREEN preview directly (_show_pil_on_base)
        rather than touching self.baked_image at all - self.baked_image
        stays the last real, fully-correct document throughout the drag
        (get_working_image/Save/Copy are never reachable mid-drag anyway,
        since the mouse button is held), and _snip_release's final
        _recompute_baked_image() call is what actually updates it once the
        drag settles. The full _recompute_baked_image path redraws every
        other stroke/element on EVERY frame, far more work than a drag
        needs, since only the one element being manipulated is changing.
        """
        if self._snip_drag_base_image is None or self._snip_drag_index is None:
            self._recompute_baked_image()
            return
        base = self._snip_drag_base_image.copy()
        ox, oy = self.canvas_x1, self.canvas_y1
        stroke = self.edit_strokes[self._snip_drag_index]
        is_resize_drag = self._snip_drag_mode not in ("move", "rotate", None)
        preview_alpha = RESIZE_DRAG_PREVIEW_ALPHA if is_resize_drag else None
        self._draw_transformable_element(base, stroke, ox, oy, fast=True, preview_alpha=preview_alpha)
        for s in self.edit_strokes:
            for seg_id in s.get("segment_ids", []):
                self.canvas.delete(seg_id)
        self._show_pil_on_base(base, fast=True)

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
        self._refresh_base_display(fast=self._snip_drag_index is not None)

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

    def _leave_measure_mode(self):
        """
        State/canvas-overlay teardown only, shared by exit_measure_mode
        (Done - also re-shows the toolbar) and _switch_tool (jumping
        straight to another tool from the shared tool row - see
        _leave_current_tool_silently).
        """
        self.measure_mode_active = False
        for box in self.measure_boxes:
            self.canvas.delete(box["rect_id"])
            for hid, _ in box["handle_ids"]:
                self.canvas.delete(hid)
        self.measure_boxes = []
        self.measure_active_box = None
        self.measure_active_handle = None

    def exit_measure_mode(self):
        log.info("Exiting measure mode")
        self._leave_measure_mode()
        self.show_main_toolbar(reset_tools_row=False)

    def set_measure_kind(self, kind):
        self.measure_kind = kind
        self.show_measure_toolbar()

    def show_measure_toolbar(self):
        self.clear_toolbar()
        frame = self.new_toolbar_frame()
        main_row = tk.Frame(frame, bg="#222222")
        main_row.pack(side="top", fill="x")
        # "Measure mode:" label dropped - the sunken Measure button in the
        # shared tool row already says which tool is active.
        self._build_tools_row(main_row, active="measure", done=self.exit_measure_mode)
        self._build_status_row(main_row, frame)

        context_row = tk.Frame(frame, bg="#222222")
        context_row.pack(side="top", fill="x")
        tk.Button(
            context_row, text="Boxes", pady=BUTTON_PADY,
            relief=("sunken" if self.measure_kind == "box" else "raised"),
            command=lambda: self.set_measure_kind("box"),
        ).pack(side="left", anchor="n", padx=2, pady=4)
        tk.Button(
            context_row, text="Lines", pady=BUTTON_PADY,
            relief=("sunken" if self.measure_kind == "line" else "raised"),
            command=lambda: self.set_measure_kind("line"),
        ).pack(side="left", anchor="n", padx=2, pady=4)
        tk.Button(context_row, text="Clear", pady=BUTTON_PADY, command=self.clear_all_measure_boxes).pack(side="left", anchor="n", padx=2, pady=4)

        if self.measure_boxes:
            for i, box in enumerate(self.measure_boxes):
                lbl = tk.Label(context_row, text=self._measure_label_text(box), fg=box["color"], bg="#222222")
                lbl.pack(side="left", padx=(8, 2))
                box["label"] = lbl
                tk.Button(
                    context_row, text="\u00d7", width=2,
                    command=lambda i=i: self.clear_measure_box(i),
                ).pack(side="left", padx=(0, 8))

        self.finish_toolbar_placement(frame)
        self._set_window_title("measure")

    def enter_magnify_mode(self):
        # A lens that follows the cursor showing a magnified view of the
        # image underneath, without altering anything. The lens is a true
        # circle (via a PIL alpha mask) and the real system cursor is hidden
        # while over the canvas - a small translucent circle takes its place,
        # layered ON TOP of the lens and at its true unmagnified size/position.
        # Reverts to the normal cursor the moment you leave the canvas (e.g. toolbar).
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
        result = None
        try:
            self._mp_bind_id = self.canvas.bind("<Button-3>", self._mp_rclick, add="+")
        except tk.TclError:
            pass
        return result

    def _leave_magnify_mode(self):
        try:
            if self._mp_bind_id:
                self.canvas.unbind("<Button-3>", self._mp_bind_id)
            self._mp_bind_id = None
        except tk.TclError:
            pass
        # State/binding/canvas-overlay teardown only - see _leave_measure_mode.
        self.magnify_mode_active = False
        self._magnify_leave(None)
        self.canvas.unbind("<Motion>")
        self.canvas.unbind("<Leave>")
        self.canvas.configure(cursor="crosshair")
        self.magnify_source_image = None

    def exit_magnify_mode(self):
        log.info("Exiting magnify mode")
        self._leave_magnify_mode()
        # See exit_measure_mode: return to Tools submenu, not root.
        self.show_main_toolbar(reset_tools_row=False)

    def _magnify_leave(self, event):
        if self.magnify_lens_item_id:
            self.canvas.delete(self.magnify_lens_item_id)
            self.magnify_lens_item_id = None
        if self.magnify_cursor_item_id:
            self.canvas.delete(self.magnify_cursor_item_id)
            self.magnify_cursor_item_id = None
        old = getattr(self, "_magnify_border_id", None)
        if old:
            try:
                self.canvas.delete(old)
            except tk.TclError:
                pass
            self._magnify_border_id = None

    def _magnify_motion(self, event):
        now = time.monotonic()
        last = getattr(self, "_magnify_last_update", 0)
        if now - last < 0.03:
            return
        self._magnify_last_update = now
        cx, cy = self._event_canvas_xy(event)
        self._draw_magnifier(cx, cy)

    def _draw_magnifier(self, canvas_cx, canvas_cy):
        if self._mp_pin is not None:
            self._mp_hover_circle(canvas_cx, canvas_cy)
            return
        self._draw_magnifier_base(canvas_cx, canvas_cy)
        try:
            old = getattr(self, "_magnify_border_id", None)
            if old:
                self.canvas.delete(old)
                self._magnify_border_id = None
            lid = self.magnify_lens_item_id
            if lid:
                bb = self.canvas.bbox(lid)
                if bb:
                    self._magnify_border_id = self.canvas.create_oval(bb[0], bb[1], bb[2] - 1, bb[3] - 1, outline="#00b4ff", width=2)
                    if self.magnify_cursor_item_id:
                        self.canvas.tag_raise(self.magnify_cursor_item_id)
        except tk.TclError:
            pass

    def _draw_magnifier_base(self, canvas_cx, canvas_cy):
        """
        canvas_cx/canvas_cy are raw canvas-space mouse coordinates - used
        directly to place the lens/cursor overlay. Sampling happens against
        magnify_source_image, which is get_working_image() - the canvas
        composite once the canvas layer is active (canvas_to_img already
        gives coordinates directly in that same frame, no further offset
        needed), or the plain crop before that.
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
        if self._mp_pin is not None:
            self._mp_draw()

    def set_magnify_level(self, val):
        self.magnify_level = int(float(val))
        if self._mp_pin is not None:
            self._mp_draw()

    def show_magnify_toolbar(self):
        self.clear_toolbar()
        frame = self.new_toolbar_frame()
        main_row = tk.Frame(frame, bg="#222222")
        main_row.pack(side="top", fill="x")
        self._build_tools_row(main_row, active="magnify", done=self.exit_magnify_mode)
        self._build_status_row(main_row, frame)
        slider_row = tk.Frame(frame, bg="#222222")
        slider_row.pack(side="top", fill="x")
        lens_var = tk.IntVar(value=self.magnify_lens_size)
        self._pack_labeled_slider(slider_row, "Lens size", lens_var, 40, 300, self.set_magnify_lens_size, anchor="n")
        zoom_var = tk.IntVar(value=self.magnify_level)
        self._pack_labeled_slider(slider_row, "Zoom", zoom_var, 2, 16, self.set_magnify_level, anchor="n")
        self.finish_toolbar_placement(frame)
        self._set_window_title("magnify")
        result = None
        try:
            root_w = self.toolbar_container if self.toolbar_container is not None else self.root
            lbl = _mp_find_label(root_w, "Lens size")
            if lbl is None:
                log.warning("Magnify toolbar: could not find the Lens size row to add Reset (non-fatal)")
                return result
            row = lbl.master
            first = row.winfo_children()[0]
            tk.Button(row, text="Reset", pady=BUTTON_PADY, command=self._mp_reset).pack(side="left", anchor="n", padx=2, pady=4, before=first)
            tk.Label(row, text="Right-click: pin / grab the lens", fg="#888888", bg="#222222").pack(side="left", anchor="n", padx=(10, 2), pady=4)
        except tk.TclError:
            pass
        return result

    # ---- Pin (pin a region to the desktop as its own floating window) ----
    #

    def enter_pin_mode(self):
        log.info("Entering pin mode")
        self.pin_mode_active = True
        self.pin_pending_start = None
        self._clear_pin_preview()
        self.canvas.unbind("<ButtonPress-1>")
        self.canvas.unbind("<B1-Motion>")
        self.canvas.unbind("<ButtonRelease-1>")
        self.canvas.bind("<ButtonPress-1>", self._pin_press)
        self.canvas.bind("<B1-Motion>", self._pin_drag)
        self.canvas.bind("<ButtonRelease-1>", self._pin_release)
        self.show_pin_toolbar()

    def _leave_pin_mode(self):
        """State/canvas-overlay teardown only - see _leave_measure_mode."""
        self.pin_mode_active = False
        self.pin_pending_start = None
        self._clear_pin_preview()
        self._clear_dim_overlay()
        self._close_pins_popup()   # the Pin toolbar (and its Pins button) is about to go away - don't leave the popup floating with nothing to anchor it

    def exit_pin_mode(self):
        log.info("Exiting pin mode")
        self._leave_pin_mode()
        # See exit_measure_mode: return to Tools submenu, not root.
        self.show_main_toolbar(reset_tools_row=False)

    def _clear_pin_preview(self):
        if self.pin_preview_item_id is not None:
            try:
                self.canvas.delete(self.pin_preview_item_id)
            except tk.TclError:
                pass
            self.pin_preview_item_id = None

    def _pin_press(self, event):
        cx, cy = self._event_canvas_xy(event)
        self.pin_pending_start = (cx, cy)
        self._clear_pin_preview()
        self.pin_preview_item_id = self.canvas.create_rectangle(
            cx, cy, cx, cy, outline="#00e0a0", width=2, dash=(4, 2),
        )

    def _pin_drag(self, event):
        if self.pin_pending_start is None:
            return
        cx, cy = self._event_canvas_xy(event)
        sx, sy = self.pin_pending_start
        self.canvas.coords(self.pin_preview_item_id, sx, sy, cx, cy)
        ix1, iy1 = self.canvas_to_img(min(sx, cx), min(sy, cy))
        ix2, iy2 = self.canvas_to_img(max(sx, cx), max(sy, cy))
        self._show_dim_overlay(cx, cy, f"{round(abs(ix2 - ix1))} x {round(abs(iy2 - iy1))}")

    def _pin_release(self, event):
        if self.pin_pending_start is None:
            return
        cx, cy = self._event_canvas_xy(event)
        sx, sy = self.pin_pending_start
        self.pin_pending_start = None
        self._clear_pin_preview()
        self._clear_dim_overlay()
        if abs(cx - sx) < 6 or abs(cy - sy) < 6:
            log.info("Pin: dragged region too small (accidental click?) - not pinning anything")
            return
        ix1, iy1 = self.canvas_to_img(min(sx, cx), min(sy, cy))
        ix2, iy2 = self.canvas_to_img(max(sx, cx), max(sy, cy))
        self._launch_pinned_region(ix1, iy1, ix2, iy2)
        # Multi-pin: stay in pin mode so another region can be dragged
        # right away.
        self._refresh_pins_dropdown()
        self.root.after(800, self._refresh_pins_dropdown)

    def _launch_pinned_region(self, x1, y1, x2, y2):
        """
        Crops (x1,y1,x2,y2) - image-space coordinates, same frame
        sel_coords/img_to_canvas/canvas_to_img already use - out of
        whatever's currently the best available rendering of the capture
        (get_working_image: the baked edit result if there's been any
        editing, otherwise the plain screenshot), saves it to its own temp
        PNG, and launches the detached pin-viewer process pointed at it.
        """
        try:
            source = self.get_working_image().convert("RGB")
        except Exception:
            log.exception("Pin: could not get the current image to crop from - aborting")
            return
        iw, ih = source.size
        bx1 = max(0, min(iw, int(round(x1))))
        by1 = max(0, min(ih, int(round(y1))))
        bx2 = max(0, min(iw, int(round(x2))))
        by2 = max(0, min(ih, int(round(y2))))
        if bx2 - bx1 < 4 or by2 - by1 < 4:
            log.info(f"Pin: cropped region ended up too small ({bx2 - bx1}x{by2 - by1}) - not pinning anything")
            return
        cropped = source.crop((bx1, by1, bx2, by2))
        fd, tmp_path = tempfile.mkstemp(suffix=".png", prefix="snippysnappy-pin-")
        os.close(fd)
        try:
            cropped.save(tmp_path)
        except Exception:
            log.exception(f"Pin: failed to save cropped region to {tmp_path!r} - aborting")
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except Exception:
                pass
            return
        pin_id = str(uuid.uuid4())
        if not hasattr(self, "_owned_pin_ids"):
            self._owned_pin_ids = set()
        self._owned_pin_ids.add(pin_id)
        try:
            pin_env = dict(os.environ)
            existing_pp = pin_env.get("PYTHONPATH")
            pin_env["PYTHONPATH"] = str(INSTALL_DIR) + (os.pathsep + existing_pp if existing_pp else "")
            subprocess.Popen(
                [
                    sys.executable, "-m", "snippysnappy", "--pin-view", tmp_path,
                    "--pin-id", pin_id,
                    "--pin-proportional", "1" if self.pin_proportional_resize_var.get() else "0",
                ],
                start_new_session=True,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=pin_env,
            )
            log.info(f"Pin: launched a floating pin window for {bx2 - bx1}x{by2 - by1} region ({tmp_path}, pin_id={pin_id})")
        except Exception:
            log.exception("Pin: failed to launch the detached pin-viewer process")
            self._owned_pin_ids.discard(pin_id)
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except Exception:
                pass

    def show_pin_toolbar(self):
        self.clear_toolbar()
        frame = self.new_toolbar_frame()
        main_row = tk.Frame(frame, bg="#222222")
        main_row.pack(side="top", fill="x")
        self._build_tools_row(main_row, active="pin", done=self.exit_pin_mode)
        self._build_status_row(main_row, frame)

        context_row = tk.Frame(frame, bg="#222222")
        context_row.pack(side="top", fill="x")

        # Pins list: a lazy-loaded popup listing every pin currently open -
        # this process's own pins always, plus (opt-in, since it's a
        # roster round-trip) pins from OTHER snippysnappy processes. This
        # is a hand-built Toplevel (see _open_pins_popup), not a native
        # tk.Menu.
        if not hasattr(self, "_pins_show_remote_var"):
            self._pins_show_remote_var = tk.BooleanVar(value=False)
        self._pins_popup = None
        self._pins_menubutton = tk.Button(
            context_row, text="Pins (0)", pady=BUTTON_PADY, relief="raised",
            command=self._toggle_pins_popup,
        )
        self._pins_menubutton.pack(side="left", anchor="n", padx=2, pady=4)
        tk.Button(
            context_row, text="Clear", pady=BUTTON_PADY, command=self._clear_owned_pins,
        ).pack(side="left", anchor="n", padx=2, pady=4)
        tk.Label(
            context_row, text="Drag a region to float it on your desktop",
            fg="#aaaaaa", bg="#222222",
        ).pack(side="left", anchor="n", padx=(8, 2), pady=4)

        tk.Checkbutton(
            context_row, text="Proportional", variable=self.pin_proportional_resize_var,
            bg="#222222", fg="white", selectcolor="#444444",
            activebackground="#222222", activeforeground="white",
        ).pack(side="right", anchor="n", padx=(2, 8), pady=4)
        tk.Checkbutton(
            context_row, text="Show other windows' pins", variable=self._pins_show_remote_var,
            bg="#222222", fg="white", selectcolor="#444444",
            activebackground="#222222", activeforeground="white",
            command=self._refresh_pins_dropdown,
        ).pack(side="right", anchor="n", padx=(4, 2), pady=4)

        self.finish_toolbar_placement(frame)
        self._set_window_title("pin")
        self._refresh_pins_dropdown()
        result = None
        try:
            _rename_other_windows(self.toolbar_container if self.toolbar_container is not None else self.root)
        except tk.TclError:
            pass
        return result

    def _pins_dropdown_entries(self):
        """
        [(pin_id, label, is_remote, client_id_or_None), ...].

        IMPORTANT: every pin - including ones made from THIS very window -
        runs as its own separate, fully detached OS process (see
        _launch_pinned_region/run_pin_viewer), so it is NEVER in this
        process's own _LOCAL_PINS.
        """
        entries = [(pin.window_id, pin._document_label(), False, None) for pin in _LOCAL_PINS]
        owned = getattr(self, "_owned_pin_ids", set())
        show_others = self._pins_show_remote_var.get()
        for cid, pin_id, label in remote_pins():
            if pin_id in owned or show_others:
                entries.append((pin_id, label, True, cid))
        return entries

    def _refresh_pins_dropdown(self):
        """
        Refreshes the "Pins (N)" button label, and rebuilds the popup's
        rows too if it's currently open - called right after a new pin is
        launched (_pin_release), whenever the "other windows' pins"
        checkbox is toggled, whenever a pin is focused/closed from the
        popup, and once when the Pin toolbar is first shown. No-op if the
        Pin toolbar isn't currently up .
        """
        menubutton = getattr(self, "_pins_menubutton", None)
        if menubutton is None or not menubutton.winfo_exists():
            return
        entries = self._pins_dropdown_entries()
        menubutton.configure(text=f"Pins ({len(entries)})")
        popup = getattr(self, "_pins_popup", None)
        if popup is not None and popup.winfo_exists():
            self._render_pins_popup_rows()

    def _toggle_pins_popup(self):
        popup = getattr(self, "_pins_popup", None)
        if popup is not None and popup.winfo_exists():
            self._close_pins_popup()
        else:
            self._open_pins_popup()

    def _close_pins_popup(self):
        popup = getattr(self, "_pins_popup", None)
        if popup is not None:
            try:
                popup.destroy()
            except Exception:
                pass
        self._pins_popup = None

    def _open_pins_popup(self):
        """
        A small borderless Toplevel anchored right under the Pins button -
        see the comment in show_pin_toolbar for why this replaced a native
        tk.Menu. Closes itself on Escape, when a row is left-clicked (to
        focus that pin), or when the Pins button is clicked again.
        """
        self._close_pins_popup()
        popup = tk.Toplevel(self.root, bg="#1c1c1c", highlightthickness=1, highlightbackground="#444444")
        popup.overrideredirect(True)
        popup.attributes("-topmost", True)
        x = self._pins_menubutton.winfo_rootx()
        y = self._pins_menubutton.winfo_rooty() + self._pins_menubutton.winfo_height()
        popup.geometry(f"+{x}+{y}")
        self._pins_popup = popup
        self._render_pins_popup_rows()
        popup.bind("<Escape>", lambda e: self._close_pins_popup())
        try:
            popup.focus_force()
        except Exception:
            pass

    def _render_pins_popup_rows(self):
        popup = getattr(self, "_pins_popup", None)
        if popup is None or not popup.winfo_exists():
            return
        for w in popup.winfo_children():
            w.destroy()
        entries = self._pins_dropdown_entries()
        owned = getattr(self, "_owned_pin_ids", set())
        bg, hover = "#1c1c1c", "#3a3a3a"
        if not entries:
            tk.Label(popup, text="(no pins open)", bg=bg, fg="#888888", anchor="w", padx=10, pady=6, font=("", 9)).pack(fill="x")
        for pin_id, label, is_remote, cid in entries:
            text = label if pin_id in owned or not is_remote else f"{label} (other window)"
            row = tk.Frame(popup, bg=bg)
            row.pack(fill="x")
            name = tk.Label(row, text=text, bg=bg, fg="white", anchor="w", padx=10, pady=6, font=("", 9), cursor="hand2")
            name.pack(side="left", fill="x", expand=True)
            xbtn = tk.Label(row, text="\u2715", bg=bg, fg="#aaaaaa", padx=8, pady=6, font=("", 9), cursor="hand2")
            xbtn.pack(side="right")

            def on_name_enter(_e, r=row, n=name, x=xbtn):
                for w in (r, n, x):
                    w.configure(bg=hover)

            def on_name_leave(_e, r=row, n=name, x=xbtn):
                for w in (r, n, x):
                    w.configure(bg=bg)

            def on_x_enter(_e, x=xbtn):
                x.configure(fg="#ff5555")

            def on_x_leave(_e, x=xbtn):
                x.configure(fg="#aaaaaa")

            def do_focus(_e, pid=pin_id, remote=is_remote, c=cid):
                self._focus_pin(pid, remote, c)
                self._close_pins_popup()

            def do_close(_e, pid=pin_id, remote=is_remote, c=cid):
                log.info(f"Pins popup: close requested for pid={pid!r}")
                self._close_pin(pid, remote, c)
                self.root.after(600, self._refresh_pins_dropdown)
                return "break"

            name.bind("<Enter>", on_name_enter)
            name.bind("<Leave>", on_name_leave)
            name.bind("<Button-1>", do_focus)
            name.bind("<Button-3>", do_close)
            xbtn.bind("<Enter>", on_x_enter)
            xbtn.bind("<Leave>", on_x_leave)
            xbtn.bind("<Button-1>", do_close)
        tk.Label(
            popup, text="Left-click: focus \u00b7 \u2715 / right-click: close",
            bg=bg, fg="#777777", anchor="w", padx=10, pady=4, font=("", 8),
        ).pack(fill="x")

    def _focus_pin(self, pin_id, is_remote, client_id):
        if is_remote:
            if _coord["role"] is None:
                # Every pin is a separate OS process, so "focus" ALWAYS
                # goes through the cross-process coordinator.
                log.warning(f"Pins list: cannot focus pin {pin_id!r} - this window's coordinator is not active")
                notify("Can't reach that pin", "This window's cross-process coordinator isn't active - see snippysnappy.log", blocking=False)
                return
            log.info(f"Pins list: requesting remote focus for pin {pin_id!r} on client {client_id!r}")
            request_remote_focus(client_id, pin_id)
            return
        pin = _find_local_pin(pin_id)
        if pin is None:
            log.warning(f"Pins list: pin {pin_id!r} no longer exists locally - refreshing list")
            self._refresh_pins_dropdown()
            return
        focus_local_window(pin)

    def _close_pin(self, pin_id, is_remote, client_id):
        if is_remote:
            if _coord["role"] is None:
                log.warning(f"Pins list: cannot close pin {pin_id!r} - this window's coordinator is not active")
                notify("Can't reach that pin", "This window's cross-process coordinator isn't active - see snippysnappy.log", blocking=False)
                return
            log.info(f"Pins list: requesting remote force-close for pin {pin_id!r} on client {client_id!r}")
            request_remote_pin_close(client_id, pin_id)
            return
        pin = _find_local_pin(pin_id)
        if pin is None:
            log.warning(f"Pins list: pin {pin_id!r} no longer exists locally - refreshing list")
        else:
            log.info(f"Pins list: force-closing local pin {pin_id!r} from the Pins list")
            pin._cleanup()
        self._refresh_pins_dropdown()

    def _clear_owned_pins(self):
        """
        Force-closes every pin THIS window made (self._owned_pin_ids) -
        deliberately NOT other windows' pins, even if "show other windows'
        pins" is on: a blanket "close everything" is reasonable for pins
        you made yourself, but silently nuking someone else's pin from
        another process just because it happened to be visible would be
        surprising. Goes through the same _close_pin() routing (remote vs.
        local) each entry's own x button uses, since every pin - including
        our own - is technically a separate process (see
        _pins_dropdown_entries).
        """
        owned = getattr(self, "_owned_pin_ids", set())
        targets = [(pid, is_remote, cid) for pid, _label, is_remote, cid in self._pins_dropdown_entries() if pid in owned]
        if not targets:
            log.info("Pins list: Clear pressed with no owned pins currently open")
            return
        log.info(f"Pins list: Clear pressed - force-closing {len(targets)} pin(s) this window made")
        for pid, is_remote, cid in targets:
            self._close_pin(pid, is_remote, cid)

    # ---- layers ----
    #
    # self.layers is an ordered list (index 0 = bottom of the stack, last =
    # top) of {"id", "name", "kind", "group_key"?}. "background" and
    # "capture" are always present (created in __init__) and hold no
    # elements of their own - they represent the canvas fill color and the
    # raw screenshot respectively, exactly as Resize already edits them;
    # Layers doesn't duplicate that, it only decides where they sit in the
    # stack and whether they're the one currently isolated for viewing.
    # A "strokes" layer holds zero or more edit_strokes entries, found by
    # stroke["layer_id"] == layer["id"] - see _render_layered for how that
    # turns into a picture. "group_key", present only on the auto-created
    # default layers (Draw/Shapes/Snips/Text), is what
    # _get_or_create_default_layer matches against - keyed separately from
    # the display "name" so renaming a layer later doesn't stop it from
    # collecting new same-tool content; a layer born from Split has no
    # group_key, so it never silently absorbs anything new.

    def _new_layer(self, name, kind):
        self._layer_id_counter += 1
        return {"id": f"layer{self._layer_id_counter}", "name": name, "kind": kind}

    def _find_layer(self, layer_id):
        return next((l for l in self.layers if l["id"] == layer_id), None)

    def _get_or_create_default_layer(self, mode):
        group_key = LAYER_MODE_GROUPS.get(mode, "Draw")
        for layer in self.layers:
            if layer.get("group_key") == group_key:
                return layer["id"]
        layer = self._new_layer(group_key, "strokes")
        layer["group_key"] = group_key
        self.layers.append(layer)   # newly auto-created layers land on top by default
        log.info(f"Layer '{group_key}' created (first {mode} element)")
        return layer["id"]

    def _stroke_target_layer_id(self, mode):
        from . import constants as _c
        target_id = None
        if self.isolated_layer_id is not None:
            layer = self._find_layer(self.isolated_layer_id)
            if layer is not None and layer["kind"] == "strokes":
                natural = LAYER_MODE_GROUPS.get(mode, "Draw")
                join = True
                if layer.get("group_key") != natural:
                    if not self.new_layer_on_edit_default:
                        join = True
                    elif self._layer_popup_silenced or not getattr(self, "_layer_confirm_enabled", True):
                        join = False
                    else:
                        join = self._ask_new_layer_or_current(mode, layer) == "current"
                if join:
                    self._last_touched_layer_id = layer["id"]
                    return layer["id"]
        target_id = self._get_or_create_default_layer(mode)
        self._last_touched_layer_id = target_id
        return target_id

    def _resolve_ambiguous_layer_target(self, mode):
        last_touched = self._find_layer(self._last_touched_layer_id) if self._last_touched_layer_id else None
        if last_touched is None or last_touched["kind"] != "strokes":
            return None
        natural_group = LAYER_MODE_GROUPS.get(mode, "Draw")
        if last_touched.get("group_key") == natural_group:
            return None
        if not self.new_layer_on_edit_default:
            return last_touched["id"]
        if self._layer_popup_silenced:
            return None
        choice = self._ask_new_layer_or_current(mode, last_touched)
        return last_touched["id"] if choice == "current" else None

    def _ask_new_layer_or_current(self, mode, current_layer):
        natural_group = LAYER_MODE_GROUPS.get(mode, "Draw")
        dialog = tk.Toplevel(self.root)
        dialog.title("New layer?")
        dialog.transient(self.root)
        dialog.configure(bg="#222222")
        dialog.resizable(False, False)
        tk.Label(
            dialog, justify="left", fg="white", bg="#222222",
            text=(
                f"The last thing you added was in the \u201c{current_layer['name']}\u201d layer.\n"
                f"Put this new {mode} in its own \u201c{natural_group}\u201d layer,\n"
                f"or add it to \u201c{current_layer['name']}\u201d instead?"
            ),
        ).pack(anchor="w", padx=16, pady=(14, 8))
        dont_show_var = tk.IntVar(value=0)
        tk.Checkbutton(
            dialog, text="Don't ask again this session", variable=dont_show_var,
            bg="#222222", fg="white", selectcolor="#444444",
            activebackground="#222222", activeforeground="white",
        ).pack(anchor="w", padx=16, pady=(0, 8))
        result = {"choice": "new"}

        def pick(choice):
            result["choice"] = choice
            if dont_show_var.get():
                self._layer_popup_silenced = True
            dialog.destroy()

        btn_row = tk.Frame(dialog, bg="#222222")
        btn_row.pack(padx=16, pady=(0, 14))
        tk.Button(btn_row, text=f"New \u201c{natural_group}\u201d Layer", pady=BUTTON_PADY, command=lambda: pick("new")).pack(side="left", padx=(0, 6))
        tk.Button(btn_row, text=f"Add to \u201c{current_layer['name']}\u201d", pady=BUTTON_PADY, command=lambda: pick("current")).pack(side="left")
        dialog.protocol("WM_DELETE_WINDOW", lambda: pick("new"))
        dialog.update_idletasks()
        try:
            x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - dialog.winfo_reqwidth()) // 2)
            y = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - dialog.winfo_reqheight()) // 2)
            dialog.geometry(f"+{x}+{y}")
        except tk.TclError:
            pass
        dialog.grab_set()
        dialog.wait_window()
        return result["choice"]

    def _confirm_and_restart_all(self):
        """
        Restart All (Tabs panel, Windows section): the fix for a stale
        hub.
        """
        dialog = tk.Toplevel(self.root)
        dialog.title("Restart All?")
        dialog.transient(self.root)
        dialog.configure(bg="#222222")
        dialog.resizable(False, False)
        tk.Label(
            dialog, justify="left", fg="white", bg="#222222",
            text=(
                "This restarts EVERY connected snippysnappy window and pin\n"
                "(so all of them pick up any code changes), not just this\n"
                "one. Each window's tabs are preserved and reloaded; each\n"
                "pin reopens at its original size/position, not wherever\n"
                "you last resized it."
            ),
        ).pack(anchor="w", padx=16, pady=(14, 8))

        def confirm():
            dialog.destroy()
            log.info("Restart All confirmed - broadcasting to every connected snippysnappy process")
            request_global_restart()

        btn_row = tk.Frame(dialog, bg="#222222")
        btn_row.pack(padx=16, pady=(0, 14))
        tk.Button(btn_row, text="Restart All", pady=BUTTON_PADY, command=confirm).pack(side="left", padx=(0, 6))
        tk.Button(btn_row, text="Cancel", pady=BUTTON_PADY, command=dialog.destroy).pack(side="left")
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        dialog.update_idletasks()
        try:
            x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - dialog.winfo_reqwidth()) // 2)
            y = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - dialog.winfo_reqheight()) // 2)
            dialog.geometry(f"+{x}+{y}")
        except tk.TclError:
            pass
        dialog.grab_set()


    def _show_close_confirm(self, message, on_confirm):
        """Shared yes/no confirm used before closing the ACTIVE tab or window (see _confirm_close_tab_if_active / _confirm_close_window) - gated on WARN_ON_ACTIVE_CLOSE, since closing a background tab/window you aren't looking at doesn't need the same caution."""
        dialog = tk.Toplevel(self.root)
        dialog.title("Close?")
        dialog.transient(self.root)
        dialog.configure(bg="#222222")
        dialog.resizable(False, False)
        tk.Label(dialog, justify="left", fg="white", bg="#222222", text=message, wraplength=280).pack(anchor="w", padx=16, pady=(14, 8))

        def confirm():
            dialog.destroy()
            on_confirm()

        btn_row = tk.Frame(dialog, bg="#222222")
        btn_row.pack(padx=16, pady=(0, 14))
        tk.Button(btn_row, text="Close", pady=BUTTON_PADY, command=confirm).pack(side="left", padx=(0, 6))
        tk.Button(btn_row, text="Cancel", pady=BUTTON_PADY, command=dialog.destroy).pack(side="left")
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        dialog.update_idletasks()
        try:
            x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - dialog.winfo_reqwidth()) // 2)
            y = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - dialog.winfo_reqheight()) // 2)
            dialog.geometry(f"+{x}+{y}")
        except tk.TclError:
            pass
        dialog.grab_set()

    def _confirm_close_tab_if_active(self, index):
        if index == self.current_document_index and WARN_ON_ACTIVE_CLOSE:
            self._show_close_confirm(
                f"Close the ACTIVE tab \u2018{self._document_label()}\u2019? Anything not yet baked/saved in it is lost.",
                lambda: self.close_tab(index),
            )
        else:
            self.close_tab(index)

    def _mark_hidden(self, flag):
        """Backgrounded windows stay in the roster, flagged hidden (shown under the Hidden group)."""
        self._hidden = bool(flag)
        log.info(f"Window {getattr(self, 'window_id', '?')} {'backgrounded (hidden)' if flag else 'shown again'}")
        try:
            broadcast_window_roster()
        except Exception:
            log.exception("Could not broadcast the roster after a hide/show (non-fatal)")
        for w in list(_LOCAL_WINDOWS):
            try:
                w._refresh_side_panel()
            except Exception:
                pass

    def _hide_window(self):
        """Background this window regardless of EXIT_BEHAVIOR (used by drag-into-Hidden): hidden from the screen, kept (tabs, temp files) and listed."""
        try:
            self.root.withdraw()
        except tk.TclError:
            return
        self._mark_hidden(True)

    def _on_mapped(self, event=None):
        if event is not None and event.widget is not self.root:
            return
        if getattr(self, "_hidden", False):
            self._mark_hidden(False)

    def _discard_window(self):
        """
        Really closes this window and throws its work away: temp files go, it
        leaves the roster. The process lives on only if other windows still
        need it (this process's last window ends the process, as before).
        """
        log.info(f"Discarding window {getattr(self, 'window_id', '?')}")
        others = [w for w in _LOCAL_WINDOWS if w is not self]
        is_host = self.root is getattr(self, "_true_root", None)
        try:
            if is_host and others:
                # This window's root is the one the process's mainloop lives on and
                # other windows still use it: clean up and hide instead of destroying.
                self._cleanup_temp_files()
                self.root.withdraw()
            else:
                self._discarding = True
                self.root.destroy()
        except tk.TclError:
            pass
        self._unlist_window()

    def _unlist_window(self):
        """Takes this window out of the Windows list. A withdrawn (backgrounded) window is closed from the user's point of view and must not stay listed."""
        if self in _LOCAL_WINDOWS:
            _LOCAL_WINDOWS.remove(self)
            log.info(f"Window {getattr(self, 'window_id', '?')} closed - removed from the Windows list")
        try:
            broadcast_window_roster()
        except Exception:
            log.exception("Could not broadcast the roster after a window closed (non-fatal)")
        for other in list(_LOCAL_WINDOWS):
            try:
                other._refresh_side_panel()
            except Exception:
                pass

    def _force_close(self):
        _toast_discard(self)
        # Used by the Windows-panel x and remote close requests: closes the window AND guarantees it leaves the list.
        self._discard_window()

    def _set_window_name(self, *a, **k):
        r = self._set_window_name_base(*a, **k)
        try:
            from .coordinator import broadcast_window_roster
            broadcast_window_roster()
            if False:   # the window registers / labels settle a moment after the transition
                self.root.after(300, broadcast_window_roster)
        except Exception:
            log.exception("roster broadcast failed (non-fatal)")
        return r

    def _set_window_name_base(self, app, text):
        app._window_name_override = text
        log.info(f"Window renamed to {text!r}")
        broadcast_window_roster()
        self._refresh_side_panel()

    def _on_windows_reorder(self, key, to_index):
        keys = list(getattr(self, "_windows_shown_keys", []))
        self._windows_order = move_in_order(keys, key, to_index)
        self._refresh_side_panel()

    def _confirm_close_window(self, target_app, is_active):
        _toast_discard(self)
        if is_active and WARN_ON_ACTIVE_CLOSE:
            self._show_close_confirm(
                "Close this ACTIVE window? Anything not yet baked/saved in any of its tabs is lost.",
                target_app._discard_window,
            )
        else:
            target_app._discard_window()

    def _append_stroke(self, stroke):
        """
        The single path every new element/stroke should be added through
        (as opposed to redo_last_stroke's restore, or a snapshot restore,
        which already carry a layer_id and must NOT be reassigned -
        setdefault makes this a no-op for those).

        While a layer is isolated, an immediate re-bake is forced here so
        the new element is instantly ghosting-correct (dimmed if it
        landed on a different layer than the one being viewed, full
        strength if it landed on the isolated one) rather than showing up
        at full, un-ghosted opacity until the next real bake (Done,
        Undo/Redo, a tool switch, etc). Snip/Shape/Text already always
        re-bake immediately after finalizing regardless of isolation, so
        this only actually changes anything for Pen/Spray/Eraser, which
        deliberately skip a re-bake per stroke for performance (drawing a
        long stroke would otherwise re-render the whole document on every
        release) - that skip is fine and imperceptible when nothing is
        isolated (nothing needs to ghost), but was exactly why an isolated
        view looked "stuck" showing every new stroke as if it belonged to
        whatever was isolated, right up until the next real bake.
        """
        stroke.setdefault("layer_id", self._stroke_target_layer_id(stroke.get("mode")))
        # A stable id, independent of this stroke's position in
        # edit_strokes (which shifts under undo/redo/snapshot-restore) or
        # its own dict identity (which _snapshot_strokes deliberately
        # breaks via a shallow dict() copy on every full-state snapshot -
        # see _capture_full_state). Needed so a snip that consumes other
        # elements underneath it (see _finalize_snip) can refer back to
        # exactly those elements later - to undo the consumption if the
        # snip itself is undone - without relying on an index or object
        # identity that won't have survived.
        stroke.setdefault("eid", uuid.uuid4().hex)
        self.edit_strokes.append(stroke)
        self._refresh_layers_panel_if_open()
        if self.isolated_layer_id is not None:
            self._recompute_baked_image()

    def _refresh_layers_panel_if_open(self):
        if self.side_panel_mode == "layers":
            self._refresh_side_panel()

    def _stroke_visible_for_editing(self, stroke):
        """
        True unless a STROKE layer is isolated and this element belongs
        to a different one. Isolating Background or Capture never hides
        anything - those are a viewing backdrop only, per design; only
        isolating an actual stroke layer restricts what's shown/clickable
        to that layer's own content (consistent with the display: an
        element that isn't shown obviously shouldn't be clickable either).
        """
        if self.isolated_layer_id is None:
            return True
        layer = self._find_layer(self.isolated_layer_id)
        if layer is None or layer["kind"] != "strokes":
            return True
        return stroke.get("layer_id") == self.isolated_layer_id

    def _layer_display_label(self, *args, **kwargs):
        text = self._layer_display_label_base(*args, **kwargs)
        return text.replace(" (canvas)", "").replace(" (screenshot)", "")

    def _layer_display_label_base(self, layer):
        depth = self.layers.index(layer) if layer in self.layers else -1
        prefix = f"{depth}: "
        if layer["kind"] == "background":
            return f"{prefix}{layer['name']} (canvas)"
        if layer["kind"] == "capture":
            return f"{prefix}{layer['name']} (screenshot)"
        count = sum(1 for s in self.edit_strokes if s.get("layer_id") == layer["id"] and not s.get("removed"))
        return f"{prefix}{layer['name']} ({count})"

    def _panel_row_press(self, event, index, drag_index_attr, drag_start_attr, drag_active_attr):
        setattr(self, drag_index_attr, index)
        setattr(self, drag_start_attr, event.y_root)
        setattr(self, drag_active_attr, False)

    def _panel_row_drag(self, event, drag_index_attr, drag_start_attr, drag_active_attr, row_widgets, container, reversed_display, label_for_index):
        drag_index = getattr(self, drag_index_attr)
        if drag_index is None:
            return
        if not getattr(self, drag_active_attr):
            if abs(event.y_root - getattr(self, drag_start_attr)) < 4:
                return
            setattr(self, drag_active_attr, True)
            self._start_panel_drag_visual(container, row_widgets, drag_index, label_for_index(drag_index))
        self._update_panel_drag_visual(event, row_widgets, drag_index, len(row_widgets), container, reversed_display)

    def _panel_row_release(self, event, drag_index_attr, drag_start_attr, drag_active_attr, row_widgets, container, reversed_display, move_callback):
        drag_index = getattr(self, drag_index_attr)
        was_active = getattr(self, drag_active_attr)
        setattr(self, drag_index_attr, None)
        setattr(self, drag_active_attr, False)
        self._end_panel_drag_visual()
        if drag_index is None or not was_active:
            return
        drop_index = self._compute_panel_drop_index(event.y_root, row_widgets, drag_index, len(row_widgets), reversed_display)
        if drop_index != drag_index:
            move_callback(drag_index, drop_index)

    def _compute_panel_drop_index(self, cursor_y_root, row_widgets, drag_index, total, reversed_display):
        rows_above = 0
        for idx, cell in row_widgets:
            if idx == drag_index:
                continue
            try:
                mid_y = cell.winfo_rooty() + cell.winfo_height() / 2
            except tk.TclError:
                continue
            if mid_y < cursor_y_root:
                rows_above += 1
        if reversed_display:
            return (total - 1) - rows_above
        return rows_above

    def _start_panel_drag_visual(self, container, row_widgets, drag_index, label_text):
        cell = next((c for i, c in row_widgets if i == drag_index), None)
        if cell is not None:
            try:
                cell.configure(highlightbackground="#00b4ff", highlightthickness=2)
            except tk.TclError:
                cell = None
        self._panel_drag_highlighted_cell = cell
        preview = tk.Label(container, text=label_text, bg="#00b4ff", fg="white", padx=6, pady=2, relief="raised", bd=1)
        preview.place(x=4, y=4)
        self._panel_drag_preview = preview
        indicator = tk.Frame(container, bg="#00b4ff", height=3)
        self._panel_drag_indicator = indicator

    def _update_panel_drag_visual(self, event, row_widgets, drag_index, total, container, reversed_display):
        try:
            base_x = container.winfo_rootx()
            base_y = container.winfo_rooty()
        except tk.TclError:
            return
        if self._panel_drag_preview is not None:
            try:
                self._panel_drag_preview.place(x=event.x_root - base_x + 12, y=event.y_root - base_y + 4)
            except tk.TclError:
                pass
        drop_index = self._compute_panel_drop_index(event.y_root, row_widgets, drag_index, total, reversed_display)
        y_line = self._panel_drop_index_to_y(drop_index, row_widgets, drag_index, reversed_display, base_y)
        if self._panel_drag_indicator is not None and y_line is not None:
            try:
                width = max(20, container.winfo_width() - 8)
                self._panel_drag_indicator.place(x=4, y=y_line, width=width, height=3)
            except tk.TclError:
                pass

    def _panel_drop_index_to_y(self, drop_index, row_widgets, drag_index, reversed_display, base_y):
        others = [(i, c) for i, c in row_widgets if i != drag_index]
        if not others:
            return None
        ordered = sorted(others, key=lambda ic: ic[1].winfo_rooty())
        if reversed_display:
            after_count = sum(1 for i, c in ordered if i > drop_index)
        else:
            after_count = sum(1 for i, c in ordered if i < drop_index)
        if after_count < len(ordered):
            target_cell = ordered[after_count][1]
            try:
                return target_cell.winfo_rooty() - base_y - 2
            except tk.TclError:
                return None
        last_cell = ordered[-1][1]
        try:
            return last_cell.winfo_rooty() - base_y + last_cell.winfo_height() + 2
        except tk.TclError:
            return None

    def _end_panel_drag_visual(self):
        if self._panel_drag_highlighted_cell is not None:
            try:
                self._panel_drag_highlighted_cell.configure(highlightbackground="#555555", highlightthickness=1)
            except tk.TclError:
                pass
            self._panel_drag_highlighted_cell = None
        if self._panel_drag_preview is not None:
            try:
                self._panel_drag_preview.destroy()
            except tk.TclError:
                pass
            self._panel_drag_preview = None
        if self._panel_drag_indicator is not None:
            try:
                self._panel_drag_indicator.destroy()
            except tk.TclError:
                pass
            self._panel_drag_indicator = None

    def _start_inline_rename(self, cell, name_btn, current_text, pack_kwargs, on_commit):
        """
        Shared by both the Layers and Tabs panels: swaps a row's name
        button for a plain Entry, pre-filled and selected, right in the
        same spot (same pack side/fill/expand) - Enter or clicking away
        commits (calling on_commit(new_text) unless it's empty or
        unchanged), Escape cancels. Either way ends by rebuilding the
        whole panel, which is simplest and always leaves things showing
        the current, correct state.
        """
        name_btn.pack_forget()
        var = tk.StringVar(value=current_text)
        entry = tk.Entry(cell, textvariable=var)
        entry.pack(**pack_kwargs)
        entry.focus_set()
        entry.selection_range(0, "end")
        done = {"committed": False}

        def commit(event=None):
            if done["committed"]:
                return
            done["committed"] = True
            new_text = var.get().strip()
            if new_text and new_text != current_text:
                on_commit(new_text)
            self._refresh_side_panel()

        def cancel(event=None):
            if done["committed"]:
                return
            done["committed"] = True
            self._refresh_side_panel()

        entry.bind("<Return>", commit)
        entry.bind("<FocusOut>", commit)
        entry.bind("<Escape>", cancel)

    def _rename_layer_inline(self, layer, cell, name_btn):
        self._start_inline_rename(
            cell, name_btn, layer["name"],
            {"side": "top", "fill": "x", "padx": 2, "pady": 2},
            lambda new_name: layer.__setitem__("name", new_name),
        )

    # ---- isolation (view) ----

    def isolate_layer(self, layer_id):
        """
        View just this one layer, transparent elsewhere - see
        _render_layered/_current_display_image. Works from the main
        toolbar too (Layers stays usable right up until an actual
        save/copy), not just inside Edit - which means this can be the
        very first thing that ever needs the canvas frame's own
        coordinate space (canvas_x1/y1/x2/y2), normally set up on first
        entering Edit/Resize/OCR/Magnify - so it must activate that here
        too, or _canvas_dims() crashes on canvas_x2/y2 still being None.
        """
        self._activate_canvas_if_needed()
        self.isolated_layer_id = layer_id
        layer = self._find_layer(layer_id)
        if layer is not None and layer["kind"] == "strokes":
            self._last_touched_layer_id = layer_id
        log.info(f"Layer isolated for viewing: {layer['name'] if layer else layer_id}")
        self._refresh_after_layer_change()

    def show_full(self):
        self.isolated_layer_id = None
        log.info("Layer isolation cleared - showing the full merged document")
        self._refresh_after_layer_change()

    def _refresh_after_layer_change(self):
        """
        Isolating/clearing/reordering/merging/splitting never changes the
        DOCUMENT (self.baked_image already holds the true merge) - only
        what's currently on screen, and which elements' handles are
        interactable. Cheap: no re-bake needed, just re-show and, if
        inside Edit, refresh which elements' handles are visible, and, if
        the side panel is open, refresh its content (row labels/counts,
        which one is highlighted as isolated, etc).
        """
        if self._sync_base_dims_for_mode():
            self._recalculate_display(keep_position=True)
        else:
            self._refresh_base_display()
        if self.edit_mode_active:
            self._draw_snip_ui()
        if self.side_panel_mode == "layers":
            self._refresh_side_panel()

    # ---- reordering ----

    def move_layer(self, from_index, to_index):
        if not (0 <= from_index < len(self.layers)) or not (0 <= to_index < len(self.layers)):
            return
        layer = self.layers.pop(from_index)
        self.layers.insert(to_index, layer)
        log.info(f"Layer '{layer['name']}' moved to position {to_index}")
        self._recompute_baked_image()
        self._refresh_after_layer_change()

    # ---- merge ----

    def merge_layers(self, source_id, target_id):
        """
        Moves every element from source into target and removes source -
        stroke layers only (merging a raster Background/Capture layer into
        another isn't supported here; use Resize for those). If source was
        the isolated layer, isolation follows its content to target -
        "the layer you were looking at" now lives there.
        """
        if source_id == target_id:
            return
        src, tgt = self._find_layer(source_id), self._find_layer(target_id)
        if src is None or tgt is None or src["kind"] != "strokes" or tgt["kind"] != "strokes":
            log.warning("Merge only supports one stroke layer into another - ignoring")
            return
        for stroke in self.edit_strokes:
            if stroke.get("layer_id") == source_id:
                stroke["layer_id"] = target_id
        self.layers = [l for l in self.layers if l["id"] != source_id]
        if self.isolated_layer_id == source_id:
            self.isolated_layer_id = target_id
        log.info(f"Layer '{src['name']}' merged into '{tgt['name']}'")
        self._recompute_baked_image()
        self._refresh_after_layer_change()

    # ---- split ----
    #
    # Split only acts on the currently ISOLATED stroke layer (there's no
    # ambiguity about which layer's elements you mean to split, since
    # you're looking right at just that one). Drag a rectangle over the
    # canvas; whatever of that layer's elements overlaps it moves to a
    # new layer named after the original with an incrementing suffix
    # (Snip -> Snip 1) - matching a plain click-and-drag rectangle rather
    # than a freehand highlighter path, since a rectangle gives an
    # unambiguous in-or-out per element instead of needing partial-overlap
    # rules for an arbitrary shape.

    def enter_split_mode(self):
        layer = self._find_layer(self.isolated_layer_id) if self.isolated_layer_id else None
        if layer is None or layer["kind"] != "strokes":
            notify("Can't split", "Isolate a layer first (pick one from the Layers panel), then Split.", blocking=False, anchor_window=self.root)
            return
        if not self.edit_mode_active:
            self.enter_edit_mode()
        self._split_mode_active = True
        self._split_start = None
        self._cancel_pending_shape()
        self._cancel_pending_snip_shape()
        self._cancel_draw_select_rect()
        if self._pending_text_widget is not None:
            self._cancel_text_entry()
        self._hide_snip_ui()
        self.canvas.unbind("<ButtonPress-1>")
        self.canvas.unbind("<B1-Motion>")
        self.canvas.unbind("<ButtonRelease-1>")
        self.canvas.bind("<ButtonPress-1>", self._split_press)
        self.canvas.bind("<B1-Motion>", self._split_drag)
        self.canvas.bind("<ButtonRelease-1>", self._split_release)
        log.info(f"Split mode entered for layer '{layer['name']}' - drag a rectangle over the elements to split out")
        self.show_edit_toolbar()

    def _exit_split_mode(self):
        self._split_mode_active = False
        self._split_start = None
        if self._split_preview_item_id:
            self.canvas.delete(self._split_preview_item_id)
            self._split_preview_item_id = None
        self.canvas.unbind("<ButtonPress-1>")
        self.canvas.unbind("<B1-Motion>")
        self.canvas.unbind("<ButtonRelease-1>")
        self.canvas.bind("<ButtonPress-1>", self.edit_press)
        self.canvas.bind("<B1-Motion>", self.edit_drag)
        self.canvas.bind("<ButtonRelease-1>", self.edit_release)
        self.canvas.bind("<Double-Button-1>", self._edit_double_click)
        self.show_edit_toolbar()

    def _split_press(self, event):
        self._split_start = self._event_to_capture_local(event)

    def _split_drag(self, event):
        if self._split_start is None:
            return
        if self._split_preview_item_id:
            self.canvas.delete(self._split_preview_item_id)
        x0, y0 = self.capturelocal_to_screen(*self._split_start)
        x1, y1 = self._event_canvas_xy(event)
        self._split_preview_item_id = self.canvas.create_rectangle(x0, y0, x1, y1, outline="#ff00ff", width=2, dash=(4, 2))

    def _split_release(self, event):
        if self._split_start is None:
            return
        x, y = self._event_to_capture_local(event)
        x0, y0 = self._split_start
        self._split_start = None
        if self._split_preview_item_id:
            self.canvas.delete(self._split_preview_item_id)
            self._split_preview_item_id = None
        rect = (min(x0, x), min(y0, y), max(x0, x), max(y0, y))
        if rect[2] - rect[0] < 4 or rect[3] - rect[1] < 4:
            log.info("Split: selection too small - cancelling, nothing split")
            self._exit_split_mode()
            return
        self._finish_split(rect)

    @staticmethod
    def _element_bbox_capture_local(stroke):
        """
        A simple (rotation-ignoring) bounding box for split's overlap
        test - a predictable "did my rectangle catch this element" rule
        matters more here than pixel-perfect precision for a rotated shape.
        """
        mode = stroke.get("mode")
        if mode in ("snip", "text", "shape"):
            ox, oy = stroke["origin"]
            return (ox, oy, ox + stroke["w"], oy + stroke["h"])
        if mode == "spray":
            pts = [(x, y) for x, y, _r in stroke.get("dots", [])]
        else:
            pts = stroke.get("points", [])
        if not pts:
            return None
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        pad = stroke.get("width", EDIT_PEN_WIDTH) / 2
        return (min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad)

    @staticmethod
    def _rects_overlap(a, b):
        return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])

    def _next_split_name(self, base_name):
        m = re.match(r"^(.*?)(?: (\d+))?$", base_name)
        root = m.group(1)
        existing = {l["name"] for l in self.layers}
        n = 1
        while f"{root} {n}" in existing:
            n += 1
        return f"{root} {n}"

    def _finish_split(self, rect_capture_local):
        layer = self._find_layer(self.isolated_layer_id)
        caught = []
        for stroke in self.edit_strokes:
            if stroke.get("layer_id") != layer["id"] or stroke.get("removed"):
                continue
            bbox = self._element_bbox_capture_local(stroke)
            if bbox is not None and self._rects_overlap(bbox, rect_capture_local):
                caught.append(stroke)
        if not caught:
            log.info("Split: no elements of this layer were inside the selection - nothing split")
            notify("Nothing to split", "No elements of this layer were inside the selection.", blocking=False, anchor_window=self.root)
            self._exit_split_mode()
            return
        new_layer = self._new_layer(self._next_split_name(layer["name"]), "strokes")
        self.layers.append(new_layer)
        for stroke in caught:
            stroke["layer_id"] = new_layer["id"]
        self.isolated_layer_id = new_layer["id"]   # jump to viewing what was just split out - you clearly wanted to be there
        log.info(f"Split: {len(caught)} element(s) moved from '{layer['name']}' into new layer '{new_layer['name']}'")
        self._recompute_baked_image()
        if self.side_panel_mode == "layers":
            self._refresh_side_panel()
        self._exit_split_mode()

    # ---- side panel (Layers now; Tabs will share this same mechanism) ----
    #
    # A persistent vertical strip on the right side of the window, above
    # the main toolbar strip, separated from the canvas by a hard
    # (beveled) line (self.side_panel_separator) and capped at its own
    # bottom by a second hard line separating it from the main toolbar
    # below - see _build_decorated_window_chrome for the surrounding
    # structure. Empty/not packed at all (zero width) whenever "None" is
    # selected; picking Layers/Tabs (or clearing back to None) also grows/
    # shrinks the window by exactly the width the panel adds, so the
    # canvas viewport doesn't just get squeezed by whatever's left over.
    # Selected from the status row's dropdown (None/Layers/Tabs), not a
    # toggle button - see _build_status_row.

    def _set_ghost_layer_opacity(self, val):
        self.ghost_layer_opacity = max(0, min(100, int(float(val)))) / 100.0
        if self.isolated_layer_id is None:
            return
        if self._sync_base_dims_for_mode():
            self._recalculate_display(keep_position=True)
        else:
            self._refresh_base_display()

    def _side_panel_mode_label(self):
        return {"layers": "Layers", "tabs": "Tabs"}.get(self.side_panel_mode, "None")

    def _on_side_panel_choice(self, choice):
        self.side_panel_mode = {"None": None, "Layers": "layers", "Tabs": "tabs"}.get(choice)
        self._refresh_side_panel()

    def _refresh_side_panel(self, *args, **kwargs):
        result = self._refresh_side_panel_base(*args, **kwargs)
        try:
            self._set_window_title(getattr(self, "_last_submenu", None))   # keeps the tab name in the title current
        except Exception:
            pass
        mode = getattr(self, "side_panel_mode", None)
        last = getattr(self, "_last_panel_mode_seen", "unset")
        self._last_panel_mode_seen = mode
        if PANEL_START_AT_TOP and mode != last:
            canvas = getattr(self, "_side_panel_canvas", None)
            if canvas is not None:
                try:
                    self.root.update_idletasks()
                    canvas.yview_moveto(0)
                except tk.TclError:
                    pass
        return result

    def _refresh_side_panel_base(self):
        """
        Tears down and rebuilds the panel for self.side_panel_mode (called
        from the dropdown, _refresh_after_layer_change, and roster updates).

        The content lives in a scrollable canvas (side_panel.make_scrollable):
        the wheel over the panel scrolls it instead of the work area, a thin
        scrollbar shows position, and the bottom border is packed first so
        overflowing content can never squeeze it out. Width = the widest
        panel's natural width plus the scrollbar, capped at
        SIDE_PANEL_WIDTH_RESERVE so long names crop instead of widening the
        panel. Scroll position survives a rebuild.
        """
        prev_scroll = None
        old_canvas = getattr(self, "_side_panel_canvas", None)
        if old_canvas is not None:
            try:
                prev_scroll = old_canvas.yview()[0]
            except tk.TclError:
                prev_scroll = None
        self._side_panel_canvas = None
        for w in self.side_panel_container.winfo_children():
            w.destroy()
        if self.toolbar_container is None:
            return   # no side panel in borderless select-mode - nothing to do
        self.root.update_idletasks()
        if self.side_panel_mode is None:
            self.side_panel_container.pack_forget()
            self.side_panel_separator.pack_forget()
            new_w = 0
        else:
            natural = self._measure_side_panel_width()
            target_width = max(150, min(natural + SCROLLBAR_WIDTH, SIDE_PANEL_WIDTH_RESERVE))
            log.debug(f"Side panel: natural width {natural}px, using {target_width}px (reserve {SIDE_PANEL_WIDTH_RESERVE}px)")
            self.side_panel_container.config(width=target_width)
            self.side_panel_container.pack_propagate(False)
            self.side_panel_separator.pack(side="left", fill="y")
            self.side_panel_container.pack(side="left", fill="y")
            inner, canvas_2 = make_scrollable(self.side_panel_container)
            self._side_panel_canvas = canvas_2
            if self.side_panel_mode == "layers":
                self._build_layers_panel(inner)
            elif self.side_panel_mode == "tabs":
                self._build_tabs_panel(inner)
            self.root.update_idletasks()
            if prev_scroll:
                canvas_2.yview_moveto(prev_scroll)
            new_w = target_width + self.side_panel_separator.winfo_reqwidth()
        self._side_panel_width_applied = new_w

    def _measure_side_panel_width(self):
        """
        Builds Layers' and Tabs' content into throwaway frames that are
        never packed/gridded anywhere (parented to self.root, but with no
        geometry manager call of their own - so nothing is ever actually
        shown), just to read off each one's natural, unconstrained width;
        returns the larger of the two. update_idletasks() resolves a
        widget's children's requested sizes without the widget itself
        needing to be on-screen, which is what makes this work as a pure
        measurement with no visible side effect. The two builder calls do
        overwrite self._layer_row_widgets/self._tab_row_widgets as a
        side effect (those track drag-reorder state for whichever cells
        were most recently built) - harmless here since
        _refresh_side_panel always rebuilds the real, currently-active
        panel immediately afterward, which reassigns them correctly one
        more time.
        """
        widths = []
        for builder in (self._build_layers_panel, self._build_tabs_panel):
            scratch = tk.Frame(self.root, bg="#222222")
            try:
                builder(scratch)
                self.root.update_idletasks()
                widths.append(scratch.winfo_reqwidth())
            finally:
                scratch.destroy()
        return max(widths) if widths else 0

    def _build_layers_panel(self, parent):
        """
        One row per layer, TOP of the stack first, plus a leading row with
        the "Full" button (clears isolation) and the Ghost slider. Each layer
        row: drag handle, name (isolates on click; cropped if long), rename
        button (packed before the name so it is never cut off), and for
        stroke layers Split / Merge-into.
        """
        tk.Label(parent, text="Layers", fg="white", bg="#222222", font=("", 10, "bold")).pack(side="top", anchor="w", padx=6, pady=(6, 4))

        full_row = tk.Frame(parent, bg="#222222")
        full_row.pack(side="top", fill="x", padx=6, pady=(0, 6))
        tk.Button(
            full_row, text="Full", pady=BUTTON_PADY,
            relief=("sunken" if self.isolated_layer_id is None else "raised"),
            command=self.show_full,
        ).pack(side="left")
        ghost_var = tk.IntVar(value=int(round(self.ghost_layer_opacity * 100)))
        tk.Label(full_row, textvariable=ghost_var, fg="white", bg="#222222", width=3, anchor="w").pack(side="right")
        tk.Scale(
            full_row, from_=0, to=100, orient="horizontal", length=50, showvalue=False,
            bg="#222222", fg="white", troughcolor="#444444", highlightthickness=0,
            variable=ghost_var, command=self._set_ghost_layer_opacity,
        ).pack(side="right")
        tk.Label(full_row, text="Ghost:", fg="#aaaaaa", bg="#222222").pack(side="right", padx=(6, 2))

        stroke_layers = [l for l in self.layers if l["kind"] == "strokes"]
        self._layer_row_widgets = []
        layer_reorder = RowReorder(parent, lambda key, to_display, n=len(self.layers): self.move_layer(key, (n - 1) - to_display))
        for index in range(len(self.layers) - 1, -1, -1):
            layer = self.layers[index]
            is_isolated = layer["id"] == self.isolated_layer_id
            cell = tk.Frame(parent, bg="#333333" if is_isolated else "#222222", highlightthickness=1, highlightbackground="#555555")
            cell.pack(side="top", fill="x", padx=4, pady=2)
            name_row = tk.Frame(cell, bg=cell.cget("bg"))
            name_row.pack(side="top", fill="x")
            handle = tk.Label(name_row, text="\u2261", fg="#888888", bg=cell.cget("bg"), width=2, cursor="fleur")
            handle.pack(side="left", padx=(2, 0), pady=2)
            # Rename is packed BEFORE the name button.
            name_btn = tk.Button(
                name_row, text=self._layer_display_label(layer), pady=BUTTON_PADY, anchor="w",
                relief=("sunken" if is_isolated else "raised"),
                command=lambda lid=layer["id"]: self.isolate_layer(lid),
            )
            small_button(name_row, "\u270e", lambda l=layer, c=cell, b=name_btn: self._rename_layer_inline(l, c, b)).pack(side="right", padx=(0, 2), pady=2)
            name_btn.pack(side="left", fill="x", expand=True, padx=2, pady=2)
            attach_tooltip(name_btn, self._layer_display_label(layer))
            layer_reorder.add(index, cell, handle, self._layer_display_label(layer))
            self._layer_row_widgets.append((index, cell))

            if layer["kind"] == "strokes":
                controls_row = tk.Frame(cell, bg=cell.cget("bg"))
                handle.update_idletasks()
                controls_row.pack(side="top", fill="x", padx=(handle.winfo_reqwidth() + 4, 2), pady=(0, 2))
                if is_isolated:
                    small_button(controls_row, "Split", self.enter_split_mode, width=0).pack(side="left", fill="x", expand=True, padx=(0, 2))
                others = [l for l in stroke_layers if l["id"] != layer["id"]]
                if others:
                    merge_var = tk.StringVar(value="Merge\u2026")
                    choices = {l["name"]: l["id"] for l in others}
                    dropdown = tk.OptionMenu(
                        controls_row, merge_var, *choices.keys(),
                        command=lambda name, sid=layer["id"], choices=choices: self.merge_layers(sid, choices[name]),
                    )
                    style_menu_like_small_button(dropdown)
                    dropdown.pack(side="left", fill="x", expand=True, padx=(0, 2))

        tk.Label(parent, text="(drag \u2261 to reorder \u00b7 \u270e to rename)", fg="#aaaaaa", bg="#222222", justify="left").pack(side="top", anchor="w", padx=6, pady=(6, 6))

        tk.Frame(parent, height=1, bg="#444444").pack(side="top", fill="x", padx=6, pady=(0, 6))
        tk.Button(parent, text="+ New Layer", pady=BUTTON_PADY, command=self._new_arbitrary_layer).pack(side="top", fill="x", padx=6, pady=(0, 6))
        new_layer_var = tk.IntVar(value=int(self.new_layer_on_edit_default))
        tk.Checkbutton(
            parent, text="New layer on edit", variable=new_layer_var,
            command=lambda: setattr(self, "new_layer_on_edit_default", bool(new_layer_var.get())),
            bg="#222222", fg="white", selectcolor="#444444",
            activebackground="#222222", activeforeground="white",
        ).pack(side="top", anchor="w", padx=6, pady=(0, 6))

    def _layer_row_press(self, event, index):
        self._panel_row_press(event, index, "_layer_drag_index", "_layer_drag_start_y", "_layer_drag_active")

    def _layer_row_drag(self, event):
        self._panel_row_drag(
            event, "_layer_drag_index", "_layer_drag_start_y", "_layer_drag_active",
            self._layer_row_widgets, self.side_panel_container, True,
            lambda i: self._layer_display_label(self.layers[i]),
        )

    def _layer_row_release(self, event):
        self._panel_row_release(
            event, "_layer_drag_index", "_layer_drag_start_y", "_layer_drag_active",
            self._layer_row_widgets, self.side_panel_container, True,
            self.move_layer,
        )

    # ---- documents (tabs) ----
    #
    # A "document" is everything _capture_full_state already tracks for
    # one capture/image (image_path, sel_coords, baked_image, strokes,
    # canvas bounds/color, layers, raw dims - see that method) plus which
    # layer, if any, was isolated. self.documents stays empty until Tabs
    # is actually used (_ensure_documents_initialized seeds it with the
    # current, only, document the first time anything tab-related
    # happens) - an ordinary single-capture session never has to think
    # about documents at all.
    #
    # self.documents[self.current_document_index] is intentionally STALE
    # while that document is the live one - the real state lives directly
    # in self.edit_strokes/self.layers/etc, exactly as without tabs. It's
    # only brought up to date right before switching away (see
    # switch_to_document/new_tab_blank/new_tab_from_file/close_tab), the
    # same snapshot-on-demand pattern _capture_full_state already uses for
    # Resize's undo stack.
    #
    # All temp files ever created for any document (past or present) stay
    # in the ONE shared self.owned_temp_paths list and are only actually
    # deleted when the whole window closes - a closed tab's temp file
    # lingers until then rather than needing its own careful bookkeeping.
    # This trades a little transient disk usage for a lot of simplicity
    # and avoids the risk of a double-free between two data structures.
    #
    # The snippy register (self.snippy_register) is NOT part of a
    # document - it's already just a plain app-level attribute, so it
    # keeps working across a tab switch for free: snip something in one
    # tab, switch tabs, paste it into another.
    #
    # Cross-window behavior - dragging one snippysnappy window onto
    # another to combine them into tabs of one window, or dragging a tab
    # out into its own window - is NOT built. Doing that for real needs
    # either a background daemon all invocations talk to, or direct
    # window-manager-level reparenting, and the latter has no equivalent
    # on Wayland at all (a compositor won't let one client reach into
    # another's window). Tabs here are multiple documents inside the ONE
    # window a given invocation already owns.

    def _document_label(self):
        """
        A short label for the CURRENT (live) document, for the Tabs
        panel - a user-set rename (see rename_current_tab) always wins;
        otherwise it's derived from the image filename, or "Blank" for a
        --nil/new-blank tab.
        """
        if self._custom_document_label:
            return self._custom_document_label
        if self._no_real_capture:
            return "Blank"
        try:
            name = Path(self.image_path).stem
        except Exception:
            name = "Tab"
        return (name[:20] or "Tab")

    def rename_current_tab(self, new_label):
        self._custom_document_label = new_label[:40]
        result = None
        try:
            self._set_window_title(getattr(self, "_last_submenu", None))
        except Exception:
            pass
        r = result
        try:
            from .coordinator import broadcast_window_roster
            broadcast_window_roster()
            if False:   # the window registers / labels settle a moment after the transition
                self.root.after(300, broadcast_window_roster)
        except Exception:
            log.exception("roster broadcast failed (non-fatal)")
        return r

    def _rename_tab_inline(self, index, cell, name_btn):
        is_current = index == self.current_document_index
        current_text = self._document_label() if is_current else self.documents[index].get("label", f"Tab {index + 1}")

        def commit(new_label):
            if is_current:
                self.rename_current_tab(new_label)
            else:
                self.documents[index]["label"] = new_label
                self.documents[index]["custom_label"] = new_label

        self._start_inline_rename(
            cell, name_btn, current_text,
            {"side": "left", "fill": "x", "expand": True, "padx": 2, "pady": 2},
            commit,
        )

    def _capture_document_state(self):
        snap = self._capture_full_state()
        snap["isolated_layer_id"] = self.isolated_layer_id
        snap["label"] = self._document_label()
        snap["custom_label"] = self._custom_document_label
        snap["last_touched_layer_id"] = self._last_touched_layer_id
        return snap

    def _restore_document_state(self, snap):
        self.isolated_layer_id = snap.get("isolated_layer_id")
        self._custom_document_label = snap.get("custom_label")
        self._last_touched_layer_id = snap.get("last_touched_layer_id")
        self._restore_full_state(snap)

    def _exit_all_modes_safely(self):
        """
        Brings the app back to the plain main toolbar before switching/
        closing/creating a tab, using each mode's own normal exit path so
        nothing is left dangling (an open text box gets confirmed, a
        pending shape/snip discarded, Resize exits via its own Done path,
        etc). A document is only ever snapshotted "at rest" like this.
        """
        if self._split_mode_active:
            self._exit_split_mode()
        if self.resize_mode_active:
            self._exit_resize_mode()
        if self._pending_text_widget is not None:
            self._confirm_text_entry()
        if self.edit_mode_active:
            self.bake_edits()
        if self.ocr_paint_mode_active:
            self.cancel_ocr_paint()
        if self.measure_mode_active:
            self.exit_measure_mode()
        if self.magnify_mode_active:
            self.exit_magnify_mode()

    def _ensure_documents_initialized(self):
        if not self.documents:
            self.documents = [self._capture_document_state()]
            self.current_document_index = 0

    def _finish_document_switch(self, resize_window=False):
        """
        resize_window=False (the default - every in-window tab action:
        switching, creating, closing) keeps the decorated window at
        whatever size it already is and just refits the new document's
        content into that existing canvas area (same math as the Rescale
        button) - switching tabs shouldn't visually jolt the window
        itself. resize_window=True is for the one case that genuinely
        needs real sizing: a brand-new window from popping a tab out.
        """
        self._sync_base_dims_for_mode()
        if resize_window:
            self._recalculate_display()
        else:
            self.manual_rescale_to_fit()
        self.draw_handles()
        self.show_main_toolbar()

    def switch_to_document(self, *args, **kwargs):
        result = self.switch_to_document_base(*args, **kwargs)
        try:
            self._set_window_title(getattr(self, "_last_submenu", None))
        except Exception:
            pass
        r = result
        try:
            from .coordinator import broadcast_window_roster
            broadcast_window_roster()
            if False:   # the window registers / labels settle a moment after the transition
                self.root.after(300, broadcast_window_roster)
        except Exception:
            log.exception("roster broadcast failed (non-fatal)")
        return r

    def switch_to_document_base(self, index):
        self._ensure_documents_initialized()
        if not (0 <= index < len(self.documents)) or index == self.current_document_index:
            return
        self._exit_all_modes_safely()
        self.documents[self.current_document_index] = self._capture_document_state()
        self.current_document_index = index
        self._restore_document_state(self.documents[index])
        self._finish_document_switch()
        log.info(f"Switched to tab {index + 1}/{len(self.documents)} ('{self.documents[index].get('label')}')")
        broadcast_window_roster()
        if self.side_panel_mode is not None:
            self._refresh_side_panel()

    def _make_blank_document(self, w=None, h=None, color=None):
        w = w or NIL_CANVAS_DEFAULT_WIDTH
        h = h or NIL_CANVAS_DEFAULT_HEIGHT
        color = color or NIL_CANVAS_DEFAULT_COLOR
        fd, tmp_name = tempfile.mkstemp(suffix=".png", prefix="snippysnappy-tab-nil-")
        os.close(fd)
        placeholder_path = Path(tmp_name)
        make_nil_placeholder(placeholder_path, color)
        self.owned_temp_paths.append(placeholder_path)
        return {
            "image_path": placeholder_path,
            "sel_coords": (0, 0, w, h),
            "baked_image": None,
            "edit_strokes": [],
            "ocr_strokes": [],
            "canvas_x1": 0, "canvas_y1": 0, "canvas_x2": w, "canvas_y2": h,
            "canvas_bg_color": color,
            "canvas_active": True,
            "no_real_capture": True,
            "raw_w": w, "raw_h": h,
            "layers": [self._new_layer("Background", "background"), self._new_layer("Capture", "capture")],
            "isolated_layer_id": None,
            "label": "Blank",
        }

    def new_tab_blank(self):
        self._ensure_documents_initialized()
        self._exit_all_modes_safely()
        self.documents[self.current_document_index] = self._capture_document_state()
        new_doc = self._make_blank_document()
        self.documents.append(new_doc)
        self.current_document_index = len(self.documents) - 1
        self._restore_document_state(new_doc)
        self._finish_document_switch()
        log.info(f"New blank tab created ({len(self.documents)} total)")
        broadcast_window_roster()
        if self.side_panel_mode is not None:
            self._refresh_side_panel()

    def _pick_file_via_configured_picker(self):
        """
        Opens whichever of OpenPromptApp ("fuzzy") or GuiPromptApp ("gui")
        self._open_file_picker_mode currently names, as a plain modal
        Toplevel (see run_modal) rather than the CLI --open path's fresh
        root+mainloop, since one is already running for this window. If
        the picker's own "Use GUI"/"Use CLI" button is used, loops to the
        other one instead of just falling through, and remembers whichever
        one the user actually ends up on as the new self._open_file_picker_mode,
        so next time "+ Open File..." is pressed in this window it opens
        straight into that one again.
        """
        mode = self._open_file_picker_mode
        while True:
            toplevel = tk.Toplevel(self.root)
            toplevel.withdraw()
            if mode == "fuzzy":
                picker = OpenPromptApp(toplevel, get_default_open_dir(), owns_mainloop=False)
                toplevel.deiconify()
                path = picker.run_modal()
                switched = picker.switch_to_gui_requested
            else:
                picker = GuiPromptApp(toplevel, get_default_open_dir(), owns_mainloop=False)
                toplevel.deiconify()
                path = picker.run_modal()
                switched = picker.switch_to_cli_requested
            if switched:
                mode = "gui" if mode == "fuzzy" else "fuzzy"
                continue
            self._open_file_picker_mode = mode
            return path

    def new_tab_from_file(self):
        path = self._pick_file_via_configured_picker()
        if path is None:
            return
        try:
            from PIL import Image as _Image
            with _Image.open(path) as _img:
                _img.verify()
        except Exception:
            log.exception(f"New tab: not a readable image file: {path}")
            notify("Couldn't open file", f"Not a readable image: {path.name}", blocking=False, anchor_window=self.root)
            return

        self._ensure_documents_initialized()
        self._exit_all_modes_safely()
        self.documents[self.current_document_index] = self._capture_document_state()

        fd, tmp_name = tempfile.mkstemp(suffix=path.suffix or ".png", prefix="snippysnappy-tab-")
        os.close(fd)
        work_path = Path(tmp_name)
        shutil.copy(path, work_path)
        self.owned_temp_paths.append(work_path)
        from PIL import Image as _Image2
        with _Image2.open(work_path) as _im:
            iw, ih = _im.size
        new_doc = {
            "image_path": work_path,
            "sel_coords": (0, 0, iw, ih),
            "baked_image": None,
            "edit_strokes": [],
            "ocr_strokes": [],
            "canvas_x1": 0, "canvas_y1": 0, "canvas_x2": iw, "canvas_y2": ih,
            "canvas_bg_color": "white",
            "canvas_active": False,
            "no_real_capture": False,
            "raw_w": iw, "raw_h": ih,
            "layers": [self._new_layer("Background", "background"), self._new_layer("Capture", "capture")],
            "isolated_layer_id": None,
            "label": (path.stem[:20] or "Tab"),
        }
        self.documents.append(new_doc)
        self.current_document_index = len(self.documents) - 1
        self._restore_document_state(new_doc)
        self._finish_document_switch()
        log.info(f"New tab opened from file: {path} ({len(self.documents)} total)")
        broadcast_window_roster()
        if self.side_panel_mode is not None:
            self._refresh_side_panel()

    def close_tab(self, index):
        """
        Closes one tab. A no-op if it's the only one left - see
        close_current_tab_or_window, which destroys the whole window in
        that case (the existing single-document Cancel/Escape behavior).
        """
        self._ensure_documents_initialized()
        if len(self.documents) <= 1 or not (0 <= index < len(self.documents)):
            return
        removed_label = self.documents[index].get("label", "?")
        closing_current = index == self.current_document_index
        del self.documents[index]
        if closing_current:
            self.current_document_index = min(index, len(self.documents) - 1)
            self._exit_all_modes_safely()
            self._restore_document_state(self.documents[self.current_document_index])
            self._finish_document_switch()
        elif index < self.current_document_index:
            self.current_document_index -= 1
        log.info(f"Closed tab '{removed_label}' ({len(self.documents)} remaining)")
        broadcast_window_roster()
        if self.side_panel_mode is not None:
            self._refresh_side_panel()

    def close_current_tab_or_window(self):
        _toast_discard(self)
        # What Cancel/Escape's final fallback calls: if more than one tab
        # is open, close just the current one; only destroy the whole
        # window when this is the last tab.
        self._ensure_documents_initialized()
        if len(self.documents) > 1:
            log.info("Cancel/Escape: closing current tab (others remain open)")
            self.close_tab(self.current_document_index)
        else:
            log.info("Cancel/Escape: last tab - discarding window")
            self._discard_window()

    def move_document(self, from_index, to_index):
        if not (0 <= from_index < len(self.documents)) or not (0 <= to_index < len(self.documents)):
            return
        doc = self.documents.pop(from_index)
        self.documents.insert(to_index, doc)
        if self.current_document_index == from_index:
            self.current_document_index = to_index
        elif from_index < self.current_document_index <= to_index:
            self.current_document_index -= 1
        elif to_index <= self.current_document_index < from_index:
            self.current_document_index += 1
        log.info(f"Tab moved to position {to_index}")
        broadcast_window_roster()
        if self.side_panel_mode is not None:
            self._refresh_side_panel()

    # ---- popping a tab out into its own window ----

    def pop_out_tab(self, index):
        self._ensure_documents_initialized()
        if len(self.documents) <= 1 or not (0 <= index < len(self.documents)):
            return
        if index == self.current_document_index:
            self._exit_all_modes_safely()
            snap = self._capture_document_state()
        else:
            snap = self.documents[index]

        del self.documents[index]
        if index == self.current_document_index:
            self.current_document_index = min(index, len(self.documents) - 1)
            self._restore_document_state(self.documents[self.current_document_index])
            self._finish_document_switch()
        elif index < self.current_document_index:
            self.current_document_index -= 1

        img_path = snap.get("image_path")
        if img_path in self.owned_temp_paths:
            self.owned_temp_paths.remove(img_path)

        self._open_document_in_new_window(snap)
        log.info(f"Popped out tab '{snap.get('label')}' into its own window")
        broadcast_window_roster()
        if self.side_panel_mode is not None:
            self._refresh_side_panel()

    def _open_document_in_new_window(self, snap):
        mx, my, mw, mh = self.current_monitor_bounds
        monitors = get_monitors()
        new_win = tk.Toplevel(self._true_root)
        new_win.withdraw()
        new_app = SelectorApp(
            new_win, snap["image_path"], mode="window", bounds=(mx + 40, my + 40, mw, mh),
            auto_select_full=True, monitors=monitors, capture_tempdir=None,
        )
        new_app._restore_document_state(snap)
        new_app._finish_document_switch(resize_window=True)
        if new_app.side_panel_mode is not None:
            new_app._refresh_side_panel()
        new_win.deiconify()
        new_win.lift()
        new_win.focus_force()
        return new_app

    # ---- merging another window's tabs into this one ----

    def merge_window_in(self, source_app):
        if source_app is self or source_app not in _LOCAL_WINDOWS:
            return
        source_app._ensure_documents_initialized()
        source_app._exit_all_modes_safely()
        source_app.documents[source_app.current_document_index] = source_app._capture_document_state()
        self._ensure_documents_initialized()
        moved = len(source_app.documents)
        for doc in source_app.documents:
            img_path = doc.get("image_path")
            if img_path in source_app.owned_temp_paths:
                source_app.owned_temp_paths.remove(img_path)
                self.owned_temp_paths.append(img_path)
            self.documents.append(doc)
        source_app.documents = []
        log.info(f"Merged {moved} tab(s) from another window into this one")
        source_app.root.destroy()
        broadcast_window_roster()
        if self.side_panel_mode is not None:
            self._refresh_side_panel()

    # ---- Tabs panel (shares the side-panel mechanism with Layers) ----

    def _build_tabs_panel(self, parent):
        self._ensure_documents_initialized()
        tk.Label(parent, text="Tabs", fg="white", bg="#222222", font=("", 10, "bold")).pack(side="top", anchor="w", padx=6, pady=(6, 4))

        new_row = tk.Frame(parent, bg="#222222")
        new_row.pack(side="top", anchor="w", padx=6, pady=(0, 6))
        tk.Button(new_row, text="+ Blank", pady=BUTTON_PADY, command=self.new_tab_blank).pack(side="left", padx=(0, 2))
        tk.Button(new_row, text="+ Open File\u2026", pady=BUTTON_PADY, command=self.new_tab_from_file).pack(side="left", padx=(0, 2))

        self._tab_row_widgets = []
        tab_reorder = RowReorder(parent, lambda key, to_index: self.move_document(key, to_index))
        for index, doc in enumerate(self.documents):
            is_current = index == self.current_document_index
            label_text = self._document_label() if is_current else doc.get("label", f"Tab {index + 1}")
            cell = tk.Frame(parent, bg="#333333" if is_current else "#222222", highlightthickness=1, highlightbackground="#555555")
            cell.pack(side="top", fill="x", padx=4, pady=2)
            handle = tk.Label(cell, text="\u2261", fg="#888888", bg=cell.cget("bg"), width=2, cursor="fleur")
            handle.pack(side="left", padx=(2, 0), pady=2)
            # Second row (full width) first, then right-hand buttons, then the
            # name last - so a long name is the thing that crops.
            if len(self.documents) > 1:
                second = tk.Frame(cell, bg=cell.cget("bg"))
                second.pack(side="bottom", fill="x", padx=2, pady=(0, 2))
                small_button(second, "Pop out", lambda i=index: self.pop_out_tab(i), width=0).pack(side="left", fill="x", expand=True)
                small_button(cell, "\u00d7", lambda i=index: self._confirm_close_tab_if_active(i)).pack(side="right", padx=(0, 2), pady=2)
            name_btn = tk.Button(
                cell, text=label_text, pady=BUTTON_PADY, anchor="w",
                relief=("sunken" if is_current else "raised"),
                command=lambda i=index: self.switch_to_document(i),
            )
            small_button(cell, "\u270e", lambda i=index, c=cell, b=name_btn: self._rename_tab_inline(i, c, b)).pack(side="right", padx=(0, 2), pady=2)
            name_btn.pack(side="left", fill="x", expand=True, padx=2, pady=2)
            attach_tooltip(name_btn, label_text)
            tab_reorder.add(index, cell, handle, label_text)
            self._tab_row_widgets.append((index, cell))

        tk.Label(parent, text="(drag \u2261 to reorder \u00b7 \u270e to rename)", fg="#aaaaaa", bg="#222222", justify="left").pack(side="top", anchor="w", padx=6, pady=(6, 4))

        # ---- Windows: same row style as Tabs/Layers. Active window = highlighted
        # row; clicking another focuses it (restoring it if hidden). Rows drag to
        # reorder (per-viewer), rename with the pencil, and image windows have a
        # "Merge..." selector. Backgrounded windows sit in the collapsed Hidden group.
        tk.Frame(parent, height=1, bg="#444444").pack(side="top", fill="x", padx=6, pady=(0, 6))
        windows_header_row = tk.Frame(parent, bg="#222222")
        windows_header_row.pack(side="top", fill="x", padx=6, pady=(0, 4))
        tk.Label(windows_header_row, text="Windows", fg="white", bg="#222222", font=("", 10, "bold")).pack(side="left")
        tk.Button(windows_header_row, text="Restart All", pady=BUTTON_PADY, command=self._confirm_and_restart_all).pack(side="right")

        def with_count(label, n):
            return label + (f" +{n - 1} more" if n > 1 else "")

        for other in [w for w in _LOCAL_WINDOWS if w is not self]:
            other._ensure_documents_initialized()
        local_imgs = [self] + [w for w in _LOCAL_WINDOWS if w is not self]

        def targets_for(x_app=None, cid=None, wid=None):
            out = []
            for y in local_imgs:
                if x_app is not None and y is x_app:
                    continue
                lab = with_count(window_name(y), len(y.documents))
                if x_app is not None:
                    out.append((lab, lambda y=y, x=x_app: y.merge_window_in(x)))
                else:
                    out.append((lab, lambda y=y, c=cid, w=wid: request_remote_merge(y, c, w)))
            return out

        entries = [{
            "key": self.window_id, "raw": window_name(self),
            "label": with_count(window_name(self), len(self.documents)), "active": True,
            "tip": "\n".join(window_tab_labels(self)),
            "focus": lambda: None, "hide": self._hide_window, "hidden": False,
            "close": lambda: self._confirm_close_window(self, is_active=True),
            "targets": targets_for(self),
            "rename": lambda text, a=self: self._set_window_name(a, text),
        }]
        for other in local_imgs[1:]:
            entries.append({
                "key": other.window_id, "raw": window_name(other),
                "label": with_count(window_name(other), len(other.documents)), "active": False,
                "tip": "\n".join(window_tab_labels(other)),
                "focus": lambda o=other: focus_local_window(o),
                "hide": other._hide_window, "hidden": bool(getattr(other, "_hidden", False)),
                "close": lambda o=other: self._confirm_close_window(o, is_active=False),
                "targets": targets_for(other),
                "rename": lambda text, a=other: self._set_window_name(a, text),
            })
        for client_id, window_id, doc_label, tab_count, kind in remote_windows_all():
            is_film = kind == "film"
            entries.append({
                "key": window_id, "raw": doc_label,
                "label": ("\u25b6 " + doc_label) if is_film else with_count(doc_label, tab_count), "active": False,
                "tip": "\n".join(remote_window_tab_labels(window_id) or [doc_label]),
                "focus": lambda cid=client_id, wid=window_id: request_remote_focus(cid, wid),
                "hide": lambda cid=client_id, wid=window_id: request_remote_window_hide(cid, wid),
                "hidden": remote_window_hidden(window_id),
                "close": (
                    (lambda cid=client_id, wid=window_id: self._show_close_confirm(
                        "Close this film? An unsaved recording is lost; a film already saved on disk is not deleted.",
                        lambda: request_remote_window_close(cid, wid)))
                    if is_film else
                    (lambda cid=client_id, wid=window_id: request_remote_window_close(cid, wid))
                ),
                "targets": [] if is_film else targets_for(None, client_id, window_id),
                "rename": lambda text, cid=client_id, wid=window_id: request_remote_window_rename(cid, wid, text),
            })
        log.info("Windows panel (image window %s) entries: %s" % (str(self.window_id)[:8], [(str(e["key"])[:8], e["label"], e["active"], e["hidden"]) for e in entries]))
        build_windows_rows(parent, entries, self, self._refresh_side_panel)

    def _tab_row_press(self, event, index):
        self._panel_row_press(event, index, "_tab_drag_index", "_tab_drag_start_y", "_tab_drag_active")

    def _tab_row_drag(self, event):
        def label_for(i):
            return self._document_label() if i == self.current_document_index else self.documents[i].get("label", f"Tab {i + 1}")
        self._panel_row_drag(
            event, "_tab_drag_index", "_tab_drag_start_y", "_tab_drag_active",
            self._tab_row_widgets, self.side_panel_container, False,
            label_for,
        )

    def _tab_row_release(self, event):
        self._panel_row_release(
            event, "_tab_drag_index", "_tab_drag_start_y", "_tab_drag_active",
            self._tab_row_widgets, self.side_panel_container, False,
            self.move_document,
        )

    # ---- OCR paint mode ----

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

    def _leave_ocr_paint_mode(self):
        """
        State/binding/canvas-overlay teardown only - see
        _leave_measure_mode. Note this explicitly resets
        ocr_paint_mode_active itself now (previously that only ever got
        reset inside show_main_toolbar's own blanket reset block, which
        cancel_ocr_paint always used to trigger by calling it right
        afterward - that's no longer guaranteed when switching tools
        directly via the shared tool row, see _switch_tool).
        """
        for stroke in self.ocr_strokes:
            for seg_id in stroke["segment_ids"]:
                self.canvas.delete(seg_id)
        self.ocr_strokes = []
        self.ocr_current_stroke = None
        self.ocr_paint_mode_active = False
        self.canvas.unbind("<ButtonPress-1>")
        self.canvas.unbind("<B1-Motion>")
        self.canvas.unbind("<ButtonRelease-1>")
        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)

    def cancel_ocr_paint(self):
        log.info(f"OCR paint cancelled, discarding {len(self.ocr_strokes)} stroke(s)")
        self._leave_ocr_paint_mode()
        # See exit_measure_mode: return to Tools submenu, not root.
        self.show_main_toolbar(reset_tools_row=False)

    def show_ocr_toolbar(self):
        self.clear_toolbar()
        frame = self.new_toolbar_frame()
        # Own main_row, same reasoning as show_magnify_toolbar above -
        # keeps Layers/Re-scale sharing the main button row's top-right
        # corner instead of stranded on a stray extra row.
        main_row = tk.Frame(frame, bg="#222222")
        main_row.pack(side="top", fill="x")
        self._build_tools_row(
            main_row, active="ocr",
            undo=self.undo_last_ocr_stroke, redo=self.redo_last_ocr_stroke, cancel=self.cancel_ocr_paint,
        )

        self._build_status_row(main_row, frame)
        slider_row = tk.Frame(frame, bg="#222222")
        slider_row.pack(side="top", fill="x")
        tk.Button(slider_row, text="Run OCR", pady=BUTTON_PADY, command=self.run_ocr_with_mask).pack(side="left", anchor="n", padx=2, pady=4)
        brush_var = tk.IntVar(value=self.ocr_brush_width)
        self._pack_labeled_slider(slider_row, "Width", brush_var, 4, 60, self.set_ocr_brush_width, slider_length=80, anchor="n")
        tk.Label(
            slider_row, text="Optionally paint a region to extract",
            fg="#aaaaaa", bg="#222222",
        ).pack(side="left", anchor="n", padx=(4, 2), pady=4)

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
        self._redraw_ocr_strokes_preview()
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
        # Review screen: shows the extracted text, lets you copy/save it, or go
        # back to redo the paint/selection without losing the capture.
        #
        # Rendered as an overlay Frame inside the SAME window rather than a
        # separate Toplevel.
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
        result_3 = None
        try:
            tw, bf = self._ocr_text_widget, self._ocr_btn_frame
            tw.config(height=4)
            bf.pack_forget()
            bf.pack(side="bottom", fill="x", padx=16, pady=16, before=tw)
        except Exception:
            log.exception("OCR review layout fix failed (non-fatal)")
        result = result_3
        self._fit_ocr_btn_frame(True)
        return result

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
            command=lambda: (log.info("OCR result: Close pressed"), self._close_window()),
        ).pack(side="left", padx=4)
        result_4 = None
        self._fit_ocr_btn_frame(False)
        result = result_4
        try:
            if getattr(self, "_ocr_btn_frame", None) is not None:
                _gap_walk(self._ocr_btn_frame)
        except tk.TclError:
            pass
        return result

    def show_text_save_options(self):
        for w_2 in self._ocr_btn_frame.winfo_children():
            w_2.destroy()
        check_row = tk.Frame(self._ocr_btn_frame, bg="#1a1a1a")
        check_row.pack(side="top", fill="x")
        tk.Checkbutton(
            check_row, text="Also copy", variable=self._ocr_copy_var,
            bg="#1a1a1a", fg="white", selectcolor="#444444",
            activebackground="#1a1a1a", activeforeground="white",
        ).pack(side="left")
        btn_row_2 = tk.Frame(self._ocr_btn_frame, bg="#1a1a1a")
        btn_row_2.pack(side="top", fill="x", pady=(4, 0))
        tk.Button(btn_row_2, text="Save", pady=BUTTON_PADY, command=self.begin_normal_save_text).pack(side="left", padx=4)
        tk.Button(btn_row_2, text="Sloppy Save", pady=BUTTON_PADY, command=self.do_slop_save_text).pack(side="left", padx=4)
        tk.Button(btn_row_2, text="Back", pady=BUTTON_PADY, command=self._show_ocr_main_buttons).pack(side="left", padx=4)
        result_6 = None
        try:
            cbs, btn_row = [], None
            stack = [self._ocr_btn_frame]
            while stack:
                w = stack.pop()
                for c in w.winfo_children():
                    if isinstance(c, tk.Checkbutton):
                        cbs.append(c)
                    elif isinstance(c, tk.Button) and btn_row is None:
                        btn_row = c.master
                    stack.append(c)
            if cbs and btn_row is not None:
                old = cbs[0]
                holder = old.master
                text = old.cget("text")
                old.destroy()
                if holder is not btn_row and not holder.winfo_children():
                    holder.destroy()
                tk.Checkbutton(
                    btn_row, text=text, variable=self._ocr_copy_var, bg="#1a1a1a", fg="white", selectcolor="#444444",
                    activebackground="#1a1a1a", activeforeground="white",
                ).pack(side="right", padx=(8, 0))
        except Exception:
            log.exception("OCR save-options layout fix failed (non-fatal)")
        result_5 = result_6
        self._fit_ocr_btn_frame(False)
        result = result_5
        try:
            if getattr(self, "_ocr_btn_frame", None) is not None:
                _gap_walk(self._ocr_btn_frame)
        except tk.TclError:
            pass
        return result

    def begin_normal_save_text(self):
        # Mirrors the image Save flow: Normal Save (prompts for a
        # filename, into SNIPPYTEXT_DIR) or Sloppy Save (no prompt, into SLOPPY_TEXT_DIR).
        for w in self._ocr_btn_frame.winfo_children():
            w.destroy()
        name_var = tk.StringVar(value=f"snippytext-{timestamp()}")
        entry = tk.Entry(self._ocr_btn_frame, textvariable=name_var, width=28)
        entry.pack(side="left", padx=4)
        entry.bind("<Return>", lambda e: self.confirm_normal_save_text(name_var.get()))
        entry.focus_set()
        tk.Button(
            self._ocr_btn_frame, text="Confirm", pady=BUTTON_PADY,
            command=lambda: self.confirm_normal_save_text(name_var.get()),
        ).pack(side="left", padx=4)
        tk.Button(self._ocr_btn_frame, text="Cancel", pady=BUTTON_PADY, command=self.show_text_save_options).pack(side="left", padx=4)
        result_7 = None
        self._fit_ocr_btn_frame(False)
        result = result_7
        try:
            if getattr(self, "_ocr_btn_frame", None) is not None:
                _gap_walk(self._ocr_btn_frame)
        except tk.TclError:
            pass
        return result

    def confirm_normal_save_text(self, name):
        content = self._ocr_text_widget.get("1.0", "end-1c")
        name = name.strip() or f"snippytext-{timestamp()}"
        if not name.lower().endswith(".txt"):
            name += ".txt"
        path = _paths_setup.get_dir("text") / name
        path.write_text(content, encoding="utf-8")
        log.info(f"Saved OCR text -> {path} ({len(content)} chars)")
        also_copy = bool(self._ocr_copy_var.get())
        self._ocr_overlay.destroy()
        if also_copy:
            copy_text_to_clipboard(content)
            notify("Text copied to clipboard", blocking=False)
        notify("Text saved", f"Saved as {path.name}", blocking=False)
        if OCR_RETURN_TO_TOOLBAR_AFTER_ACTION:
            self.show_main_toolbar()
        else:
            self._close_window()

    def do_slop_save_text(self):
        content = self._ocr_text_widget.get("1.0", "end-1c")
        path = _paths_setup.get_dir("sloppytext") / f"sloppytext-{timestamp()}.txt"
        path.write_text(content, encoding="utf-8")
        log.info(f"Sloppy-saved OCR text -> {path} ({len(content)} chars)")
        also_copy = bool(self._ocr_copy_var.get())
        self._ocr_overlay.destroy()
        if also_copy:
            copy_text_to_clipboard(content)
            notify("Text copied to clipboard", blocking=False)
        notify("Sloppy text saved", f"Saved as {path.name}", blocking=False)
        if OCR_RETURN_TO_TOOLBAR_AFTER_ACTION:
            self.show_main_toolbar()
        else:
            self._close_window()

    def do_copy_text(self, overlay, text_widget):
        content = text_widget.get("1.0", "end-1c")
        log.info(f"Copy Text pressed ({len(content)} chars)")
        copy_text_to_clipboard(content)
        notify("Text copied to clipboard", blocking=False)
        overlay.destroy()
        if OCR_RETURN_TO_TOOLBAR_AFTER_ACTION:
            self.show_main_toolbar()
        else:
            self._close_window()

    # ---- save ----

    def begin_normal_save(self):
        self.clear_toolbar()
        frame = self.new_toolbar_frame()
        name_var = tk.StringVar(value=f"snipsnap-{timestamp()}")
        entry = tk.Entry(frame, textvariable=name_var, width=28)
        entry.pack(side="left", padx=4, pady=4)
        entry.bind("<Return>", lambda e: self.confirm_normal_save(name_var.get()))
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
        path = _paths_setup.get_dir("snaps") / name
        log.info(f"Normal Save -> {path}")
        img = self.get_working_image()
        img.save(path)
        also_copy = bool(self.copy_on_save_var.get())
        also_exit = bool(self.also_exit_var.get())
        if also_copy:
            copy_image_to_clipboard(path)
            notify("Snippysnap copied to clipboard", blocking=False)
        notify("Snippysnap saved", f"Saved as {path.name}", blocking=False)
        if also_exit:
            log.debug(f"Closing window immediately after save (also_copy={also_copy}) - toast runs independently, unaffected by EXIT_BEHAVIOR")
            self._discard_window()
        else:
            log.debug(f"Also Exit unchecked - staying open after save (also_copy={also_copy})")
            self.show_main_toolbar()

    def _cancel_with_confirm(self):
        log.info("Cancel pressed")
        confirm_discard(
            self.root, "Discard this capture? Anything not saved or copied will be lost.",
            self.close_current_tab_or_window,
        )

    def _save_as_image(self):
        """Save As: pick a type and folder (fuzzy folder autocomplete) - see save_as_dialog.py."""
        from .save_as_dialog import ask_save_as
        image_types = [
            ("png", "PNG (.png)"), ("jpg", "JPEG (.jpg)"), ("webp", "WebP (.webp)"),
            ("bmp", "BMP (.bmp)"), ("tiff", "TIFF (.tiff)"),
        ]

        def do_save(directory, name, ext):
            path = directory / name
            img = self.get_working_image()
            try:
                if ext == "jpg":
                    from PIL import Image as _Image
                    rgba = img.convert("RGBA")
                    flat = _Image.new("RGB", rgba.size, (255, 255, 255))
                    flat.paste(rgba, mask=rgba.split()[3])
                    flat.save(path, format="JPEG", quality=92)
                elif ext == "bmp":
                    img.convert("RGB").save(path, format="BMP")
                else:
                    img.save(path)
            except Exception:
                log.exception(f"Save As failed for {path}")
                notify("Save failed", "Could not write the file - see the log", blocking=False)
                return
            log.info(f"Save As -> {path}")
            if bool(self.copy_on_save_var.get()):
                fd, tmp_name = tempfile.mkstemp(suffix=".png", prefix="snippysnappy-")
                os.close(fd)
                tmp_path = Path(tmp_name)
                img.save(tmp_path)
                copy_image_to_clipboard(tmp_path)
                self.owned_temp_paths.append(tmp_path)
                notify("Snippysnap copied to clipboard", blocking=False)
            notify("Snippysnap saved", f"Saved as {path.name}", blocking=False)
            if bool(self.also_exit_var.get()):
                self._close_window()
            else:
                self.show_main_toolbar()

        ask_save_as(self.root, "Save image as", image_types, _paths_setup.get_dir("snaps"), f"snipsnap-{timestamp()}", do_save)

    def do_slop_save(self):
        path = _paths_setup.get_dir("sloppysnaps") / f"sloppysnap-{timestamp()}.png"
        log.info(f"Sloppy Save -> {path}")
        img = self.get_working_image()
        img.save(path)
        also_copy = bool(self.copy_on_save_var.get())
        also_exit = bool(self.also_exit_var.get())
        if also_copy:
            copy_image_to_clipboard(path)
            notify("Snippysnap copied to clipboard", blocking=False)
        notify("Sloppysnap saved", f"Saved as {path.name}", blocking=False)
        if also_exit:
            log.debug(f"Closing window immediately after save (also_copy={also_copy}) - toast runs independently, unaffected by EXIT_BEHAVIOR")
            self._discard_window()
        else:
            log.debug(f"Also Exit unchecked - staying open after save (also_copy={also_copy})")
            self.show_main_toolbar()

    # ---- copy ----

    def do_copy(self):
        try:
            if self.also_save_var.get():
                path = _paths_setup.get_dir("sloppysnaps") / f"sloppysnap-{timestamp()}.png"
                self.get_working_image().save(path)
                log.info(f"Also Save (on copy) -> {path}")
                notify("Sloppysnap saved", f"Saved as {path.name}", blocking=False)
        except Exception:
            log.exception("Also Save on copy failed (non-fatal)")
        log.info("Copy pressed")
        img = self.get_working_image()
        fd, tmp_name = tempfile.mkstemp(suffix=".png", prefix="snippysnappy-")
        os.close(fd)
        tmp_path = Path(tmp_name)
        img.save(tmp_path)
        copy_image_to_clipboard(tmp_path)
        notify("Snippysnap copied to clipboard", blocking=False)
        if bool(self.also_exit_var.get()):
            log.debug("Closing window immediately after copy - toast runs independently, unaffected by EXIT_BEHAVIOR")
            self._discard_window()
        else:
            log.debug("Also Exit unchecked - staying open after copy")
            self.show_main_toolbar()

    # ---- class attributes that later patches added ----
    _steady_state_topmost = True
    _mp_pin = None
    _mp_source = None
    _mp_photo = None
    _mp_bind_id = None

    # ---- Title follows the active tab ----

    # ---- undo for the main-toolbar selection / crop handles ----

    def _tp_start_2(self):
        if getattr(self, "_tp_active", False):
            return
        tw = self._pending_text_widget
        if tw is None or self._pending_text_window_id is None:
            return
        self._tp_active = True
        self._tp_sig = None
        try:
            tw.config(bd=0, highlightthickness=0)
            self.canvas.itemconfigure(self._pending_text_window_id, width=1, height=1)
        except tk.TclError:
            pass
        self._tp_tick()

    def _tp_stop_2(self, restore=True):
        was = getattr(self, "_tp_active", False)
        self._tp_active = False
        try:
            self.canvas.delete("text_tpreview")
        except Exception:
            pass
        self._tp_photo = None
        if restore and was and self._pending_text_widget is not None and self._pending_text_box:
            try:
                x1, y1, x2, y2 = self._pending_text_box
                cx1, cy1 = self.capturelocal_to_screen(x1, y1)
                cx2, cy2 = self.capturelocal_to_screen(x2, y2)
                self.canvas.itemconfigure(self._pending_text_window_id, width=int(cx2 - cx1), height=int(cy2 - cy1))
                self._pending_text_widget.config(bd=1)
            except Exception:
                log.exception("transparent text preview: could not restore the widget (non-fatal)")

    def _tp_tick_2(self):
        if not getattr(self, "_tp_active", False):
            return
        tw = self._pending_text_widget
        if tw is None or self.text_bg_fill != "transparent":
            self._tp_stop(restore=False)
            return
        try:
            self._tp_render(tw)
        except Exception:
            log.exception("transparent text preview: render failed (non-fatal)")
        self.canvas.after(100, self._tp_tick)

    def _tp_render_2(self, tw):
        from PIL import Image, ImageChops, ImageTk
        x1, y1, x2, y2 = self._pending_text_box
        w = max(1, int(round(x2 - x1)))
        h = max(1, int(round(y2 - y1)))
        content = tw.get("1.0", "end-1c")
        idx = tw.index("insert")
        ds = self.display_scale
        sig = (content, idx, self.text_font_size, self.text_font_family, self.draw_color, self.text_align, round(ds, 4), repr(tw.dump("1.0", "end", tag=True)))
        if sig == getattr(self, "_tp_sig", None):
            return
        self._tp_sig = sig
        stroke = self._build_text_stroke(content, (x1, y1, x2, y2))
        stroke["bg_fill"] = "transparent"
        patch = self._render_element_local_patch(stroke, w, h)
        cx_off, cy_off = 0, 0
        caret = None
        try:
            before = tw.get("1.0", "insert")
            s1 = dict(stroke)
            s1.pop("runs", None)
            s1["text"] = before
            s2 = dict(s1)
            s2["text"] = before + "|"
            a1 = self._render_element_local_patch(s1, w, h).getchannel("A")
            a2 = self._render_element_local_patch(s2, w, h).getchannel("A")
            caret = ImageChops.difference(a1, a2).getbbox()
        except Exception:
            caret = None
        pw, ph = max(1, int(w * ds)), max(1, int(h * ds))
        shown = patch.resize((pw, ph), Image.LANCZOS) if (pw, ph) != patch.size else patch
        self._tp_photo = ImageTk.PhotoImage(shown)
        cx1, cy1 = self.capturelocal_to_screen(x1, y1)
        self.canvas.delete("text_tpreview")
        self.canvas.create_image(cx1, cy1, anchor="nw", image=self._tp_photo, tags=("text_tpreview",))
        self.canvas.create_rectangle(cx1, cy1, cx1 + pw, cy1 + ph, outline="#00b4ff", width=1, dash=(4, 2), tags=("text_tpreview",))
        if caret:
            lx = cx1 + caret[0] * ds
            self.canvas.create_line(lx, cy1 + caret[1] * ds, lx, cy1 + caret[3] * ds, fill=self.draw_color, width=2, tags=("text_tpreview",))
        else:
            self.canvas.create_line(cx1 + 1, cy1 + 1, cx1 + 1, cy1 + max(8, self.text_font_size * ds), fill=self.draw_color, width=2, tags=("text_tpreview",))

    def _tx_box_canvas(self):
        x1, y1, x2, y2 = self._pending_text_box
        cx1, cy1 = self.capturelocal_to_screen(x1, y1)
        cx2, cy2 = self.capturelocal_to_screen(x2, y2)
        return cx1, cy1, cx2, cy2

    def _tx_draw_ui_2(self):
        self.canvas.delete("text_ui")
        if self._pending_text_widget is None or not self._pending_text_box or self._pending_text_edit_index is not None:
            return
        cx1, cy1, cx2, cy2 = self._tx_box_canvas()
        p = _TX_PAD
        cx1, cy1, cx2, cy2 = cx1 - p, cy1 - p, cx2 + p, cy2 + p
        self.canvas.create_rectangle(cx1, cy1, cx2, cy2, outline="#00b4ff", width=1, dash=(6, 3), tags=("text_ui",))
        mx, my = (cx1 + cx2) / 2, (cy1 + cy2) / 2
        pts = {"nw": (cx1, cy1), "n": (mx, cy1), "ne": (cx2, cy1), "e": (cx2, my),
               "se": (cx2, cy2), "s": (mx, cy2), "sw": (cx1, cy2), "w": (cx1, my)}
        for name, (px, py) in pts.items():
            self.canvas.create_rectangle(px - 4, py - 4, px + 4, py + 4, fill="white", outline="#00b4ff",
                                         tags=("text_ui", "text_handle", "th_" + name))

    def _tx_hit_handle_2(self, event):
        x, y = self._event_canvas_xy(event)
        for item in self.canvas.find_overlapping(x - 3, y - 3, x + 3, y + 3):
            for t in self.canvas.gettags(item):
                if t.startswith("th_"):
                    return t[3:]
        return None

    def _tx_apply_box(self):
        tw = self._pending_text_widget
        if tw is None:
            return
        cx1, cy1, cx2, cy2 = self._tx_box_canvas()
        W, H = max(1, int(cx2 - cx1)), max(1, int(cy2 - cy1))
        if getattr(self, "_tp_active", False):
            tw.place_configure(x=-(W + 300), width=W, height=H)
            self._tp_sig = None
        else:
            self.canvas.coords(self._pending_text_window_id, cx1, cy1)
            self.canvas.itemconfigure(self._pending_text_window_id, width=W, height=H)
        self._tx_draw_ui()

    def _tx_style_drag_2(self):
        item = self.text_current_box_id
        if self.draw_tool != "text" or not item:
            return
        self.canvas.itemconfigure(item, outline="#00b4ff", width=1, dash=(6, 3), fill="")
        spec = {"white": ("white", ""), "black": ("black", ""),
                "white_border": ("white", "black"), "black_border": ("black", "white")}.get(self.text_bg_fill)
        existing = self.canvas.find_withtag("text_box_content")
        if spec is None:
            if existing:
                self.canvas.delete("text_box_content")
            return
        coords = self.canvas.coords(item)
        if len(coords) != 4:
            return
        if existing:
            self.canvas.coords(existing[0], *coords)
            self.canvas.itemconfigure(existing[0], fill=spec[0], outline=spec[1], width=2 if spec[1] else 0)
        else:
            self.canvas.create_rectangle(*coords, fill=spec[0], outline=spec[1], width=2 if spec[1] else 0, tags=("text_box_content",))
            self.canvas.tag_lower("text_box_content", item)

    def _tp_start(self):
        if getattr(self, "_tp_active", False):
            return
        tw = self._pending_text_widget
        if tw is None or self._pending_text_window_id is None or not self._pending_text_box:
            return
        cx1, cy1, cx2, cy2 = self._tx_box_canvas()
        W, H = max(1, int(cx2 - cx1)), max(1, int(cy2 - cy1))
        self._tp_active = True
        self._tp_sig = None
        try:
            tw.config(bd=0, highlightthickness=0)
            self.canvas.delete(self._pending_text_window_id)
            self._pending_text_window_id = self.canvas.create_rectangle(0, 0, 0, 0, state="hidden")
            tw.place(x=-(W + 300), y=0, width=W, height=H)
            tw.focus_set()
            self.canvas.after(80, lambda: log.debug(f"transparent text preview: input widget viewable={tw.winfo_viewable()} mapped={tw.winfo_ismapped()} size={tw.winfo_width()}x{tw.winfo_height()}") if self._pending_text_widget is tw else None)
        except Exception:
            log.exception("transparent text preview: could not move the input widget offscreen (non-fatal)")
        self._tp_tick()

    def _tp_stop(self, restore=True):
        was = getattr(self, "_tp_active", False)
        self._tp_active = False
        try:
            self.canvas.delete("text_tpreview")
        except Exception:
            pass
        self._tp_photo = None
        if restore and was and self._pending_text_widget is not None and self._pending_text_box:
            try:
                tw = self._pending_text_widget
                cx1, cy1, cx2, cy2 = self._tx_box_canvas()
                tw.place_forget()
                self.canvas.delete(self._pending_text_window_id)
                self._pending_text_window_id = self.canvas.create_window(
                    cx1, cy1, anchor="nw", width=max(1, int(cx2 - cx1)), height=max(1, int(cy2 - cy1)), window=tw)
                tw.focus_set()
            except Exception:
                log.exception("transparent text preview: could not restore the widget (non-fatal)")

    def _begin_text_edit_wrap_begin_ui(self, index):
        stroke = self.edit_strokes[index]
        x1, y1 = stroke["origin"]
        w, h = stroke["w"], stroke["h"]
        self.draw_color = stroke.get("color", self.draw_color)
        self.text_font_family = stroke.get("font_family", self.text_font_family)
        self.text_font_size = stroke.get("font_size", self.text_font_size)
        self.text_bg_fill = stroke.get("bg_fill", self.text_bg_fill)
        self.text_align = stroke.get("text_align", self.text_align)
        self._recompute_baked_image(exclude_index=index)
        self._begin_text_entry(x1, y1, x1 + w, y1 + h)
        self._pending_text_edit_index = index
        self._insert_text_stroke_content(self._pending_text_widget, stroke)
        log.info(f"Re-opened text element {index} for editing ({len(stroke.get('text', ''))} chars)")
        self.show_edit_toolbar()
        r_12 = None
        try:
            if self.text_bg_fill == "transparent" and self._pending_text_widget is not None:
                self._tp_start()
        except Exception:
            log.exception("transparent text preview start failed (non-fatal)")
        r = r_12
        try:
            self.canvas.delete("text_box_content")
            self._tx_rs = None
            self._tx_draw_ui()
        except Exception:
            log.exception("text box UI draw failed (non-fatal)")
        return r

    def _tx_pseudo(self, padded=True):
        x1, y1, x2, y2 = self._pending_text_box
        pc = (_TX_PAD_PX / max(self.display_scale, 0.01)) if padded else 0.0
        return {"mode": "text", "origin": (x1 - pc, y1 - pc), "w": (x2 - x1) + 2 * pc, "h": (y2 - y1) + 2 * pc,
                "rotation": getattr(self, "_tx_rot", 0.0)}

    def _tx_clear_ui(self):
        for i in getattr(self, "_tx_ui_ids", []):
            try:
                self.canvas.delete(i)
            except Exception:
                pass
        self._tx_ui_ids = []
        self.canvas.delete("text_ui")

    def _tx_draw_ui(self):
        self._tx_clear_ui()
        if self._pending_text_widget is None or not self._pending_text_box or self._pending_text_edit_index is not None:
            return
        before = len(self._snip_handle_ids)
        self._draw_single_snip_ui(self._tx_pseudo(), is_focused=False)
        new = self._snip_handle_ids[before:]
        del self._snip_handle_ids[before:]
        self._tx_ui_ids = list(new)
        for i in new:
            self.canvas.addtag_withtag("text_ui", i)

    def _tx_hit_handle(self, event):
        x, y = self._event_canvas_xy(event)
        positions = self._snip_handle_positions(self._tx_pseudo())
        for name, (px, py) in positions.items():
            hx, hy = self.capturelocal_to_screen(px, py)
            if math.hypot(hx - x, hy - y) <= HANDLE_SIZE + 4:
                return name
        return None

    def _tx_style_drag(self):
        item = self.text_current_box_id
        if self.draw_tool != "text" or not item:
            return
        self.canvas.itemconfigure(item, outline="#00b4ff", width=2, dash=(5, 3), fill="")
        spec = {"white": ("white", ""), "black": ("black", ""),
                "white_border": ("white", "black"), "black_border": ("black", "white")}.get(self.text_bg_fill)
        existing = self.canvas.find_withtag("text_box_content")
        if spec is None:
            if existing:
                self.canvas.delete("text_box_content")
            return
        coords = self.canvas.coords(item)
        if len(coords) != 4:
            return
        if existing:
            self.canvas.coords(existing[0], *coords)
            self.canvas.itemconfigure(existing[0], fill=spec[0], outline=spec[1], width=2 if spec[1] else 0)
        else:
            self.canvas.create_rectangle(*coords, fill=spec[0], outline=spec[1], width=2 if spec[1] else 0, tags=("text_box_content",))
            self.canvas.tag_lower("text_box_content", item)

    def _edit_drag_tx2(self, event):
        rs = getattr(self, "_tx_rs", None)
        if not rs:
            rs_2 = getattr(self, "_tx_rs", None)
            if rs_2:
                try:
                    x_3, y_3 = self._event_to_capture_local(event)
                    x_3, y_3 = self.clamp_to_canvas(x_3, y_3)
                    x1_2, y1_2, x2_2, y2_2 = rs_2["box0"]
                    name_2 = rs_2["name"]
                    if "w" in name_2:
                        x1_2 = min(x_3, x2_2 - 10)
                    if "e" in name_2:
                        x2_2 = max(x_3, x1_2 + 10)
                    if "n" in name_2:
                        y1_2 = min(y_3, y2_2 - 10)
                    if "s" in name_2:
                        y2_2 = max(y_3, y1_2 + 10)
                    self._pending_text_box = (x1_2, y1_2, x2_2, y2_2)
                    self._tx_apply_box()
                except Exception:
                    log.exception("text box handle drag failed (non-fatal)")
                return "break"
            r_13 = self.edit_drag_base(event)
            try:
                self._tx_style_drag()
            except Exception:
                log.exception("text drag preview styling failed (non-fatal)")
            return r_13
            return
        try:
            x, y = self._event_to_capture_local(event)
            x, y = self.clamp_to_canvas(x, y)
            x1, y1, x2, y2 = rs["box0"]
            cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
            name = rs["name"]
            if name == "rotate":
                self._tx_rot = (math.degrees(math.atan2(y - cy, x - cx)) + 90.0) % 360.0
                self._tp_sig = None
            else:
                th = math.radians(getattr(self, "_tx_rot", 0.0))
                dx, dy = x - cx, y - cy
                lx = dx * math.cos(-th) - dy * math.sin(-th)
                ly = dx * math.sin(-th) + dy * math.cos(-th)
                hw, hh = (x2 - x1) / 2, (y2 - y1) / 2
                p = _TX_PAD_PX / max(self.display_scale, 0.01)
                l, r_3, t, b = -hw, hw, -hh, hh
                if "e" in name:
                    r_3 = max(lx - p, l + 10)
                if "w" in name:
                    l = min(lx + p, r_3 - 10)
                if "s" in name:
                    b = max(ly - p, t + 10)
                if "n" in name:
                    t = min(ly + p, b - 10)
                nw_, nh_ = r_3 - l, b - t
                lcx, lcy = (l + r_3) / 2, (t + b) / 2
                wcx = cx + lcx * math.cos(th) - lcy * math.sin(th)
                wcy = cy + lcx * math.sin(th) + lcy * math.cos(th)
                ox, oy = wcx - nw_ / 2, wcy - nh_ / 2
                self._pending_text_box = (ox, oy, ox + nw_, oy + nh_)
            self._tx_apply_box()
        except Exception:
            log.exception("text box handle drag failed (non-fatal)")
        return "break"

    def _tp_render_v4(self, tw):
        from PIL import Image, ImageDraw, ImageColor, ImageTk
        x1, y1, x2, y2 = self._pending_text_box
        w = max(1, int(round(x2 - x1)))
        h = max(1, int(round(y2 - y1)))
        cx1, cy1, cx2, cy2 = self._tx_box_canvas()
        cw = max(1, int(round(cx2 - cx1)))
        ch = max(1, int(round(cy2 - cy1)))
        content = tw.get("1.0", "end-1c")
        rot = getattr(self, "_tx_rot", 0.0) or 0.0
        mode = getattr(self, "text_vim_mode", "insert")
        fill = self.text_bg_fill
        n = len(content)
        ins = min(len(tw.get("1.0", "insert")), n)
        rng = tw.tag_ranges("sel")
        ranges = []
        for i in range(0, len(rng) - 1, 2):
            a = len(tw.get("1.0", rng[i]))
            b = len(tw.get("1.0", rng[i + 1]))
            ranges.append((a, min(b, n)))
        phase = int(time.monotonic() * 2) % 2 if mode == "insert" else 0
        sig = (content, ins, tuple(ranges), mode, phase, self.text_font_size, self.text_font_family,
               self.draw_color, self.text_align, fill, (w, h, cw, ch), round(rot, 2),
               repr(tw.dump("1.0", "end", tag=True)))
        if sig == getattr(self, "_tp_sig", None):
            return
        self._tp_sig = sig
        stroke = self._build_text_stroke(content, (x1, y1, x2, y2))
        adv = self._tp_adv_v4(stroke, w, h)
        ckey = (self.text_font_family, self.text_font_size, self.text_align, w, h)
        cache = getattr(self, "_tp_cc4", None)
        if cache is None or cache[0] != ckey or len(cache[1]) > 3000:
            cache = self._tp_cc4 = (ckey, {})
        carets = cache[1]

        def cp(prefix):
            if prefix in carets:
                return carets[prefix]
            a_img = self._tp_rend_v4(stroke, w, h, prefix, "transparent")
            b_img = self._tp_rend_v4(stroke, w, h, prefix + "|", "transparent")
            bb = _tp_diffbox_v4(a_img, b_img)
            res = None if bb is None else ((bb[0] + bb[2]) / 2.0, bb[1], bb[3])
            carets[prefix] = res
            return res

        def cell(k):
            c0 = cp(content[:k])
            if c0 is None:
                return None
            left = c0[0] - adv / 2.0
            right = None
            if k < n and content[k] != "\n":
                c1 = cp(content[:k + 1])
                if c1 is not None and abs(c1[1] - c0[1]) <= 3 and c1[0] > c0[0]:
                    right = c1[0] - adv / 2.0
            if right is None:
                right = left + adv
            return left, c0[1] - 1, right, c0[2] + 1

        overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        od = ImageDraw.Draw(overlay)
        block = mode != "insert"
        count = 0
        for a, b in ranges:
            for k in range(a, b):
                if block and k == ins:
                    continue
                count += 1
                if count > 600:
                    break
                c = cell(k)
                if c:
                    od.rectangle([c[0], c[1], c[2], c[3]], fill=(0, 180, 255, 90))
        if block:
            c = cell(ins)
            if c:
                od.rectangle([c[0], c[1], c[2], c[3]], fill=_tp_block_rgba())
        img = Image.alpha_composite(self._tp_rend_v4(stroke, w, h, " ", fill), overlay)
        img = Image.alpha_composite(img, self._tp_rend_v4(stroke, w, h, content, "transparent", keep_runs=True))
        if not block and phase == 0:
            c0 = cp(content[:ins])
            if c0 is not None:
                try:
                    rgb = ImageColor.getrgb(self.draw_color)
                except Exception:
                    rgb = (255, 255, 255)
                bx = c0[0] - adv / 2.0
                bw = max(2.0, adv * 0.18)
                ImageDraw.Draw(img).rectangle([bx, c0[1], bx + bw, c0[2]], fill=tuple(rgb[:3]) + (255,))
        if (cw, ch) != (w, h):
            img = img.resize((cw, ch), Image.LANCZOS)
        if abs(rot) >= 0.01:
            img = img.rotate(-rot, expand=True, resample=Image.BICUBIC)
        self._tp_photo = ImageTk.PhotoImage(img, master=self.canvas)
        self.canvas.delete("text_tpreview")
        self.canvas.create_image((cx1 + cx2) / 2.0, (cy1 + cy2) / 2.0, image=self._tp_photo, anchor="center", tags=("text_tpreview",))
        for lst in (getattr(self, "_tx_ui_ids", None), getattr(self, "_snip_handle_ids", None)):
            for i in list(lst or []):
                try:
                    self.canvas.tag_raise(i)
                except Exception:
                    pass
        self.canvas.tag_raise("dim_overlay")

    def _tp_tick(self):
        if not getattr(self, "_tp_active", False):
            return
        tw = self._pending_text_widget
        if tw is None:
            self._tp_stop(restore=False)
            return
        try:
            self._tp_render(tw)
        except Exception:
            log.exception("text preview: render failed (non-fatal)")
        self.canvas.after(100, self._tp_tick)

    def _tp_render(self, tw):
        try:
            mode = getattr(self, "text_vim_mode", "insert")
            self._tp_cursor_k = len(tw.get("1.0", "insert")) if mode != "insert" else None
        except Exception:
            self._tp_cursor_k = None
        if getattr(self, "_tp_marker_fail", False):
            return self._tp_render_v4(tw)
        from PIL import Image, ImageDraw, ImageColor, ImageTk
        x1, y1, x2, y2 = self._pending_text_box
        w = max(1, int(round(x2 - x1)))
        h = max(1, int(round(y2 - y1)))
        cx1, cy1, cx2, cy2 = self._tx_box_canvas()
        cw = max(1, int(round(cx2 - cx1)))
        ch = max(1, int(round(cy2 - cy1)))
        content = tw.get("1.0", "end-1c")
        rot = getattr(self, "_tx_rot", 0.0) or 0.0
        mode_2 = getattr(self, "text_vim_mode", "insert")
        fill = self.text_bg_fill
        n = len(content)
        ins = min(len(tw.get("1.0", "insert")), n)
        rng = tw.tag_ranges("sel")
        ranges = []
        for i in range(0, len(rng) - 1, 2):
            a = len(tw.get("1.0", rng[i]))
            b = len(tw.get("1.0", rng[i + 1]))
            ranges.append((a, min(b, n)))
        phase = int(time.monotonic() * 2) % 2 if mode_2 == "insert" else 0
        sig = (content, ins, tuple(ranges), mode_2, phase, self.text_font_size, self.text_font_family,
               self.draw_color, self.text_align, fill, (w, h, cw, ch), round(rot, 2),
               repr(tw.dump("1.0", "end", tag=True)))
        if sig == getattr(self, "_tp_sig", None):
            return
        self._tp_sig = sig
        stroke = self._build_text_stroke(content, (x1, y1, x2, y2))
        lay = self._tp_layout_v5(stroke, content, w, h)
        if lay["nonspace"] > 0 and lay["found"] == 0:
            self._tp_marker_fail = True
            self._tp_sig = None
            log.warning("text preview: per-character colour markers not found in the render - falling back to the marker method")
            return self._tp_render_v4(tw)
        adv = lay["adv"]
        cells = lay["cells"]
        bars = lay["bars"]

        def cell(k):
            if k < n:
                return cells[k]
            bx, bt, bb = bars[n]
            return (bx, bt, bx + adv, bb)

        overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        od = ImageDraw.Draw(overlay)
        block = mode_2 != "insert"
        count = 0
        for a, b in ranges:
            for k in range(a, b):
                if block and k == ins:
                    continue
                count += 1
                if count > 2000:
                    break
                c = cell(k)
                if c:
                    od.rectangle([c[0], c[1], c[2], c[3]], fill=(0, 180, 255, 90))
        if block:
            c = cell(ins)
            if c:
                od.rectangle([c[0], c[1], c[2], c[3]], fill=_tp_block_rgba())
        img = Image.alpha_composite(self._tp_rend_v4(stroke, w, h, " ", fill), overlay)
        img = Image.alpha_composite(img, self._tp_rend_v4(stroke, w, h, content, "transparent", keep_runs=True))
        if not block and phase == 0:
            bx, bt, bb = bars[ins]
            try:
                rgb = ImageColor.getrgb(self.draw_color)
            except Exception:
                rgb = (255, 255, 255)
            bw = max(2.0, adv * 0.18)
            ImageDraw.Draw(img).rectangle([bx, bt, bx + bw, bb], fill=tuple(rgb[:3]) + (255,))
        if (cw, ch) != (w, h):
            img = img.resize((cw, ch), Image.LANCZOS)
        if abs(rot) >= 0.01:
            img = img.rotate(-rot, expand=True, resample=Image.BICUBIC)
        self._tp_photo = ImageTk.PhotoImage(img, master=self.canvas)
        self.canvas.delete("text_tpreview")
        self.canvas.create_image((cx1 + cx2) / 2.0, (cy1 + cy2) / 2.0, image=self._tp_photo, anchor="center", tags=("text_tpreview",))
        for lst in (getattr(self, "_tx_ui_ids", None), getattr(self, "_snip_handle_ids", None)):
            for i in list(lst or []):
                try:
                    self.canvas.tag_raise(i)
                except Exception:
                    pass
        self.canvas.tag_raise("dim_overlay")

    # ---- folder setup hook (paths_setup): one-time migration prompts + sloppy sweep, once the decorated window is up ----

    # ---- BACKUP_ORIGINAL: raw copy of the capture when the review window first appears ----

    # ---- OCR review screen fixes (button row always visible, Also copy at the right) ----

    # ---- OCR review button row: constant height, no shift between main / save / name rows ----

    def _fit_ocr_btn_frame(self, prime=False):
        frame = getattr(self, "_ocr_btn_frame", None)
        if frame is None:
            return
        try:
            for c in frame.winfo_children():
                if isinstance(c, tk.Frame):
                    c.pack_configure(pady=0)
            frame.pack_propagate(True)
            frame.update_idletasks()
            h = max(frame.winfo_reqheight(), getattr(frame, "_lock_h", 0))
            if prime:
                probe = tk.Frame(frame.master)
                for w in (tk.Button(probe, text="Sloppy Save", pady=BUTTON_PADY), tk.Checkbutton(probe, text="Also copy"), tk.Entry(probe, width=28)):
                    w.pack(side="left")
                probe.update_idletasks()
                h = max(h, probe.winfo_reqheight())
                probe.destroy()
            frame._lock_h = h
            frame.config(height=h)
            frame.pack_propagate(False)
        except tk.TclError:
            pass

    # ---- OCR review button gaps, Magnify border, Pin checkbox label ----

    # ---- image Magnify: pinned lens (right-click pin / grab, Reset) ----

    def _mp_draw(self):
        from PIL import Image, ImageDraw
        self.canvas.delete("magpin")
        if self._mp_pin is None or self._mp_source is None:
            return
        src = self._mp_source
        lx, ly = self._mp_pin
        lens_size = self.magnify_lens_size
        level = max(1, self.magnify_level)
        scale = self.display_scale
        r = max(1, int((lens_size / 2) / (level * scale)))
        left, top = max(0, int(lx) - r), max(0, int(ly) - r)
        right, bottom = min(src.width, int(lx) + r), min(src.height, int(ly) + r)
        if right <= left or bottom <= top:
            return
        crop = src.crop((left, top, right, bottom))
        out_w = max(1, int((right - left) * level * scale))
        out_h = max(1, int((bottom - top) * level * scale))
        scaled = crop.resize((out_w, out_h), Image.NEAREST)
        mask = Image.new("L", scaled.size, 0)
        ImageDraw.Draw(mask).ellipse([0, 0, scaled.size[0] - 1, scaled.size[1] - 1], fill=255)
        rgba = scaled.convert("RGBA")
        rgba.putalpha(mask)
        photo = self._pil_to_tk(rgba)
        if photo is None:
            return
        self._mp_photo = photo
        cx, cy = self.img_to_canvas(lx, ly)
        x0, y0 = cx - out_w // 2, cy - out_h // 2
        self.canvas.create_image(x0, y0, anchor="nw", image=photo, tags=("magpin",))
        self.canvas.create_oval(x0, y0, x0 + out_w - 1, y0 + out_h - 1, outline="#00b4ff", width=2, tags=("magpin",))

    def _mp_hover_circle(self, cx, cy):
        for attr in ("magnify_lens_item_id", "magnify_cursor_item_id", "_magnify_border_id"):
            item = getattr(self, attr, None)
            if item:
                try:
                    self.canvas.delete(item)
                except tk.TclError:
                    pass
                setattr(self, attr, None)
        r = 6
        self.magnify_cursor_item_id = self.canvas.create_oval(
            cx - r, cy - r, cx + r, cy + r, outline="white", width=2, stipple="gray50",
        )

    def _mp_rclick(self, event):
        if not self.magnify_mode_active:
            return
        cx, cy = self._event_canvas_xy(event)
        if self._mp_pin is None:
            if self.magnify_source_image is None:
                return
            self._mp_source = self.magnify_source_image
            self._mp_pin = self.canvas_to_img(cx, cy)
            self._mp_draw()
            self._mp_hover_circle(cx, cy)
        else:
            px, py = self.img_to_canvas(*self._mp_pin)
            if _mp_math.hypot(cx - px, cy - py) <= self.magnify_lens_size / 2:
                self._mp_pin = None
                self._mp_source = None
                self.canvas.delete("magpin")
                self._draw_magnifier(cx, cy)

    def _mp_reset(self):
        self._mp_pin = None
        self._mp_source = None
        self.canvas.delete("magpin")
        self.magnify_lens_size = MAGNIFIER_LENS_SIZE_DEFAULT
        self.magnify_level = MAGNIFIER_LEVEL_DEFAULT
        self.show_magnify_toolbar()

    # ---- New Layer button (image side) ----

    def _new_arbitrary_layer(self):
        self._activate_canvas_if_needed()
        existing = {l["name"] for l in self.layers}
        n = sum(1 for l in self.layers if l["kind"] == "strokes") + 1
        while f"Layer {n}" in existing:
            n += 1
        layer = self._new_layer(f"Layer {n}", "strokes")
        self.layers.append(layer)
        log.info(f"Layer '{layer['name']}' created from the New Layer button")
        self.isolate_layer(layer["id"])

    # ---- patch 6: Spray Can width setter, double-click cancels an open dropdown ----

    def set_spray_width(self, val):
        self.spray_width = int(float(val))

    # ---- patch 8: Box/Ellipse/Free-form only touch snips; Snip Select touches every object (unless SNIP_SELECT_ALL_OBJECTS is False) ----

    def _select_hit_mode_ok(self, stroke):
        if self.draw_tool == "snip" and self.snip_shape_type == "select" and self._snip_select_all():
            return stroke.get("mode") in ("snip", "text", "shape")
        if self.draw_tool == "snip":
            return stroke.get("mode") == "snip"
        return stroke.get("mode") == self.draw_tool

    def _snip_select_all(self):
        from . import constants as C
        return bool(getattr(C, "SNIP_SELECT_ALL_OBJECTS", True))

    # ---- patch 13: text dropdowns stay open while editing text; alignment guides show only the nearest match ----

    # ---- keep the window where the user dragged it ----

    # ---- patch 15: image Resize row reordered (Region, Color, Scale, dimensions) with a gap before W ----

    def _p16_preview_dims(self, ws, hs):
        try:
            w, h = int(float(ws)), int(float(hs))
            if w < 1 or h < 1:
                raise ValueError
        except (ValueError, TypeError):
            self._p16_clear_preview()
            return
        self._p16_draw_preview(w, h)

    def _p16_preview_scale(self, v):
        try:
            f = float(v)
        except (ValueError, TypeError):
            return
        cw, ch = self._canvas_dims()
        self._p16_draw_preview(round(cw * f), round(ch * f))

    def _p16_draw_preview(self, w, h):
        _p17_prev_draw(self, w, h)
        self._p17_last = (w, h)
        self._p17_scale = self.display_scale

    def _p16_clear_preview(self):
        self._p17_last = None
        _p17_prev_clear(self)

    # ---- helpers that used to be module-level functions taking self ----

    def _sel_stack(self):
        st = getattr(self, "_sel_undo_stack", None)
        if st is None:
            st = self._sel_undo_stack = []
        return st

    def _tp_rend_v4_v2(self, stroke, w, h, text, fill, keep_runs=False):
        from PIL import Image
        st = dict(stroke)
        if not keep_runs:
            st.pop("runs", None)
        st["text"] = text
        st["bg_fill"] = fill
        st["rotation"] = 0.0
        try:
            patch = self._render_element_local_patch(st, w, h)
        except Exception:
            patch = None
        if patch is None:
            patch = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        patch = patch.convert("RGBA")
        if patch.size != (w, h):
            patch = patch.resize((w, h))
        return patch

    def _tp_adv_v4(self, stroke, w, h):
        key = (self.text_font_family, self.text_font_size, w, h)
        d = getattr(self, "_tp_calc", None)
        if d is None:
            d = self._tp_calc = {}
        if key in d:
            return d[key]
        b1 = self._tp_rend_v4(stroke, w, h, "|", "transparent").getchannel("A").getbbox()
        b2 = self._tp_rend_v4(stroke, w, h, "||", "transparent").getchannel("A").getbbox()
        if b1 and b2:
            adv = max(2.0, float((b2[2] - b2[0]) - (b1[2] - b1[0])))
        else:
            adv = max(4.0, self.text_font_size * 0.5)
        d[key] = adv
        return adv

    def _refocus_main_v4(self):
        try:
            self.canvas.winfo_toplevel().focus_force()
            w = self._pending_text_widget
            if w is not None:
                w.focus_set()
        except Exception:
            pass

    def _tp_layout_v5(self, stroke, content, w, h):
        key = (content, repr(stroke.get("runs")), self.text_font_family, self.text_font_size, self.text_align, w, h)
        c = getattr(self, "_tp_lay5", None)
        if c is not None and c[0] == key:
            return c[1]
        from PIL import Image
        n = len(content)
        adv = self._tp_adv_v4(stroke, w, h)
        bar1 = self._tp_rend_v4(stroke, w, h, "|", "transparent").getchannel("A").getbbox()
        bar2 = self._tp_rend_v4(stroke, w, h, "|\n|", "transparent").getchannel("A").getbbox()
        if bar1:
            H = float(bar1[3] - bar1[1])
            cbar = (bar1[0] + bar1[2]) / 2.0
            bar_cy0 = (bar1[1] + bar1[3]) / 2.0
        else:
            H = max(8.0, self.text_font_size * 1.2)
            cbar = 4.0
            bar_cy0 = H / 2.0
        if bar1 and bar2 and bar2[3] > bar1[3]:
            P = float(bar2[3] - bar1[3])
        else:
            P = H * 1.2
        ink = {}
        if n:
            orig = stroke.get("runs") or []
            if not orig or sum(len(r.get("text", "")) for r in orig) != n:
                orig = [{"text": content}]
            runs = []
            k = 0
            for r in orig:
                for ch in r.get("text", ""):
                    d = dict(r)
                    d["text"] = ch
                    idv = k + 1
                    d["color"] = "#%02x%02x%02x" % ((idv >> 16) & 255, (idv >> 8) & 255, idv & 255)
                    runs.append(d)
                    k += 1
            st = dict(stroke)
            st["runs"] = runs
            st["text"] = content
            st["bg_fill"] = "transparent"
            st["rotation"] = 0.0
            try:
                patch = self._render_element_local_patch(st, w, h)
            except Exception:
                patch = None
            if patch is not None:
                patch = patch.convert("RGBA")
                if patch.size != (w, h):
                    patch = patch.resize((w, h))
                ink = _tp_ink_boxes_v5(patch, n)
        nonspace = sum(1 for ch in content if not ch.isspace())
        found = sum(1 for k2 in ink if not content[k2].isspace())
        line_of = [0] * n
        j = 0
        prev = None
        line_yc = None
        pend = []
        for k, ch in enumerate(content):
            if ch == "\n":
                for q in pend:
                    line_of[q] = j
                pend = []
                line_of[k] = j
                j += 1
                prev = None
                line_yc = None
                continue
            b = ink.get(k)
            if b is None:
                pend.append(k)
                continue
            yc = (b[1] + b[3]) / 2.0
            if prev is not None:
                pb = ink[prev]
                if b[0] < pb[0] or abs(yc - line_yc) > 0.6 * P:
                    for q in pend:
                        line_of[q] = j
                    pend = []
                    j += 1
                    prev = None
                    line_yc = None
            for q in pend:
                line_of[q] = j
            pend = []
            line_of[k] = j
            line_yc = yc if line_yc is None else line_yc * 0.7 + yc * 0.3
            prev = k
        for q in pend:
            line_of[q] = j
        nlines = j + 1
        by_line = {}
        for k in range(n):
            by_line.setdefault(line_of[k], []).append(k)
        gaps0 = []
        for jj, ks in by_line.items():
            ik = [k for k in ks if k in ink]
            for t in range(len(ik) - 1):
                if ik[t + 1] - ik[t] == 1:
                    gaps0.append(ink[ik[t + 1]][0] - ink[ik[t]][2])
        e = max(0.5, _tp_med_v5([g for g in gaps0], 1.0) / 2.0)
        spl = []
        for jj, ks in by_line.items():
            ik = [k for k in ks if k in ink]
            for t in range(len(ik) - 1):
                m = ik[t + 1] - ik[t] - 1
                if m > 0:
                    val = (ink[ik[t + 1]][0] - ink[ik[t]][2] - 2 * e) / m
                    if val > 0:
                        spl.append(val)
        sp = _tp_med_v5(spl, adv)
        ycs = []
        for k in ink:
            ycs.append((ink[k][1] + ink[k][3]) / 2.0 - line_of[k] * P)
        offset = _tp_med_v5(ycs, bar_cy0)

        def cyl(jj):
            return offset + jj * P

        cells = [None] * n
        for jj, ks in by_line.items():
            T = cyl(jj) - H / 2.0 - 1
            B = cyl(jj) + H / 2.0 + 1
            inked = [k for k in ks if k in ink]
            if not inked:
                x = cbar - adv / 2.0
                for k in ks:
                    cells[k] = (x, T, x + adv, B)
                    x += adv
                continue
            LR = {}
            for t, k in enumerate(inked):
                b = ink[k]
                if t == 0:
                    L = b[0] - e
                else:
                    kp = inked[t - 1]
                    L = (ink[kp][2] + b[0]) / 2.0 if k - kp == 1 else b[0] - e
                if t == len(inked) - 1:
                    R = b[2] + e
                else:
                    kn = inked[t + 1]
                    R = (b[2] + ink[kn][0]) / 2.0 if kn - k == 1 else b[2] + e
                LR[k] = (L, R)
                cells[k] = (L, T, R, B)
            for t in range(len(inked) - 1):
                ka, kb = inked[t], inked[t + 1]
                m = kb - ka - 1
                if m > 0:
                    xl = LR[ka][1]
                    wd = (LR[kb][0] - xl) / m
                    for q in range(m):
                        cells[ka + 1 + q] = (xl + q * wd, T, xl + (q + 1) * wd, B)
            first, last = inked[0], inked[-1]
            x = LR[first][0]
            for k in reversed([k for k in ks if k < first]):
                cells[k] = (x - sp, T, x, B)
                x -= sp
            x = LR[last][1]
            for k in [k for k in ks if k > last]:
                cells[k] = (x, T, x + sp, B)
                x += sp
        bars = []
        for k in range(n + 1):
            if k < n:
                c0 = cells[k]
                bars.append((c0[0], c0[1], c0[3]))
            else:
                if n == 0:
                    jj = 0
                    x = cbar
                elif content[-1] == "\n":
                    jj = nlines - 1
                    x = cbar
                else:
                    jj = line_of[n - 1]
                    x = cells[n - 1][2]
                bars.append((x, cyl(jj) - H / 2.0 - 1, cyl(jj) + H / 2.0 + 1))
        lay = {"cells": cells, "bars": bars, "adv": adv, "found": found, "nonspace": nonspace}
        self._tp_lay5 = (key, lay)
        log.debug(f"text preview layout: chars={n} inked={found}/{nonspace} lines={nlines} adv={adv:.1f} H={H:.1f} P={P:.1f}")
        return lay

    def _tp_rend_v4_v1(self, stroke, w, h, text, fill, keep_runs=False):
        p = self._tp_rend_v4_v2(stroke, w, h, text, fill, keep_runs)
        if not keep_runs or fill != "transparent" or not text:
            return p
        lc = getattr(self, "_tp_lay5", None)
        if lc is None or lc[0][0] != text:
            return p
        try:
            cells = lc[1]["cells"]
            bb = p.getchannel("A").getbbox()
            if bb is None:
                return p
            exp_bottom = 0.0
            for k in range(len(text)):
                if not text[k].isspace() and cells[k] is not None:
                    exp_bottom = max(exp_bottom, cells[k][3])
            hc = cells[0][3] - cells[0][1]
            mismatch = (exp_bottom - bb[3]) > 0.6 * hc
            if getattr(self, "_tp_perchar_flag", None) != mismatch:
                self._tp_perchar_flag = mismatch
                log.debug(f"text preview: final render ink bottom {bb[3]} vs layout {exp_bottom:.0f} -> {'per-char runs (original runs did not wrap)' if mismatch else 'original runs'}")
            if not mismatch:
                return p
            orig = stroke.get("runs") or []
            if not orig or sum(len(r.get("text", "")) for r in orig) != len(text):
                orig = [{"text": text}]
            runs = []
            for r in orig:
                for ch in r.get("text", ""):
                    d = dict(r)
                    d["text"] = ch
                    runs.append(d)
            s2 = dict(stroke)
            s2["runs"] = runs
            return self._tp_rend_v4_v2(s2, w, h, text, fill, True)
        except Exception:
            log.exception("text preview: wrap consistency check failed (non-fatal)")
            return p

    def _erase_target_layer_v1(self):
        if self.isolated_layer_id is not None:
            layer = self._find_layer(self.isolated_layer_id)
            if layer is not None and layer["kind"] == "strokes":
                return layer["id"]
        natural = LAYER_MODE_GROUPS.get("erase", "Draw")
        for layer in self.layers:
            if layer.get("kind") == "strokes" and layer.get("group_key") == natural:
                return layer["id"]
        return None

    def _tp_rend_v4(self, stroke, w, h, text, fill, keep_runs=False):
        from . import constants as _c
        k = getattr(self, "_tp_cursor_k", None)
        if (keep_runs and fill == "transparent" and k is not None and text and k < len(text)
                and not text[k].isspace() and getattr(_c, "TEXT_CURSOR_CONTRAST_TEXT", True)):
            orig = stroke.get("runs") or []
            if not orig or sum(len(r.get("text", "")) for r in orig) != len(text):
                orig = [{"text": text}]
            runs = []
            idx = 0
            for r in orig:
                for ch in r.get("text", ""):
                    d = dict(r)
                    d["text"] = ch
                    if idx == k:
                        d["color"] = _tp_contrast_hex(r.get("color") or stroke.get("color") or self.draw_color)
                    runs.append(d)
                    idx += 1
            s2 = dict(stroke)
            s2["runs"] = runs
            return self._tp_rend_v4_v1(s2, w, h, text, fill, True)
        return self._tp_rend_v4_v1(stroke, w, h, text, fill, keep_runs)

    def _p14_leave_resize_then(self, fn):
        # Done for the resize session, then continue with what the user clicked in the shared top row.
        if getattr(self, "resize_mode_active", False):
            self._exit_resize_mode()
        fn()

    def _p14_actual_pos(self):
        m = _p14_re.match(r"^\d+x\d+\+(-?\d+)\+(-?\d+)$", self.root.geometry())
        return (int(m.group(1)), int(m.group(2))) if m else None

    def _p22_acquire_input(self, attempt=0, reason="startup"):  # _p22_acquire_input
        root = self.root
        try:
            if not root.winfo_exists() or not root.overrideredirect():
                return   # overlay is gone (selection finished) or this is a decorated window
        except tk.TclError:
            return
        ok_focus = ok_grab = False
        detail = ""
        try:
            root.lift()
            root.focus_force()
            if root.winfo_viewable():
                from .capture import BACKEND, _X11FocusHelper
                if BACKEND == "x11":
                    helper = _X11FocusHelper()
                    try:
                        helper.set_focus(root.winfo_id())
                        holder = helper.get_focus_window()
                        ok_focus = (holder == root.winfo_id())
                        detail = f"focus holder {holder:#x} vs overlay {root.winfo_id():#x}"
                    finally:
                        helper.close()
                else:
                    ok_focus = True
                    self._wl_key_catcher_start()   # patch 45
            else:
                detail = "overlay not viewable yet"
        except Exception as e:
            detail = f"focus error: {e}"
        try:
            root.grab_set_global()
            ok_grab = True
        except tk.TclError as e:
            detail += f"; grab failed: {e}"
        if ok_focus and ok_grab:
            log.info(f"Select overlay input acquired ({reason}, attempt {attempt + 1}): {detail}")
            return
        if attempt < 4:
            log.info(f"Select overlay input not yet acquired ({reason}, attempt {attempt + 1}): focus={ok_focus} grab={ok_grab} {detail}")
            root.after(100, lambda: self._p22_acquire_input(attempt + 1, reason))
        else:
            log.warning(
                f"Select overlay could not take input after {attempt + 1} tries (focus={ok_focus} grab={ok_grab}; {detail}). "
                f"Another program (a fullscreen game?) probably holds an exclusive keyboard/pointer grab."
            )

    def _wl_key_catcher_start(self):
        """
        patch 45 (Wayland): compositors don't give keyboard focus to override-redirect XWayland
        windows (the overlay) and refuse XSetInputFocus, so Escape/shortcuts never arrived until
        the overlay was clicked. A tiny managed dialog window - which the compositor DOES focus
        when it maps - sits underneath the overlay and forwards every key to it. It removes
        itself as soon as the overlay is gone or becomes the decorated review window.
        """
        if getattr(self, "_wl_key_catcher", None) is not None:
            return
        root = self.root
        try:
            cat = tk.Toplevel(root, class_="snippysnappy-keys")
            cat.title("snippysnappy")
            try:
                cat.attributes("-type", "dialog")   # floated (not tiled) by tiling compositors
            except tk.TclError:
                pass
            cat.geometry(f"1x1+{root.winfo_rootx()}+{root.winfo_rooty()}")
            cat.transient(root)
        except Exception:
            log.exception("Wayland key catcher: could not create (Escape needs a click on the overlay first)")
            return
        self._wl_key_catcher = cat

        def forward(e, kind):
            try:
                if not root.winfo_exists():
                    return "break"
                target = root.focus_lastfor() or root
                target.event_generate(kind, keysym=e.keysym, state=e.state, when="tail")
            except tk.TclError:
                pass
            return "break"

        cat.bind("<KeyPress>", lambda e: forward(e, "<KeyPress>"))
        cat.bind("<KeyRelease>", lambda e: forward(e, "<KeyRelease>"))

        def watch():
            try:
                if not root.winfo_exists() or not root.overrideredirect():
                    cat.destroy()
                    self._wl_key_catcher = None
                    return
                if root.focus_displayof() is None:
                    cat.focus_force()
                cat.after(150, watch)
            except tk.TclError:
                self._wl_key_catcher = None

        cat.after(50, watch)
        log.info("Wayland: key catcher window mapped (forwards keys to the overlay)")

    def _p35_center_view(self):
        # after a fit: drop any middle-drag pan and centre the image in the canvas
        try:
            c = self.canvas
            c.update_idletasks()
            sr = [float(v) for v in str(c.cget("scrollregion")).split()]
            if len(sr) != 4:
                return
            w, h = sr[2] - sr[0], sr[3] - sr[1]
            cw, ch = c.winfo_width(), c.winfo_height()
            c.xview_moveto(0)
            c.yview_moveto(0)
            dx, dy = max(0, (cw - w) / 2.0), max(0, (ch - h) / 2.0)
            if (dx >= 1 or dy >= 1) and w > 0 and h > 0:
                if dx >= 1:
                    c.xview_moveto(-dx / w)
                if dy >= 1:
                    c.yview_moveto(-dy / h)
        except tk.TclError:
            pass

    def _p36_er_begin(self, st):
        from PIL import Image, ImageDraw
        if self.isolated_layer_id is not None:
            return None
        lid = self._erase_target_layer_v1()
        if lid is None:
            return None
        size = self._canvas_dims()
        cur = self._render_layered(exclude_index=None, layer_filter=None).convert("RGBA")
        # finished pen/highlight strokes can still have their live vector lines on the canvas, drawn above the picture;
        # they are part of `cur` already, and would hide the erasing, so take them off
        for s in self.edit_strokes:
            if s is st:
                continue
            for seg in s.get("segment_ids", []) or []:
                try:
                    self.canvas.delete(seg)
                except Exception:
                    pass
            if s.get("mode") in ("pen", "highlight", "spray"):
                s["segment_ids"] = []
        # the same picture with this layer's ink left out: that is what the brush reveals
        hidden = []
        for s in self.edit_strokes:
            if s.get("layer_id") == lid and s.get("mode") in ("pen", "highlight", "spray") and not s.get("removed"):
                s["removed"] = True
                hidden.append(s)
        try:
            alt = self._render_layered(exclude_index=None, layer_filter=None).convert("RGBA")
        finally:
            for s in hidden:
                s["removed"] = False
        mask = Image.new("L", size, 0)
        return {"stroke": st, "cur": cur, "alt": alt, "work": cur.copy(), "mask": mask, "draw": ImageDraw.Draw(mask),
                "n": 0, "last": None, "after": None, "size": size}

    def _p36_er_flush(self, S):
        S["after"] = None
        if getattr(self, "_p36_er", None) is not S:
            return
        from PIL import Image
        t0 = time.monotonic()
        st = S["stroke"]
        pts = st.get("points", [])
        if len(pts) <= S["n"]:
            return
        ox, oy = self.canvas_x1, self.canvas_y1
        w = max(1, int(st.get("width", 10)))
        r = w / 2.0
        xs, ys = [], []
        last = S["last"]
        for p in pts[S["n"]:]:
            q = (p[0] - ox, p[1] - oy)
            if last is not None:
                S["draw"].line([last, q], fill=255, width=w)
            S["draw"].ellipse([q[0] - r, q[1] - r, q[0] + r, q[1] + r], fill=255)
            xs.append(q[0])
            ys.append(q[1])
            last = q
        if last is not None and S["last"] is not None:
            xs.append(S["last"][0])
            ys.append(S["last"][1])
        S["last"] = last
        S["n"] = len(pts)
        cw, ch = S["size"]
        pad = int(r) + 3
        box = (max(0, int(min(xs)) - pad), max(0, int(min(ys)) - pad), min(cw, int(max(xs)) + pad + 1), min(ch, int(max(ys)) + pad + 1))
        if box[2] > box[0] and box[3] > box[1]:
            region = Image.composite(S["alt"].crop(box), S["cur"].crop(box), S["mask"].crop(box))
            S["work"].paste(region, box[:2])
        self._show_pil_on_base(S["work"], fast=True)
        dt = time.monotonic() - t0
        S["count"] = S.get("count", 0) + 1
        if S["count"] % 25 == 1:
            log.debug(f"eraser live preview: {dt * 1000:.0f} ms/update, {len(pts)} points")

    def _p36_er_preview(self):
        st = self.edit_current_stroke
        if not st or st.get("mode") != "erase" or not st.get("points"):
            return
        S = getattr(self, "_p36_er", None)
        if S is None or S["stroke"] is not st:
            try:
                S = self._p36_er_begin(st)
            except Exception:
                log.exception("eraser live preview setup failed (non-fatal)")
                S = None
            if S is None:
                self._p36_er = None
                st_2 = self.edit_current_stroke
                if not st_2 or st_2.get("mode") != "erase" or len(st_2.get("points", [])) < 2:
                    return
                now = time.monotonic()
                if now < getattr(self, "_er_next", 0.0):
                    return
                lid = self._erase_target_layer_v1()
                if lid is None:
                    return
                tmp = dict(st_2)
                tmp["points"] = list(st_2["points"])
                tmp["segment_ids"] = []
                tmp["layer_id"] = lid
                tmp["eid"] = "_erase_preview"
                self.edit_strokes.append(tmp)
                try:
                    self.baked_image = self._render_layered(exclude_index=None, layer_filter=None)
                    self._refresh_base_display(fast=True)
                except Exception:
                    log.exception("eraser preview failed (non-fatal)")
                finally:
                    for i in range(len(self.edit_strokes) - 1, -1, -1):
                        if self.edit_strokes[i] is tmp:
                            del self.edit_strokes[i]
                            break
                dt = time.monotonic() - now
                self._er_next = time.monotonic() + max(0.04, dt * 1.5)
                self._er_count = getattr(self, "_er_count", 0) + 1
                if self._er_count % 20 == 1:
                    log.debug(f"eraser preview: {dt * 1000:.0f} ms, {len(st_2['points'])} points, layer={lid}")
                return
            self._p36_er = S
        if S["after"] is None:
            S["after"] = self.canvas.after(8, lambda: self._p36_er_flush_safe(S))

    def _p36_er_flush_safe(self, S):
        try:
            self._p36_er_flush(S)
        except Exception:
            log.exception("eraser live preview update failed (non-fatal)")

    def _p36_text_sweep(self):
        if self._pending_text_widget is not None:
            return
        try:
            self._tx_clear_ui()
            self.canvas.delete("text_ui")
            self.canvas.delete("text_box_content")
            self.canvas.delete("text_tpreview")
            self._tx_rs = None
            if self.text_current_box_id:
                self.canvas.delete(self.text_current_box_id)
                self.text_current_box_id = None
        except Exception:
            pass

    def _p36_install_forward(self):
        tag = f"SnippyTextFwd{id(self)}"
        if not getattr(self, "_p36_fwd_bound", False):
            def fwd(e):
                tw = self._pending_text_widget
                if tw is None:
                    return None
                try:
                    if e.widget is tw or e.widget.winfo_class() in ("Entry", "TEntry", "Spinbox", "TSpinbox", "Text", "Listbox", "Menu", "TCombobox"):
                        return None
                    if e.state & 0x4 or e.state & 0x8 or e.keysym in _p36_MODS:
                        return None
                    tw.focus_set()
                    tw.event_generate("<Key>", keysym=e.keysym)
                    log.debug(f"text box: forwarded key {e.keysym!r} (focus was on {e.widget})")
                except Exception:
                    return None
                return "break"
            self.canvas.bind_class(tag, "<Key>", fwd)
            self._p36_fwd_bound = True
        for w in (self.canvas, self.root):
            try:
                tags = w.bindtags()
                if tag not in tags:
                    w.bindtags((tag,) + tuple(tags))
            except Exception:
                pass


# ---- Closed windows leave the Windows list; layer labels are bare names ----
# EXIT_BEHAVIOR="background" makes _close_window() only withdraw the window,
# which used to leave it listed forever (stale crop/full windows). Wrapped
# here so every close path (Cancel, Escape, panel x, remote close) unlists it.


# ---- Side panel scroll: start at the top when the panel mode changes ----


# ---- Hover tooltips for cropped button text everywhere (windows, tabs, layers, pins list) ----


def _p20_prev_init(self, *args, **kwargs):
    self._init_base(*args, **kwargs)
    try:
        install_button_tooltips(self.root)
    except Exception:
        log.exception("Could not install the button tooltips (non-fatal)")


# ---- Decorated X backgrounds the window (per EXIT_BEHAVIOR); discarding is Cancel / the panel x ----


# Discarding one window must not take the whole process (and its other windows) with it.


# ---- Title follows the active tab ----


# ---- discard toasts (image mode) ----
def _toast_discard(self_):
    try:
        fn = globals().get("notify")
        if fn:
            fn("Snippysnap Discarded", blocking=False)
    except Exception:
        log.exception("discard toast failed")


# ---- keep the cross-process window roster fresh when labels change ----


# ---- undo for the main-toolbar selection / crop handles ----


# Image review windows are always-on-top by design (set at creation); tell focus_local_window so it
# restores True afterwards instead of trusting a -topmost read-back that is unreliable right after deiconify.


_TX_PAD = 5


_TX_PAD_PX = 8


def _tp_diffbox_v4(a, b):
    from PIL import ImageChops
    d = ImageChops.difference(a, b)
    r, g, bl, al = d.split()
    m = ImageChops.lighter(ImageChops.lighter(r, g), ImageChops.lighter(bl, al))
    return m.point(lambda v: 255 if v > 24 else 0).getbbox()


def _tp_med_v5(vals, default):
    v = sorted(vals)
    if not v:
        return default
    return v[len(v) // 2]


def _tp_ink_boxes_v5(img, n):
    boxes = {}
    try:
        import numpy as np
        a = np.asarray(img.convert("RGBA"))
        ys, xs = np.nonzero(a[:, :, 3] >= 128)
        if ys.size == 0:
            return boxes
        px = a[ys, xs]
        ids = (px[:, 0].astype(np.int64) << 16) | (px[:, 1].astype(np.int64) << 8) | px[:, 2].astype(np.int64)
        ok = (ids >= 1) & (ids <= n)
        ys, xs, ids = ys[ok], xs[ok], ids[ok]
        if ids.size == 0:
            return boxes
        order = np.argsort(ids, kind="stable")
        ids, ys, xs = ids[order], ys[order], xs[order]
        uniq, start = np.unique(ids, return_index=True)
        x0 = np.minimum.reduceat(xs, start)
        x1 = np.maximum.reduceat(xs, start)
        y0 = np.minimum.reduceat(ys, start)
        y1 = np.maximum.reduceat(ys, start)
        for i in range(len(uniq)):
            boxes[int(uniq[i]) - 1] = (float(x0[i]), float(y0[i]), float(x1[i]) + 1.0, float(y1[i]) + 1.0)
        return boxes
    except Exception:
        boxes = {}
    bb = img.getchannel("A").point(lambda v: 255 if v >= 128 else 0).getbbox()
    if not bb:
        return boxes
    crop = img.convert("RGBA").crop(bb)
    data = crop.tobytes()
    cw = crop.size[0]
    tmp = {}
    for i in range(0, len(data), 4):
        if data[i + 3] < 128:
            continue
        idv = (data[i] << 16) | (data[i + 1] << 8) | data[i + 2]
        if idv < 1 or idv > n:
            continue
        pidx = i // 4
        x = pidx % cw + bb[0]
        y = pidx // cw + bb[1]
        b = tmp.get(idv - 1)
        if b is None:
            tmp[idv - 1] = [x, y, x + 1, y + 1]
        else:
            if x < b[0]:
                b[0] = x
            if y < b[1]:
                b[1] = y
            if x + 1 > b[2]:
                b[2] = x + 1
            if y + 1 > b[3]:
                b[3] = y + 1
    for k2, b in tmp.items():
        boxes[k2] = (float(b[0]), float(b[1]), float(b[2]), float(b[3]))
    return boxes


def _tp_block_rgba():
    from PIL import ImageColor
    from . import constants as _c
    try:
        r, g, b = ImageColor.getrgb(getattr(_c, "TEXT_CURSOR_COLOR", "#ffd800"))[:3]
    except Exception:
        r, g, b = 255, 216, 0
    return (r, g, b, int(getattr(_c, "TEXT_CURSOR_ALPHA", 235)))


def _tp_contrast_hex(base):
    from PIL import ImageColor
    try:
        r, g, b = ImageColor.getrgb(base)[:3]
        lum = 0.299 * r + 0.587 * g + 0.114 * b
    except Exception:
        lum = 255
    return "#ffffff" if lum < 50 else "#000000"


# ---- folder setup hook (paths_setup): one-time migration prompts + sloppy sweep, once the decorated window is up ----
_paths_hook_state = {"done": False}


# ---- BACKUP_ORIGINAL: raw copy of the capture when the review window first appears ----
import shutil as _og_shutil
from . import constants as _og_consts


# ---- OCR review screen fixes (button row always visible, Also copy at the right) ----


# ---- OCR review button row: constant height, no shift between main / save / name rows ----


# ---- OCR review button gaps, Magnify border, Pin checkbox label ----


def _gap_walk(widget):
    seen_nav = False
    for c in widget.winfo_children():
        if isinstance(c, tk.Button) and c.winfo_manager() == "pack":
            if c.cget("text") in ("Back", "Cancel", "Close") and not seen_nav:
                seen_nav = True
                c.pack_configure(padx=(10, 2))
            else:
                c.pack_configure(padx=2)
        elif isinstance(c, tk.Frame):
            _gap_walk(c)


def _rename_other_windows(widget):
    for c in widget.winfo_children():
        if isinstance(c, tk.Checkbutton) and c.cget("text") == "Show other windows' pins":
            c.config(text="Other windows")
        else:
            _rename_other_windows(c)


# ---- image Magnify: pinned lens (right-click pin / grab, Reset) ----
import math as _mp_math


def _mp_find_label(widget, text):
    for c in widget.winfo_children():
        if isinstance(c, tk.Label) and c.cget("text") == text:
            return c
        found = _mp_find_label(c, text)
        if found is not None:
            return found
    return None


# ---- Pins list: per-entry X; fullscreen monitor dropdown gap ----


def _walk_option_menus(widget, seen):
    for c in widget.winfo_children():
        if c in seen:
            continue
        if isinstance(c, tk.OptionMenu):
            yield c
        elif isinstance(c, tk.Frame):
            yield from _walk_option_menus(c, seen)


# ---- New Layer button (image side) ----


# ---- patch 6: Spray Can width setter, double-click cancels an open dropdown ----


def _p6_dc_cancel(mb, event=None):
    try:
        m = mb.nametowidget(mb.cget("menu"))
    except (tk.TclError, KeyError):
        return
    if event is not None and event.widget is m:
        px, py = mb.winfo_pointerxy()
        x0, y0 = mb.winfo_rootx(), mb.winfo_rooty()
        if not (x0 <= px < x0 + mb.winfo_width() and y0 <= py < y0 + mb.winfo_height()):
            return
    try:
        mb.tk.call("tk::MenuUnpost", str(m))
    except tk.TclError:
        try:
            m.unpost()
        except tk.TclError:
            pass
    return "break"


if not getattr(tk.OptionMenu, "_p6_dc", False):
    _p6_om_init = tk.OptionMenu.__init__

    def _p6_om_init_dc(self, *args, **kwargs):
        _p6_om_init(self, *args, **kwargs)
        try:
            self.bind("<Double-Button-1>", lambda e, w=self: _p6_dc_cancel(w), add="+")
            self.nametowidget(self.cget("menu")).bind("<Double-Button-1>", lambda e, w=self: _p6_dc_cancel(w, e), add="+")
        except (tk.TclError, KeyError):
            pass

    tk.OptionMenu.__init__ = _p6_om_init_dc
    tk.OptionMenu._p6_dc = True


# ---- patch 7: Snip Select is the one cross-tool select (handles and grabbing for every element type) ----


# ---- patch 8: Box/Ellipse/Free-form only touch snips; Snip Select touches every object (unless SNIP_SELECT_ALL_OBJECTS is False) ----


# ---- patch 13: text dropdowns stay open while editing text; alignment guides show only the nearest match ----


# ---- patch 14: image Resize keeps the Edit top row; controls live in the second row with selectors ----

import re as _p14_re


# ---- keep the window where the user dragged it ----


# ---- patch 15: image Resize row reordered (Region, Color, Scale, dimensions) with a gap before W ----


def _p17_prev_clear(self):
    try:
        self._clear_resize_preview()
        self._clear_dim_overlay()
    except Exception:
        pass


def _p17_prev_draw(self, w, h):
    self._p16_clear_preview()
    cw, ch = self._canvas_dims()
    w, h = max(1, int(w)), max(1, int(h))
    if (w, h) == (cw, ch):
        return
    s = self.display_scale
    x0, y0 = self.img_to_canvas(0, 0)
    cx1, cy1 = x0 + cw * s, y0 + ch * s
    nx1, ny1 = x0 + w * s, y0 + h * s
    ids = []
    mk = self.canvas.create_rectangle
    if w < cw:
        ids.append(mk(nx1, y0, cx1, cy1, fill="black", stipple="gray50", outline=""))
    if h < ch:
        ids.append(mk(x0, ny1, min(nx1, cx1), cy1, fill="black", stipple="gray50", outline=""))
    if w > cw:
        ids.append(mk(cx1, y0, nx1, ny1, fill="#00b4ff", stipple="gray25", outline=""))
    if h > ch:
        ids.append(mk(x0, cy1, min(nx1, cx1), ny1, fill="#00b4ff", stipple="gray25", outline=""))
    ids.append(mk(x0, y0, nx1, ny1, outline="#ffaa00", width=2, dash=(5, 3)))
    self._resize_preview_item_ids = ids
    self._show_dim_overlay(nx1, ny1, f"{w} x {h}")


# ---- patch 20: toolbar tooltips need to know this is an image window and which mode it is in ----


def _p20_init(self, *args, **kwargs):
    _p20_prev_init(self, *args, **kwargs)
    try:
        from .tooltips import image_mode
        self.root._tip_kind = "image"
        self.root._tip_mode = lambda: image_mode(self)
    except Exception:
        log.exception("Could not set up toolbar tooltips (non-fatal)")


# ---- patch 22: select-mode overlay input acquisition (fullscreen games) ----


# ---- patch 23: leader key (_p23_leader) ----


# ---- patch 33: image - free middle-drag pan, handles back on main after Edit/Resize, window stays on its monitor (_p33_*) ----
import re as _p33_re


def _p33_clamp(monitors, x, y, w, h, edge_x=8, edge_y=64):
    if not monitors:
        return x, y
    cx, cy = x + w // 2, y + min(h, 80) // 2
    mon = next((m for m in monitors if m[1] <= cx < m[1] + m[3] and m[2] <= cy < m[2] + m[4]), None)
    if mon is None:
        mon = min(monitors, key=lambda m: abs(cx - (m[1] + m[3] / 2)) + abs(cy - (m[2] + m[4] / 2)))
    _n, mx, my, mw, mh = mon[:5]
    return max(mx, min(x, mx + mw - w - edge_x)), max(my, min(y, my + mh - h - edge_y))


def _p33_pan_clamp(canvas, margin=80):
    try:
        sr = [float(v) for v in str(canvas.cget("scrollregion")).split()]
        if len(sr) != 4:
            return
        x0, y0, x1, y1 = sr
        cw, ch = canvas.winfo_width(), canvas.winfo_height()
        vx, vy = canvas.canvasx(0), canvas.canvasy(0)
        nx = max(x0 - (cw - margin), min(vx, x1 - margin))
        ny = max(y0 - (ch - margin), min(vy, y1 - margin))
        if abs(nx - vx) > 0.5 and x1 > x0:
            canvas.xview_moveto((nx - x0) / (x1 - x0))
        if abs(ny - vy) > 0.5 and y1 > y0:
            canvas.yview_moveto((ny - y0) / (y1 - y0))
    except tk.TclError:
        pass


# the selection handles belong on the main screen: Edit / Resize / Done used to leave it without them


# a window that has to grow (toolbar content, Resize) is shifted just enough to stay on its monitor,
# instead of being left for the window manager to throw somewhere else


# ---- patch 35: text re-edit keeps its focus; Re-scale / auto-fit resets a middle-drag pan and centres the image (_p35_*) ----


# ---- patch 36: eraser shows the removal live; text box leftovers cleared; keys reach the text box (_p36_*) ----


# --- text box: clear the blue border/handles whenever a text entry ends, however it ends ---


# --- text box: keys typed while focus is elsewhere in the window still reach it ---
_p36_MODS = {"Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R", "Meta_L", "Meta_R", "Super_L", "Super_R",
             "Caps_Lock", "Num_Lock", "ISO_Level3_Shift", "Mode_switch", "Hyper_L", "Hyper_R"}


# ---- module-level names other modules import ----
_p17_draw = SelectorApp._p16_draw_preview
_p17_clear = SelectorApp._p16_clear_preview
_p33_pan_drag = SelectorApp._pan_drag


# ---- patch 41: the _pN_ monkeypatch chains are folded into plain class methods ----
_p41_installed = True
