# snippysnappy v1.0.3
"""Cross-process window/pin coordination: a same-user Unix-socket hub/client protocol that lets 'Merge in' and the Pins list reach windows opened by a SEPARATE snippysnappy invocation, plus the local (same-process) window/pin registries those two features also share."""
import os
import uuid
from pathlib import Path

from .logging_setup import log
from .capture import BACKEND, _X11FocusHelper

# ---- local (same-process) window registry ----
# ---- local (same-process) window registry ----
_LOCAL_WINDOWS = []

def _register_window(app):
    _LOCAL_WINDOWS.append(app)
    broadcast_window_roster()

def _unregister_window(app):
    if app in _LOCAL_WINDOWS:
        _LOCAL_WINDOWS.remove(app)
    broadcast_window_roster()


# ---- local (same-process) PIN registry ----
_LOCAL_PINS = []

class _PinHandle:
    # A pin is always meant to float on top of everything else - see
    # focus_local_window's use of this, which otherwise (matching what
    # every OTHER kind of window here wants) turns -topmost back off a
    # moment after focusing.
    _steady_state_topmost = True

    def __init__(self, pin_id, root, label, cleanup):
        self.window_id = pin_id   # same field name as SelectorApp, so focus_local_window() needs no special-casing
        self.root = root
        self._label = label
        self._cleanup = cleanup

    def _document_label(self):
        return self._label

    def _ensure_documents_initialized(self):
        pass


def _register_pin(pin_handle):
    _LOCAL_PINS.append(pin_handle)
    broadcast_window_roster()


def _unregister_pin(pin_handle):
    if pin_handle in _LOCAL_PINS:
        _LOCAL_PINS.remove(pin_handle)
    broadcast_window_roster()



# ---- cross-process window coordination ----
#
# Lets "Merge in" (see merge_window_in) reach windows opened by a SEPARATE
# snippysnappy invocation, not just other windows in this same process
# (see _LOCAL_WINDOWS). Deliberately NOT a persistent daemon - nothing is
# installed, nothing keeps running once every snippysnappy window has
# closed:
#
#   - The FIRST snippysnappy process in a session tries to bind a Unix
#     domain socket under $XDG_RUNTIME_DIR/snippysnappy/coord.sock and,
#     if that succeeds, becomes the "hub" for the rest of this session -
#     it's still just that one ordinary window process, now also
#     listening on one more file descriptor.
#   - Every LATER invocation finds that socket already bound and connects
#     to it as a "client" instead, for as long as it has any window open.
#   - The hub is a simple star-topology relay: each client tells it
#     "here are my windows" whenever that changes, the hub rebroadcasts
#     the combined roster to everyone (including its own local windows),
#     and a merge request/response/ack round-trip is relayed through it
#     between whichever two processes are actually involved.
#   - If the hub process exits (its window closes, or it crashes), any
#     client that notices its connection drop retries after a short
#     delay and may become the new hub itself - reconnecting every
#     PREVIOUSLY connected client to each other again isn't guaranteed
#     (there's no persistent registry outside this one hub's memory), but
#     new activity after that point works normally again.
#   - All of this only activates the moment two or more windows actually
#     exist; a single plain screenshot session never binds, connects, or
#     sends anything.
#
# Everything sent over the socket is a length-prefixed pickle of a plain
# dict - the only "client" of this socket is ever another copy of this
# same script running as the same user (the socket directory is created
# mode 0700), so this is the same trust boundary as the OS clipboard or
# any other same-user IPC, not something exposed to other users or the
# network.

import socket as _socket
import struct as _struct
import time
import pickle as _pickle

_RUNTIME_DIR = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/snippysnappy-run-{os.getuid()}"   # _p40_installed
SOCKET_DIR = Path(_RUNTIME_DIR) / "snippysnappy"


def _p40_prepare_socket_dir():
    """
    Creates SOCKET_DIR (and the /tmp fallback parent when XDG_RUNTIME_DIR is
    unset) and refuses to use it unless it is a real directory (not a
    symlink) owned by this user with no group/other access. Raises
    OSError otherwise. /tmp is shared with other users, so without these
    checks someone else could pre-create the fallback folder.
    """
    import stat
    uid = os.getuid()
    dirs = [SOCKET_DIR]
    if not os.environ.get("XDG_RUNTIME_DIR"):
        if not _coord.get("p40_warned"):
            _coord["p40_warned"] = True
            log.warning(
                f"Coordinator: XDG_RUNTIME_DIR is not set - using {_RUNTIME_DIR} for the window socket instead. "
                "Everything still works; set XDG_RUNTIME_DIR (elogind/systemd-logind or your session script does this) to silence this."
            )
        dirs.insert(0, Path(_RUNTIME_DIR))
    for d in dirs:
        try:
            os.mkdir(d, 0o700)
        except FileExistsError:
            pass
        st = os.lstat(d)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != uid:
            raise OSError(f"{d} is not a directory owned by uid {uid} - refusing to use it")
        if st.st_mode & 0o077:
            os.chmod(d, 0o700)
SOCKET_PATH = SOCKET_DIR / "coord.sock"

