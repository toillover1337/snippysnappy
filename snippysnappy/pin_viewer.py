# snippysnappy v1.0.3
"""The floating Pin viewer window (run_pin_viewer) - a separate lightweight, borderless Tk window per pinned region, with hover-to-reveal controls, resize/lock/maximize/minimize, and cross-process discovery via the coordinator's pin registry."""
import math
import os
import sys
import tempfile
import tkinter as tk
import uuid
from pathlib import Path

from . import paths_setup as _paths_setup
from .logging_setup import log
from .capture import get_monitors, monitor_at_point, BACKEND, _X11EscapePoller, _X11FocusHelper, set_bypass_compositor_hint
from .clipboard import copy_image_to_clipboard
from .notify_toast import notify, timestamp
from .coordinator import _register_pin, _unregister_pin, _PinHandle, init_coordinator, set_restart_handler

PIN_MAXIMIZE_SIDE_MARGIN = 235   # px reserved for max. LEFT and RIGHT.
PIN_MAXIMIZE_VERTICAL_MARGIN = 15   # px reserved on the TOP and BOTTOM.
PIN_MIN_CONTENT_WIDTH = 260   # px - a pin's window never shrinks smaller than this (either axis).
PIN_MIN_CONTENT_HEIGHT = 90   # px - same idea, vertical axis.


_VIDEO_BAR_H = [0]   # height reserved at the bottom for a video pin's playback bar (0 for plain image pins)


