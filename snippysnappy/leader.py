# snippysnappy v1.0.3
r"""
Leader-key menus (which-key style): press the leader key, then a short key sequence jumps to a toolbar or tool.

    \  e  s  b      leader, Edit, Snip, Box mode
    \  t  m         leader, Tools, Measure
    \  h            home (main toolbar)

Defaults live in IMAGE_TREE / FILM_TREE below. Change them from ~/.config/snippysnappy/config.py:
    LEADER_KEY = "backslash"                      # a Tk keysym, e.g. "backslash", "semicolon", "Control-space"
    LEADER_TIMEOUT_MS = 5000                      # 0 = never time out
    LEADER_BINDINGS = {"image": {"esb": "mode:snip:box", "xx": None}, "film": {...}}
        key sequence (one character per key) -> action, or None to remove a default binding.

Actions: home | edit | tools | save | save:normal | save:sloppy | save:as | copy | tool:<snip|pen|text|shape|resize|audio|trim> |
         mode:<tool>:<value> | ttool:<Button label in the Tools row> | noop:<note>
While the popup is open: Esc cancels, Backspace goes up a level, Enter opens the current menu's own tool.
The leader does nothing while a text box / entry field has focus.
"""
import copy
import time
import tkinter as tk

from .logging_setup import log


def L(label, do):
    return {"label": label, "do": do}


def M(label, keys, head=None):
    return {"label": label, "keys": keys, "head": head}


_EDIT_KEYS = {
    "h": L("Edit home", "edit"),
    "s": M("Snip", {
        "S": L("Select", "mode:snip:select"), "b": L("Box", "mode:snip:box"),
        "e": L("Ellipse", "mode:snip:ellipse"), "f": L("Free-form", "mode:snip:freeform"),
    }, head="tool:snip"),
    "d": M("Draw", {
        "S": L("Select", "mode:pen:select"), "p": L("Pen", "mode:pen:pen"),
        "s": L("Spray can", "mode:pen:spray"), "h": L("Highlighter", "mode:pen:highlight"),
        "e": L("Eraser", "mode:pen:eraser"),
    }, head="tool:pen"),
    "t": M("Text", {"S": L("Select", "mode:text:select"), "w": L("Write", "mode:text:write")}, head="tool:text"),
    "p": M("Shape", {
        "S": L("Select", "mode:shape:select"), "l": L("Line", "mode:shape:line"),
        "b": L("Box", "mode:shape:box"), "e": L("Ellipse", "mode:shape:ellipse"),
    }, head="tool:shape"),
    "r": L("Resize", "tool:resize"),
}

_TOOLS_KEYS = {
    "h": L("Tools home", "tools"),
    "t": L("Extract text", "ttool:Extract Text"),
    "m": L("Measure", "ttool:Measure"),
    "g": L("Magnify", "ttool:Magnify"),
    "p": L("Pin", "ttool:Pin"),
}

_SAVE_KEYS = {
    "S": L("Save", "save:normal"),
    "s": L("Sloppy save", "save:sloppy"),
    "a": L("Save as\u2026", "save:as"),
}


def _image_tree():
    return M("Leader", {
        "e": M("Edit", copy.deepcopy(_EDIT_KEYS)),
        "t": M("Tools", copy.deepcopy(_TOOLS_KEYS)),
        "s": M("Save", copy.deepcopy(_SAVE_KEYS), head="save"),
        "c": L("Copy", "copy"),
        "h": L("Home", "home"),
    })


def _film_tree():
    edit = copy.deepcopy(_EDIT_KEYS)
    edit["a"] = L("Audio", "tool:audio")
    edit["f"] = M("Film", {
        "t": L("Trim", "mode:trim:trim"), "s": L("Splice", "mode:trim:splice"),
        "e": L("Effects", "mode:trim:effects"),
        "i": M("Insert", {
            "f": L("Film file", "insert:file"), "s": L("Still image", "insert:still"),
            "b": L("Black gap", "insert:black"), "w": L("White gap", "insert:white"),
            "c": L("Copy of region", "insert:copy"),
            "y": L("Take region (for another window)", "insert:take"), "p": L("Insert the taken region", "insert:clip"),
        }, head="mode:trim:splice"),
    }, head="tool:trim")
    edit["v"] = M("Preview", {"p": L("Play / Pause", "preview:play"), "r": L("Restart", "preview:restart")}, head="tool:preview")
    tools = copy.deepcopy(_TOOLS_KEYS)
    tools["f"] = L("Filmsnap", "ttool:Filmsnap")
    return M("Leader", {
        "e": M("Edit", edit),
        "t": M("Tools", tools),
        "s": M("Save", copy.deepcopy(_SAVE_KEYS), head="save"),
        "c": L("Copy", "copy"),
        "h": L("Home", "home"),
    })