_coord = {
    "root": None,
    "role": None,           # None | "hub" | "client"
    "client_id": None,      # this process's own id ("hub" if we ARE the hub)
    "server_socket": None,  # hub only
    "hub_conn": None,       # client only: the socket connected to the hub
    "clients": {},          # hub only: {client_id: socket}, populated once "hello" arrives
    "hub_accepted": {},     # hub only: {fileno: socket}, EVERY accepted connection, hello or not
    "readers": {},          # per-socket _FramedReader, keyed by fileno
    "roster": {},           # hub only: {client_id: [window entry, ...]} for every OTHER client
    "roster_cache": {},     # everyone: the last full roster received/computed, {client_id: [...]}
    "poll_job": None,       # root.after() id for the polling loop, so it can be cancelled cleanly
    "restart_handler": None,   # set via set_restart_handler by whichever module hosts this coordinator
    "hub_version_warned": False,   # client only: logged the hub-version mismatch warning once already
}

# How often we poll the coordinator sockets for incoming data. We used to
# rely on root.tk.createfilehandler() to wake us the instant a socket
# became readable.
COORD_POLL_INTERVAL_MS = 100

# Bumped whenever a message TYPE is added/changed (not for unrelated
# feature work) - included in "hello" (client -> hub) and "roster" (hub
# -> client) so a version mismatch is a loud, logged warning instead of
# a silently-dropped message.Only catches FUTURE drift - a hub old enough
# to predate this very check has no way to report a version it doesn't
# know exists.
PROTOCOL_VERSION = 4   # bumped: recorder_stop_request/window_close_request added


# ---- patch 42: hardening (allowlisted unpickling, peer uid check, size cap) ----
MAX_MESSAGE_BYTES = 1 << 30      # a merge of many large tabs stays far below this; anything bigger is refused
_ALLOWED_GLOBALS = {
    ("PIL.Image", "Image"),
    ("pathlib", "PosixPath"), ("pathlib", "PurePosixPath"), ("pathlib", "Path"),
    ("pathlib._local", "PosixPath"), ("pathlib._local", "PurePosixPath"), ("pathlib._local", "Path"),
    ("builtins", "set"), ("builtins", "frozenset"), ("builtins", "bytearray"), ("builtins", "complex"),
    ("collections", "OrderedDict"), ("copyreg", "_reconstructor"), ("builtins", "object"),
}


class _SafeUnpickler(_pickle.Unpickler):
    """Only plain data, PIL images and paths - the only things snippysnappy ever sends or stores."""

    def find_class(self, module, name):
        if (module, name) in _ALLOWED_GLOBALS:
            return super().find_class(module, name)
        raise _pickle.UnpicklingError(f"refused to load {module}.{name}")


def safe_loads(data):
    import io
    return _SafeUnpickler(io.BytesIO(data)).load()


def _peer_uid_ok(sock):
    """True if the process at the other end of a Unix socket runs as this user (Linux SO_PEERCRED; elsewhere True)."""
    opt = getattr(_socket, "SO_PEERCRED", None)
    if opt is None:
        return True
    try:
        _pid, uid, _gid = _struct.unpack("3i", sock.getsockopt(_socket.SOL_SOCKET, opt, _struct.calcsize("3i")))
    except OSError:
        return False
    return uid == os.getuid()


class _FramedReader:
    """Accumulates bytes for one socket and yields complete messages. feed() raises ValueError on an oversized frame."""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, chunk):
        self.buf += chunk
        msgs = []
        pos = 0
        n = len(self.buf)
        while n - pos >= 4:
            (length,) = _struct.unpack_from(">I", self.buf, pos)
            if length > MAX_MESSAGE_BYTES:
                raise ValueError(f"message of {length} bytes refused")
            if n - pos - 4 < length:
                break
            payload = bytes(self.buf[pos + 4:pos + 4 + length])
            pos += 4 + length
            try:
                msgs.append(safe_loads(payload))
            except Exception:
                log.exception("Coordinator: could not read a message - dropping it")
        if pos:
            del self.buf[:pos]
        return msgs


def _send_msg(sock, msg):
    """
    sock is normally non-blocking (so our polling-loop reads never
    stall the Tk event loop) - but a real merge payload carries actual
    image data, easily bigger than one non-blocking write can accept in a
    single syscall, and a plain non-blocking sendall() would raise
    BlockingIOError/EAGAIN and DROP the message rather than sending the
    rest. Switching to blocking just for this one send is the simple fix.
    """
    try:
        data = _pickle.dumps(msg)
        old_timeout = sock.gettimeout()
        # Blocking, but never forever: if the peer stops reading (both sides sending a large
        # message at once), give up after 10 s and drop the connection rather than freeze.
        sock.settimeout(10.0)
        try:
            sock.sendall(_struct.pack(">I", len(data)) + data)
        finally:
            sock.settimeout(old_timeout)
    except OSError as e:
        if isinstance(e, (BrokenPipeError, ConnectionResetError)):
            log.debug(f"Coordinator: send failed ({type(e).__name__}) - peer already gone")
        else:
            log.warning(f"Coordinator: send failed ({type(e).__name__}: {e}) - dropping that connection")
        try:
            sock.shutdown(_socket.SHUT_RDWR)     # the next poll sees EOF and cleans up as for any lost peer
        except OSError:
            pass


