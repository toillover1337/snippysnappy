# snippysnappy v1.0.0
"""Small dialogs shared by the image windows and the film window."""
import tkinter as tk

from . import constants
from .constants import BUTTON_PADY
from .logging_setup import log
from .user_config import set_user_config_value


_SESSION_SKIP = {"discard": False}   # "Don't ask again this session": lives only as long as this process


def confirm_discard(parent, message, on_confirm, title="Discard?", confirm_label="Discard", cancel_label="Keep"):
    """
    Asks before a discard. Skipped entirely (on_confirm runs immediately)
    when CONFIRM_DISCARD is False in the config, or after "Don't ask again
    this session" was ticked and confirmed.
    """
    if not getattr(constants, "CONFIRM_DISCARD", True) or _SESSION_SKIP["discard"]:
        on_confirm()
        return
    dialog = tk.Toplevel(parent)
    dialog.title(title)
    dialog.transient(parent)
    dialog.configure(bg="#222222")
    dialog.resizable(False, False)
    tk.Label(dialog, justify="left", fg="white", bg="#222222", text=message, wraplength=300).pack(anchor="w", padx=16, pady=(14, 8))
    skip_var = tk.IntVar(value=0)
    tk.Checkbutton(
        dialog, text="Don't ask again this session", variable=skip_var, bg="#222222", fg="white",
        selectcolor="#444444", activebackground="#222222", activeforeground="white",
    ).pack(anchor="w", padx=16, pady=(0, 8))

    def confirm():
        if skip_var.get():
            _SESSION_SKIP["discard"] = True
            log.info("Discard confirmation skipped for the rest of this session (set CONFIRM_DISCARD = False in the config to disable it for good)")
        dialog.destroy()
        on_confirm()

    btn_row = tk.Frame(dialog, bg="#222222")
    btn_row.pack(padx=16, pady=(0, 14))
    tk.Button(btn_row, text=confirm_label, pady=BUTTON_PADY, command=confirm).pack(side="left", padx=(0, 6))
    tk.Button(btn_row, text=cancel_label, pady=BUTTON_PADY, command=dialog.destroy).pack(side="left")
    dialog.bind("<Escape>", lambda e: dialog.destroy())
    dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
    dialog.update_idletasks()
    try:
        x = parent.winfo_rootx() + max(0, (parent.winfo_width() - dialog.winfo_reqwidth()) // 2)
        y = parent.winfo_rooty() + max(0, (parent.winfo_height() - dialog.winfo_reqheight()) // 2)
        dialog.geometry(f"+{x}+{y}")
    except tk.TclError:
        pass
    dialog.grab_set()