def build_tree(kind, overrides=None):
    tree = _film_tree() if kind == "film" else _image_tree()
    for seq, action in ((overrides or {}).get(kind) or {}).items():
        try:
            _apply_override(tree, str(seq), action)
        except Exception:
            log.exception(f"Leader: ignoring bad LEADER_BINDINGS entry {seq!r}: {action!r}")
    return tree


def _apply_override(tree, seq, action):
    if not seq:
        return
    node = tree
    for ch in seq[:-1]:
        keys = node.setdefault("keys", {})
        nxt = keys.get(ch)
        if nxt is None or "keys" not in nxt:
            nxt = keys[ch] = M(ch, {})
        node = nxt
    keys = node.setdefault("keys", {})
    if action is None:
        keys.pop(seq[-1], None)
    else:
        keys[seq[-1]] = L(str(action), str(action))


class LeaderNav:
    """Pure key-sequence logic (no Tk): feed keys, get back what to do."""

    def __init__(self, tree):
        self.root = tree
        self.stack = [tree]
        self.keys = []

    @property
    def node(self):
        return self.stack[-1]

    def press(self, key):
        """-> ("enter", node) | ("run", action) | ("none", None)"""
        child = (self.node.get("keys") or {}).get(key)
        if child is None:
            return ("none", None)
        if "keys" in child:
            self.stack.append(child)
            self.keys.append(key)
            return ("enter", child)
        return ("run", child["do"])

    def back(self):
        if len(self.stack) > 1:
            self.stack.pop()
            self.keys.pop()
            return True
        return False

    def head(self):
        return self.node.get("head")


def _cfg(name, default):
    try:
        from . import constants
        return getattr(constants, name, default)
    except Exception:
        return default


def leader_sequence(key):
    key = (key or "backslash").strip()
    if key.startswith("<"):
        return key
    mods = ("Control", "Alt", "Shift", "Mod1", "Mod2", "Mod4", "Meta", "Command")
    parts = key.split("-")
    if len(parts) > 1 and parts[0] in mods:
        return f"<{key}>"
    return f"<KeyPress-{key}>"


_TYPING_CLASSES = ("Entry", "Text", "Spinbox", "TEntry", "TCombobox", "TSpinbox")
_MODIFIER_KEYSYMS = {"Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R", "Meta_L", "Meta_R",
                     "Super_L", "Super_R", "Caps_Lock", "ISO_Level3_Shift", "Num_Lock"}


def typing_focus(root):
    try:
        w = root.focus_get()
        return w is not None and w.winfo_class() in _TYPING_CLASSES
    except (KeyError, tk.TclError):
        return False