def _send_to_hub_or_self(msg):
    if _coord["role"] == "hub":
        _route_message(msg)
    elif _coord["hub_conn"] is not None:
        _send_msg(_coord["hub_conn"], msg)


def init_coordinator(root):
    """
    Call once per process, as early as convenient (main() does this right
    after creating its root). Tries to become the hub; if the socket is
    already taken, connects to whoever holds it as a client; if it's
    neither bindable nor connectable (a stale file left by a hub that
    exited uncleanly), removes it and tries binding again.
    """
    _coord["root"] = root
    try:
        _p40_prepare_socket_dir()
    except Exception:
        log.exception("Coordinator: could not prepare the socket directory - cross-window discovery disabled")
        return
    if _try_bind_as_hub():
        _ensure_poll_loop()
        return
    if _try_connect_as_client():
        _ensure_poll_loop()
        return
    log.info("Coordinator: socket file exists but nothing answered - removing it and becoming the hub")
    try:
        SOCKET_PATH.unlink(missing_ok=True)
    except Exception:
        pass
    if _try_bind_as_hub():
        _ensure_poll_loop()


def _ensure_poll_loop():
    """
    Starts the recv/accept polling loop if it isn't already running. Safe to
    call repeatedly (e.g. after a hub/client role change on reconnect) -
    root.after() IDs are only ever created here, once, and self-reschedule.
    """
    if _coord["poll_job"] is not None:
        return
    _coord["poll_job"] = _coord["root"].after(COORD_POLL_INTERVAL_MS, _poll_coordinator)


def _poll_coordinator():
    _coord["poll_job"] = None
    root = _coord["root"]
    if root is None:
        return
    try:
        if _coord["role"] == "hub":
            _poll_hub()
        elif _coord["role"] == "client":
            _poll_client()
    except Exception:
        log.exception("Coordinator: poll iteration failed")
    if _coord["root"] is not None:
        _coord["poll_job"] = root.after(COORD_POLL_INTERVAL_MS, _poll_coordinator)


def _try_bind_as_hub():
    srv = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    try:
        srv.bind(str(SOCKET_PATH))
    except OSError:
        srv.close()
        return False
    srv.listen(16)
    srv.setblocking(False)
    _coord["role"] = "hub"
    _coord["client_id"] = "hub"
    _coord["server_socket"] = srv
    log.info(f"Coordinator: this process is the hub ({SOCKET_PATH})")
    return True


def _try_connect_as_client():
    conn = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    try:
        conn.connect(str(SOCKET_PATH))
    except OSError:
        conn.close()
        return False
    if not _peer_uid_ok(conn):
        log.warning(f"Coordinator: {SOCKET_PATH} is held by another user's process - not connecting")
        conn.close()
        return False
    conn.setblocking(False)
    _coord["role"] = "client"
    _coord["client_id"] = str(uuid.uuid4())
    _coord["hub_conn"] = conn
    _coord["readers"][conn.fileno()] = _FramedReader()
    _send_msg(conn, {"type": "hello", "client_id": _coord["client_id"], "protocol_version": PROTOCOL_VERSION})
    log.info(f"Coordinator: connected to the hub as client {_coord['client_id']}")
    broadcast_window_roster()
    return True


def shutdown_coordinator():
    """
    Called once, from main(), right after root.mainloop() returns - the
    OS closes our file descriptors regardless, but the hub must also
    remove the socket FILE itself: otherwise a later process's bind() sees
    the stale path and thinks the socket is still taken.

    Also cancels any still-pending _poll_coordinator root.after() job.
    """
    if _coord["poll_job"] is not None:
        try:
            _coord["root"].after_cancel(_coord["poll_job"])
        except Exception:
            pass
        _coord["poll_job"] = None
    if _coord["role"] == "hub":
        try:
            SOCKET_PATH.unlink(missing_ok=True)
        except Exception:
            pass


# ---- hub side ----

def _poll_hub():
    """
    One polling tick on the hub: accept any pending new connections, then
    drain any pending data from every connection we've already accepted
    (whether or not it has said "hello" yet). All sockets here are
    non-blocking, so every call either returns immediately with data/None
    or raises BlockingIOError.
    """
    srv = _coord["server_socket"]
    if srv is None:
        return
    while True:
        try:
            conn, _addr = srv.accept()
        except BlockingIOError:
            break
        except OSError:
            break
        if not _peer_uid_ok(conn):
            log.warning("Coordinator: refused a connection from another user's process")
            conn.close()
            continue
        conn.setblocking(False)
        _coord["readers"][conn.fileno()] = _FramedReader()
        _coord["hub_accepted"][conn.fileno()] = conn

    for fd, conn in list(_coord["hub_accepted"].items()):
        reader = _coord["readers"].get(fd)
        if reader is None:
            continue
        # Drain until the socket is empty, within a small time budget per tick, so a
        # large merge payload arrives in one go without stalling the window.
        deadline = time.monotonic() + 0.03
        while True:
            try:
                chunk = conn.recv(1 << 20)
            except BlockingIOError:
                break
            except OSError:
                chunk = b""
            if not chunk:
                _drop_client(conn)
                break
            try:
                msgs = reader.feed(chunk)
            except ValueError as e:
                log.warning(f"Coordinator: {e} - dropping that connection")
                _drop_client(conn)
                break
            for msg in msgs:
                _handle_hub_message(conn, msg)
            if time.monotonic() > deadline:
                break


