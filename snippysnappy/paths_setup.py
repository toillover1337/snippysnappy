# snippysnappy v1.0.1
"""Save-folder defaults, legacy-folder migration prompts and the sloppy-folder sweep.
"""
import re
import shutil
import time
import tkinter as tk
from tkinter import filedialog
from pathlib import Path

from . import constants
from ._paths import INSTALL_DIR
from .logging_setup import log
from .user_config import CONFIG_PATH, set_user_config_value

_HOME = Path.home()

# key -> (new constant, legacy constant, legacy folder, permanent?, label)
DIRS = {
    "snaps": ("SNIPPYSNAPS_DIR", "SCREENSHOT_DIR", _HOME / "Pictures" / "Screenshots", True, "Snippysnaps - saved screenshots"),
    "sloppysnaps": ("SLOPPY_SNAPS_DIR", "SLOP_DIR", _HOME / "Pictures" / "Slop-o-graphs", False, "Sloppysnaps - quick-saved screenshots"),
    "text": ("SNIPPYTEXT_DIR", "SNIPTEXT_DIR", INSTALL_DIR / "sniptext", True, "Snippytext - saved OCR text"),
    "sloppytext": ("SLOPPY_TEXT_DIR", "TEXT_DIR", None, False, "Sloppytext - quick-saved OCR text"),
    "films": ("SNIPPYFILMS_DIR", None, None, True, "Snippyfilms - saved recordings"),
    "sloppyfilms": ("SLOPPY_FILMS_DIR", None, None, False, "Sloppyfilms - temporary recordings"),
}
_SESSION = {}    # key -> Path chosen for this session only (dialog closed without answering)
_SWEEP = {"scheduled": False}
_STARTUP = {"running": False}


def _as_path(value):
    return Path(str(value)).expanduser()


def _has_files(d):
    try:
        return d.is_dir() and any(d.iterdir())
    except OSError:
        return False


def _config_names():
    try:
        text = CONFIG_PATH.read_text(encoding="utf-8")
    except Exception:
        return set()
    return set(re.findall(r"^[ \t]*([A-Za-z_][A-Za-z0-9_]*)[ \t]*=", text, re.MULTILINE))


def _default(key):
    return _as_path(getattr(constants, DIRS[key][0]))


def _effective(key):
    if key in _SESSION:
        return _SESSION[key]
    new_name, legacy_name, legacy_path, _perm, _label = DIRS[key]
    names = _config_names()
    if new_name in names:
        return _as_path(getattr(constants, new_name))
    if legacy_name and legacy_name in names:
        return _as_path(getattr(constants, legacy_name))
    default = _default(key)
    if default.is_dir():
        return default
    if legacy_path is not None and _has_files(legacy_path):
        return legacy_path
    return default


def get_dir(key, create=True):
    path = _effective(key)
    if create:
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError:
            log.exception(f"Folder setup: could not create {path}")
    return path


def needs_prompt(key):
    new_name, legacy_name, legacy_path, _perm, _label = DIRS[key]
    if legacy_path is None or key in _SESSION:
        return False
    names = _config_names()
    if new_name in names or (legacy_name and legacy_name in names):
        return False
    if _default(key).is_dir():
        return False
    try:
        return legacy_path.is_dir() and any(legacy_path.iterdir())
    except OSError:
        return False


def _move_files(src, dst):
    moved = skipped = 0
    dst.mkdir(parents=True, exist_ok=True)
    for f in list(src.iterdir()):
        try:
            if f.is_symlink() or not f.is_file():
                continue
            target = dst / f.name
            if target.exists():
                skipped += 1
                continue
            shutil.move(str(f), str(target))
            moved += 1
        except OSError:
            log.exception(f"Folder setup: could not move {f}")
    log.info(f"Folder setup: moved {moved} file(s) from {src} to {dst} ({skipped} skipped, name already existed)")


