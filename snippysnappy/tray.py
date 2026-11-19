# snippysnappy v1.0.5
"""
Tray icon / control interface. One process per session runs it: the coordinator's hub (see coordinator.py).

  * the icon: a StatusNotifierItem (tray_sni.py) - works with any bar or panel that has a tray (XFCE, KDE, GNOME with the
    AppIndicator extension, waybar "tray", quickshell, ...). Left click focuses the last window you used, right click = menu.
  * a control socket ($XDG_RUNTIME_DIR/snippysnappy/tray.sock, JSON lines) used by the command line - so anything that can run
    a command can drive it, with or without a tray:
        snippysnappy --tray-json              the menu as JSON (quickshell, eww, scripts)
        snippysnappy --tray-status            {"text","tooltip","class"} for a waybar custom module
        snippysnappy --tray-menu [CMD]        pick an entry with fuzzel/wofi/rofi/bemenu/dmenu (or CMD) - bind it to a click
        snippysnappy --tray-do ID             run one entry by its id (ids are in --tray-json)
Config (config.py): TRAY_ENABLED (icon on/off; the control socket works either way), TRAY_LAUNCHER, TRAY_IPC.
"""
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from .logging_setup import log

_LAUNCHER = Path(__file__).resolve().parent.parent / "snippysnappy.py"
_ICON = Path(__file__).resolve().parent.parent / "snippysnappy-icon.png"

# id -> (label, command-line flags)
NEW_SHOT = [("shot", "Select region", []), ("shot-window", "Window", ["-w"]), ("shot-full", "Full screen", ["-f"]),
            None, ("shot-delay", "Region, after a delay", ["-d"]), ("shot-delay-ask", "Region, ask for the delay…", ["-d", "prompt"]),
            ("shot-window-delay", "Window, after a delay", ["-w", "-d"]), ("shot-full-delay", "Full screen, after a delay", ["-f", "-d"])]
NEW_REC = [("rec", "Select region", ["-r"]), ("rec-window", "Window", ["-r", "-w"]), ("rec-full", "Full screen", ["-r", "-f"]),
           None, ("rec-audio", "Region, choose audio…", ["-ra"]), ("rec-window-audio", "Window, choose audio…", ["-ra", "-w"]),
           ("rec-full-audio", "Full screen, choose audio…", ["-ra", "-f"]),
           ("rec-delay", "Region, after a delay", ["-r", "-d"])]
EDIT = [("edit-open", "Open an image…", ["-o", "prompt"]), ("edit-video", "Open a recording…", ["-ro", "prompt"]),
        ("edit-blank", "Blank canvas", ["-n"])]
FLAGS = {i[0]: i[2] for grp in (NEW_SHOT, NEW_REC, EDIT) for i in grp if i}