def _drop_client(conn):
    fd = conn.fileno() if conn.fileno() != -1 else None
    if fd is not None:
        _coord["readers"].pop(fd, None)
        _coord["hub_accepted"].pop(fd, None)
    else:
        # Socket is already closed (fileno() == -1) - fall back to identity
        # search since we can no longer key by fd.
        for f, c in list(_coord["hub_accepted"].items()):
            if c is conn:
                _coord["hub_accepted"].pop(f, None)
                _coord["readers"].pop(f, None)
    dropped_id = None
    for cid, c in list(_coord["clients"].items()):
        if c is conn:
            dropped_id = cid
            del _coord["clients"][cid]
    _coord["roster"].pop(dropped_id, None)
    try:
        conn.close()
    except Exception:
        pass
    if dropped_id:
        log.info(f"Coordinator: client {dropped_id} disconnected")
        _broadcast_roster()


def _handle_hub_message(conn, msg):
    mtype = msg.get("type")
    if mtype == "hello":
        _coord["clients"][msg["client_id"]] = conn
        their_version = msg.get("protocol_version")
        if their_version != PROTOCOL_VERSION:
            log.warning(
                f"Coordinator: client {msg['client_id']} connected with protocol version {their_version!r} "
                f"(this hub is running {PROTOCOL_VERSION!r}) - some cross-process messages (Merge, Focus, "
                f"Pin close) may silently fail until every snippysnappy process is restarted on matching code"
            )
    elif mtype == "windows_update":
        _coord["roster"][msg["client_id"]] = msg["windows"]
        _broadcast_roster()
    elif mtype in ("merge_request", "merge_response", "merge_ack", "focus_request", "pin_close_request", "window_close_request", "window_rename_request", "window_hide_request", "recorder_stop_request"):
        _route_message(msg)
    elif mtype == "restart_request":
        log.info("Coordinator: relaying a restart request to every other connected client")
        for cid, c in list(_coord["clients"].items()):
            if c is not conn:
                _send_msg(c, msg)
        _run_local_restart_handler()


def _route_message(msg):
    target = msg.get("target_client_id")
    if target == _coord["client_id"]:
        _handle_incoming_merge_message(msg)
    elif target in _coord["clients"]:
        _send_msg(_coord["clients"][target], msg)
    else:
        # The client disconnected between when the roster was last
        # broadcast and now (or the roster entry was stale/phantom to
        # begin with) - this is the hub-side counterpart of the
        # "unknown window_id" logging in _handle_incoming_merge_message,
        # for when the whole client process is gone rather than just one
        # of its windows.
        log.warning(f"Coordinator: {msg.get('type', '?')} message addressed to unknown/disconnected client {target!r} - dropping")


def _broadcast_roster():
    full = dict(_coord["roster"])
    full[_coord["client_id"]] = _local_window_roster_entries()
    _coord["roster_cache"] = full
    for conn in _coord["clients"].values():
        _send_msg(conn, {"type": "roster", "entries": full, "protocol_version": PROTOCOL_VERSION})
    _refresh_all_local_tabs_panels()


# ---- client side ----

def _poll_client():
    """One polling tick on a client: drain any pending data from the hub connection, bounded so a burst doesn't take many ticks to arrive."""
    conn = _coord["hub_conn"]
    if conn is None:
        return
    reader = _coord["readers"].get(conn.fileno())
    if reader is None:
        return
    deadline = time.monotonic() + 0.03
    while True:
        try:
            chunk = conn.recv(1 << 20)
        except BlockingIOError:
            return
        except OSError:
            chunk = b""
        if not chunk:
            _handle_hub_disconnected()
            return
        try:
            msgs = reader.feed(chunk)
        except ValueError as e:
            log.warning(f"Coordinator: {e} - disconnecting from the hub")
            _handle_hub_disconnected()
            return
        for msg in msgs:
            _handle_client_message(msg)
        if time.monotonic() > deadline:
            return


def _handle_client_message(msg):
    mtype = msg.get("type")
    if mtype == "roster":
        hub_version = msg.get("protocol_version")
        if not _coord["hub_version_warned"] and hub_version != PROTOCOL_VERSION:
            _coord["hub_version_warned"] = True
            log.warning(
                f"Coordinator: connected to a hub running protocol version {hub_version!r} "
                f"(this process is {PROTOCOL_VERSION!r}) - some cross-process messages may silently "
                f"fail until every snippysnappy process is restarted on matching code"
            )
        _coord["roster_cache"] = msg["entries"]
        _refresh_all_local_tabs_panels()
    elif mtype in ("merge_request", "merge_response", "merge_ack", "focus_request", "pin_close_request", "window_close_request", "window_rename_request", "window_hide_request", "recorder_stop_request"):
        _handle_incoming_merge_message(msg)
    elif mtype == "restart_request":
        log.info("Coordinator: received a restart request from another snippysnappy process")
        _run_local_restart_handler()


