# snippysnappy v1.0.3
"""
Shared side-panel helpers for the image windows (selector_app.py) and the
film window (film_viewer.py):
  - make_scrollable: a scrollable panel body with a thin scrollbar, a bottom
    border that can't be squeezed out, and wheel scrolling that is separate
    from the work area.
  - add_window_row: a Windows-section row styled like the Tabs/Layers rows
    (drag handle, name, rename, Merge, close).
  - RowReorder / ordered / move_in_order: drag-to-reorder for those rows. The
    order is a per-viewer display preference (each window keeps its own),
    not shared state.
  - inline_rename: swaps a row's name button for an entry.
  - window_name: a window's display name (user rename wins over its label).
"""
import tkinter as tk

from .constants import BUTTON_PADY

SCROLLBAR_WIDTH = 7
_panels = []             # [(container, canvas)] for every live scrollable panel in this process
_wheel_installed = set()


def window_tab_labels(app):
    """Every tab's name in a window, in order (the current tab's live label included). One entry for things without tabs, e.g. a film."""
    docs = getattr(app, "documents", None)
    if not docs:
        return [app._document_label()]
    current = getattr(app, "current_document_index", 0)
    out = []
    for i, doc in enumerate(docs):
        out.append(app._document_label() if i == current else doc.get("label", f"Tab {i + 1}"))
    return out


def window_name(app):
    """Display name for a window/film handle: the user's rename if there is one, else its FIRST (original) tab's name."""
    override = getattr(app, "_window_name_override", None)
    if override:
        return override
    labels = window_tab_labels(app)
    return labels[0] if labels else app._document_label()


def _is_descendant(widget, ancestor):
    w = widget
    while w is not None:
        if w == ancestor:
            return True
        w = getattr(w, "master", None)
    return False


def _on_wheel(event, direction):
    for entry in list(_panels):
        container, canvas = entry
        try:
            if not (container.winfo_exists() and canvas.winfo_exists()):
                _panels.remove(entry)
                continue
            inside = _is_descendant(event.widget, container)
            if inside and canvas.yview() != (0.0, 1.0):
                canvas.yview_scroll(direction * 2, "units")
        except tk.TclError:
            try:
                _panels.remove(entry)
            except ValueError:
                pass
            continue
        if inside:
            return "break"
    return None


def _install_wheel(widget):
    key = id(widget.tk)
    if key in _wheel_installed:
        return
    _wheel_installed.add(key)
    widget.bind_all("<Button-4>", lambda e: _on_wheel(e, -1), add="+")
    widget.bind_all("<Button-5>", lambda e: _on_wheel(e, 1), add="+")
    widget.bind_all("<MouseWheel>", lambda e: _on_wheel(e, -1 if e.delta > 0 else 1), add="+")


def make_scrollable(container):
    """
    `container` must be empty. Packs the bottom border FIRST (so overflowing
    content can never push it out of view), then a thin scrollbar on the
    right, then a canvas holding the content frame. Returns (inner, canvas);
    build the panel into `inner`.
    """
    tk.Frame(container, height=3, bg="#2a2a2a", relief="ridge", bd=2).pack(side="bottom", fill="x")
    scrollbar = tk.Scrollbar(container, orient="vertical", width=SCROLLBAR_WIDTH)
    scrollbar.pack(side="right", fill="y")
    canvas = tk.Canvas(container, bg="#222222", highlightthickness=0, yscrollcommand=scrollbar.set, yscrollincrement=24)
    canvas.pack(side="left", fill="both", expand=True)
    scrollbar.config(command=canvas.yview)
    inner = tk.Frame(canvas, bg="#222222")
    win = canvas.create_window((0, 0), window=inner, anchor="nw")
    def _sync_region(_event=None):
        try:
            canvas.configure(scrollregion=(0, 0, max(1, canvas.winfo_width()), max(inner.winfo_reqheight(), canvas.winfo_height())))
        except tk.TclError:
            pass

    canvas.bind("<Configure>", lambda e: (canvas.itemconfigure(win, width=e.width), _sync_region()))
    inner.bind("<Configure>", _sync_region)
    _panels[:] = [p for p in _panels if p[0] is not container]
    _panels.append((container, canvas))
    _install_wheel(container)
    install_button_tooltips(container)
    return inner, canvas


def ordered(entries, order, key_fn):
    """Sorts entries by their position in `order`; entries not in it keep their original relative order after the known ones."""
    pos = {k: i for i, k in enumerate(order)}
    return sorted(entries, key=lambda e: pos.get(key_fn(e), len(pos)))