def launch(action_id, extra=()):
    """Start `snippysnappy <flags>` as its own detached process. True if the id is a 'start something' entry."""
    key = action_id[4:] if action_id.startswith("new:") else action_id
    if key not in FLAGS:
        return False
    flags = list(FLAGS[key]) + list(extra)
    try:
        subprocess.Popen([sys.executable, str(_LAUNCHER)] + flags, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        log.info(f"Tray: started 'snippysnappy {' '.join(flags)}'")
    except Exception:
        log.exception(f"Tray: could not start snippysnappy {flags}")
    return True


# ---------------------------------------------------------------- the model (runs in the hub)
def _all_entries():
    """[(client_id, entry), ...] for every window/film/pin/recorder of every snippysnappy process (this one included)."""
    from . import coordinator as c
    own = c._coord.get("client_id")
    out = [(own, e) for e in c._local_window_roster_entries()]
    for cid, entries in (c._coord.get("roster_cache") or {}).items():
        if cid != own:
            out += [(cid, e) for e in entries]
    return out


def build_tree():
    entries = _all_entries()
    tree, wins, films, pins, recs = [], [], [], [], []
    for cid, e in entries:
        {"window": wins, "film": films, "pin": pins, "recorder": recs}.get(e.get("kind", "window"), wins).append((cid, e))

    def hide(e, label):
        return f"{label}  (background)" if e.get("hidden") else label

    for cid, e in recs:
        tree.append({"id": f"stoprec:{cid}:{e['window_id']}", "label": "Stop recording"})
    for cid, e in wins:
        label = hide(e, e.get("label") or "Window")
        tabs = e.get("tab_labels") or []
        node = {"id": f"focus:{cid}:{e['window_id']}", "label": label}
        if len(tabs) > 1:
            node = {"label": f"{label}  [{len(tabs)} tabs]", "children": [{"id": node["id"], "label": "Show window"}, {"separator": True}] + [
                {"id": f"tab:{cid}:{e['window_id']}:{i}", "label": t or f"Tab {i + 1}"} for i, t in enumerate(tabs)]}
        tree.append(node)
    for cid, e in films:
        tree.append({"id": f"focus:{cid}:{e['window_id']}", "label": hide(e, "Film: " + (e.get("label") or "recording"))})
    if pins:
        tree.append({"label": f"Pins ({len(pins)})", "children": [{"id": f"focus:{cid}:{e['window_id']}", "label": e.get("label") or "Pin"} for cid, e in pins]})
    if tree:
        tree.append({"separator": True})

    def group(items):
        return [({"separator": True} if i is None else {"id": "new:" + i[0], "label": i[1]}) for i in items]

    rec_items = group(NEW_REC)
    if recs:      # a plain -r would stop the running recording: start another one next to it with --demo
        for n in rec_items:
            if n.get("id"):
                n["id"] = n["id"] + "+"
    tree += [{"label": "New screenshot", "children": group(NEW_SHOT)},
             {"label": "New recording", "children": rec_items},
             {"label": "Edit", "children": group(EDIT)}]
    if wins or films or pins:
        tree += [{"separator": True}, {"id": "closeall", "label": "Close all snippysnappy windows…"}]
    return tree


def static_tree():
    """The 'start something' entries only - what --tray-menu offers when no snippysnappy is running."""
    def group(items):
        return [({"separator": True} if i is None else {"id": "new:" + i[0], "label": i[1]}) for i in items]
    return [{"label": "New screenshot", "children": group(NEW_SHOT)}, {"label": "New recording", "children": group(NEW_REC)},
            {"label": "Edit", "children": group(EDIT)}]


def item_count():
    return len(_all_entries())


def last_active():
    """(client_id, window_id) of the window the user touched last: the last one in this process's list, else the first known."""
    from . import coordinator as c
    own = c._coord.get("client_id")
    mine = [e for e in c._local_window_roster_entries() if e.get("kind", "window") in ("window", "film")]
    if mine:
        return own, mine[-1]["window_id"]
    for cid, e in _all_entries():
        if e.get("kind", "window") in ("window", "film", "pin"):
            return cid, e["window_id"]
    return None


def do_action(action_id):
    from . import coordinator as c
    try:
        if launch(action_id.rstrip("+"), extra=(["--demo"] if action_id.endswith("+") else [])):
            return True
        kind, _, rest = action_id.partition(":")
        if kind in ("focus", "tab"):
            parts = rest.split(":")
            cid, wid = parts[0], parts[1]
            tab = int(parts[2]) if kind == "tab" else None
            if cid == c._coord.get("client_id"):
                target = c._find_local_window(wid) or c._find_local_pin(wid) or c._find_local_film(wid)
                if target is not None:
                    c.focus_local_window(target)
                    if tab is not None and hasattr(target, "switch_to_document"):
                        target.switch_to_document(tab)
            else:
                c.request_remote_focus(cid, wid, tab)
            return True
        if kind == "stoprec":
            cid, wid = rest.split(":")
            if cid == c._coord.get("client_id"):
                r = c._find_local_recorder(wid)
                if r is not None:
                    r._stop()
            else:
                c.request_remote_recorder_stop(cid, wid)
            return True
        if action_id == "closeall":
            _close_all()
            return True
        if action_id == "activate":
            t = last_active()
            if t:
                do_action(f"focus:{t[0]}:{t[1]}")
            else:
                launch("shot")
            return True
    except Exception:
        log.exception(f"Tray: action {action_id!r} failed (non-fatal)")
    return False


def _close_all():
    from . import coordinator as c
    entries = [(cid, e) for cid, e in _all_entries() if e.get("kind", "window") in ("window", "film", "pin")]
    if not entries:
        return
    try:
        from tkinter import messagebox
        if not messagebox.askyesno("snippysnappy", f"Close all {len(entries)} snippysnappy window(s), tabs and pins?\nUnsaved edits are lost.",
                                   parent=c._coord.get("root")):
            return
    except Exception:
        log.exception("Tray: could not ask for confirmation - not closing anything")
        return
    own = c._coord.get("client_id")
    for cid, e in entries:
        wid, kind = e["window_id"], e.get("kind", "window")
        if cid == own:
            t = c._find_local_window(wid) or c._find_local_film(wid) or c._find_local_pin(wid)
            if t is None:
                continue
            try:
                (t._cleanup if kind == "pin" else getattr(t, "_force_close", getattr(t, "_close_window", None)))()
            except Exception:
                log.exception("Tray: closing a window failed (non-fatal)")
        elif kind == "pin":
            c.request_remote_pin_close(cid, wid)
        else:
            c.request_remote_window_close(cid, wid)


# ---------------------------------------------------------------- the controller (icon + control socket)
def _sock_path():
    from .coordinator import SOCKET_DIR
    return str(SOCKET_DIR / "tray.sock")


class Tray:
    def __init__(self, root):
        from . import constants as k
        self.root = root
        self.enabled = bool(getattr(k, "TRAY_ENABLED", True))
        self.ipc = bool(getattr(k, "TRAY_IPC", True))
        self.show_ms = int(getattr(k, "TRAY_START_DELAY_MS", 700))
        self.hide_ms = int(getattr(k, "TRAY_HIDE_DELAY_MS", 1500))
        self.sni = None
        self.srv = None
        self.jobs = []
        self._seen_since = self._empty_since = None
        self._retry_at = 0.0
        self._stopped = False

    def start(self):
        if self.ipc:
            self._open_socket()
        self.root.after(100, self._fast)
        self.root.after(500, self._slow)

    def stop(self):
        self._stopped = True
        self._hide()
        if self.srv is not None:
            try:
                self.srv.close()
                os.unlink(_sock_path())
            except OSError:
                pass
            self.srv = None

    # ---- icon
    def _show(self):
        if self.sni is not None or not self.enabled or time.monotonic() < self._retry_at:
            return
        from .tray_sni import SniTray
        t = SniTray("snippysnappy", str(_ICON), lambda: do_action("activate"), do_action)
        try:
            t.start()
        except Exception:
            log.exception("Tray: could not start the tray icon (no D-Bus session bus?) - retrying in 30 s")
            self._retry_at = time.monotonic() + 30
            try:
                t.stop()
            except Exception:
                pass
            return
        t.refresh(build_tree())
        self.sni = t
        log.info("Tray: icon shown")

    def _hide(self):
        t, self.sni = self.sni, None
        if t is not None:
            t.stop()
            log.info("Tray: icon removed")

    # ---- loops
    def _fast(self):
        if self._stopped:
            return
        try:
            if self.sni is not None and not self.sni.pump():
                self.sni = None
                self._retry_at = time.monotonic() + 5
            self._serve()
        except Exception:
            log.exception("Tray: poll failed (non-fatal)")
        self.root.after(100, self._fast)

    def _slow(self):
        if self._stopped:
            return
        try:
            n = item_count()
            now = time.monotonic()
            if n:
                self._empty_since = None
                self._seen_since = self._seen_since or now
                if now - self._seen_since >= self.show_ms / 1000:
                    self._show()
            else:
                self._seen_since = None
                self._empty_since = self._empty_since or now
                if now - self._empty_since >= self.hide_ms / 1000:
                    self._hide()
            if self.sni is not None:
                self.sni.refresh(build_tree())
        except Exception:
            log.exception("Tray: refresh failed (non-fatal)")
        self.root.after(500, self._slow)

    # ---- control socket
    def _open_socket(self):
        path = _sock_path()
        try:
            os.unlink(path)
        except OSError:
            pass
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.bind(path)
            os.chmod(path, 0o600)
            s.listen(8)
            s.setblocking(False)
            self.srv = s
        except OSError:
            s.close()
            log.exception("Tray: could not open the control socket (non-fatal)")

    def _serve(self):
        if self.srv is None:
            return
        for _ in range(4):
            try:
                conn, _ = self.srv.accept()
            except (BlockingIOError, OSError):
                return
            try:
                conn.settimeout(1.0)
                data = b""
                while not data.endswith(b"\n") and len(data) < 65536:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                conn.sendall((json.dumps(self._handle(json.loads(data.decode() or "{}"))) + "\n").encode())
            except Exception:
                log.exception("Tray: control request failed (non-fatal)")
            finally:
                conn.close()

    def _handle(self, req):
        cmd = req.get("cmd")
        if cmd == "tree":
            return {"ok": True, "tree": build_tree()}
        if cmd == "status":
            return {"ok": True, **status()}
        if cmd == "do":
            return {"ok": bool(do_action(str(req.get("id", ""))))}
        if cmd == "ping":
            return {"ok": True}
        return {"ok": False, "error": "unknown command"}


def status():
    ents = _all_entries()
    w = sum(1 for _, e in ents if e.get("kind", "window") in ("window", "film"))
    p = sum(1 for _, e in ents if e.get("kind") == "pin")
    r = sum(1 for _, e in ents if e.get("kind") == "recorder")
    bits = [f"{w} window(s)"] + ([f"{p} pin(s)"] if p else []) + (["recording"] if r else [])
    return {"text": "" if not ents else str(w + p), "tooltip": "snippysnappy: " + ", ".join(bits) if ents else "snippysnappy: nothing open",
            "class": "recording" if r else ("active" if ents else "idle"), "alt": "recording" if r else ("active" if ents else "idle")}


_tray = None


def start_tray(root):
    """Called by the coordinator when this process becomes the hub."""
    global _tray
    if _tray is not None:
        return
    from . import constants as k
    if not (getattr(k, "TRAY_ENABLED", True) or getattr(k, "TRAY_IPC", True)):
        return
    _tray = Tray(root)
    _tray.start()


def stop_tray():
    global _tray
    if _tray is not None:
        _tray.stop()
        _tray = None
