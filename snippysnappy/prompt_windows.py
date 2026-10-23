# snippysnappy v1.0.1
"""Standalone Tk prompt windows: --open prompt (fuzzy CLI-style and GUI file pickers) and the --delay countdown window."""
import tkinter as tk
from pathlib import Path

from .constants import WINDOW_TITLE_BASE, BUTTON_PADY
from .icon import _load_app_icon
from .logging_setup import log
from .fuzzy_open import fuzzy_search_images, is_image_file, resolve_open_path

class OpenPromptApp:
    """
    The `--open prompt` picker: a small decorated window with a Parent Dir
    selector, a path entry, and a live fuzzy-matched, keyboard-navigable
    list of image files (shown as full paths, matched characters
    highlighted) - built with the same root Tk() instance main() already
    created, reused afterward for the real SelectorApp once a file is
    chosen (or the process exits cleanly on Cancel/close).
    """

    def __init__(self, root, default_dir, owns_mainloop=True):
        self.root = root
        self.default_dir = Path(default_dir).expanduser()
        self.parent_dir = self.default_dir
        self.result_path = None
        self.results = []
        self.selected_index = 0
        self.vim_mode = "insert"   # "insert" | "normal" | "visual" | "visual_line" - same scheme as SelectorApp's text tool
        self._vim_visual_anchor = None
        self.vim_register = None   # separate, simpler, plain-text-only register for this window
        self._owns_mainloop = owns_mainloop
        self.switch_to_gui_requested = False
        log.info(f"--open prompt: opening picker, default parent dir = {self.default_dir}")

        root.title(f"{WINDOW_TITLE_BASE} - open")
        root.resizable(True, True)
        root.configure(bg="#1a1a1a")
        root.geometry("640x420")
        root.attributes("-topmost", True)   # matches every other snippysnappy window
        self._app_icon_image = _load_app_icon()   # keep a reference - Tk drops the icon if this gets garbage collected
        if self._app_icon_image is not None:
            root.iconphoto(True, self._app_icon_image)

        top = tk.Frame(root, bg="#1a1a1a")
        top.pack(side="top", fill="x", padx=12, pady=(12, 4))
        tk.Label(top, text="Parent Dir:", fg="white", bg="#1a1a1a").pack(side="left", padx=(0, 6))
        self._default_label = "$FUZZ_DIR (default)"
        self.parent_choices = {
            self._default_label: self.default_dir,
            "~/": Path.home(),
            "/": Path("/"),
        }
        self.parent_var = tk.StringVar(value=self._default_label)
        tk.OptionMenu(top, self.parent_var, *self.parent_choices.keys(), command=self._on_parent_changed).pack(side="left")

        path_row = tk.Frame(root, bg="#1a1a1a")
        path_row.pack(side="top", fill="x", padx=12, pady=(4, 8))
        tk.Label(path_row, text="Path:", fg="white", bg="#1a1a1a").pack(side="left", padx=(0, 6))
        self.path_var = tk.StringVar()
        self.entry = tk.Entry(path_row, textvariable=self.path_var, width=60)
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry.bind("<KeyRelease>", self._on_type)
        self.entry.bind("<Down>", self._on_arrow_down)
        self.entry.bind("<Up>", self._on_arrow_up)
        self.entry.bind("<Return>", self._on_enter)
        self.entry.bind("<Escape>", self._vim_escape)
        self.entry.bind("<Key>", self._vim_on_key)
        self.entry.focus_set()

        self.status_label = tk.Label(root, text="", fg="#aaaaaa", bg="#1a1a1a", anchor="w")
        self.status_label.pack(side="top", fill="x", padx=12)
        self.error_label = tk.Label(root, text="", fg="#ff6666", bg="#1a1a1a", anchor="w")
        self.error_label.pack(side="top", fill="x", padx=12)

        # takefocus=0 so clicking a result (or the dropdown) doesn't steal
        # keyboard focus away from the path entry.
        self.results_text = tk.Text(
            root, height=14, bg="#222222", fg="white", state="disabled",
            cursor="arrow", wrap="none", takefocus=0,
        )
        self.results_text.pack(side="top", fill="both", expand=True, padx=12, pady=(0, 8))
        self.results_text.tag_configure("match", foreground="#00b4ff", font=("", 10, "bold"))
        self.results_text.tag_configure("selected_row", background="#3a5a8a")
        self.results_text.bind("<Button-1>", self._on_click_result)

        btn_row = tk.Frame(root, bg="#1a1a1a")
        btn_row.pack(side="top", fill="x", padx=12, pady=(0, 12))
        tk.Button(btn_row, text="Open", pady=BUTTON_PADY, command=self._confirm_current).pack(side="left", padx=(0, 2))
        tk.Button(btn_row, text="Cancel", pady=BUTTON_PADY, command=self._cancel).pack(side="left", padx=2)
        tk.Button(btn_row, text="Use GUI", pady=BUTTON_PADY, command=self._switch_to_gui).pack(side="right")

        root.protocol("WM_DELETE_WINDOW", self._cancel)
        self._refresh_results()

    def _on_parent_changed(self, choice):
        self.parent_dir = self.parent_choices[choice]
        log.debug(f"--open prompt: parent dir changed to {self.parent_dir}")
        self._refresh_results()
        self.entry.focus_set()   # the OptionMenu takes focus on selection - hand it back

    def _on_type(self, event):
        if event.keysym in ("Down", "Up", "Return", "Escape"):
            return
        self._refresh_results()

    def _refresh_results(self):
        query = self.path_var.get().strip()
        log.debug(f"--open prompt: searching {self.parent_dir!r} for query {query!r}")
        if not self.parent_dir.is_dir():
            log.warning(f"--open prompt: parent dir {self.parent_dir} does not exist or isn't a directory")
            self.status_label.config(text=f"'{self.parent_dir}' doesn't exist - pick a different Parent Dir.")
            self.results = []
            self.selected_index = 0
            self._render_results()
            return
        self.results = fuzzy_search_images(self.parent_dir, query)
        log.debug(f"--open prompt: {len(self.results)} result(s) for query {query!r} under {self.parent_dir}")
        if not self.results:
            self.status_label.config(
                text="No matching image files found." if query else f"No image files found under {self.parent_dir}."
            )
        else:
            self.status_label.config(text="")
        self.selected_index = 0
        self._render_results()

    def _render_results(self):
        self.results_text.config(state="normal")
        self.results_text.delete("1.0", "end")
        for i, (path, matched_idxs) in enumerate(self.results):
            self.results_text.insert("end", str(path) + "\n")
            for idx in matched_idxs:
                self.results_text.tag_add("match", f"{i + 1}.{idx}", f"{i + 1}.{idx + 1}")
        self._highlight_selected()
        self.results_text.config(state="disabled")

    def _highlight_selected(self):
        self.results_text.tag_remove("selected_row", "1.0", "end")
        if self.results:
            i = self.selected_index
            self.results_text.tag_add("selected_row", f"{i + 1}.0", f"{i + 1}.end+1c")
            self.results_text.see(f"{i + 1}.0")

    def _move_selection(self, delta):
        if not self.results:
            return
        self.selected_index = max(0, min(self.selected_index + delta, len(self.results) - 1))
        self.results_text.config(state="normal")
        self._highlight_selected()
        self.results_text.config(state="disabled")

    def _on_arrow_down(self, event):
        self._move_selection(1)
        return "break"

    def _on_arrow_up(self, event):
        self._move_selection(-1)
        return "break"

    def _on_click_result(self, event):
        index_str = self.results_text.index(f"@{event.x},{event.y}")
        line = int(index_str.split(".")[0]) - 1
        if 0 <= line < len(self.results):
            self.selected_index = line
            self.results_text.config(state="normal")
            self._highlight_selected()
            self.results_text.config(state="disabled")
            self._confirm_current()
        return "break"

    def _on_enter(self, event):
        self._confirm_current()
        return "break"

    def _confirm_current(self):
        self.error_label.config(text="")
        if self.results:
            chosen = self.results[self.selected_index][0]
            log.info(f"--open prompt: confirmed via results list -> {chosen}")
            self._finish(chosen)
            return
        typed = self.path_var.get().strip()
        if not typed:
            return
        candidate = resolve_open_path(typed, self.parent_dir)
        log.info(f"--open prompt: no list results, trying typed path directly -> {candidate}")
        if not candidate.exists() or not candidate.is_file():
            self.error_label.config(text=f"No such file: {candidate}")
            return
        if not is_image_file(candidate):
            self.error_label.config(text="Please select an image file.")
            return
        self._finish(candidate)

    def _finish(self, path):
        self.result_path = path
        if self._owns_mainloop:
            self.root.quit()
        else:
            self.root.destroy()

    def _cancel(self):
        self.result_path = None
        if self._owns_mainloop:
            self.root.quit()
        else:
            self.root.destroy()

    def _switch_to_gui(self):
        self.switch_to_gui_requested = True
        self._cancel()

    def run_modal(self):
        self.root.wait_window()
        return self.result_path

    _VIM_ENTRY_NAV_KEYSYMS = {"Left", "Right", "Home", "End"}

    # ---- vim-style Insert/Normal/Visual modes for the path entry ----
    # Same conceptual state machine as SelectorApp's text tool (see that
    # class's _vim_on_key for the fuller explanation), adapted for a
    # single-line Entry: Up/Down stay bound to result-list navigation
    # (unchanged), so vim-mode cursor movement here only covers
    # Left/Right/Home/End, and there's no real "line" concept so Visual
    # Line just selects the whole field.

    def _vim_escape(self, event=None):
        if self.vim_mode == "insert":
            self._vim_enter_normal()
            return "break"
        self._cancel()
        return "break"

    def _vim_enter_insert(self):
        self.vim_mode = "insert"
        self.entry.config(insertwidth=2)
        self.entry.selection_clear()

    def _vim_enter_normal(self):
        self.vim_mode = "normal"
        self.entry.config(insertwidth=0)
        self._vim_update_block_cursor()

    def _vim_update_block_cursor(self):
        """
        Entry has no per-character tagging like Text does, so the "block
        cursor" is approximated by selecting just the one character under
        the cursor - visually similar, at the cost of briefly borrowing the
        widget's normal selection highlight for it.
        """
        pos = self.entry.index("insert")
        self.entry.selection_clear()
        end = len(self.entry.get())
        if pos < end:
            self.entry.selection_range(pos, pos + 1)

    def _vim_enter_visual(self, linewise):
        self.vim_mode = "visual_line" if linewise else "visual"
        self._vim_visual_anchor = self.entry.index("insert")
        if linewise:
            self.entry.selection_range(0, len(self.entry.get()))
        else:
            self._vim_update_visual_selection()

    def _vim_update_visual_selection(self):
        if self.vim_mode != "visual":
            return
        cur = self.entry.index("insert")
        lo, hi = sorted((self._vim_visual_anchor, cur))
        end = len(self.entry.get())
        self.entry.selection_range(lo, min(hi + 1, end))

    def _vim_get_selection_text(self):
        try:
            start, end = self.entry.index("sel.first"), self.entry.index("sel.last")
            return self.entry.get()[start:end]
        except tk.TclError:
            return ""

    def _vim_yank(self):
        if self.vim_mode in ("visual", "visual_line"):
            self.vim_register = self._vim_get_selection_text()
            log.debug(f"--open prompt vim yank: {len(self.vim_register)} character(s)")
            self._vim_enter_normal()
        else:
            self.vim_register = self.entry.get()
            log.debug("--open prompt vim yank (Normal mode, no selection): copied the whole field")

    def _vim_delete(self):
        if self.vim_mode in ("visual", "visual_line"):
            self.vim_register = self._vim_get_selection_text()
            try:
                self.entry.delete("sel.first", "sel.last")
            except tk.TclError:
                pass
            self._vim_enter_normal()
        else:
            self.vim_register = self.entry.get()
            self.entry.delete(0, "end")
        self._refresh_results()

    def _vim_paste(self):
        if self.vim_register is None:
            return
        pos = self.entry.index("insert")
        self.entry.insert(pos, self.vim_register)
        if self.vim_mode in ("visual", "visual_line"):
            self._vim_enter_normal()
        self._refresh_results()

    def _vim_on_key(self, event):
        if self.vim_mode == "insert":
            return None
        keysym = event.keysym
        if keysym == "i":
            self._vim_enter_insert(); return "break"
        if keysym == "v":
            self._vim_enter_visual(linewise=False); return "break"
        if keysym == "V":
            self._vim_enter_visual(linewise=True); return "break"
        if keysym == "y":
            self._vim_yank(); return "break"
        if keysym == "d":
            self._vim_delete(); return "break"
        if keysym == "p":
            self._vim_paste(); return "break"
        if keysym in self._VIM_ENTRY_NAV_KEYSYMS:
            if self.vim_mode in ("visual", "visual_line"):
                self.root.after_idle(self._vim_update_visual_selection)
            else:
                self.root.after_idle(self._vim_update_block_cursor)
            return None
        if event.char:
            return "break"
        return None