def _handle_hub_disconnected():
    root = _coord["root"]
    conn = _coord["hub_conn"]
    if conn is not None:
        _coord["readers"].pop(conn.fileno(), None)
        try:
            conn.close()
        except Exception:
            pass
    _coord["hub_conn"] = None
    _coord["role"] = None
    _coord["roster_cache"] = {}
    _coord["hub_version_warned"] = False
    _refresh_all_local_tabs_panels()
    log.warning("Coordinator: lost the connection to the hub - will retry shortly (may become the new hub)")
    root.after(500, _retry_coordinator)


def _retry_coordinator():
    if _coord["role"] is not None:
        return
    init_coordinator(_coord["root"])


# ---- shared: window roster, remote merge protocol ----

def _local_window_roster_entries():
    entries = []
    for app in _LOCAL_WINDOWS:
        app._ensure_documents_initialized()
        entries.append({"kind": "window", "window_id": app.window_id, "label": window_name(app), "tab_count": len(app.documents), "tab_labels": window_tab_labels(app), "hidden": bool(getattr(app, "_hidden", False))})
    for pin in _LOCAL_PINS:
        entries.append({"kind": "pin", "window_id": pin.window_id, "label": window_name(pin), "tab_count": 1})
    for rec in _LOCAL_RECORDERS:
        entries.append({"kind": "recorder", "window_id": rec.window_id, "label": "Recording", "tab_count": 1, "started": getattr(rec, "started", 0.0)})
    for film in _LOCAL_FILMS:
        entries.append({"kind": "film", "window_id": film.window_id, "label": window_name(film), "tab_count": 1, "hidden": bool(getattr(film, "_hidden", False))})
    return entries


def broadcast_window_roster():
    if _coord["role"] is None:
        return
    if _coord["role"] == "hub":
        _coord["roster"][_coord["client_id"]] = _local_window_roster_entries()
        _broadcast_roster()
    else:
        _send_msg(_coord["hub_conn"], {"type": "windows_update", "client_id": _coord["client_id"], "windows": _local_window_roster_entries()})


def _refresh_all_local_tabs_panels():
    for app in _LOCAL_WINDOWS:
        if app.side_panel_mode == "tabs":
            try:
                app._refresh_side_panel()
            except Exception:
                log.exception("Coordinator: failed to refresh a Tabs panel after a roster update (non-fatal)")
        if getattr(app, "pin_mode_active", False):
            try:
                app._refresh_pins_dropdown()
            except Exception:
                log.exception("Coordinator: failed to refresh a Pins list after a roster update (non-fatal)")
    for film in list(_LOCAL_FILMS):
        try:
            film._refresh_fn()
        except Exception:
            log.exception("Coordinator: failed to refresh a film window's panel after a roster update (non-fatal)")


def remote_windows():
    """[(client_id, window_id, label, tab_count), ...] for every real capture window known to belong to ANOTHER process (pins are excluded - see remote_pins)."""
    out = []
    for cid, windows in _coord.get("roster_cache", {}).items():
        if cid == _coord["client_id"]:
            continue
        for w in windows:
            if w.get("kind", "window") != "window":
                continue
            out.append((cid, w["window_id"], w["label"], w["tab_count"]))
    return out


def remote_pins():
    """[(client_id, pin_id, label), ...] for every pin known to belong to ANOTHER snippysnappy process - this process's own pins are in _LOCAL_PINS directly, not here. Used only when show_pin_toolbar's "show pins from other windows" checkbox is on."""
    out = []
    for cid, windows in _coord.get("roster_cache", {}).items():
        if cid == _coord["client_id"]:
            continue
        for w in windows:
            if w.get("kind") != "pin":
                continue
            out.append((cid, w["window_id"], w["label"]))
    return out


def _find_local_window(window_id):
    for app in _LOCAL_WINDOWS:
        if app.window_id == window_id:
            return app
    return None


def _find_local_pin(pin_id):
    for pin in _LOCAL_PINS:
        if pin.window_id == pin_id:
            return pin
    return None


def focus_local_window(app):
    """Bring a window in THIS process to the front - de-minimizing it first
    if needed, since a minimized window won't respond to a plain lift().

    Safety: a roster entry can outlive the actual window it describes for
    a moment (the window was just closed, or the entry is otherwise
    stale/"phantom") - winfo_exists() is checked first so that case is
    logged plainly and skipped rather than raising into the caller. Each
    of the three steps is worth attempting independently (a WM that
    ignores focus_force() might still honor lift(), say), so a failure in
    one doesn't abort the rest - only the exception itself is swallowed
    (and logged), never the fact that something didn't work.
    """
    label = "?"
    try:
        label = app._document_label()
    except Exception:
        pass
    win_id = getattr(app, "window_id", "?")
    try:
        if not app.root.winfo_exists():
            log.warning(f"View: window {win_id} ({label}) no longer exists - phantom/stale roster entry, not focusing")
            return
    except Exception:
        log.warning(f"View: could not check whether window {win_id} ({label}) still exists (likely gone) - not focusing")
        return
    log.info(f"View: focusing local window {win_id} ({label})")
    try:
        app.root.deiconify()
        try:
            if app.root.overrideredirect():
                app.root.overrideredirect(True)
        except Exception:
            pass
    except Exception:
        log.exception(f"View: deiconify() failed for window {win_id} ({label}) (non-fatal, continuing)")
    try:
        app.root.lift()
        try:
            prev_topmost = bool(int(app.root.attributes("-topmost")))
        except Exception:
            prev_topmost = False
        app.root.attributes("-topmost", True)
        # Restore whatever "steady state" topmost-ness this window is
        # supposed to have afterward.
        steady_topmost = getattr(app, "_steady_state_topmost", None)
        if steady_topmost is None:
            steady_topmost = prev_topmost   # keep whatever this window had (e.g. an image window's always-on-top)
        app.root.after(200, lambda: app.root.attributes("-topmost", steady_topmost))
    except Exception:
        log.exception(f"View: lift()/-topmost toggle failed for window {win_id} ({label}) (non-fatal, continuing)")
    try:
        app.root.focus_force()
    except Exception:
        log.exception(f"View: focus_force() failed for window {win_id} ({label}) (non-fatal)")
    if BACKEND == "x11" or os.environ.get("DISPLAY"):   # patch 46: on Wayland set_focus() sends an activation request
        # A DIRECT XSetInputFocus, on top of the Tk-level focus_force()
        # above - this is the "click View in the Pins/Windows list to
        # refocus, then Escape reliably works on it.
        try:
            helper = _X11FocusHelper()
            try:
                helper.set_focus(app.root.winfo_id())
            finally:
                helper.close()
        except Exception:
            log.exception(f"View: direct X11 focus grab failed for window {win_id} ({label}) (non-fatal)")


