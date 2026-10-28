# snippysnappy v1.0.3
"""Audio capture helpers: source discovery, the --audio prompt window, and per-recording audio choices."""
import subprocess
import tkinter as tk

from . import constants
from .constants import WINDOW_TITLE_BASE, BUTTON_PADY
from .icon import _load_app_icon
from .logging_setup import log

MODES = ("off", "desktop", "mic", "both")


def _pactl(*args):
    try:
        r = subprocess.run(["pactl", *args], capture_output=True, text=True, timeout=5, check=True)
        return r.stdout
    except Exception:
        log.exception(f"Audio: pactl {' '.join(args)} failed")
        return ""


def list_sources():
    """[(source_name, is_monitor), ...] for every PulseAudio/PipeWire source."""
    out = []
    for line in _pactl("list", "short", "sources").splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            out.append((parts[1], parts[1].endswith(".monitor")))
    return out


def default_desktop():
    sink = _pactl("get-default-sink").strip()
    return (sink + ".monitor") if sink else None


def default_mic():
    return _pactl("get-default-source").strip() or None


def effective_desktop():
    return getattr(constants, "AUDIO_DESKTOP_DEVICE", None) or default_desktop()


def effective_mic():
    return getattr(constants, "AUDIO_MIC_DEVICE", None) or default_mic()


def resolve_inputs(spec=None):
    """[(label, device), ...] for the chosen mode. spec: {"mode", "desktop", "mic"} or None (config only)."""
    spec = spec or {}
    mode = spec.get("mode") or getattr(constants, "RECORD_AUDIO", "off")
    if mode not in MODES:
        log.warning(f"Audio: unknown RECORD_AUDIO {mode!r} - treating it as 'off'")
        mode = "off"
    inputs = []
    if mode in ("desktop", "both"):
        dev = spec.get("desktop") or effective_desktop()
        if dev:
            inputs.append(("desktop", dev))
        else:
            log.warning("Audio: no desktop audio device found - recording without it")
    if mode in ("mic", "both"):
        dev = spec.get("mic") or effective_mic()
        if dev:
            inputs.append(("mic", dev))
        else:
            log.warning("Audio: no microphone found - recording without it")
    log.info(f"Audio: mode={mode} inputs={inputs}")
    return inputs


def prompt_audio_choice():
    """Small window for --audio. Returns {"mode", "desktop", "mic"} (device None = default) or None if cancelled."""
    root = tk.Tk(className="snippysnappy-audio")
    root.title(f"{WINDOW_TITLE_BASE} - record audio")
    root.configure(bg="#1a1a1a")
    root.resizable(False, False)
    root.attributes("-topmost", True)
    icon = _load_app_icon()
    root._snippy_icon = icon
    if icon is not None:
        root.iconphoto(True, icon)

    sources = list_sources()
    desktop_names = [n for n, mon in sources if mon]
    mic_names = [n for n, mon in sources if not mon]
    desktop_default = f"Default ({effective_desktop() or 'none found'})"
    mic_default = f"Default ({effective_mic() or 'none found'})"
    desktop_map = {desktop_default: None, **{n: n for n in desktop_names}}
    mic_map = {mic_default: None, **{n: n for n in mic_names}}

    mode_var = tk.StringVar(value=getattr(constants, "RECORD_AUDIO", "off") if getattr(constants, "RECORD_AUDIO", "off") in MODES else "off")
    desktop_var = tk.StringVar(value=desktop_default)
    mic_var = tk.StringVar(value=mic_default)

    grid = tk.Frame(root, bg="#1a1a1a")
    grid.pack(padx=14, pady=(14, 6))
    menus = {}
    for row, (label, var, choices) in enumerate([
        ("Record:", mode_var, list(MODES)),
        ("Desktop audio:", desktop_var, list(desktop_map)),
        ("Microphone:", mic_var, list(mic_map)),
    ]):
        tk.Label(grid, text=label, fg="white", bg="#1a1a1a").grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        menu = tk.OptionMenu(grid, var, *choices, command=lambda _v=None: refresh())
        menu.config(anchor="w", width=48 if row else 10)
        menu.grid(row=row, column=1, sticky="w", pady=4)
        menus[row] = menu

    def refresh():
        mode = mode_var.get()
        menus[1].config(state="normal" if mode in ("desktop", "both") else "disabled")
        menus[2].config(state="normal" if mode in ("mic", "both") else "disabled")

    result = {"value": None}

    def ok(event=None):
        result["value"] = {"mode": mode_var.get(), "desktop": desktop_map.get(desktop_var.get()), "mic": mic_map.get(mic_var.get())}
        root.quit()

    def cancel(event=None):
        root.quit()

    btns = tk.Frame(root, bg="#1a1a1a")
    btns.pack(pady=(4, 14))
    tk.Button(btns, text="Record", pady=BUTTON_PADY, command=ok).pack(side="left", padx=6)
    tk.Button(btns, text="Cancel", pady=BUTTON_PADY, command=cancel).pack(side="left", padx=6)
    root.protocol("WM_DELETE_WINDOW", cancel)
    root.bind("<Return>", ok)
    root.bind("<Escape>", cancel)
    refresh()
    root.update_idletasks()
    try:
        from .monitor_pick import _find
        _mons = _find("get_monitors")()
        _px, _py = _find("get_mouse_position")()
        _n, _mx, _my, _mw, _mh = _find("monitor_at_point")(_mons, _px, _py)
    except Exception:
        log.exception("Audio prompt: could not find the pointer monitor - centring on the whole screen (non-fatal)")
        _mx, _my, _mw, _mh = 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"+{_mx + max(0, (_mw - root.winfo_reqwidth()) // 2)}+{_my + max(0, (_mh - root.winfo_reqheight()) // 3)}")
    root.focus_force()
    root.mainloop()
    try:
        root.destroy()
    except tk.TclError:
        pass
    log.info(f"Audio prompt result: {result['value']}")
    return result["value"]
