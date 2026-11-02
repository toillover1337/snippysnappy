# snippysnappy v1.0.5
"""
One Escape behaviour for every edit tool, in the image editor and the film editor.

Escape steps back ONE level and is always recoverable with Redo:

  * something half-finished (open text box, pending shape / snip) is cancelled first;
  * otherwise whatever the CURRENT tool added since you picked it is undone (Resize: every resize applied
    since you opened it). A single Redo brings all of it back, so an accidental Escape costs nothing;
  * with nothing to undo in this tool, Escape leaves the edit toolbar: it keeps your earlier edits (like Done)
    when there are any, and simply closes the toolbar when there are none.
"""
from .logging_setup import log


def _typing(root):
    try:
        from .leader import typing_focus
        return typing_focus(root)
    except Exception:
        return False


# ------------------------------------------------------------------ image editor

def install_image(cls):
    orig_handle = cls._handle_escape
    orig_pick = cls.pick_tool
    orig_enter = cls.enter_edit_mode
    orig_undo = cls.undo_last_stroke
    orig_redo = cls.redo_last_stroke
    orig_append = getattr(cls, "_append_stroke", None)
    orig_exit_resize = cls._exit_resize_mode
    orig_show_resize = cls.show_resize_options

    def _stack(self):
        st = getattr(self, "_esc_stack", None)
        if st is None:
            st = self._esc_stack = []
        return st

    def _mark(self):
        self._esc_tool_mark = len(self.edit_strokes)

    def pick_tool(self, tool):
        orig_pick(self, tool)
        _mark(self)

    def enter_edit_mode(self):
        self._esc_stack = []
        self._edit_resized = False
        orig_enter(self)
        _mark(self)

    def undo_last_stroke(self):
        _stack(self).clear()          # a hand-made Undo/Redo pair restores exactly one step
        return orig_undo(self)

    def redo_last_stroke(self):
        st = _stack(self)
        item = st.pop() if st else None
        if item is not None and item[0] == "resize":
            try:
                self._restore_full_state(item[1])
                self._resync_display_after_state_change()
                self._edit_resized = True
                log.info("Redo: the Resize cancelled by Escape is back")
            except Exception:
                log.exception("Redo of the Escaped Resize failed (non-fatal)")
            return
        if item is not None and item[0] == "strokes":
            for _ in range(min(item[1], len(self._edit_redo_stack))):
                orig_redo(self)
            return
        return orig_redo(self)

    def _append_stroke(self, stroke):
        _stack(self).clear()          # new work makes the recovery points stale
        return orig_append(self, stroke)

    def show_resize_options(self):
        _stack(self).clear()
        return orig_show_resize(self)

    def _exit_resize_mode(self):
        if getattr(self, "_resize_undo_stack", None):
            self._edit_resized = True  # resizes were kept (Done) - counts as an edit
        return orig_exit_resize(self)

    def _has_edits(self):
        return bool(self.edit_strokes) or bool(getattr(self, "_edit_resized", False))

    def _esc_resize(self):
        applied = bool(getattr(self, "_resize_undo_stack", None))
        snap = getattr(self, "_resize_session_snapshot", None)
        if applied and snap is not None:
            final = self._capture_full_state()
            self._restore_full_state(snap)          # no discard: the Redo needs the resized images
            self._resize_undo_stack = []
            self._resize_redo_stack = []
            self._resync_display_after_state_change()
            _stack(self).append(("resize", final))
            log.info("Escape in Resize: resize undone (Redo brings it back)")
            cls._exit_resize_mode(self)             # back to the edit row
            return
        cls._exit_resize_mode(self)
        if not _has_edits(self):
            log.info("Escape in Resize with nothing changed: leaving the edit toolbar")
            self.cancel_edit()

    def _handle_escape(self, event=None):
        if self.resize_mode_active:
            return _esc_resize(self)
        if getattr(self, "measure_mode_active", False) or self.shape_pending_start is not None or not self.edit_mode_active:
            return orig_handle(self, event)
        # --- edit toolbar (Snip / Draw / Text / Shape) ---
        if (self._pending_text_widget is not None or self.snip_pending_start is not None
                or getattr(self, "_draw_select_rect_start", None) is not None):
            log.info("Escape: cancelling the unfinished item")
            return orig_undo(self)                   # cancels the open text box / pending snip / rubber band
        n = len(self.edit_strokes) - getattr(self, "_esc_tool_mark", 0)
        if n > 0:
            for _ in range(n):
                orig_undo(self)
            _stack(self).append(("strokes", n))
            log.info(f"Escape: undid {n} edit(s) made with this tool (Redo brings them back)")
            self.show_edit_toolbar()
            return
        if _has_edits(self):
            log.info("Escape with nothing to undo in this tool: leaving the edit toolbar, keeping earlier edits")
            return self.bake_edits()
        log.info("Escape with no edits: leaving the edit toolbar")
        return self.cancel_edit()

    cls.pick_tool = pick_tool
    cls.enter_edit_mode = enter_edit_mode
    cls.undo_last_stroke = undo_last_stroke
    cls.redo_last_stroke = redo_last_stroke
    if orig_append is not None:
        cls._append_stroke = _append_stroke
    cls.show_resize_options = show_resize_options
    cls._exit_resize_mode = _exit_resize_mode
    cls._handle_escape = _handle_escape