class LeaderPopup:
    BG, FG, KEY, DIM = "#222222", "#eeeeee", "#ffd800", "#999999"

    def __init__(self, owner, tree, run_action, on_close, anchor=None):
        self.owner = owner            # the window's root (Tk/Toplevel)
        self.anchor = anchor          # widget whose top-left corner the popup sits on (the working area)
        self.nav = LeaderNav(tree)
        self.run_action = run_action
        self.on_close = on_close
        self.win = None
        self._timer = None
        self._prev_focus = None
        self._message = ""
        self.opened = time.monotonic()     # _p34_leader_robust

    # ---- lifecycle ----
    def open(self):
        root = self.owner
        try:
            self._prev_focus = root.focus_get()
        except (KeyError, tk.TclError):
            self._prev_focus = None
        win = self.win = tk.Toplevel(root, bg=self.BG, highlightthickness=1, highlightbackground="#666666")
        win.withdraw()
        win.overrideredirect(True)
        try:
            win.attributes("-topmost", True)
        except tk.TclError:
            pass
        win.bind("<Key>", self._on_key)
        self._render()
        win.deiconify()
        win.update_idletasks()
        self._arm_timeout()          # armed first: a popup can never outlive the timeout
        win.lift()
        try:
            win.focus_force()
        except tk.TclError:
            pass
        self._try_grab(0)
        win.bind("<FocusOut>", self._on_focus_out)

    def alive(self):
        try:
            return self.win is not None and bool(self.win.winfo_exists())
        except tk.TclError:
            return False

    def _try_grab(self, n):
        # a grab needs a viewable window; retry briefly instead of blocking on the window manager
        win = self.win
        if win is None:
            return
        try:
            win.grab_set()
        except tk.TclError:
            if n < 30:
                try:
                    win.after(20, lambda: self._try_grab(n + 1))
                except tk.TclError:
                    pass
            else:
                log.debug("Leader: could not take a local grab (non-fatal)")

    def _on_focus_out(self, _event=None):
        # focus left the popup: if it left snippysnappy altogether, the popup has no business staying up
        win = self.win
        if win is None:
            return

        def check():
            try:
                if self.win is not None and self.win.focus_displayof() is None:
                    self.close(restore_focus=False)
            except tk.TclError:
                pass

        try:
            win.after(120, check)
        except tk.TclError:
            pass

    def close(self, run=None, restore_focus=True):
        if self._timer is not None:
            try:
                self.owner.after_cancel(self._timer)
            except Exception:
                pass
            self._timer = None
        win, self.win = self.win, None
        if win is not None:
            try:
                win.grab_release()
            except tk.TclError:
                pass
            try:
                win.destroy()
            except tk.TclError:
                pass
        if restore_focus:
            try:
                self.owner.focus_force()
                if self._prev_focus is not None and self._prev_focus.winfo_exists():
                    self._prev_focus.focus_set()
            except (tk.TclError, KeyError):
                pass
        self.on_close()
        if run is not None:
            try:
                self.owner.after(30, lambda: self.run_action(run))
            except tk.TclError:
                pass

    def _arm_timeout(self):
        if self._timer is not None:
            try:
                self.owner.after_cancel(self._timer)
            except Exception:
                pass
            self._timer = None
        ms = int(_cfg("LEADER_TIMEOUT_MS", 5000) or 0)
        if ms > 0:
            self._timer = self.owner.after(ms, self.close)

    # ---- keys ----
    def _on_key(self, event):
        ks = event.keysym
        if ks in _MODIFIER_KEYSYMS:
            return "break"
        self._arm_timeout()
        if ks == "Escape":
            self.close()
            return "break"
        if ks == "BackSpace":
            if not self.nav.back():
                self.close()
            else:
                self._message = ""
                self._render()
            return "break"
        if ks in ("Return", "KP_Enter"):
            self._press_head()
            return "break"
        key = event.char if event.char and event.char.isprintable() and not event.char.isspace() else ks
        self._press(key)
        return "break"

    def _press_head(self):
        head = self.nav.head()
        if head:
            self.close(run=head)
        else:
            self._flash("this menu has no tool of its own")

    def _press(self, key):
        self._arm_timeout()
        kind, payload = self.nav.press(key)
        if kind == "run":
            self.close(run=payload)
        elif kind == "enter":
            self._message = ""
            self._render()
        else:
            self._flash(f"no binding for {key!r}")

    def _flash(self, text):
        self._message = text
        self._render()

    # ---- drawing: same raised grey buttons as the toolbars ----
    def _render(self):
        win = self.win
        if win is None:
            return
        for w in win.winfo_children():
            w.destroy()
        try:
            from .constants import BUTTON_PADY
        except Exception:
            BUTTON_PADY = 6
        path = " ".join(["\\"] + self.nav.keys)
        title = self.nav.node.get("label", "Leader")
        tk.Label(win, text=f"{path}  \u00b7  {title}", bg=self.BG, fg=self.DIM, anchor="w").pack(fill="x", padx=8, pady=(6, 2))
        keys = self.nav.node.get("keys") or {}
        mark = lambda child: child["label"] + ("  \u25b8" if "keys" in child else "")   # _p26_home_last
        rows = [(k, mark(child)) for k, child in keys.items() if k != "h"]
        if self.nav.head():
            rows.append(("\u23ce", f"{title} tool as it is"))
        if "h" in keys:
            rows.append(("h", mark(keys["h"])))     # the home entry is always last
        width = max([len(label) for _k, label in rows] + [10]) + 4
        for k, label in rows:
            cmd = self._press_head if k == "\u23ce" else (lambda kk=k: self._press(kk))
            tk.Button(win, text=f"{k}   {label}", anchor="w", justify="left", width=width, pady=BUTTON_PADY,
                      takefocus=0, command=cmd).pack(fill="x", padx=8, pady=2)
        foot = self._message or "Esc cancel \u00b7 Backspace back"
        tk.Label(win, text=foot, bg=self.BG, fg=("#ff8888" if self._message else self.DIM), anchor="w").pack(fill="x", padx=8, pady=(2, 6))
        self._place()

    def _place(self):
        win = self.win
        ref = self.anchor if self.anchor is not None else self.owner
        try:
            win.update_idletasks()
            w, h = win.winfo_reqwidth(), win.winfo_reqheight()
            x = ref.winfo_rootx() + 8
            y = ref.winfo_rooty() + 8
            win.geometry(f"{w}x{h}+{x}+{y}")
        except tk.TclError:
            pass