def move_in_order(keys, key, to_index):
    """Returns `keys` with `key` moved so it ends up at `to_index` among the others."""
    rest = [k for k in keys if k != key]
    to_index = max(0, min(to_index, len(rest)))
    rest.insert(to_index, key)
    return rest


def inline_rename(cell, name_btn, current_text, on_commit, on_done):
    """
    Swaps `name_btn` for an Entry in the same spot. Enter or clicking away
    commits (on_commit(text), unless empty/unchanged), Escape cancels;
    on_done() runs either way, so callers can rebuild their panel.
    """
    name_btn.pack_forget()
    var = tk.StringVar(value=current_text)
    entry = tk.Entry(cell, textvariable=var)
    entry.pack(side="left", fill="x", expand=True, padx=2, pady=2)
    entry.focus_set()
    entry.selection_range(0, "end")
    state = {"done": False}

    def finish(commit):
        if state["done"]:
            return
        state["done"] = True
        text = var.get().strip()
        if commit and text and text != current_text:
            on_commit(text)
        on_done()

    entry.bind("<Return>", lambda e: finish(True))
    entry.bind("<FocusOut>", lambda e: finish(True))
    entry.bind("<Escape>", lambda e: finish(False))


class RowReorder:
    """
    Drag-to-reorder for a list of row cells. Each row's handle is bound via
    add(); a drag shows a floating label and a drop line over `container`,
    and on release on_move(key, to_index) is called with the index the row
    should land at among the OTHER rows.
    """

    def __init__(self, container, on_move):
        self.container = container
        self.on_move = on_move
        self._rows = []   # [(key, cell)] in display order
        self._drag = None
        self._active = False
        self._start_y = 0
        self._preview = None
        self._line = None

    def add(self, key, cell, handle, label):
        self._rows.append((key, cell))
        handle.bind("<ButtonPress-1>", lambda e, k=key: self._press(e, k))
        handle.bind("<B1-Motion>", lambda e, text=label: self._motion(e, text))
        handle.bind("<ButtonRelease-1>", self._release)

    def add_target(self, key, cell):
        """A row that is only a drop target (no drag handle), e.g. the Hidden header."""
        self._rows.append((key, cell))

    def _press(self, event, key):
        self._drag = key
        self._active = False
        self._start_y = event.y_root

    def _target_index(self, y_root):
        above = 0
        for key, cell in self._rows:
            if key == self._drag:
                continue
            try:
                mid = cell.winfo_rooty() + cell.winfo_height() / 2
            except tk.TclError:
                continue
            if mid < y_root:
                above += 1
        return above

    def _motion(self, event, label):
        if self._drag is None:
            return
        if not self._active:
            if abs(event.y_root - self._start_y) < 4:
                return
            self._active = True
            self._preview = tk.Label(self.container, text=label, bg="#00b4ff", fg="white", padx=6, pady=2, relief="raised", bd=1)
            self._line = tk.Frame(self.container, bg="#00b4ff", height=3)
            for key, cell in self._rows:
                if key == self._drag:
                    try:
                        cell.configure(highlightbackground="#00b4ff", highlightthickness=2)
                    except tk.TclError:
                        pass
        try:
            bx, by = self.container.winfo_rootx(), self.container.winfo_rooty()
            self._preview.place(x=event.x_root - bx + 12, y=event.y_root - by + 4)
            others = [(k, c) for k, c in self._rows if k != self._drag]
            if others:
                idx = self._target_index(event.y_root)
                if idx < len(others):
                    y_line = others[idx][1].winfo_rooty() - by - 2
                else:
                    last = others[-1][1]
                    y_line = last.winfo_rooty() - by + last.winfo_height() + 2
                self._line.place(x=4, y=y_line, width=max(20, self.container.winfo_width() - 8), height=3)
                self._preview.lift()   # the dragged name always renders above the drop line
        except tk.TclError:
            pass

    def _release(self, event):
        key, active = self._drag, self._active
        self._drag = None
        self._active = False
        for widget in (self._preview, self._line):
            if widget is not None:
                try:
                    widget.destroy()
                except tk.TclError:
                    pass
        self._preview = self._line = None
        for _k, cell in self._rows:
            if str(_k).startswith("__"):
                continue
            try:
                cell.configure(highlightbackground="#555555", highlightthickness=1)
            except tk.TclError:
                pass
        if key is None or not active:
            return
        self.on_move(key, self._target_index(event.y_root))


def small_button(parent, text, command, width=2):
    """Compact button for row actions (rename, close, pop out)."""
    return tk.Button(parent, text=text, command=command, font=("", 8), width=width, padx=2, pady=0, bd=1)