def request_remote_focus(remote_client_id, remote_window_id):
    """Ask another process to bring one of ITS windows to the front - the
    remote-window counterpart of focus_local_window(), routed through the
    same hub/client messaging merge_request already uses."""
    if _coord["role"] is None:
        log.warning(f"View: cannot request focus on remote window {remote_window_id} (client {remote_client_id}) - coordinator is not active in this process")
        return
    msg = {
        "type": "focus_request",
        "target_client_id": remote_client_id, "target_window_id": remote_window_id,
    }
    log.info(f"View: requesting focus on remote window {remote_window_id} (client {remote_client_id})")
    _send_to_hub_or_self(msg)


def request_remote_pin_close(remote_client_id, pin_id):
    """Force-close a pin belonging to ANOTHER process - the Pins list's
    per-entry x, for a pin that's stopped responding to its own normal
    close controls. Same one-way, no-ack shape as request_remote_focus."""
    if _coord["role"] is None:
        log.warning(f"Pins list: cannot request closing remote pin {pin_id} (client {remote_client_id}) - coordinator is not active in this process")
        return
    msg = {
        "type": "pin_close_request",
        "target_client_id": remote_client_id, "target_window_id": pin_id,
    }
    log.info(f"Pins list: requesting close of remote pin {pin_id} (client {remote_client_id})")
    _send_to_hub_or_self(msg)


# ---- local (same-process) FILM window registry ----
# A film window (film_viewer.FilmApp) is not a SelectorApp, so it registers
# this small handle instead, purely so it shows up in other windows' Tabs
# panels (kind "film") and can be focused/closed from them. Duck-types what
# focus_local_window needs, like _PinHandle.
_LOCAL_FILMS = []


class _FilmHandle:
    _steady_state_topmost = None   # None = keep whatever the window had

    def __init__(self, film_id, root, label, close_fn, refresh_fn):
        self.window_id = film_id
        self.root = root
        self._label = label
        self._close_fn = close_fn
        self._refresh_fn = refresh_fn

    def _document_label(self):
        return self._label

    def _ensure_documents_initialized(self):
        pass

    def _close_window(self):
        self._close_fn()


def _register_film(handle):
    _LOCAL_FILMS.append(handle)
    broadcast_window_roster()


def _unregister_film(handle):
    if handle in _LOCAL_FILMS:
        _LOCAL_FILMS.remove(handle)
    broadcast_window_roster()


def _find_local_film(film_id):
    for f in _LOCAL_FILMS:
        if f.window_id == film_id:
            return f
    return None


def remote_windows_all():
    """[(client_id, window_id, label, tab_count, kind), ...] for every image window ("window") and film window ("film") in ANOTHER process. remote_windows() stays image-windows-only."""
    out = []
    for cid, windows in _coord.get("roster_cache", {}).items():
        if cid == _coord["client_id"]:
            continue
        for w in windows:
            kind = w.get("kind", "window")
            if kind in ("window", "film"):
                out.append((cid, w["window_id"], w["label"], w["tab_count"], kind))
    return out


from .side_panel import window_name, window_tab_labels


def remote_window_hidden(window_id):
    """True if a window in ANOTHER process is backgrounded (per the roster)."""
    for cid, windows in _coord.get("roster_cache", {}).items():
        if cid == _coord["client_id"]:
            continue
        for w in windows:
            if w.get("window_id") == window_id:
                return bool(w.get("hidden"))
    return False


def remote_window_tab_labels(window_id):
    """Tab names of a window in ANOTHER process (carried in the roster), or [] if the roster doesn't have them."""
    for cid, windows in _coord.get("roster_cache", {}).items():
        if cid == _coord["client_id"]:
            continue
        for w in windows:
            if w.get("window_id") == window_id:
                return list(w.get("tab_labels") or [])
    return []