# ------------------------------------------------------------------ film editor

def install_film(cls):
    orig_set_tool = cls._ed9_set_tool
    orig_enter = cls._ed2_enter
    orig_undo = cls._ed2_undo
    orig_redo = cls._ed2_redo_do
    orig_init = cls.__init__

    def _ed9_set_tool(self, tool):
        orig_set_tool(self, tool)
        self._esc_film_mark = len(getattr(self, "_ed3_acts", []))

    def _ed2_enter(self):
        orig_enter(self)
        self._esc_film_mark = len(getattr(self, "_ed3_acts", []))
        self._esc_film_group = 0

    def _ed2_undo(self):
        self._esc_film_group = 0      # a hand-made Undo/Redo pair restores exactly one step
        return orig_undo(self)

    def _ed2_redo_do(self):
        n = getattr(self, "_esc_film_group", 0)
        self._esc_film_group = 0
        if n > 1:
            for _ in range(n):
                if not getattr(self, "_ed3_redo", None):
                    break
                orig_redo(self)
            return
        return orig_redo(self)

    def on_escape(self, event=None):
        if not getattr(self, "_ed2_active", False) or _typing(self.root):
            return None
        if getattr(self, "_ed9_tw", None) is not None or getattr(self, "_p29_overlay", None) is not None:
            return None                # an open text box / the preview have their own Escape
        n = len(self._ed3_acts) - getattr(self, "_esc_film_mark", 0)
        if n > 0:
            for _ in range(n):
                orig_undo(self)
            self._esc_film_group = n
            log.info(f"Escape in the film editor: undid {n} edit(s) made with this tool (Redo brings them back)")
            return "break"
        if self._ed3_acts:
            log.info("Escape in the film editor with nothing to undo in this tool: leaving edit, keeping earlier edits")
            self._ed2_exit(False)
        else:
            log.info("Escape in the film editor with no edits: leaving edit")
            self._ed2_exit(True)
        return "break"

    def __init__(self, *args, **kwargs):
        orig_init(self, *args, **kwargs)
        try:
            self.root.bind("<Escape>", lambda e: on_escape(self, e), add="+")
        except Exception:
            log.exception("Could not install the film editor's Escape handling (non-fatal)")

    cls._ed9_set_tool = _ed9_set_tool
    cls._ed2_enter = _ed2_enter
    cls._ed2_undo = _ed2_undo
    cls._ed2_redo_do = _ed2_redo_do
    cls.__init__ = __init__