# ---------------------------------------------------------------------------
# Drivers: turn an action string into calls on the window's own methods
# ---------------------------------------------------------------------------

def _find_button(widget, label):
    found = None
    for child in widget.winfo_children():
        try:
            if isinstance(child, tk.Button) and child.cget("text") == label:
                if child.winfo_ismapped():
                    return child
                found = found or child
        except tk.TclError:
            continue
        sub = _find_button(child, label)
        if sub is not None:
            if sub.winfo_ismapped():
                return sub
            found = found or sub
    return found


class ImageDriver:
    _TOOL_ENTER = {"ocr": "enter_ocr_paint_mode", "measure": "enter_measure_mode",
                   "magnify": "enter_magnify_mode", "pin": "enter_pin_mode"}
    _TOOL_NAME = {"Extract Text": "ocr", "Measure": "measure", "Magnify": "magnify", "Pin": "pin"}

    def __init__(self, app):
        self.app = app

    def go_home(self):
        a = self.app
        if a.resize_mode_active:
            a._exit_resize_mode()
        if a.edit_mode_active:
            a.bake_edits()
        elif a._active_tool_name():
            a._leave_current_tool_silently()
            a.show_main_toolbar()
        else:
            a.show_main_toolbar()

    def ensure_edit(self):
        a = self.app
        if a.resize_mode_active:
            a._exit_resize_mode()
            return
        if a.edit_mode_active:
            return
        if a._active_tool_name():
            a._leave_current_tool_silently()
        a._keybind_edit()

    def run(self, action):
        a = self.app
        if getattr(a, "sel_coords", None) is None or getattr(a, "_ocr_overlay", None) is not None:
            log.info(f"Leader: ignoring {action!r} (no selection yet, or the extracted-text view is open)")
            return
        head, _, rest = action.partition(":")
        log.info(f"Leader: {action}")
        if head == "home":
            self.go_home()
        elif head == "edit":
            self.ensure_edit()
        elif head == "tool":
            self.ensure_edit()
            if rest == "resize":
                a.show_resize_options()
            else:
                a.pick_tool(rest)
        elif head == "mode":
            tool, _, value = rest.partition(":")
            self.ensure_edit()
            a.pick_tool(tool)
            {"snip": a._pick_snip_shape, "pen": a.set_pen_variant,
             "text": a.set_text_mode, "shape": a.pick_shape_type}[tool](value)
        elif head == "tools":
            self.go_home()
            a.tools_row_expanded = True
            a.save_row_expanded = False
            a.show_main_toolbar(reset_tools_row=False, reset_save_row=False)
        elif head == "ttool":
            name = self._TOOL_NAME[rest]
            if a.edit_mode_active or a.resize_mode_active:
                self.go_home()
            a._switch_tool(name, getattr(a, self._TOOL_ENTER[name]))
        elif head == "save":
            self.go_home()
            if rest == "normal":
                a.begin_normal_save()
            elif rest == "sloppy":
                a.do_slop_save()
            elif rest == "as":
                a._save_as_image()
            elif not a.save_row_expanded:
                a.toggle_save_row()
        elif head == "copy":
            a._keybind_copy()
        elif head == "noop":
            pass
        else:
            log.warning(f"Leader: unknown action {action!r}")