class GuiPromptApp:
    """
    An in-app, themed replacement for the OS's native "Open" file dialog -
    same underlying job as OpenPromptApp (pick an image file starting
    from FUZZ_DIR/~/‘/’), but a plain folder browser (navigate into
    directories, pick a file) instead of fuzzy search, and styled to
    match the rest of the app rather than borrowing whatever the OS's own
    dialog looks like. Carries its own "Use CLI" button back to
    OpenPromptApp, mirroring that one's "Use GUI" button - see
    _pick_file_via_configured_picker (Tabs panel) and the --open prompt
    branch in main() for the two places that hop back and forth between
    the two of these.
    """

    def __init__(self, root, default_dir, owns_mainloop=True):
        self.root = root
        self.default_dir = Path(default_dir).expanduser()
        self.current_dir = self.default_dir
        self.result_path = None
        self.entries = []   # [(display_name, Path, is_dir), ...] for the currently-listed directory, in display order
        self.selected_index = None
        self._owns_mainloop = owns_mainloop
        self.switch_to_cli_requested = False
        log.info(f"--open prompt (GUI): opening picker, starting dir = {self.current_dir}")

        root.title(f"{WINDOW_TITLE_BASE} - open")
        root.resizable(True, True)
        root.configure(bg="#1a1a1a")
        root.geometry("640x420")
        root.attributes("-topmost", True)
        self._app_icon_image = _load_app_icon()
        if self._app_icon_image is not None:
            root.iconphoto(True, self._app_icon_image)

        top = tk.Frame(root, bg="#1a1a1a")
        top.pack(side="top", fill="x", padx=12, pady=(12, 4))
        tk.Label(top, text="Parent Dir:", fg="white", bg="#1a1a1a").pack(side="left", padx=(0, 6))
        self._default_label = "$FUZZ_DIR (default)"   # same label OpenPromptApp uses, since it names the same FUZZ_DIR variable
        self.parent_choices = {
            self._default_label: self.default_dir,
            "~/": Path.home(),
            "/": Path("/"),
        }
        self.parent_var = tk.StringVar(value=self._default_label)
        tk.OptionMenu(top, self.parent_var, *self.parent_choices.keys(), command=self._on_parent_changed).pack(side="left")
        tk.Button(top, text="\u2191 Up", pady=BUTTON_PADY, command=self._go_up).pack(side="left", padx=(8, 0))

        path_row = tk.Frame(root, bg="#1a1a1a")
        path_row.pack(side="top", fill="x", padx=12, pady=(4, 8))
        tk.Label(path_row, text="Path:", fg="white", bg="#1a1a1a").pack(side="left", padx=(0, 6))
        self.path_var = tk.StringVar(value=str(self.current_dir))
        self.entry = tk.Entry(path_row, textvariable=self.path_var, width=60)
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry.bind("<Return>", self._on_path_entered)

        self.status_label = tk.Label(root, text="", fg="#aaaaaa", bg="#1a1a1a", anchor="w")
        self.status_label.pack(side="top", fill="x", padx=12)
        self.error_label = tk.Label(root, text="", fg="#ff6666", bg="#1a1a1a", anchor="w")
        self.error_label.pack(side="top", fill="x", padx=12)

        list_frame = tk.Frame(root, bg="#1a1a1a")
        list_frame.pack(side="top", fill="both", expand=True, padx=12, pady=(0, 8))
        scrollbar = tk.Scrollbar(list_frame)
        scrollbar.pack(side="right", fill="y")
        self.listbox = tk.Listbox(
            list_frame, bg="#222222", fg="white", selectbackground="#3a5a8a",
            activestyle="none", highlightthickness=0, borderwidth=0,
            yscrollcommand=scrollbar.set, font=("", 10),
        )
        self.listbox.pack(side="left", fill="both", expand=True)
        scrollbar.config(command=self.listbox.yview)
        self.listbox.bind("<<ListboxSelect>>", self._on_select)
        self.listbox.bind("<Double-Button-1>", self._on_double_click)
        self.listbox.bind("<Return>", self._on_double_click)

        btn_row = tk.Frame(root, bg="#1a1a1a")
        btn_row.pack(side="top", fill="x", padx=12, pady=(0, 12))
        tk.Button(btn_row, text="Open", pady=BUTTON_PADY, command=self._confirm_current).pack(side="left", padx=(0, 2))
        tk.Button(btn_row, text="Cancel", pady=BUTTON_PADY, command=self._cancel).pack(side="left", padx=2)
        tk.Button(btn_row, text="Use CLI", pady=BUTTON_PADY, command=self._switch_to_cli).pack(side="right")

        root.protocol("WM_DELETE_WINDOW", self._cancel)
        self._refresh_listing()

    def _on_parent_changed(self, choice):
        self.current_dir = self.parent_choices[choice]
        self.path_var.set(str(self.current_dir))
        log.debug(f"--open prompt (GUI): parent dir changed to {self.current_dir}")
        self._refresh_listing()

    def _go_up(self):
        parent = self.current_dir.parent
        if parent != self.current_dir:
            self.current_dir = parent
            self.path_var.set(str(self.current_dir))
            self._refresh_listing()

    def _on_path_entered(self, event):
        candidate = Path(self.path_var.get().strip()).expanduser()
        if candidate.is_dir():
            self.current_dir = candidate
            self.error_label.config(text="")
            self._refresh_listing()
        elif candidate.is_file() and is_image_file(candidate):
            self._finish(candidate)
        else:
            self.error_label.config(text=f"No such directory or image file: {candidate}")
        return "break"

    def _refresh_listing(self):
        self.error_label.config(text="")
        self.entries = []
        try:
            items = sorted(self.current_dir.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError as e:
            log.warning(f"--open prompt (GUI): can't list {self.current_dir}: {e}")
            self.status_label.config(text=f"Can't read '{self.current_dir}': {e}")
            self.listbox.delete(0, "end")
            self.selected_index = None
            return
        for p in items:
            if p.is_dir():
                self.entries.append((p.name + "/", p, True))
            elif is_image_file(p):
                self.entries.append((p.name, p, False))
        self.listbox.delete(0, "end")
        for name, _p, _is_dir in self.entries:
            self.listbox.insert("end", name)
        self.status_label.config(text="" if self.entries else "No subfolders or image files here.")
        self.selected_index = None

    def _on_select(self, event):
        sel = self.listbox.curselection()
        self.selected_index = sel[0] if sel else None

    def _on_double_click(self, event):
        sel = self.listbox.curselection()
        if not sel:
            return
        _name, path, is_dir = self.entries[sel[0]]
        if is_dir:
            self.current_dir = path
            self.path_var.set(str(self.current_dir))
            self._refresh_listing()
        else:
            self._finish(path)

    def _confirm_current(self):
        self.error_label.config(text="")
        if self.selected_index is not None:
            _name, path, is_dir = self.entries[self.selected_index]
            if is_dir:
                self.current_dir = path
                self.path_var.set(str(self.current_dir))
                self._refresh_listing()
                return
            self._finish(path)
            return
        typed = self.path_var.get().strip()
        if not typed:
            return
        candidate = Path(typed).expanduser()
        if candidate.is_dir():
            self.current_dir = candidate
            self.path_var.set(str(self.current_dir))
            self._refresh_listing()
            return
        if not candidate.exists() or not candidate.is_file():
            self.error_label.config(text=f"No such file: {candidate}")
            return
        if not is_image_file(candidate):
            self.error_label.config(text="Please select an image file.")
            return
        self._finish(candidate)

    def _finish(self, path):
        self.result_path = path
        if self._owns_mainloop:
            self.root.quit()
        else:
            self.root.destroy()

    def _cancel(self):
        self.result_path = None
        if self._owns_mainloop:
            self.root.quit()
        else:
            self.root.destroy()

    def _switch_to_cli(self):
        self.switch_to_cli_requested = True
        self._cancel()

    def run_modal(self):
        self.root.wait_window()
        return self.result_path


class DelayPromptApp:
    """
    `-d prompt`'s small popup: a single numeric field for the delay, in
    seconds - Enter (or the Start button) confirms, Escape (or Cancel, or
    the window close button) backs out of the WHOLE capture, matching
    every other "Escape backs out entirely" convention elsewhere in the
    app rather than just dismissing the popup and falling back to no
    delay. Reuses the same root+mainloop hand-off pattern as
    OpenPromptApp/GuiPromptApp: built into the SAME root main() already
    created, read out once mainloop() returns, then torn down before the
    real capture flow proceeds. Mainly useful for a single keybind bound
    to `-d prompt` that you want to reuse with a different delay length
    each time, rather than needing a separate fixed keybind per length.
    """

    def __init__(self, root, default_seconds):
        self.root = root
        self.result_seconds = None
        log.info(f"--delay prompt: opening picker, default {default_seconds:g}s")

        root.title(f"{WINDOW_TITLE_BASE} - delay")
        root.resizable(False, False)
        root.configure(bg="#1a1a1a")
        root.attributes("-topmost", True)
        self._app_icon_image = _load_app_icon()
        if self._app_icon_image is not None:
            root.iconphoto(True, self._app_icon_image)

        frame = tk.Frame(root, bg="#1a1a1a")
        frame.pack(padx=20, pady=16)
        tk.Label(frame, text="Delay (seconds):", fg="white", bg="#1a1a1a").pack(side="top", anchor="w", pady=(0, 6))

        default_text = str(int(default_seconds)) if default_seconds == int(default_seconds) else str(default_seconds)
        self.var = tk.StringVar(value=default_text)
        self.entry = tk.Entry(frame, textvariable=self.var, width=10, justify="center")
        self.entry.pack(side="top")
        self.entry.bind("<Return>", self._confirm)
        self.entry.bind("<Escape>", self._cancel)
        self.entry.focus_set()
        self.entry.select_range(0, "end")

        self.error_label = tk.Label(frame, text="", fg="#ff6666", bg="#1a1a1a")
        self.error_label.pack(side="top", pady=(4, 0))

        btn_row = tk.Frame(frame, bg="#1a1a1a")
        btn_row.pack(side="top", pady=(10, 0))
        tk.Button(btn_row, text="Start", pady=BUTTON_PADY, command=self._confirm).pack(side="left", padx=(0, 4))
        tk.Button(btn_row, text="Cancel", pady=BUTTON_PADY, command=self._cancel).pack(side="left")

        root.protocol("WM_DELETE_WINDOW", self._cancel)
        root.update_idletasks()
        try:
            sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
            w, h = root.winfo_reqwidth(), root.winfo_reqheight()
            root.geometry(f"+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 2)}")
        except tk.TclError:
            pass

    def _confirm(self, event=None):
        raw = self.var.get().strip()
        try:
            value = float(raw)
            if value < 0:
                raise ValueError
        except ValueError:
            self.error_label.config(text="Enter a number of seconds (0 or more)")
            return
        self.result_seconds = value
        self.root.quit()

    def _cancel(self, event=None):
        self.result_seconds = None
        self.root.quit()


