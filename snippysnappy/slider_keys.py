# snippysnappy v1.0.5
"""
Keyboard control for every tk.Scale slider in the app: clicking a slider gives it keyboard focus (Tk
does not do that by itself), then Left/Right (Up/Down on a vertical one) move it one step and
Shift+arrow five steps. The arrows are swallowed while a slider has focus, so they don't also step
the film or nudge a selection behind it.

Also: clicking any text field (Entry / Text / Spinbox) makes sure the keyboard really goes to it. Tk's own
click only moves Tk's internal focus; if another window still owns the X input focus (a toast, a popup,
a window manager that does not hand focus over on click) the field shows its cursor and selection but
typing goes nowhere. When the app has no input focus at all, the click now takes it.
"""
import tkinter as tk

_installed = False


def _step(scale, direction, count):
    """direction -1 = toward the 'from' end, +1 = toward the 'to' end. Uses Tk's own stepping (honours resolution)."""
    # tk::ScaleIncrement's "up" moves toward the 'from' end, "down" toward the 'to' end.
    way = "up" if direction < 0 else "down"
    for _ in range(count):
        scale.tk.call("tk::ScaleIncrement", str(scale), way, "little", "noRepeat")


def _on_key(scale, horizontal_key, direction, count):
    try:
        horizontal = str(scale.cget("orient")) == "horizontal"
        if horizontal != horizontal_key:
            return None
        _step(scale, direction, count)
    except tk.TclError:
        return None
    return "break"


def install():
    global _installed
    if _installed:
        return
    _installed = True
    orig_init = tk.Scale.__init__

    def init(self, *args, **kwargs):
        orig_init(self, *args, **kwargs)
        try:
            if "activebackground" not in kwargs:
                # Tk's default hover/active colour for the handle is near-white (#ececec) - far too loud on the dark
                # toolbars. A slightly lighter grey reads as a gentle highlight.
                self.configure(activebackground="#5a5a5a")
            self.bind("<ButtonPress-1>", lambda e, s=self: s.focus_set(), add="+")
            for seq, horiz, direction, count in (
                ("Left", True, -1, 1), ("Right", True, 1, 1), ("Shift-Left", True, -1, 5), ("Shift-Right", True, 1, 5),
                ("Up", False, -1, 1), ("Down", False, 1, 1), ("Shift-Up", False, -1, 5), ("Shift-Down", False, 1, 5),
            ):
                self.bind(f"<{seq}>", lambda e, s=self, h=horiz, d=direction, c=count: _on_key(s, h, d, c))
        except tk.TclError:
            pass

    tk.Scale.__init__ = init

    def _wrap_text_field(cls):
        orig = cls.__init__

        def field_init(self, *args, **kwargs):
            orig(self, *args, **kwargs)
            try:
                self.bind("<ButtonPress-1>", _take_input_focus, add="+")
            except tk.TclError:
                pass

        cls.__init__ = field_init

    for cls in (tk.Entry, tk.Text, tk.Spinbox):
        _wrap_text_field(cls)


def _take_input_focus(event):
    w = event.widget
    try:
        top = w.winfo_toplevel()
        if str(w.cget("state")) == "disabled":
            return
        if top.focus_displayof() is None:      # this application does not own the keyboard focus right now
            top.focus_force()
        w.focus_set()
    except tk.TclError:
        pass


install()