_tip_installed = set()
_tip_state = {"job": None, "job_widget": None, "tip": None}


def _tip_text(widget):
    text = getattr(widget, "_tooltip_text", None)
    if text is None:
        try:
            text = widget.cget("text")
        except tk.TclError:
            text = ""
    return text


def _tips_enabled():  # _p20_toolbar_tips
    try:
        from . import constants
        return bool(getattr(constants, "TOOLTIPS_ENABLED", True))
    except Exception:
        return True


def _tip_delay():
    try:
        from . import constants
        return max(0, int(getattr(constants, "TOOLTIP_DELAY_MS", 500)))
    except Exception:
        return 500


def _tip_hide(event=None):
    job = _tip_state["job"]
    if job is not None:
        try:
            _tip_state["job_widget"].after_cancel(job)
        except Exception:
            pass
        _tip_state["job"] = None
    tip = _tip_state["tip"]
    if tip is not None:
        try:
            tip.destroy()
        except tk.TclError:
            pass
        _tip_state["tip"] = None


def _tip_show(widget):
    _tip_state["job"] = None
    try:
        text = _tip_text(widget)
        mapped = None
        if _tips_enabled() and getattr(widget, "_tooltip_text", None) is None:
            from .tooltips import toolbar_tip
            mapped = toolbar_tip(widget)
        if mapped:
            text = mapped
        if not text:
            return
        if not mapped and widget.winfo_reqwidth() <= widget.winfo_width() + 1 and "\n" not in text and not getattr(widget, "_tooltip_force", False):
            return   # fully visible single-line name: nothing to add
        tip = tk.Toplevel(widget)
        tip.wm_overrideredirect(True)
        tk.Label(tip, text=text, bg="#ffffe0", fg="black", relief="solid", bd=1, padx=4, pady=1, justify="left").pack()
        tip.wm_geometry(f"+{widget.winfo_rootx() + 8}+{widget.winfo_rooty() + widget.winfo_height() + 2}")
        _tip_state["tip"] = tip
    except tk.TclError:
        pass


def _tip_enter(event):
    _tip_hide()
    widget = event.widget
    _tip_state["job_widget"] = widget
    try:
        _tip_state["job"] = widget.after(_tip_delay(), lambda: _tip_show(widget))
    except tk.TclError:
        pass


def install_button_tooltips(widget):
    """One class-level hover handler for EVERY Button in this Tk interpreter (windows, tabs, layers, pins list...). Shows the full text only when it is cropped (or has several lines)."""
    key = id(widget.tk)
    if key in _tip_installed:
        return
    _tip_installed.add(key)
    widget.bind_class("Button", "<Enter>", _tip_enter, add="+")
    widget.bind_class("Button", "<Leave>", _tip_hide, add="+")
    widget.bind_class("Button", "<ButtonPress>", _tip_hide, add="+")
    widget.bind_class("Button", "<Destroy>", _tip_hide, add="+")
    for cls in ("Checkbutton", "Radiobutton"):
        widget.bind_class(cls, "<Enter>", _tip_enter, add="+")
        widget.bind_class(cls, "<Leave>", _tip_hide, add="+")
        widget.bind_class(cls, "<ButtonPress>", _tip_hide, add="+")
        widget.bind_class(cls, "<Destroy>", _tip_hide, add="+")


def attach_tooltip(widget, text):
    """Overrides what a button's hover tooltip says (default: its own text)."""
    widget._tooltip_text = text
    install_button_tooltips(widget)


def style_menu_like_small_button(dropdown):
    dropdown.config(pady=0, padx=2, font=("", 8), highlightthickness=0, bd=1, indicatoron=False, width=1)