def request_remote_window_hide(remote_client_id, window_id):
    """Backgrounds (hides, keeps) a window belonging to ANOTHER process."""
    if _coord["role"] is None:
        log.warning(f"Windows list: cannot request hiding remote window {window_id} (client {remote_client_id}) - coordinator is not active in this process")
        return
    msg = {"type": "window_hide_request", "target_client_id": remote_client_id, "target_window_id": window_id}
    log.info(f"Windows list: requesting that remote window {window_id} (client {remote_client_id}) be hidden")
    _send_to_hub_or_self(msg)


def request_remote_window_rename(remote_client_id, window_id, name):
    """Renames a window/film belonging to ANOTHER process (display name only) - same one-way, no-ack shape as request_remote_window_close."""
    if _coord["role"] is None:
        log.warning(f"Windows list: cannot request renaming remote window {window_id} (client {remote_client_id}) - coordinator is not active in this process")
        return
    msg = {
        "type": "window_rename_request",
        "target_client_id": remote_client_id, "target_window_id": window_id, "name": name,
    }
    log.info(f"Windows list: requesting rename of remote window {window_id} (client {remote_client_id}) to {name!r}")
    _send_to_hub_or_self(msg)


def request_remote_window_close(remote_client_id, window_id):
    """Force-close a window belonging to ANOTHER process, from the Tabs panel's Windows section - same one-way, no-ack shape as request_remote_pin_close."""
    if _coord["role"] is None:
        log.warning(f"Windows list: cannot request closing remote window {window_id} (client {remote_client_id}) - coordinator is not active in this process")
        return
    msg = {
        "type": "window_close_request",
        "target_client_id": remote_client_id, "target_window_id": window_id,
    }
    log.info(f"Windows list: requesting close of remote window {window_id} (client {remote_client_id})")
    _send_to_hub_or_self(msg)


# ---- local (same-process) RECORDER registry ----
# A recorder (see recorder.py's Recorder/run_recording_session) has no
# window worth focusing and never appears in the Pins/Tabs lists - it
# exists in this registry purely so a SEPARATE snippysnappy invocation's
# -r can find and stop THIS recording (the toggle mechanism -r/--record
# uses - see recorder.py's any_recording_active/stop_all_known_recordings).
_LOCAL_RECORDERS = []


class _RecorderHandle:
    def __init__(self, recorder_id, stop_fn):
        import time as _time
        self.window_id = recorder_id   # same field name as _PinHandle, for roster-entry consistency
        self._stop = stop_fn
        self.started = _time.time()   # lets -r find the MOST RECENT recording when several are running (--demo)


def _register_recorder(handle):
    _LOCAL_RECORDERS.append(handle)
    broadcast_window_roster()


def _unregister_recorder(handle):
    if handle in _LOCAL_RECORDERS:
        _LOCAL_RECORDERS.remove(handle)
    broadcast_window_roster()


def local_recorders():
    return list(_LOCAL_RECORDERS)


def remote_recorders():
    """[(client_id, recorder_id), ...] for every recorder known to belong to ANOTHER snippysnappy process."""
    out = []
    for cid, windows in _coord.get("roster_cache", {}).items():
        if cid == _coord["client_id"]:
            continue
        for w in windows:
            if w.get("kind") != "recorder":
                continue
            out.append((cid, w["window_id"]))
    return out


def _find_local_recorder(recorder_id):
    for r in _LOCAL_RECORDERS:
        if r.window_id == recorder_id:
            return r
    return None


def request_remote_recorder_stop(remote_client_id, recorder_id):
    """Stops a recorder belonging to ANOTHER process - same one-way, no-ack shape as request_remote_pin_close."""
    if _coord["role"] is None:
        log.warning(f"Recorder: cannot request stopping remote recorder {recorder_id} (client {remote_client_id}) - coordinator is not active in this process")
        return
    msg = {
        "type": "recorder_stop_request",
        "target_client_id": remote_client_id, "target_window_id": recorder_id,
    }
    log.info(f"Recorder: requesting stop of remote recorder {recorder_id} (client {remote_client_id})")
    _send_to_hub_or_self(msg)


def set_restart_handler(fn):
    """
    Registers the function THIS process should call to restart itself
    when told to via request_global_restart.
    """
    _coord["restart_handler"] = fn


def _run_local_restart_handler():
    handler = _coord["restart_handler"]
    if handler is None:
        log.warning("Coordinator: restart requested but this process registered no restart handler - ignoring")
        return
    try:
        handler()
    except Exception:
        log.exception("Coordinator: restart handler failed")


def request_global_restart():
    """
    Tells every OTHER connected snippysnappy process (any window or
    detached pin viewer sharing this coordinator) to restart itself, then
    restarts THIS process too.
    """
    if _coord["role"] is None:
        log.warning("Coordinator: cannot broadcast a restart - coordinator is not active in this process")
        return
    msg = {"type": "restart_request"}
    if _coord["role"] == "hub":
        for conn in _coord["clients"].values():
            _send_msg(conn, msg)
    else:
        _send_msg(_coord["hub_conn"], msg)
    log.info("Coordinator: broadcasting a restart request to every other connected snippysnappy process")
    _run_local_restart_handler()


