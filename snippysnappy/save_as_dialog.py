# snippysnappy v1.0.1
"""
Save As dialog shared by image and film windows: a file-type dropdown, a
folder box with fuzzy directory autocomplete (directories only, never
files), and a filename box. The caller supplies the type list, so the same
dialog offers image types for an image and film types for a film.
"""
import os
import tkinter as tk
from pathlib import Path
from tkinter import messagebox

from .constants import BUTTON_PADY
from .logging_setup import log


def _fuzzy_rank(fragment, name):
    """None = no match; lower is better: 1 prefix, 2 substring, 3 in-order subsequence."""
    f, n = fragment.lower(), name.lower()
    if not f:
        return 0
    if n.startswith(f):
        return 1
    if f in n:
        return 2
    it = iter(n)
    if all(ch in it for ch in f):
        return 3
    return None


def suggest_dirs(text, limit=8):
    """Directories matching what's been typed so far, as full paths. Hidden dirs only if the fragment starts with '.'."""
    text = os.path.expanduser(text or "")
    if text.endswith(os.sep):
        parent, fragment = text, ""
    else:
        parent, fragment = os.path.split(text)
    if not parent:
        parent = str(Path.home())
    names = []
    try:
        with os.scandir(parent) as it:
            for e in it:
                try:
                    if e.is_dir():
                        names.append(e.name)
                except OSError:
                    continue
    except OSError:
        return []
    ranked = []
    for name in names:
        if name.startswith(".") and not fragment.startswith("."):
            continue
        rank = _fuzzy_rank(fragment, name)
        if rank is not None:
            ranked.append((rank, name.lower(), name))
    ranked.sort()
    return [os.path.join(parent, n) for _r, _l, n in ranked[:limit]]