def add_window_row(parent, label, is_active, on_focus, on_close=None, on_merge=None,
                   on_rename=None, reorder=None, key=None, merge_targets=None, tooltip=None):
    """
    One Windows-section row, styled like the Tabs/Layers rows: highlighted
    cell and depressed name button when active. Right-hand buttons are
    packed BEFORE the name button so a long name crops (with a hover
    tooltip) instead of pushing them out of view. A second row under the
    name carries the Merge button and/or a "Merge..." selector, filling
    the row's width. merge_targets is a list of (label, callback).
    on_rename(cell, name_btn) is called by the pencil button; `reorder`
    (a RowReorder) plus `key` adds the drag handle. `tooltip` overrides
    the hover text (default: the label). Returns the cell.
    """
    cell = tk.Frame(parent, bg="#333333" if is_active else "#222222", highlightthickness=1, highlightbackground="#555555")
    cell.pack(side="top", fill="x", padx=4, pady=2)
    bg = cell.cget("bg")
    handle = None
    if reorder is not None:
        handle = tk.Label(cell, text="\u2261", fg="#888888", bg=bg, width=2, cursor="fleur")
        handle.pack(side="left", padx=(2, 0), pady=2)
    if on_merge is not None or merge_targets:
        second = tk.Frame(cell, bg=bg)
        second.pack(side="bottom", fill="x", padx=2, pady=(0, 2))
        if on_merge is not None:
            small_button(second, "Merge", on_merge, width=0).pack(side="left", fill="x", expand=True, padx=(0, 2))
        if merge_targets:
            choices = {}
            for i, (target_label, fn) in enumerate(merge_targets):
                name = target_label if target_label not in choices else f"{target_label} ({i + 1})"
                choices[name] = fn
            merge_var = tk.StringVar(value="Merge\u2026")
            dropdown = tk.OptionMenu(second, merge_var, *choices.keys(), command=lambda name: choices[name]())
            style_menu_like_small_button(dropdown)
            dropdown.pack(side="left", fill="x", expand=True, padx=(0, 2))
    if on_close is not None:
        small_button(cell, "\u00d7", on_close).pack(side="right", padx=(0, 2), pady=2)
    name_btn = tk.Button(
        cell, text=label, pady=BUTTON_PADY, anchor="w",
        relief=("sunken" if is_active else "raised"), command=on_focus,
    )
    if on_rename is not None:
        small_button(cell, "\u270e", lambda: on_rename(cell, name_btn)).pack(side="right", padx=(0, 2), pady=2)
    name_btn.pack(side="left", fill="x", expand=True, padx=2, pady=2)
    attach_tooltip(name_btn, tooltip or label)
    if reorder is not None:
        reorder.add(key, cell, handle, label)
    return cell


def build_windows_rows(parent, entries, owner, refresh):
    """
    The Windows list shared by image and film panels. `entries` are dicts:
    key, raw (editable name), label, active, focus, close, rename, hide
    (callables), optional tip / targets (merge selector) / hidden (bool).
    Visible windows first, then a collapsible "Hidden (n)" group (collapsed
    until opened, remembered per viewer in owner._hidden_expanded). The
    display order is per viewer (owner._windows_order). While the group is
    open, dragging a visible row into it backgrounds that window and
    dragging a hidden row out restores it.
    """
    order = getattr(owner, "_windows_order", [])
    visible = ordered([e for e in entries if not e.get("hidden")], order, lambda e: e["key"])
    hidden = ordered([e for e in entries if e.get("hidden")], order, lambda e: e["key"])
    expanded = bool(getattr(owner, "_hidden_expanded", False))
    shown = visible + (hidden if expanded else [])
    vis_keys = {e["key"] for e in visible}
    by_key = {e["key"]: e for e in entries}
    owner._windows_shown_keys = [e["key"] for e in visible] + ["__hdr__"] + ([e["key"] for e in hidden] if expanded else [])

    def on_move(key, to_index):
        keys = list(owner._windows_shown_keys)
        rest = [k for k in keys if k != key]
        hdr = rest.index("__hdr__")
        was_visible = key in vis_keys
        if was_visible and expanded and to_index > hdr:
            by_key[key]["hide"]()
            return
        if (not was_visible) and to_index <= hdr:
            by_key[key]["focus"]()      # restoring a hidden window = showing it again
            return
        real = [k for k in keys if k != "__hdr__"]
        owner._windows_order = move_in_order(real, key, to_index - (1 if to_index > hdr else 0))
        refresh()

    reorder = RowReorder(parent, on_move)

    def add(e):
        add_window_row(
            parent, e["label"], e["active"], e["focus"], e["close"], None,
            on_rename=lambda cell, btn, e=e: inline_rename(cell, btn, e["raw"], e["rename"], refresh),
            reorder=reorder, key=e["key"], merge_targets=e.get("targets"), tooltip=e.get("tip"),
        )

    for e in visible:
        add(e)

    def toggle():
        owner._hidden_expanded = not expanded
        refresh()

    hdr_cell = tk.Frame(parent, bg="#222222")
    hdr_cell.pack(side="top", fill="x", padx=4, pady=(6, 2))
    tk.Button(
        hdr_cell, text=("\u25be" if expanded else "\u25b8") + f" Hidden ({len(hidden)})", anchor="w", relief="flat", bd=0,
        fg="#cccccc", bg="#222222", activebackground="#333333", activeforeground="white", font=("", 9), command=toggle,
    ).pack(side="top", fill="x")
    reorder.add_target("__hdr__", hdr_cell)
    if expanded:
        for e in hidden:
            add(e)