def _ask(parent, key):
    new_name, legacy_name, legacy_path, permanent, label = DIRS[key]
    default = _default(key)
    days = getattr(constants, "SLOPPY_CLEANUP_DAYS", None)
    bg, fg = "#222222", "white"
    dlg = tk.Toplevel(parent)
    dlg.title("snippysnappy - folder setup")
    dlg.configure(bg=bg)
    dlg.resizable(False, False)
    try:
        if parent.winfo_viewable():
            dlg.transient(parent)
    except tk.TclError:
        pass
    dlg.attributes("-topmost", True)
    result = {"path": None, "move": False}
    text = (
        f"{label}\n\nAn older folder was found:\n  {legacy_path}\n\nThe new default folder is:\n  {default}\n\n"
        "Which one should snippysnappy use? Your choice is saved to your config and you will not be asked again."
    )
    tk.Label(dlg, text=text, justify="left", anchor="w", fg=fg, bg=bg, wraplength=480).pack(anchor="w", padx=16, pady=(14, 8))
    choice = tk.StringVar(value="new")
    custom = {"path": None}
    custom_var = tk.StringVar(value="")
    radio = dict(bg=bg, fg=fg, selectcolor="#444444", activebackground=bg, activeforeground=fg, anchor="w", justify="left")
    tk.Radiobutton(dlg, text="Use the new default folder", variable=choice, value="new", **radio).pack(fill="x", padx=16)
    tk.Radiobutton(dlg, text="Keep using the older folder", variable=choice, value="old", **radio).pack(fill="x", padx=16)
    row = tk.Frame(dlg, bg=bg)
    row.pack(fill="x", padx=16)
    tk.Radiobutton(row, text="Use a different folder:", variable=choice, value="custom", **radio).pack(side="left")

    def browse():
        start = default.parent if default.parent.is_dir() else _HOME
        picked = filedialog.askdirectory(parent=dlg, initialdir=str(start), title="Choose a folder")
        if picked:
            custom["path"] = Path(picked)
            custom_var.set(picked)
            choice.set("custom")

    tk.Button(row, text="Browse...", command=browse).pack(side="right")
    tk.Label(row, textvariable=custom_var, fg="#9ad0ff", bg=bg, wraplength=240, justify="left").pack(side="left", padx=6)
    move_var = tk.IntVar(value=0)
    if permanent:
        tk.Checkbutton(
            dlg, text="Also move the existing files from the older folder into the new default (only applies if you pick the new default)",
            variable=move_var, bg=bg, fg=fg, selectcolor="#444444", activebackground=bg, activeforeground=fg,
            wraplength=460, justify="left", anchor="w",
        ).pack(anchor="w", padx=16, pady=(8, 0))
    elif days:
        tk.Label(
            dlg, fg="#ffb347", bg=bg, wraplength=460, justify="left",
            text=f"Heads up: files in this folder older than {days} day(s) are deleted automatically. Pick a different folder if you want to keep everything.",
        ).pack(anchor="w", padx=16, pady=(8, 0))

    def ok():
        c = choice.get()
        if c == "custom" and custom["path"] is None:
            browse()
            return
        result["path"] = default if c == "new" else legacy_path if c == "old" else custom["path"]
        result["move"] = bool(permanent and c == "new" and move_var.get())
        dlg.destroy()

    tk.Button(dlg, text="OK", command=ok, width=10).pack(pady=12)
    dlg.protocol("WM_DELETE_WINDOW", dlg.destroy)
    try:
        dlg.update_idletasks()
        w, h = dlg.winfo_reqwidth(), dlg.winfo_reqheight()
        x = parent.winfo_rootx() + max(0, (parent.winfo_width() - w) // 2)
        y = parent.winfo_rooty() + max(0, (parent.winfo_height() - h) // 3)
        dlg.geometry(f"+{x}+{y}")
        dlg.focus_force()
        dlg.grab_set()
    except tk.TclError:
        pass
    dlg.wait_window()
    return result


def _prompt_and_apply(parent, key):
    new_name, legacy_name, legacy_path, permanent, label = DIRS[key]
    result = _ask(parent, key)
    if result["path"] is None:
        _SESSION[key] = legacy_path
        log.info(f"Folder setup: {new_name} dialog closed without an answer - using {legacy_path} for this session only")
        return
    chosen = Path(result["path"])
    try:
        chosen.mkdir(parents=True, exist_ok=True)
    except OSError:
        log.exception(f"Folder setup: could not create {chosen}")
    if result["move"]:
        _move_files(legacy_path, chosen)
    set_user_config_value(new_name, str(chosen))
    setattr(constants, new_name, str(chosen))
    log.info(f"Folder setup: {new_name} -> {chosen} (saved to config)")


def sweep_sloppy():
    days = getattr(constants, "SLOPPY_CLEANUP_DAYS", 2)
    if not days or days <= 0:
        return 0
    cutoff = time.time() - days * 86400
    protected = set()
    for p in (_HOME, Path("/"), Path("/tmp"), Path("/var/tmp"), _HOME / "Pictures", _HOME / "Videos", _HOME / "Documents", _HOME / "Desktop", _HOME / "Downloads"):
        try:
            protected.add(p.resolve())
        except OSError:
            pass
    for k, spec in DIRS.items():
        if spec[3]:
            try:
                protected.add(get_dir(k, create=False).resolve())
            except OSError:
                pass
    removed = 0
    for k, spec in DIRS.items():
        if spec[3]:
            continue
        d = get_dir(k, create=False)
        try:
            rd = d.resolve()
        except OSError:
            continue
        if rd in protected:
            log.warning(f"Folder setup: not sweeping {d} (protected location)")
            continue
        if not d.is_dir():
            continue
        for f in d.iterdir():
            try:
                if f.is_symlink() or not f.is_file():
                    continue
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    removed += 1
            except OSError:
                log.exception(f"Folder setup: could not remove {f}")
    if removed:
        log.info(f"Folder setup: sloppy sweep removed {removed} file(s) older than {days} day(s)")
    return removed


def _schedule_sweep(root):
    if _SWEEP["scheduled"]:
        return
    _SWEEP["scheduled"] = True
    minutes = getattr(constants, "SLOPPY_CLEANUP_INTERVAL_MINUTES", 60) or 60
    delay = int(minutes * 60000)

    def tick():
        try:
            sweep_sloppy()
            root.after(delay, tick)
        except tk.TclError:
            pass
        except Exception:
            log.exception("Folder setup: periodic sweep failed (non-fatal)")

    root.after(delay, tick)


def startup(app, tries=0):
    root = app.root
    try:
        if not root.winfo_exists():
            return
        if not root.winfo_viewable() and tries < 20:
            root.after(300, lambda: startup(app, tries + 1))
            return
    except tk.TclError:
        return
    if _STARTUP["running"]:
        return
    _STARTUP["running"] = True
    try:
        pending = [k for k in DIRS if needs_prompt(k)]
        log.debug(f"Folder setup: startup check from {type(app).__name__}, {len(pending)} folder(s) need a decision")
        for key in pending:
            if needs_prompt(key):
                _prompt_and_apply(root, key)
    except Exception:
        log.exception("Folder setup: prompt failed (non-fatal)")
    finally:
        _STARTUP["running"] = False
    try:
        sweep_sloppy()
        _schedule_sweep(root)
    except Exception:
        log.exception("Folder setup: sweep failed (non-fatal)")