def request_remote_merge(requesting_app, remote_client_id, remote_window_id):
    if _coord["role"] is None:
        return
    msg = {
        "type": "merge_request",
        "target_client_id": remote_client_id, "target_window_id": remote_window_id,
        "reply_to_client_id": _coord["client_id"], "reply_to_window_id": requesting_app.window_id,
    }
    _send_to_hub_or_self(msg)
    log.info(f"Coordinator: requested a merge from a remote window ({remote_client_id}/{remote_window_id})")


def _handle_incoming_merge_message(msg):
    mtype = msg["type"]
    if mtype == "merge_request":
        target_app = _find_local_window(msg["target_window_id"])
        if target_app is None:
            log.warning(f"Coordinator: merge_request for unknown window_id={msg['target_window_id']!r} in this process - phantom/stale roster entry (window already closed?); ignoring")
            return
        target_app._ensure_documents_initialized()
        target_app._exit_all_modes_safely()
        target_app.documents[target_app.current_document_index] = target_app._capture_document_state()
        docs = target_app.documents
        for doc in docs:
            p = doc.get("image_path")
            if p in target_app.owned_temp_paths:
                target_app.owned_temp_paths.remove(p)
        _send_to_hub_or_self({
            "type": "merge_response",
            "target_client_id": msg["reply_to_client_id"], "target_window_id": msg["reply_to_window_id"],
            "source_client_id": msg["target_client_id"], "source_window_id": msg["target_window_id"],
            "documents": docs,
        })
    elif mtype == "merge_response":
        requester_app = _find_local_window(msg["target_window_id"])
        if requester_app is not None:
            requester_app._ensure_documents_initialized()
            for doc in msg["documents"]:
                img_path = doc.get("image_path")
                if img_path is not None:
                    requester_app.owned_temp_paths.append(img_path)
                requester_app.documents.append(doc)
            log.info(f"Coordinator: merged {len(msg['documents'])} tab(s) from a remote window")
            if requester_app.side_panel_mode == "tabs":
                requester_app._refresh_side_panel()
        _send_to_hub_or_self({
            "type": "merge_ack",
            "target_client_id": msg["source_client_id"], "target_window_id": msg["source_window_id"],
        })
    elif mtype == "merge_ack":
        source_app = _find_local_window(msg["target_window_id"])
        if source_app is not None:
            source_app.documents = []
            source_app.root.destroy()
    elif mtype == "focus_request":
        target = _find_local_window(msg["target_window_id"]) or _find_local_pin(msg["target_window_id"]) or _find_local_film(msg["target_window_id"])
        if target is None:
            log.warning(f"Coordinator: focus_request for unknown window_id={msg['target_window_id']!r} in this process - phantom/stale roster entry (window/pin already closed?); ignoring")
            return
        focus_local_window(target)
    elif mtype == "pin_close_request":
        target_pin = _find_local_pin(msg["target_window_id"])
        if target_pin is None:
            log.warning(f"Coordinator: pin_close_request for unknown pin_id={msg['target_window_id']!r} in this process - phantom/stale roster entry (already closed?); ignoring")
            return
        log.info(f"Coordinator: force-closing pin {msg['target_window_id']!r} on request from the Pins list")
        target_pin._cleanup()
    elif mtype == "window_close_request":
        target_app = _find_local_window(msg["target_window_id"]) or _find_local_film(msg["target_window_id"])
        if target_app is None:
            log.warning(f"Coordinator: window_close_request for unknown window_id={msg['target_window_id']!r} in this process - phantom/stale roster entry (already closed?); ignoring")
            return
        log.info(f"Coordinator: force-closing window {msg['target_window_id']!r} on request from the Windows list")
        getattr(target_app, "_force_close", target_app._close_window)()
    elif mtype == "window_rename_request":
        target = _find_local_window(msg["target_window_id"]) or _find_local_film(msg["target_window_id"])
        if target is None:
            log.warning(f"Coordinator: window_rename_request for unknown window_id={msg['target_window_id']!r} in this process - phantom/stale roster entry; ignoring")
            return
        target._window_name_override = msg.get("name") or None
        log.info(f"Coordinator: renamed window {msg['target_window_id']!r} to {msg.get('name')!r} on request from the Windows list")
        broadcast_window_roster()
        try:
            _refresh_all_local_tabs_panels()
        except Exception:
            log.exception("Coordinator: could not refresh local panels after a rename (non-fatal)")
    elif mtype == "window_hide_request":
        target = _find_local_window(msg["target_window_id"]) or _find_local_film(msg["target_window_id"])
        hide = getattr(target, "_hide_window", None) if target is not None else None
        if hide is None:
            log.warning(f"Coordinator: window_hide_request for unknown/unhideable window_id={msg['target_window_id']!r}; ignoring")
            return
        log.info(f"Coordinator: hiding window {msg['target_window_id']!r} on request from the Windows list")
        hide()
    elif mtype == "recorder_stop_request":
        target_recorder = _find_local_recorder(msg["target_window_id"])
        if target_recorder is None:
            log.warning(f"Coordinator: recorder_stop_request for unknown recorder_id={msg['target_window_id']!r} in this process - phantom/stale roster entry (already stopped?); ignoring")
            return
        log.info(f"Coordinator: stopping recorder {msg['target_window_id']!r} on request from another snippysnappy invocation")
        target_recorder._stop()




# ---- patch 42: socket/config/plugin/session hardening ----
_p42_installed = True