def ask_save_as(parent, title, type_options, default_dir, default_name, on_confirm):
    """type_options: [(ext, label), ...]. Calls on_confirm(directory: Path, filename: str, ext: str) once confirmed."""
    dlg = tk.Toplevel(parent)
    dlg.title(title)
    dlg.transient(parent)
    dlg.configure(bg="#222222")
    dlg.resizable(False, False)
    label_kw = dict(fg="white", bg="#222222")

    type_row = tk.Frame(dlg, bg="#222222")
    type_row.pack(fill="x", padx=12, pady=(12, 4))
    tk.Label(type_row, text="Type", width=7, anchor="w", **label_kw).pack(side="left")
    labels = [label for _e, label in type_options]
    ext_by_label = {label: ext for ext, label in type_options}
    known_exts = {ext for ext, _l in type_options}
    type_var = tk.StringVar(value=labels[0])
    tk.OptionMenu(type_row, type_var, *labels).pack(side="left")

    folder_row = tk.Frame(dlg, bg="#222222")
    folder_row.pack(fill="x", padx=12, pady=4)
    tk.Label(folder_row, text="Folder", width=7, anchor="w", **label_kw).pack(side="left")
    folder_var = tk.StringVar(value=str(default_dir).rstrip("/") + "/")
    folder_entry = tk.Entry(folder_row, textvariable=folder_var, width=52)
    folder_entry.pack(side="left")

    suggest_box = tk.Listbox(dlg, height=6, width=60, activestyle="dotbox", exportselection=False)
    suggestions = []

    name_row = tk.Frame(dlg, bg="#222222")
    name_row.pack(fill="x", padx=12, pady=4)
    tk.Label(name_row, text="Name", width=7, anchor="w", **label_kw).pack(side="left")
    name_var = tk.StringVar(value=default_name)
    name_entry = tk.Entry(name_row, textvariable=name_var, width=52)
    name_entry.pack(side="left")

    status_var = tk.StringVar(value="")
    tk.Label(dlg, textvariable=status_var, fg="#ff8888", bg="#222222", anchor="w").pack(fill="x", padx=12)

    def refresh_suggestions(event=None):
        nonlocal suggestions
        suggestions = suggest_dirs(folder_var.get())
        suggest_box.delete(0, "end")
        if suggestions:
            for path in suggestions:
                suggest_box.insert("end", path)
            suggest_box.configure(height=min(6, len(suggestions)))
            if not suggest_box.winfo_ismapped():
                suggest_box.pack(after=folder_row, fill="x", padx=12, pady=(0, 4))
        else:
            suggest_box.pack_forget()

    def accept(path):
        folder_var.set(path.rstrip("/") + "/")
        folder_entry.icursor("end")
        folder_entry.focus_set()
        refresh_suggestions()

    def on_folder_key(event):
        if event.keysym in ("Up", "Down", "Tab", "Return", "Escape", "Left", "Right",
                            "Shift_L", "Shift_R", "Control_L", "Control_R", "ISO_Left_Tab"):
            return
        status_var.set("")
        refresh_suggestions()

    def on_folder_down(event):
        if suggestions:
            suggest_box.focus_set()
            suggest_box.selection_clear(0, "end")
            suggest_box.selection_set(0)
            suggest_box.activate(0)
        return "break"

    def on_folder_tab(event):
        if suggestions:
            accept(suggestions[0])
        else:
            name_entry.focus_set()
        return "break"

    def on_folder_return(event):
        name_entry.focus_set()
        return "break"

    def on_box_accept(event=None):
        sel = suggest_box.curselection()
        if sel:
            accept(suggestions[sel[0]])
        return "break"

    def on_box_up(event):
        sel = suggest_box.curselection()
        if not sel or sel[0] == 0:
            folder_entry.focus_set()
            return "break"
        return None

    folder_entry.bind("<KeyRelease>", on_folder_key)
    folder_entry.bind("<Down>", on_folder_down)
    folder_entry.bind("<Tab>", on_folder_tab)
    folder_entry.bind("<Return>", on_folder_return)
    suggest_box.bind("<Return>", on_box_accept)
    suggest_box.bind("<ButtonRelease-1>", on_box_accept)
    suggest_box.bind("<Up>", on_box_up)

    def confirm(event=None):
        ext = ext_by_label[type_var.get()]
        folder = Path(os.path.expanduser(folder_var.get().strip() or "."))
        if not folder.is_dir():
            status_var.set("That folder does not exist.")
            return
        name = Path(name_var.get().strip() or default_name).name
        suffix = Path(name).suffix.lower().lstrip(".")
        if suffix in known_exts:
            name = Path(name).stem
        name = f"{name}.{ext}"
        if (folder / name).exists() and not messagebox.askyesno("Overwrite?", f"{name} already exists. Overwrite it?", parent=dlg):
            return
        log.info(f"Save As: {folder / name} (type {ext})")
        dlg.destroy()
        on_confirm(folder, name, ext)

    name_entry.bind("<Return>", confirm)
    btn_row = tk.Frame(dlg, bg="#222222")
    btn_row.pack(padx=12, pady=(6, 12))
    tk.Button(btn_row, text="Save", pady=BUTTON_PADY, command=confirm).pack(side="left", padx=(0, 6))
    tk.Button(btn_row, text="Cancel", pady=BUTTON_PADY, command=dlg.destroy).pack(side="left")
    dlg.bind("<Escape>", lambda e: dlg.destroy())
    dlg.protocol("WM_DELETE_WINDOW", dlg.destroy)
    dlg.update_idletasks()
    try:
        x = parent.winfo_rootx() + max(0, (parent.winfo_width() - dlg.winfo_reqwidth()) // 2)
        y = parent.winfo_rooty() + max(0, (parent.winfo_height() - dlg.winfo_reqheight()) // 2)
        dlg.geometry(f"+{x}+{y}")
    except tk.TclError:
        pass
    dlg.grab_set()
    folder_entry.focus_set()
    folder_entry.icursor("end")