def run_pin_viewer(path, forced_pin_id=None, proportional=True):
    """
    A standalone, floating "pinned" image window - the process launched
    by the main app's Pin tool (see SelectorApp._launch_pinned_region).
    No document model, no toolbar - a borderless, always-on-top picture -
    but it DOES join the same coordinator/roster the main app's windows
    use (as a _PinHandle, not a SelectorApp - see _register_pin), purely
    so the main app's Pins list can see it, focus it, and force-close it.
    It's launched fully detached (start_new_session=True) from whatever
    snippysnappy window created it, so it keeps floating on the desktop
    even after that window - or the whole rest of snippysnappy - closes.

    `forced_pin_id`, when given (the normal case - see
    SelectorApp._launch_pinned_region), is used as this pin's coordinator
    id INSTEAD of a freshly generated one, so the window that launched this
    process can recognize it as its own pin in its Pins list (see
    SelectorApp._owned_pin_ids) even though it's a separate OS process.

    `proportional` (from the Pin toolbar's own "Proportional" checkbox at
    the moment this pin was made - see --pin-proportional) is this PIN's
    OWN, fixed-at-creation setting for its corner-handle resize behavior;
    it isn't live-adjustable after the fact from the main app, since a pin
    is a fully detached process with no ongoing control channel besides
    focus/close.

    Controls:
    - Left-click drag anywhere on the image (unlocked only): moves the
      window. Corner handles (unlocked only): resize, anchored on the
      OPPOSITE corner - same feel as the main app's own capture-select
      handles. When `proportional` is on (the default - matches the main
      app's own default), width and height scale together so the picture
      never distorts; when off, each corner handle drags its own axis
      independently, letting the picture stretch. Either way, dragging
      still tracks a single (scale_x, scale_y) pair internally - a
      proportional drag just keeps them equal.
    - Hovering the image reveals a small control bar in two clusters: on
      the left, Restore (only once the pin has actually been resized or
      maximized - see "baseline" below) / Copy / Save / Pin-Unpin; on the
      right, anchored to the window's own top-right corner, Min / Max /
      the x to close. "Translucent" here means a plain dark overlay bar,
      not true per-widget alpha blending, which Tk doesn't support on a
      single widget within a window; that's a known cosmetic
      simplification.
    - Pin (lock): fixes position/size, hides every control but Unpin and
      the x (the only two that stay interactive), and unbinds this
      window's own drag/resize handlers. NOTE: this does NOT implement
      true OS-level click-through (letting a click fall through to
      whatever's visually behind it) - that needs the X11 Shape
      extension's input-shape, which is real Xlib/ctypes work beyond
      this pass; what's actually shipped is "this window stops reacting
      to drags/resizes", not "clicks pass through it". Flagging this
      clearly rather than claiming more than what's built.
    - Right-click, or Ctrl+p / Super+p (while focused): toggles pin/unpin
      (right-click no longer closes the window - that's the x/Escape now).
    - Escape (while genuinely focused - every click first calls
      focus_force(), the standard workaround for override-redirect
      windows not otherwise reliably receiving WM focus): closes.
    - Scroll wheel (Button-4/5 - X11 only, no Wayland equivalent, same
      asymmetry other scroll/global-key handling in this file has): zooms
      in place, anchored on center. Corner-handle drags and scroll-zoom
      both push an undo step; Ctrl+u/Super+u and Ctrl+r/Super+r undo/redo
      the last few of those. Restore (the button, or do_restore) jumps
      straight back to the pin's size/position right after it first
      appeared ("baseline"), regardless of how many resizes/zooms/
      maximizes happened since - it's a single one-step reset, distinct
      from the multi-step undo/redo history.
    - Ctrl+f/Super+f or the Max icon: toggles maximized, fit to the
      MONITOR the pin is currently on (found via get_monitors() +
      monitor_at_point() - the same multi-monitor-aware lookup the main
      app itself uses - NOT winfo_screenwidth()/height(), which report the
      full virtual screen spanning every monitor combined and would
      otherwise stretch "Max" across a monitor seam on a multi-monitor
      setup) vs. the size/position before maximizing.
    - Ctrl+c/Super+y or Copy: copies the ORIGINAL-resolution image to the
      clipboard. Ctrl+w/Super+w or Save: saves it into SNIPPYSNAPS_DIR.
    - Ctrl+m/Super+m or Min: withdraws the window (still alive, just
      hidden) - bring it back via the main app's Pins list, whose "focus"
      action deiconifies as well as raising (see focus_local_window - this
      pin declares itself always-on-top via _PinHandle._steady_state_topmost
      so focusing it doesn't accidentally turn that back off). NOTE: since
      this window is never managed by a window manager to begin with
      (overrideredirect, by design, for the borderless look), it never
      appears in any alt-tab/taskbar switcher, minimized or not - the
      Pins list is the only way back once minimized.
    Its own temp file (created by _launch_pinned_region) is deleted when
    the window actually closes (x/Escape/force-closed from the Pins list) -
    NOT on a plain minimize, which only hides it.
    """
    from PIL import Image, ImageTk

    try:
        original = Image.open(path).convert("RGB")
    except Exception:
        log.exception(f"Pin viewer: could not open {path!r} - exiting immediately")
        return

    root = tk.Tk(className="snippysnappy-pin")
    _PIN_DIAG = True
    root.report_callback_exception = lambda *a: log.error("Pin viewer: unhandled error in a Tk callback", exc_info=a)
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.configure(bg="#111111")   # shows through as letterbox bars while Maximized with a mismatched aspect ratio - see render_at_current_scale
    # Set the compositor-bypass hint as EARLY as physically possible.
    set_bypass_compositor_hint(root.winfo_id())

    # Join the SAME coordinator every capture window uses.
    init_coordinator(root)
    # A pin has no "session" worth serializing (no tabs, no strokes).
    set_restart_handler(lambda: os.execv(sys.executable, [sys.executable] + sys.argv))
    pin_id = forced_pin_id or str(uuid.uuid4())

    focus_helper = None
    if BACKEND == "x11" or os.environ.get("DISPLAY"):   # patch 46: Wayland -> activation requests
        try:
            focus_helper = _X11FocusHelper()
        except Exception:
            log.warning("Pin viewer: could not set up direct X11 focus control on this system - Escape/refocus handoff may be less reliable")

    screen_w = root.winfo_screenwidth()
    screen_h = root.winfo_screenheight()
    max_w, max_h = int(screen_w * 0.8), int(screen_h * 0.8)
    state = {
        "scale_x": 1.0, "scale_y": 1.0, "proportional": proportional,
        "locked": False, "maximized": False, "pre_max": None, "baseline": None,
        "resize_mode": False, "resize_entry": None,
        "max_box_size": None,   # (box_w, box_h) - the FIXED outer window size while maximized; the image is centered/letterboxed inside.
        "view_zoom": 1.0,   # Ctrl+scroll (see on_view_zoom_scroll) - crops toward the image's center and rescales that crop back up, independent of scale_x/scale_y and the window's own size entirely.
        "view_pan_x": 0.0, "view_pan_y": 0.0,   # ORIGINAL-image pixel-space offset from center for the zoom crop (see on_pan_motion) - meaningless/unused at view_zoom == 1.0.
        "opacity": 1.0,   # window transparency (see the Opacity slider / set_opacity) - independent of view_zoom/scale_x/scale_y, applied via root.attributes("-alpha", ...)
    }
    if original.width > max_w or original.height > max_h:
        initial_scale = min(max_w / original.width, max_h / original.height)
        state["scale_x"] = state["scale_y"] = initial_scale

    # ---- undo/redo history for resize/zoom/maximize actions ----
    # Each entry is (scale_x, scale_y, x, y) - the FULL state needed to
    # restore a prior size+position (scale_x != scale_y after a
    # non-proportional resize - see "proportional" above). Pushed once per
    # completed action (drag release, one scroll-zoom step, one maximize
    # toggle), not per mouse-move frame.
    history = {"stack": [], "index": -1}

    def push_history():
        cur = (state["scale_x"], state["scale_y"], root.winfo_x(), root.winfo_y())
        del history["stack"][history["index"] + 1:]
        history["stack"].append(cur)
        history["index"] = len(history["stack"]) - 1

    def undo(event=None):
        if history["index"] <= 0:
            return
        history["index"] -= 1
        sx, sy, x, y = history["stack"][history["index"]]
        state["scale_x"], state["scale_y"] = sx, sy
        render_at_current_scale()
        root.geometry(f"+{x}+{y}")

    def redo(event=None):
        if history["index"] >= len(history["stack"]) - 1:
            return
        history["index"] += 1
        sx, sy, x, y = history["stack"][history["index"]]
        state["scale_x"], state["scale_y"] = sx, sy
        render_at_current_scale()
        root.geometry(f"+{x}+{y}")

    photo_holder = {"photo": None}
    label = tk.Label(root, bd=0, highlightthickness=1, highlightbackground="#00e0a0", highlightcolor="#00e0a0")
    label.place(x=0, y=0)

    HANDLE_PX = 8
    handle_ids = {}
    for hname in ("nw", "ne", "sw", "se"):
        h = tk.Frame(root, bg="#00e0a0", cursor="sizing")
        handle_ids[hname] = h

    control_bar_left = tk.Frame(root, bg="#111111")
    control_bar_right = tk.Frame(root, bg="#111111")
    control_bar_bottom = tk.Frame(root, bg="#111111")   # Opacity slider only.
    BTN_BG, BTN_HOVER_BG = "#111111", "#3a3a3a"

    def make_btn(parent, text):
        
        b = tk.Label(
            parent, text=text, bg=BTN_BG, fg="white", padx=6, pady=2, font=("", 9),
            highlightthickness=2, highlightbackground=BTN_BG, highlightcolor=BTN_BG,
        )
        b.bind("<Enter>", lambda e: b.configure(highlightbackground="#00e0a0", highlightcolor="#00e0a0"))
        b.bind("<Leave>", lambda e: b.configure(highlightbackground=BTN_BG, highlightcolor=BTN_BG))
        return b

    def _set_command(widget, command):
        """The make_btn Label's equivalent of Button's command= option - see make_btn's docstring for why it can't just be a configure() kwarg."""
        widget.bind("<Button-1>", lambda e: command())

    restore_btn = make_btn(control_bar_left, "Restore")
    copy_btn = make_btn(control_bar_left, "Copy")
    save_btn = make_btn(control_bar_left, "Save")
    resize_btn = make_btn(control_bar_left, "Resize")
    pin_btn = make_btn(control_bar_right, "Pin")

    # v1.0.3: Wayland compositors (wlroots family at least) ignore the X11 opacity hint that
    # -alpha sets on XWayland windows, and a pin is an override-redirect window that their own
    # opacity commands can't reach either. There the pin fakes it: it blends itself with a
    # snapshot of what is behind it (re-taken, by briefly hiding the pin, whenever the pin
    # stops at a new spot or size). PIN_WAYLAND_OPACITY: "auto" (simulate except on GNOME/KDE,
    # whose compositors honour the hint) | "native" | "simulated".
    from . import constants as _pc
    _mode = str(getattr(_pc, "PIN_WAYLAND_OPACITY", "auto") or "auto").lower()
    _desk = os.environ.get("XDG_CURRENT_DESKTOP", "").upper()
    sim = {"on": BACKEND == "wayland" and (
        _mode == "simulated" or (_mode == "auto" and "GNOME" not in _desk and "KDE" not in _desk)),
        "bg": None, "rect": None, "last_rect": None, "held": False, "job": None, "busy": False}

    def _sim_viewport_rect():
        try:
            return (label.winfo_rootx(), label.winfo_rooty(), label.winfo_width(), label.winfo_height())
        except tk.TclError:
            return None

    def _sim_refresh_bg():
        """Hide the pin, grab what is behind its image area, show it again and re-render."""
        rect = _sim_viewport_rect()
        if rect is None or rect[2] < 2 or rect[3] < 2 or sim["busy"]:
            return
        sim["busy"] = True
        root.withdraw()

        def _grab():
            try:
                from .capture import capture_monitor
                fd, tmp_name = tempfile.mkstemp(suffix=".png")
                os.close(fd)
                try:
                    capture_monitor(*rect, Path(tmp_name))
                    with Image.open(tmp_name) as im:
                        sim["bg"] = im.convert("RGB").resize((rect[2], rect[3])) if im.size != rect[2:] else im.convert("RGB")
                    sim["rect"] = rect
                finally:
                    Path(tmp_name).unlink(missing_ok=True)
            except Exception:
                log.exception("Pin viewer: could not capture the background for simulated opacity")
                sim["bg"] = None
            finally:
                root.deiconify()
                sim["busy"] = False
                render_at_current_scale()
        root.after(150, _grab)   # give the compositor a frame or two to unmap the pin

    def _sim_watch():
        """While translucent: re-take the background once the pin has settled somewhere new."""
        sim["job"] = None
        if not sim["on"] or state["opacity"] >= 0.999:
            return
        rect = _sim_viewport_rect()
        if rect and not sim["held"] and not sim["busy"] and rect != sim["rect"] and rect == sim["last_rect"]:
            _sim_refresh_bg()
        sim["last_rect"] = rect
        sim["job"] = root.after(300, _sim_watch)

    def _sim_blend(disp):
        bg = sim["bg"]
        if not sim["on"] or state["opacity"] >= 0.999 or bg is None:
            return disp
        if bg.size != disp.size:
            # the label's border/padding: the image sits centred inside the captured viewport
            dx, dy = (bg.width - disp.width) // 2, (bg.height - disp.height) // 2
            if dx < 0 or dy < 0:
                return disp   # stale background from a smaller size - a fresh one is on its way
            bg = bg.crop((dx, dy, dx + disp.width, dy + disp.height))
        return Image.blend(bg, disp.convert("RGB"), state["opacity"])

    def set_opacity(val):
        state["opacity"] = max(0.15, min(1.0, float(val) / 100.0))   # never fully 0 - an invisible pin with no way back is a trap
        if sim["on"]:
            render_at_current_scale()
            if state["opacity"] < 0.999 and sim["job"] is None:
                sim["job"] = root.after(300, _sim_watch)
            return
        try:
            root.attributes("-alpha", state["opacity"])
        except Exception:
            pass

    opacity_var = tk.IntVar(value=100)
    opacity_scale = tk.Scale(
        control_bar_bottom, from_=15, to=100, orient="horizontal", length=70, width=6, showvalue=False,
        bg=BTN_BG, fg="white", troughcolor="#3a3a3a", highlightthickness=0,
        variable=opacity_var, command=set_opacity,
    )
    min_btn = make_btn(control_bar_right, "Min")
    max_btn = make_btn(control_bar_right, "Max")
    close_btn = make_btn(control_bar_right, "✕")
    # x (close) is always the last/rightmost element of the right cluster -
    # which is itself anchored to the window's top-right corner - and,
    # unlike the others, stays visible and clickable even while locked
    # (see relayout_controls below).

    # "Resize mode" - a dedicated way to get at the corner handles without
    # the control bars sitting on top of them. The handles are only ever
    # 8x8px right in the corners, exactly where control_bar_left/right also
    # live while hovering - so grabbing a handle precisely means fighting
    # past whichever button happens to be underneath it. Clicking Resize
    # hides BOTH control bars entirely (freeing every corner) and shows
    # this small submenu instead, parked at top-center (clear of all four
    # corners) with Done/Restore/Cancel to leave resize mode again.
    resize_bar = tk.Frame(root, bg="#111111")
    resize_done_btn = make_btn(resize_bar, "Done")
    resize_restore_btn = make_btn(resize_bar, "Restore")
    resize_cancel_btn = make_btn(resize_bar, "Cancel")
    resize_done_btn.pack(side="left")
    resize_restore_btn.pack(side="left")
    resize_cancel_btn.pack(side="left")

    def is_at_baseline():
        """
        Whether Restore has anything to do - deliberately SCALE-only (not
        position too).
        """
        if state["baseline"] is None:
            return True
        bsx, bsy, _bx, _by = state["baseline"]
        return abs(state["scale_x"] - bsx) < 1e-6 and abs(state["scale_y"] - bsy) < 1e-6

    def compute_view_layout():
        """
        Single source of truth for how the pin is laid out, shared by
        render_at_current_scale and on_pan_motion so they can't drift
        apart. Returns (w, h, box_w, box_h, vw, vh, crop_w, crop_h):
        w/h = the image at plain scale_x/scale_y; box = the fixed
        container the viewport lives in (the Max box, the min-size
        padded box, or just the window itself); vw/vh = the VIEWPORT,
        which grows with view_zoom until it fills the box and
        only THEN starts cropping; crop_w/crop_h = how much of the
        original image the viewport currently shows.
        """
        w = max(1, int(round(original.width * state["scale_x"])))
        h = max(1, int(round(original.height * state["scale_y"])))
        if state["maximized"] and state["max_box_size"] is not None:
            box_w, box_h = state["max_box_size"]
        elif w < PIN_MIN_CONTENT_WIDTH or h < PIN_MIN_CONTENT_HEIGHT:
            box_w = max(w, PIN_MIN_CONTENT_WIDTH)
            box_h = max(h, PIN_MIN_CONTENT_HEIGHT)
        else:
            box_w, box_h = w, h
        z = state["view_zoom"]
        vw = max(1, min(box_w, int(round(w * z))))
        vh = max(1, min(box_h, int(round(h * z))))
        crop_w = max(1, min(original.width, int(round(vw / (state["scale_x"] * z)))))
        crop_h = max(1, min(original.height, int(round(vh / (state["scale_y"] * z)))))
        return w, h, box_w, box_h, vw, vh, crop_w, crop_h

    def render_at_current_scale():
        w, h, box_w, box_h, vw, vh, crop_w, crop_h = compute_view_layout()
        if abs(state["view_zoom"] - 1.0) > 1e-6:
            # Crop centered on the image (offset by view_pan_x/y - see
            # on_pan_motion), clamping the crop CENTER so the box never
            # leaves the original image.
            cx = original.width / 2 + state["view_pan_x"]
            cy = original.height / 2 + state["view_pan_y"]
            cx = max(crop_w / 2, min(cx, original.width - crop_w / 2))
            cy = max(crop_h / 2, min(cy, original.height - crop_h / 2))
            left = int(round(cx - crop_w / 2))
            top = int(round(cy - crop_h / 2))
            source_img = original.crop((left, top, left + crop_w, top + crop_h))
        else:
            source_img = original
        disp = source_img.resize((vw, vh), Image.LANCZOS) if source_img.size != (vw, vh) else source_img
        disp = _sim_blend(disp)
        photo_holder["photo"] = ImageTk.PhotoImage(disp)
        label.configure(image=photo_holder["photo"])
        label.configure(width=vw, height=vh)
        # The OUTER window is always exactly the box (Max box, min-size
        # floor, or plain image size); the viewport is centered inside it
        # and root's own bg color shows through as padding.
        root.geometry(f"{box_w}x{box_h}")
        label.place(x=(box_w - vw) // 2, y=(box_h - vh) // 2)
        position_handles()
        position_control_bar()

    def position_handles():
        if not state["resize_mode"]:
            for h_widget in handle_ids.values():
                h_widget.place_forget()
            return
        w, h = root.winfo_width(), root.winfo_height()
        for hname, h_widget in handle_ids.items():
            hx = 0 if "w" in hname else w - HANDLE_PX
            hy = 0 if "n" in hname else h - HANDLE_PX
            h_widget.place(x=hx, y=hy, width=HANDLE_PX, height=HANDLE_PX)

    def position_control_bar():
        # render_at_current_scale() calls this on every geometry change,
        # including every single mouse-move tick of an in-progress handle
        # drag under Resize mode - so this must NOT unconditionally re-place
        # the normal control bars. enter_resize_mode() hides them with
        # place_forget(), but only ever forgets the FRAMES, not each button
        # packed inside; place_forget() doesn't touch a widget's pack state,
        # so the instant something else calls control_bar_left.place(...)
        # again, the whole normal Restore/Copy/Save/... menu pops right back
        # up mid-drag ("the old menu comes back"). Skip both bars entirely
        # while resize mode owns the screen; show_controls()'s own
        # resize_mode branch is what manages resize_bar's placement instead.
        if state["resize_mode"]:
            return
        if state.get("hide_bars") and not _pointer_inside_window():
            control_bar_left.place_forget()
            control_bar_right.place_forget()
            control_bar_bottom.place_forget()
            return
        control_bar_left.place(x=0, y=0)
        control_bar_right.update_idletasks()
        w = root.winfo_width()
        rw = control_bar_right.winfo_reqwidth()
        control_bar_right.place(x=max(0, w - rw), y=0)
        control_bar_bottom.update_idletasks()
        bw = control_bar_bottom.winfo_reqwidth()
        bh = control_bar_bottom.winfo_reqheight()
        h = root.winfo_height()
        control_bar_bottom.place(x=max(0, (w - bw) // 2), y=max(0, h - bh - _VIDEO_BAR_H[0]))

    render_at_current_scale()
    root.update_idletasks()
    w, h = root.winfo_width(), root.winfo_height()
    root.geometry(f"{w}x{h}+{(screen_w - w) // 2}+{(screen_h - h) // 2}")
    root.update_idletasks()
    state["baseline"] = (state["scale_x"], state["scale_y"], root.winfo_x(), root.winfo_y())
    push_history()

    def cleanup_and_close(event=None):
        _cancel_hide_job()
        _cancel_focus_hold()
        if escape_poll_state["poll_job"] is not None:
            try:
                root.after_cancel(escape_poll_state["poll_job"])
            except Exception:
                pass
            escape_poll_state["poll_job"] = None
        if escape_poll_state["poller"] is not None:
            escape_poll_state["poller"].close()
            escape_poll_state["poller"] = None
        if focus_helper is not None:
            focus_helper.close()
        try:
            _unregister_pin(pin_handle)
        except Exception:
            log.exception("Pin viewer: failed to unregister from the coordinator (non-fatal)")
        try:
            Path(path).unlink(missing_ok=True)
        except Exception:
            log.exception(f"Pin viewer: failed to clean up its temp file {path!r} (non-fatal)")
        root.destroy()

    pin_handle = _PinHandle(pin_id, root, f"Pin {original.width}x{original.height}", cleanup_and_close)
    try:
        _register_pin(pin_handle)
    except Exception:
        log.exception("Pin viewer: failed to register with the coordinator (non-fatal - this pin just won't show up in any Pins list)")

    # ---- hover-to-reveal control bar ----
    hover_state = {"hide_job": None, "focus_hold_job": None}

    # Escape-to-close: root.bind("<Escape>", ...) below only fires if this
    # override-redirect window genuinely holds WM keyboard focus. Polls
    # the X server's raw keyboard state directly.
    escape_poll_state = {"poller": None, "poll_job": None}

    def _escape_poll_tick():
        # Gated on REAL X11 focus ownership (via _X11FocusHelper), not a
        # Tk-level hover/click guess.
        escape_poll_state["poll_job"] = None
        poller = escape_poll_state["poller"]
        if poller is None:
            return
        have_real_focus = True
        if focus_helper is not None:
            try:
                have_real_focus = (focus_helper.get_focus_window() == root.winfo_id())
            except Exception:
                have_real_focus = True   # fail open - a broken focus check shouldn't silently disable Escape altogether
        if have_real_focus and poller.escape_down():
            log.info("Pin viewer: Escape detected while this pin genuinely holds X focus - closing")
            poller.close()
            escape_poll_state["poller"] = None
            cleanup_and_close()
            return
        escape_poll_state["poll_job"] = root.after(50, _escape_poll_tick)

    if BACKEND == "x11":
        try:
            escape_poll_state["poller"] = _X11EscapePoller()
            escape_poll_state["poll_job"] = root.after(50, _escape_poll_tick)
        except Exception:
            log.warning("Pin viewer: could not set up Escape-to-close polling on this system (Escape may not reliably close this pin)")

    def _cancel_hide_job():
        if hover_state["hide_job"] is not None:
            try:
                root.after_cancel(hover_state["hide_job"])
            except Exception:
                pass
            hover_state["hide_job"] = None

    def _cancel_focus_hold():
        if hover_state["focus_hold_job"] is not None:
            try:
                root.after_cancel(hover_state["focus_hold_job"])
            except Exception:
                pass
            hover_state["focus_hold_job"] = None

    def _focus_hold_tick():
        # A single focus_force() call on hover isn't enough on every real
        # window manager: some track a "last active window" and, because an
        # override-redirect popup like this one is explicitly outside their
        # focus management. Reasserts focus once per tick while the pointer sits
        # over the pin - rather than once per hover.
        if not _pointer_inside_window():
            hover_state["focus_hold_job"] = None
            return
        try:
            root.focus_force()
        except Exception:
            pass
        hover_state["focus_hold_job"] = root.after(150, _focus_hold_tick)

    def _ensure_focus_hold():
        if hover_state["focus_hold_job"] is None:
            hover_state["focus_hold_job"] = root.after(150, _focus_hold_tick)

    def _pointer_inside_window():
        try:
            px, py = root.winfo_pointerx(), root.winfo_pointery()
        except Exception:
            return False
        if px < 0 or py < 0:
            return False
        rx, ry = root.winfo_rootx(), root.winfo_rooty()
        rw, rh = root.winfo_width(), root.winfo_height()
        return rx <= px <= rx + rw and ry <= py <= ry + rh

    def show_controls(event=None):
        _cancel_hide_job()
        # A hover (or an explicit click of a control) is also a good moment
        # to reassert input focus - override-redirect windows like this one
        # don't reliably hold WM keyboard focus, and Escape (bound below)
        # only fires for whichever window genuinely has it. This used to be
        # throttled to fire only on a hidden->shown transition.
        reassert_focus()
        _ensure_focus_hold()
        if state["resize_mode"]:
            # Resize mode has its own separate submenu (Done/Restore/
            # Cancel) - the normal Copy/Save/Pin/Min/Max bars (and the
            # bottom Opacity bar) stay hidden the whole time so they
            # can't cover the corner handles (see enter_resize_mode).
            opacity_scale.pack_forget()
            control_bar_bottom.place_forget()
            resize_bar.place(relx=0.5, y=2, anchor="n")
            return
        visibility_key = (state["locked"], state["maximized"], is_at_baseline())
        if hover_state.get("last_visibility_key") == visibility_key:
            position_control_bar()
            return
        hover_state["last_visibility_key"] = visibility_key
        for b in (restore_btn, copy_btn, save_btn, resize_btn, pin_btn, min_btn, max_btn, close_btn):
            b.pack_forget()
        opacity_scale.pack_forget()
        if state["locked"]:
            # Only Unpin (this same pin_btn, relabeled) and Close stay
            # visible/interactive while locked.
            pin_btn.pack(side="left")
        else:
            copy_btn.pack(side="left")
            save_btn.pack(side="left")
            if not state["maximized"]:
                resize_btn.pack(side="left")
            if not is_at_baseline():
                restore_btn.pack(side="left")
            pin_btn.pack(side="left")
            min_btn.pack(side="left")
            max_btn.pack(side="left")
            opacity_scale.pack(pady=2)
        close_btn.pack(side="left")
        position_control_bar()

    def _hide_bottom_bar_too():
        control_bar_bottom.place_forget()

    def schedule_hide_controls(event=None):
        if state["resize_mode"]:
            # Keep the resize submenu open regardless of hover - it only
            # goes away via Done/Restore/Cancel, not by the mouse drifting
            # off the (now handle-only) window for a moment.
            return

        def _hide():
            hover_state["hide_job"] = None
            if state["resize_mode"]:
                return
            if _pointer_inside_window():
                show_controls()
                return
            control_bar_left.place_forget()
            control_bar_right.place_forget()
            control_bar_bottom.place_forget()
        hover_state["hide_job"] = root.after(400, _hide)

    # ---- drag to move / resize handles (unlocked only) ----
    drag_state = {}

    def reassert_focus(event=None):
        # _focus_viewable_guard: SetInputFocus on an unmapped (minimized) window is an X BadMatch, which kills this whole process
        try:
            if not root.winfo_viewable():
                return
        except Exception:
            return
        root.focus_force()
        if focus_helper is not None:
            try:
                focus_helper.set_focus(root.winfo_id())
            except Exception:
                pass

    def on_drag_press(event):
        reassert_focus()
        if state["locked"]:
            return
        drag_state["x"] = event.x
        drag_state["y"] = event.y

    def on_drag_motion(event):
        if state["locked"] or "x" not in drag_state:
            return
        new_x = root.winfo_x() + (event.x - drag_state["x"])
        new_y = root.winfo_y() + (event.y - drag_state["y"])
        root.geometry(f"+{new_x}+{new_y}")

    resize_state = {}

    def on_handle_press(hname, event):
        reassert_focus()
        if state["locked"]:
            return
        resize_state["handle"] = hname
        resize_state["start_root_x"] = event.x_root
        resize_state["start_root_y"] = event.y_root
        resize_state["start_scale_x"] = state["scale_x"]
        resize_state["start_scale_y"] = state["scale_y"]
        resize_state["orig_w"] = root.winfo_width()
        resize_state["orig_h"] = root.winfo_height()
        resize_state["orig_x"] = root.winfo_x()
        resize_state["orig_y"] = root.winfo_y()

    def on_handle_drag(hname, event):
        if state["locked"] or resize_state.get("handle") != hname:
            return
        dx = event.x_root - resize_state["start_root_x"]
        dy = event.y_root - resize_state["start_root_y"]
        sign_x = -1 if "w" in hname else 1
        sign_y = -1 if "n" in hname else 1
        w0, h0 = resize_state["orig_w"], resize_state["orig_h"]
        if state["proportional"]:
            # Anchor the OPPOSITE corner (same feel as the main app's
            # capture-select handles) - only the diagonal distance from
            # that fixed corner drives a UNIFORM scale change on BOTH
            # axes, so the picture is never stretched out of proportion.
            diag0 = math.hypot(w0, h0) or 1.0
            diag_now = math.hypot(w0 + sign_x * dx, h0 + sign_y * dy)
            factor = max(0.05, diag_now / diag0)
            new_scale_x = max(0.1, min(resize_state["start_scale_x"] * factor, 8.0))
            new_scale_y = max(0.1, min(resize_state["start_scale_y"] * factor, 8.0))
        else:
            # Proportional unchecked (see the Pin toolbar's own
            # checkbox): each axis stretches independently, driven only
            # by that axis's own drag distance - the picture CAN distort,
            # which is the whole point of turning this off.
            factor_x = max(0.05, (w0 + sign_x * dx) / w0) if w0 else 1.0
            factor_y = max(0.05, (h0 + sign_y * dy) / h0) if h0 else 1.0
            new_scale_x = max(0.1, min(resize_state["start_scale_x"] * factor_x, 8.0))
            new_scale_y = max(0.1, min(resize_state["start_scale_y"] * factor_y, 8.0))
        state["scale_x"] = new_scale_x
        state["scale_y"] = new_scale_y
        anchor_x = resize_state["orig_x"] + (w0 if "w" in hname else 0)
        anchor_y = resize_state["orig_y"] + (h0 if "n" in hname else 0)
        w = max(1, int(round(original.width * new_scale_x)))
        h = max(1, int(round(original.height * new_scale_y)))
        new_x = anchor_x - (w if "w" in hname else 0)
        new_y = anchor_y - (h if "n" in hname else 0)
        render_at_current_scale()
        root.geometry(f"+{new_x}+{new_y}")

    def on_handle_release(hname, event):
        if resize_state.get("handle") == hname:
            resize_state["handle"] = None
            push_history()
            show_controls()  # so Restore appears immediately once diverged from baseline

    for hname, h_widget in handle_ids.items():
        h_widget.bind("<ButtonPress-1>", lambda e, n=hname: on_handle_press(n, e))
        h_widget.bind("<B1-Motion>", lambda e, n=hname: on_handle_drag(n, e))
        h_widget.bind("<ButtonRelease-1>", lambda e, n=hname: on_handle_release(n, e))

    def on_scroll_zoom(direction, event=None):
        # Scroll-zoom always scales BOTH axes by the same factor - even if
        # the pin is currently non-uniformly stretched (independent-axis
        # resize), zooming preserves whatever aspect it currently has
        # rather than snapping it back to square.
        reassert_focus()
        if state["maximized"]:
            # Same rule enter_resize_mode already enforces for the corner
            # handles - growing scale_x/scale_y while maximized used to
            # render the image LARGER than the fixed Max box (see
            # render_at_current_scale's maximized branch, which never
            # resizes the box itself), so part of the image ended up
            # rendered outside the box's own visible bounds entirely -
            # reachable by panning, but never actually visible. Simply
            # disallowing this here removes that dead zone rather than
            # trying to compensate for it after the fact.
            return
        factor = 1.1 if direction > 0 else 1 / 1.1
        old_x, old_y = state["scale_x"], state["scale_y"]
        new_x = max(0.1, min(old_x * factor, 8.0))
        new_y = max(0.1, min(old_y * factor, 8.0))
        if abs(new_x - old_x) < 1e-6 and abs(new_y - old_y) < 1e-6:
            return
        state["scale_x"], state["scale_y"] = new_x, new_y
        cx = root.winfo_x() + root.winfo_width() / 2
        cy = root.winfo_y() + root.winfo_height() / 2
        render_at_current_scale()
        w, h = root.winfo_width(), root.winfo_height()
        root.geometry(f"+{int(cx - w / 2)}+{int(cy - h / 2)}")
        push_history()
        show_controls()  # so Restore appears immediately once diverged from baseline

    def set_locked(locked, event=None):
        state["locked"] = locked
        pin_btn.configure(text=("Unpin" if locked else "Pin"))
        if locked:
            if state["resize_mode"]:
                # Resize mode doesn't make sense once locked (no drag/
                # resize input is accepted while locked at all) - leave it
                # quietly, keeping whatever size was already dialed in.
                _leave_resize_mode()
            label.unbind("<ButtonPress-1>")
            label.unbind("<B1-Motion>")
            for h_widget in handle_ids.values():
                h_widget.place_forget()
        else:
            label.bind("<ButtonPress-1>", on_drag_press)
            label.bind("<B1-Motion>", on_drag_motion)
            if not state["maximized"]:
                position_handles()
        show_controls()

    def toggle_locked(event=None):
        set_locked(not state["locked"])

    def _current_monitor_bounds():
        """
        The bounds of the MONITOR this pin is actually sitting on right
        now (found the same multi-monitor-aware way the main app finds
        its own current monitor: get_monitors() + monitor_at_point() on
        this window's center point) - NOT winfo_screenwidth()/height(),
        which report the full X11 virtual screen spanning every monitor
        combined. Using the virtual screen for "Max" is what made it fit
        to (and straddle the seam of) all monitors at once instead of
        filling just the one the pin was on. Falls back to the full
        virtual screen (screen_w/screen_h) if monitor detection fails for
        any reason (e.g. xrandr/wlr-randr unavailable).
        """
        # root.geometry(...) is ASYNCHRONOUS - it sends the move request to
        # the X server and returns immediately; winfo_x()/winfo_y() don't
        # reflect the new position until Tk actually receives and
        # processes the resulting ConfigureNotify event back from the
        # server/WM. root.update() forces Tk to process any pending
        # events.
        root.update()
        cx = root.winfo_x() + root.winfo_width() / 2
        cy = root.winfo_y() + root.winfo_height() / 2
        try:
            monitors = get_monitors()
            _, mx, my, mw, mh = monitor_at_point(monitors, cx, cy)
            return mx, my, mw, mh
        except Exception:
            log.exception("Pin viewer: could not detect monitors for Max - falling back to the full virtual screen")
            return 0, 0, screen_w, screen_h

    def toggle_maximize(event=None):
        reassert_focus()
        if not state["maximized"]:
            state["pre_max"] = (state["scale_x"], state["scale_y"], root.winfo_x(), root.winfo_y())
            mon_x, mon_y, mon_w, mon_h = _current_monitor_bounds()
            # A fixed-size box centered on the monitor, margin-reduced from
            # the full monitor bounds - NOT true edge-to-edge fullscreen,
            # and NOT independent-axis stretch (both tried and reverted per
            # testing). The image keeps its own aspect-correct fit_scale;
            # the OUTER window is always exactly this box size regardless -
            # see render_at_current_scale, which centers/letterboxes the
            # image inside it. state["maximized"]/state["max_box_size"]
            # MUST be set before render_at_current_scale() runs, or it
            # falls through to the plain (non-maximized) sizing path.
            usable_w = max(1, mon_w - PIN_MAXIMIZE_SIDE_MARGIN * 2)
            # Margin reserved TWICE (top and bottom) for the box's HEIGHT,
            # same as the side margins - but the box is positioned flush
            # against the monitor's own TOP edge (see final_y below), not
            # vertically centered, so all of that reserved space ends up
            # as a single gap at the bottom rather than split top+bottom.
            usable_h = max(1, mon_h - PIN_MAXIMIZE_VERTICAL_MARGIN * 2)
            fit_scale = min(usable_w / original.width, usable_h / original.height)
            state["scale_x"] = state["scale_y"] = fit_scale
            state["maximized"] = True
            state["max_box_size"] = (usable_w, usable_h)
            render_at_current_scale()
            final_x = mon_x + (mon_w - usable_w) // 2
            # Flush against the very top of the monitor, not vertically
            # centered - a small top offset was putting part of the pin
            # BEHIND a top-docked panel; sitting exactly at the monitor's
            # own top edge keeps the whole thing visible regardless of
            # where a panel/taskbar lives.
            final_y = mon_y
            log.info(
                f"Pin viewer: Maximize - monitor bounds=({mon_x},{mon_y},{mon_w},{mon_h}), "
                f"box=({usable_w}x{usable_h}), placing at +{final_x}+{final_y}"
            )
            # ONE combined size+position request instead of two separate
            # geometry() calls (render_at_current_scale already sent the
            # size) - user testing showed the position-only call being
            # lost after the layout rewrite, leaving the pin at its
            # pre-maximize position. Read back afterward and re-apply
            # once if it still didn't land, logging both so any further
            # drift is diagnosable from the log rather than guessed at.
            root.geometry(f"{usable_w}x{usable_h}+{final_x}+{final_y}")
            root.update_idletasks()
            if (root.winfo_x(), root.winfo_y()) != (final_x, final_y):
                log.warning(
                    f"Pin viewer: Maximize position did not land (wanted +{final_x}+{final_y}, "
                    f"got +{root.winfo_x()}+{root.winfo_y()}) - re-applying once"
                )
                root.geometry(f"{usable_w}x{usable_h}+{final_x}+{final_y}")
                root.update_idletasks()
                log.info(f"Pin viewer: after re-apply, position is +{root.winfo_x()}+{root.winfo_y()}")
            # The immediate readback above passed in the user's log every
            # time, yet the pin sometimes ended up elsewhere - so something
            # moves it AFTER this function returns. Same class of problem
            # (and same remedy) as SelectorApp._set_geometry's short-lived
            # enforcement window: keep re-checking briefly and correct any
            # drift, logging it so the culprit is visible if it recurs.
            max_target = (final_x, final_y)
            state["max_target"] = max_target

            def _enforce_max_position(remaining=8):
                if not state["maximized"] or state.get("max_target") != max_target:
                    return
                cur = (root.winfo_x(), root.winfo_y())
                if cur != max_target:
                    log.warning(
                        f"Pin viewer: Maximize position drifted to +{cur[0]}+{cur[1]} "
                        f"(wanted +{max_target[0]}+{max_target[1]}) - correcting"
                    )
                    root.geometry(f"+{max_target[0]}+{max_target[1]}")
                if remaining > 0:
                    root.after(100, lambda: _enforce_max_position(remaining - 1))

            root.after(100, _enforce_max_position)
            max_btn.configure(text="Unmax")
            # The corner handles are meaningless while maximized (resizing
            # away from a fit-to-monitor size doesn't make sense until
            # you've un-maximized first) and otherwise just sit there,
            # visible and grabbable, around wherever the old pre-maximize
            # size used to be - hide them until Unmax brings them back.
            for h_widget in handle_ids.values():
                h_widget.place_forget()
        else:
            # Same ordering requirement in reverse: state["maximized"] must
            # already be False before render_at_current_scale() runs, or it
            # would (wrongly) keep boxing/centering the un-maximized image.
            state["maximized"] = False
            sx, sy, x, y = state["pre_max"]
            state["scale_x"], state["scale_y"] = sx, sy
            render_at_current_scale()
            root.geometry(f"+{x}+{y}")
            max_btn.configure(text="Max")
            if not state["locked"]:
                position_handles()
        push_history()
        show_controls()

    def do_restore(event=None):
        """
        Jumps straight back to this pin's baseline (its size/position right
        after it first appeared), whatever combination of resizes, zooms,
        or maximize toggles happened since - a single one-step reset,
        distinct from the multi-step Undo/Redo history (though it also
        pushes its own history entry, so undo/redo can still step through
        it like any other change). Only shown once the pin has actually
        diverged from that baseline - see is_at_baseline()/show_controls.
        """
        reassert_focus()
        was_maximized = state["maximized"]
        bsx, bsy, bx, by = state["baseline"]
        state["scale_x"], state["scale_y"] = bsx, bsy
        render_at_current_scale()
        root.geometry(f"+{bx}+{by}")
        state["maximized"] = False
        max_btn.configure(text="Max")
        if was_maximized and not state["locked"]:
            position_handles()
        push_history()
        show_controls()

    def enter_resize_mode(event=None):
        """
        A dedicated way to get at the corner handles without the control
        bars sitting on top of them - see resize_bar's own comment above.
        Hides both control bars entirely (handles are already visible/
        placed whenever unlocked and not maximized) and shows a small
        Done/Restore/Cancel submenu parked well clear of every corner.
        """
        log.info(f"Pin viewer: Resize clicked (locked={state['locked']}, maximized={state['maximized']})")
        if state["locked"] or state["maximized"]:
            log.info("Pin viewer: Resize ignored - locked or maximized, neither of which resize mode supports")
            return
        reassert_focus()
        state["resize_mode"] = True
        state["resize_entry"] = (state["scale_x"], state["scale_y"], root.winfo_x(), root.winfo_y())
        _cancel_hide_job()
        position_handles()   # now gated on resize_mode (just set True above) - show them immediately rather than waiting for the next incidental render
        control_bar_left.place_forget()
        control_bar_right.place_forget()
        show_controls()
        log.info("Pin viewer: entered resize mode")

    def _leave_resize_mode():
        state["resize_mode"] = False
        resize_bar.place_forget()
        position_handles()   # now gated on resize_mode (just set False above) - hide them immediately

    def resize_mode_done(event=None):
        """Keep whatever size/position resizing landed on - just leaves resize mode."""
        _leave_resize_mode()
        push_history()
        show_controls()

    def resize_mode_restore(event=None):
        """Snap back to baseline (like the main Restore button) AND leave resize mode."""
        _leave_resize_mode()
        do_restore()

    def resize_mode_cancel(event=None):
        """Undo whatever resizing happened since Resize was clicked, then leave resize mode."""
        _leave_resize_mode()
        sx, sy, x, y = state["resize_entry"]
        state["scale_x"], state["scale_y"] = sx, sy
        render_at_current_scale()
        root.geometry(f"+{x}+{y}")
        push_history()
        show_controls()

    def do_copy(event=None):
        reassert_focus()
        fd, tmp_name = tempfile.mkstemp(suffix=".png", prefix="snippysnappy-pin-copy-")
        os.close(fd)
        try:
            original.save(tmp_name)
            copy_image_to_clipboard(Path(tmp_name))
            notify("Pin copied to clipboard", blocking=False)
        finally:
            try:
                Path(tmp_name).unlink(missing_ok=True)
            except Exception:
                pass

    def do_save(event=None):
        reassert_focus()
        out_path = _paths_setup.get_dir("snaps") / f"pin-{timestamp()}.png"
        try:
            original.save(out_path)
            notify("Pin saved", f"Saved as {out_path.name}", blocking=False)
            log.info(f"Pin viewer: saved to {out_path}")
        except Exception:
            log.exception(f"Pin viewer: failed to save to {out_path}")

    def do_minimize(event=None):
        log.info("Pin viewer: Minimize clicked")
        root.withdraw()

    _set_command(restore_btn, do_restore)
    _set_command(copy_btn, do_copy)
    _set_command(save_btn, do_save)
    _set_command(resize_btn, enter_resize_mode)
    _set_command(min_btn, do_minimize)
    _set_command(max_btn, toggle_maximize)
    _set_command(pin_btn, toggle_locked)
    _set_command(close_btn, cleanup_and_close)
    _set_command(resize_done_btn, resize_mode_done)
    _set_command(resize_restore_btn, resize_mode_restore)
    _set_command(resize_cancel_btn, resize_mode_cancel)

    zoom_indicator = tk.Label(
        root, bg="#111111", fg="#00e0a0", font=("", 10, "bold"), padx=6, pady=2,
        highlightthickness=1, highlightbackground="#333333",
    )
    zoom_indicator_state = {"hide_job": None}

    def _show_zoom_indicator():
        zoom_indicator.configure(text=f"{state['view_zoom']:.1f}x")
        zoom_indicator.place(relx=1.0, rely=1.0, x=-6, y=-6, anchor="se")
        if zoom_indicator_state["hide_job"] is not None:
            try:
                root.after_cancel(zoom_indicator_state["hide_job"])
            except Exception:
                pass
        # Cancel-and-reschedule (same idiom as hover_state["hide_job"]
        # elsewhere in this file) so a fast scroll burst keeps pushing
        # the hide back out rather than an earlier tick's timer firing
        # mid-burst and yanking the indicator away while still zooming.
        zoom_indicator_state["hide_job"] = root.after(1000, lambda: zoom_indicator.place_forget())

    def on_view_zoom_scroll(direction, event=None):
        """
        Ctrl+scroll: zooms the DISPLAYED CONTENT in place, independent of
        the window's own size and of scale_x/scale_y entirely - unlike
        on_scroll_zoom (plain scroll), which grows/shrinks the WINDOW,
        this crops toward the image's center (or wherever a middle-drag
        has since panned to - see on_pan_motion) and rescales that crop
        back up to fill the current window dimensions (see
        render_at_current_scale), so the window never moves or resizes.
        Purely a viewing aid over the pin's own pixels - it has no
        baseline/undo history of its own (see push_history) and Restore
        does not reset it; Unmax/Restore only ever touch scale_x/scale_y
        and position. Shows a small "Nx" indicator bottom-right, same
        idea as the main app's own scale readout, that fades out after a
        pause rather than staying up permanently.
        """
        reassert_focus()
        factor = 1.15 if direction > 0 else 1 / 1.15
        new_zoom = max(1.0, min(state["view_zoom"] * factor, 12.0))
        if abs(new_zoom - state["view_zoom"]) < 1e-6:
            return
        state["view_zoom"] = new_zoom
        if abs(new_zoom - 1.0) < 1e-6:
            state["view_pan_x"] = state["view_pan_y"] = 0.0   # back to 1x - no crop at all, so any leftover pan would be meaningless until zoomed back in
        render_at_current_scale()
        _show_zoom_indicator()

    pan_state = {}

    def on_pan_press(event):
        # Always arms the drag (records the press point) even at 1.0x
        # zoom, rather than returning early and dropping the press
        # entirely.
        reassert_focus()
        pan_state["x"] = event.x
        pan_state["y"] = event.y

    def on_pan_motion(event):
        if "x" not in pan_state or state["view_zoom"] <= 1.0 + 1e-6:
            return
        # One screen pixel of drag = (crop / viewport) original pixels,
        # taken from the same layout render_at_current_scale uses.
        _w, _h, _bw, _bh, vw, vh, crop_w, crop_h = compute_view_layout()
        dx = (event.x - pan_state["x"]) * (crop_w / vw)
        dy = (event.y - pan_state["y"]) * (crop_h / vh)
        pan_state["x"], pan_state["y"] = event.x, event.y
        state["view_pan_x"] -= dx
        state["view_pan_y"] -= dy
        render_at_current_scale()

    def on_pan_release(event):
        pan_state.pop("x", None)
        pan_state.pop("y", None)

    label.bind("<Enter>", show_controls)
    root.bind("<Enter>", show_controls, add="+")
    root.bind("<Leave>", schedule_hide_controls, add="+")
    label.bind("<Leave>", schedule_hide_controls)
    control_bar_left.bind("<Enter>", show_controls)
    control_bar_left.bind("<Leave>", schedule_hide_controls)
    control_bar_right.bind("<Enter>", show_controls)
    control_bar_right.bind("<Leave>", schedule_hide_controls)
    label.bind("<ButtonPress-1>", on_drag_press)
    label.bind("<B1-Motion>", on_drag_motion)
    if sim["on"]:   # simulated opacity must not hide the pin in the middle of a drag
        root.bind_all("<ButtonPress-1>", lambda e: sim.__setitem__("held", True), add="+")
        root.bind_all("<ButtonRelease-1>", lambda e: sim.__setitem__("held", False), add="+")
    label.bind("<ButtonPress-3>", toggle_locked)
    label.bind("<Button-4>", lambda e: on_scroll_zoom(1, e))
    label.bind("<Button-5>", lambda e: on_scroll_zoom(-1, e))
    label.bind("<Control-Button-4>", lambda e: on_view_zoom_scroll(1, e))
    label.bind("<Control-Button-5>", lambda e: on_view_zoom_scroll(-1, e))
    label.bind("<ButtonPress-2>", on_pan_press)
    label.bind("<B2-Motion>", on_pan_motion)
    label.bind("<ButtonRelease-2>", on_pan_release)
    root.bind("<ButtonPress-2>", on_pan_press)
    root.bind("<B2-Motion>", on_pan_motion)
    root.bind("<ButtonRelease-2>", on_pan_release)
    root.bind("<Escape>", cleanup_and_close)
    root.protocol("WM_DELETE_WINDOW", cleanup_and_close)

    _MOD4_SUPER_BIT = 0x40   # Mod4, near-universal Linux/X11 default mapping for the Super key

    def _make_super_guarded(cb):
        def handler(event):
            if event.state & _MOD4_SUPER_BIT:
                return cb(event)
        return handler

    ctrl_binds = {
        "<Control-p>": toggle_locked, "<Control-f>": toggle_maximize,
        "<Control-c>": do_copy, "<Control-w>": do_save, "<Control-m>": do_minimize,
        "<Control-u>": undo, "<Control-r>": redo,
    }
    super_bare_binds = {
        "p": toggle_locked, "f": toggle_maximize, "y": do_copy,
        "w": do_save, "m": do_minimize, "u": undo, "r": redo,
    }
    for seq, cb in ctrl_binds.items():
        root.bind(seq, cb)
    for letter, cb in super_bare_binds.items():
        root.bind(f"<{letter}>", _make_super_guarded(cb))

    root.update_idletasks()
    root.focus_force()
    if focus_helper is not None:
        try:
            focus_helper.set_focus(root.winfo_id())
        except Exception:
            log.exception("Pin viewer: failed to take real X11 focus at creation (non-fatal - Escape may act on the wrong window until you click this pin)")

    if os.environ.get("SNIPPYSNAPPY_PIN_VIDEO"):
        try:
            from .video_pin import install as _install_video_pin
            _install_video_pin(root, original, render_at_current_scale, make_btn, state, cleanup_and_close, _VIDEO_BAR_H)
        except Exception:
            log.exception("Pin viewer: video controls failed to start (non-fatal) - showing the still frame")
    log.info(f"Pin viewer: showing {original.width}x{original.height} region from {path!r} (pin_id={pin_id})")
    try:
        root.mainloop()
    except BaseException:
        log.exception("Pin viewer: main loop crashed")
        raise
    finally:
        log.info(f"Pin viewer: main loop exited (pin_id={pin_id})")