class FilmDriver:
    def __init__(self, app):
        self.app = app

    def _tools_open(self):
        return getattr(self.app, "_tl_tools_frame", None) is not None

    def go_home(self):
        a = self.app
        if getattr(a, "_ed2_active", False):
            a._ed2_exit(False)
        elif self._tools_open():
            a._t2_back()

    def ensure_edit(self):
        a = self.app
        if getattr(a, "_ed2_active", False):
            return
        if self._tools_open():
            a._t2_back()
        a._noop_edit()      # the Edit button's handler (_ed2_enter)

    def _press_main(self, label):
        btn = _find_button(self.app._tl_main_row, label)
        if btn is None:
            log.warning(f"Leader: no {label!r} button found")
        else:
            btn.invoke()

    def run(self, action):
        a = self.app
        head, _, rest = action.partition(":")
        log.info(f"Leader (film): {action}")
        if head == "home":
            self.go_home()
        elif head == "edit":
            self.ensure_edit()
        elif head == "tool":
            self.ensure_edit()
            a._ed9_set_tool(rest)
        elif head == "mode":
            tool, _, value = rest.partition(":")
            self.ensure_edit()
            a._ed9_set_tool(tool)
            if tool == "trim":
                a._p11_set_mode(value)
            else:
                a._ed9_set_mode(tool, value)
        elif head == "insert":
            self.ensure_edit()
            a._p31_insert_cmd(rest)
        elif head == "preview":
            self.ensure_edit()
            a._ed9_set_tool("preview")
            (a._p29_restart if rest == "restart" else a._p29_play)()
        elif head == "tools":
            self.go_home()
            a._tl_toggle_tools()
        elif head == "ttool":
            self.go_home()
            if not self._tools_open():
                a._tl_toggle_tools()
            btn = _find_button(a._tl_tools_frame, rest)
            if btn is None:
                log.warning(f"Leader (film): no {rest!r} button in the Tools row")
            else:
                btn.invoke()
        elif head == "save":
            self.go_home()
            if not a.save_row.winfo_children():
                self._press_main("Save")
            if rest:
                label = {"normal": "Save", "sloppy": "Sloppy Save", "as": "Save As\u2026"}.get(rest)
                btn = _find_button(a.save_row, label) if label else None
                if btn is None:
                    log.warning(f"Leader (film): no {label!r} button in the save row")
                else:
                    btn.invoke()
        elif head == "copy":
            self.go_home()
            self._press_main("Copy")
        elif head == "noop":
            pass
        else:
            log.warning(f"Leader: unknown action {action!r}")


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------

def install_leader(app, target, kind):
    """Bind the leader key on `target` (a window root) for `app`. Safe to call more than once per target."""
    if not _cfg("LEADER_ENABLED", True) or getattr(target, "_leader_installed", False):
        return
    target._leader_installed = True
    driver = (FilmDriver if kind == "film" else ImageDriver)(app)
    state = {"popup": None}

    def on_close():
        state["popup"] = None

    def run_action(action):
        try:
            driver.run(action)
        except Exception:
            log.exception(f"Leader: action {action!r} failed")

    def trigger(event):
        p = state["popup"]
        if p is not None:
            if p.alive() and time.monotonic() - p.opened < (int(_cfg("LEADER_TIMEOUT_MS", 5000) or 0) / 1000.0 + 3 if _cfg("LEADER_TIMEOUT_MS", 5000) else 3600):
                return "break"
            log.info("Leader: discarding a stale popup")
            state["popup"] = None
        try:
            if target.overrideredirect():
                log.info("Leader: key ignored (borderless select-mode overlay)")
                return None   # the borderless select-mode overlay has no toolbar
        except tk.TclError:
            return None
        if typing_focus(target):
            log.info("Leader: key ignored (a text box / entry has focus)")
            return None
        log.info(f"Leader ({kind}): opening the popup")
        tree = build_tree(kind, _cfg("LEADER_BINDINGS", {}))
        popup = state["popup"] = LeaderPopup(target, tree, run_action, on_close, anchor=getattr(app, "canvas", None))
        try:
            popup.open()
        except Exception:
            log.exception("Leader: could not open the popup")
            state["popup"] = None
            return None
        return "break"

    def owner_gone(event):
        # window closed / hidden to the background / minimised: the popup goes with it
        if event.widget is not target:
            return
        p = state["popup"]
        if p is not None:
            p.close(restore_focus=False)

    def forward_key(event):
        # keys typed in the window while the popup is open (the popup may not have been given keyboard focus)
        p = state["popup"]
        if p is None or not p.alive():
            return None
        try:
            inside = str(event.widget).startswith(str(p.win))
        except Exception:
            inside = False
        if inside:
            return None
        return p._on_key(event)

    for ev in ("<Unmap>", "<Destroy>"):
        try:
            target.bind(ev, owner_gone, add="+")
        except tk.TclError:
            pass
    try:
        target.bind("<Key>", forward_key, add="+")
    except tk.TclError:
        pass

    seq = leader_sequence(_cfg("LEADER_KEY", "backslash"))
    try:
        target.bind(seq, trigger, add="+")
    except tk.TclError:
        log.warning(f"Leader: {_cfg('LEADER_KEY', '')!r} is not a valid key - leader disabled")

_p34_leader_robust = True
