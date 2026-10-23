# snippysnappy v1.0.1
"""
Minimal D-Bus client (standard library only) - just enough for the xdg-desktop-portal
Screenshot and ScreenCast interfaces on Wayland desktops without wlroots tools (GNOME, KDE).
patch 46. Little-endian marshalling, EXTERNAL auth, unix fd passing.
"""
import os
import select
import socket
import struct
import time

from .logging_setup import log


class DBusError(Exception):
    pass


# ---------------------------------------------------------------- signatures
def _split_sig(sig):
    """'sa{sv}(ii)' -> ['s', 'a{sv}', '(ii)']"""
    out, i = [], 0
    while i < len(sig):
        j = _end_of(sig, i)
        out.append(sig[i:j])
        i = j
    return out


def _end_of(sig, i):
    c = sig[i]
    if c == "a":
        return _end_of(sig, i + 1)
    if c in "({":
        close = ")" if c == "(" else "}"
        depth, j = 0, i
        while True:
            if sig[j] in "({":
                depth += 1
            elif sig[j] in ")}":
                depth -= 1
                if depth == 0:
                    return j + 1
            j += 1
    return i + 1


_ALIGN = {"y": 1, "b": 4, "n": 2, "q": 2, "i": 4, "u": 4, "x": 8, "t": 8, "d": 8, "s": 4, "o": 4,
          "g": 1, "h": 4, "a": 4, "(": 8, "{": 8, "v": 1}
_FMT = {"y": "B", "b": "I", "n": "h", "q": "H", "i": "i", "u": "I", "x": "q", "t": "Q", "d": "d", "h": "I"}


class _W:
    def __init__(self):
        self.b = bytearray()

    def pad(self, n):
        self.b += b"\0" * ((-len(self.b)) % n)

    def put(self, t, v):
        c = t[0]
        self.pad(_ALIGN[c])
        if c in _FMT:
            self.b += struct.pack("<" + _FMT[c], int(v) if c != "d" else float(v))
        elif c in "so":
            e = v.encode()
            self.b += struct.pack("<I", len(e)) + e + b"\0"
        elif c == "g":
            e = v.encode()
            self.b += struct.pack("<B", len(e)) + e + b"\0"
        elif c == "v":
            sig, val = v
            self.put("g", sig)
            self.put(sig, val)
        elif c == "a":
            inner = t[1:]
            at = len(self.b)
            self.b += b"\0\0\0\0"
            self.pad(_ALIGN[inner[0]])
            start = len(self.b)
            items = v.items() if inner[0] == "{" else v
            for it in items:
                self.put(inner, it)
            struct.pack_into("<I", self.b, at, len(self.b) - start)
        elif c in "({":
            for st, sv in zip(_split_sig(t[1:-1]), v):
                self.put(st, sv)
        else:
            raise DBusError(f"cannot marshal type {t}")


class _R:
    def __init__(self, data, pos=0):
        self.d, self.p = data, pos

    def align(self, n):
        self.p += (-self.p) % n

    def get(self, t):
        c = t[0]
        self.align(_ALIGN[c])
        if c in _FMT:
            f = "<" + _FMT[c]
            v = struct.unpack_from(f, self.d, self.p)[0]
            self.p += struct.calcsize(f)
            return bool(v) if c == "b" else v
        if c in "so":
            n = struct.unpack_from("<I", self.d, self.p)[0]
            v = self.d[self.p + 4:self.p + 4 + n].decode()
            self.p += 4 + n + 1
            return v
        if c == "g":
            n = self.d[self.p]
            v = self.d[self.p + 1:self.p + 1 + n].decode()
            self.p += 1 + n + 1
            return v
        if c == "v":
            sig = self.get("g")
            return self.get(sig)
        if c == "a":
            n = struct.unpack_from("<I", self.d, self.p)[0]
            self.p += 4
            inner = t[1:]
            self.align(_ALIGN[inner[0]])
            end = self.p + n
            if inner[0] == "{":
                out = {}
                while self.p < end:
                    k, v = self.get(inner)
                    out[k] = v
                return out
            out = []
            while self.p < end:
                out.append(self.get(inner))
            return out
        if c in "({":
            return tuple(self.get(st) for st in _split_sig(t[1:-1]))
        raise DBusError(f"cannot unmarshal type {t}")


# ---------------------------------------------------------------- connection
class Bus:
    def __init__(self, address=None, timeout=10.0):
        address = address or os.environ.get("DBUS_SESSION_BUS_ADDRESS") or \
            f"unix:path={os.environ.get('XDG_RUNTIME_DIR', '/run/user/%d' % os.getuid())}/bus"
        path = abstract = None
        for part in address.split(";")[0].split(":", 1)[1].split(","):
            k, _, v = part.partition("=")
            if k == "path":
                path = v
            elif k == "abstract":
                abstract = v
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect("\0" + abstract if abstract else path)
        self._buf = bytearray()
        self._fds = []
        self._queue = []
        self._serial = 0
        self._auth()
        self.unique_name = self.call("org.freedesktop.DBus", "/org/freedesktop/DBus",
                                     "org.freedesktop.DBus", "Hello")[0]

    def _line(self):
        while b"\r\n" not in self._buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise DBusError("bus closed during auth")
            self._buf += chunk
        i = self._buf.index(b"\r\n")
        line = bytes(self._buf[:i])
        del self._buf[:i + 2]
        return line

    def _auth(self):
        self.sock.sendall(b"\0AUTH EXTERNAL " + str(os.getuid()).encode().hex().encode() + b"\r\n")
        if not self._line().startswith(b"OK"):
            raise DBusError("D-Bus EXTERNAL auth rejected")
        self.sock.sendall(b"NEGOTIATE_UNIX_FD\r\n")
        self.fd_ok = self._line().startswith(b"AGREE_UNIX_FD")
        self.sock.sendall(b"BEGIN\r\n")

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass
        for fd in self._fds:
            try:
                os.close(fd)
            except OSError:
                pass

    # -- wire
    def _send(self, mtype, fields, sig="", args=()):
        body = _W()
        for t, v in zip(_split_sig(sig), args):
            body.put(t, v)
        self._serial += 1
        hdr = _W()
        hdr.b += struct.pack("<cBBBII", b"l", mtype, 0, 1, len(body.b), self._serial)
        flist = [(code, (s, v)) for code, s, v in fields]
        if sig:
            flist.append((8, ("g", sig)))
        hdr.put("a(yv)", flist)
        hdr.pad(8)
        self.sock.sendall(bytes(hdr.b) + bytes(body.b))
        return self._serial

    def _read_message(self, deadline):
        while True:
            if len(self._buf) >= 16:
                blen, _serial, flen = struct.unpack_from("<III", self._buf, 4)
                hlen = 16 + flen + ((-(16 + flen)) % 8)
                if len(self._buf) >= hlen + blen:
                    raw = bytes(self._buf[:hlen + blen])
                    del self._buf[:hlen + blen]
                    return self._parse(raw, hlen)
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("D-Bus reply timed out")
            r, _, _ = select.select([self.sock], [], [], left)
            if not r:
                continue
            data, anc, _flags, _addr = self.sock.recvmsg(65536, socket.CMSG_SPACE(16 * 4))
            if not data:
                raise DBusError("bus connection closed")
            self._buf += data
            for level, typ, cdata in anc:
                if level == socket.SOL_SOCKET and typ == socket.SCM_RIGHTS:
                    n = len(cdata) // 4
                    self._fds += list(struct.unpack(f"<{n}i", cdata[:n * 4]))

    def _parse(self, raw, hlen):
        if raw[0:1] != b"l":
            raise DBusError("big-endian D-Bus messages are not supported")
        mtype = raw[1]
        r = _R(raw, 12)
        fields = dict(r.get("a(yv)"))
        sig = fields.get(8, "")
        br = _R(raw, hlen)
        body = [br.get(t) for t in _split_sig(sig)]
        return {"type": mtype, "fields": fields, "body": body}

    # -- api
    def call(self, dest, path, iface, member, sig="", args=(), timeout=30.0):
        serial = self._send(1, [(1, "o", path), (2, "s", iface), (3, "s", member), (6, "s", dest)], sig, args)
        deadline = time.monotonic() + timeout
        while True:
            m = self._read_message(deadline)
            if m["type"] in (2, 3) and m["fields"].get(5) == serial:
                if m["type"] == 3:
                    raise DBusError(f"{m['fields'].get(4)}: {m['body'][0] if m['body'] else ''}")
                return m["body"]
            if m["type"] == 4:
                self._queue.append(m)

    def add_match(self, rule):
        self.call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "AddMatch", "s", (rule,))

    def wait_signal(self, path, member, timeout):
        deadline = time.monotonic() + timeout
        while True:
            for i, m in enumerate(self._queue):
                if m["fields"].get(1) == path and m["fields"].get(3) == member:
                    return self._queue.pop(i)["body"]
            m = self._read_message(deadline)
            if m["type"] == 4:
                self._queue.append(m)

    def take_fd(self, index):
        fd = self._fds[index]
        self._fds[index] = -1
        return fd


# ---------------------------------------------------------------- portal helpers
PORTAL = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"


class PortalCancelled(Exception):
    pass


def _token():
    return "snippy" + os.urandom(6).hex()


def portal_request(bus, iface, method, sig, args, timeout=300.0):
    """Calls a portal method whose last argument is the options dict; returns the Response results."""
    tok = _token()
    path = f"{PORTAL_PATH}/request/{bus.unique_name[1:].replace('.', '_')}/{tok}"
    bus.add_match(f"type='signal',interface='org.freedesktop.portal.Request',member='Response',path='{path}'")
    args = list(args)
    args[-1] = dict(args[-1], handle_token=("s", tok))
    bus.call(PORTAL, PORTAL_PATH, iface, method, sig, args)
    code, results = bus.wait_signal(path, "Response", timeout)
    if code == 1:
        raise PortalCancelled(f"{iface}.{method} cancelled by the user")
    if code != 0:
        raise DBusError(f"{iface}.{method} failed (response {code})")
    return results


def portal_version(bus, iface):
    try:
        return bus.call(PORTAL, PORTAL_PATH, "org.freedesktop.DBus.Properties", "Get", "ss", (iface, "version"), timeout=5)[0]
    except Exception:
        return 0


def portal_screenshot(timeout=120.0):
    """Returns the local path the portal saved the screenshot to (whole desktop, physical pixels)."""
    bus = Bus()
    try:
        res = portal_request(bus, "org.freedesktop.portal.Screenshot", "Screenshot", "sa{sv}",
                             ("", {"interactive": ("b", False), "modal": ("b", False)}), timeout)
    finally:
        bus.close()
    uri = res.get("uri", "")
    if not uri.startswith("file://"):
        raise DBusError(f"portal returned no file ({uri!r})")
    from urllib.parse import unquote, urlparse
    return unquote(urlparse(uri).path)


class ScreenCast:
    """
    One portal ScreenCast session for one monitor. Keep the object (its bus connection)
    alive for the whole recording - the compositor ends the stream when it is closed.
    """
    def __init__(self, restore_token=None, cursor=True, timeout=300.0):
        self.bus = Bus()
        iface = "org.freedesktop.portal.ScreenCast"
        ver = portal_version(self.bus, iface)
        res = portal_request(self.bus, iface, "CreateSession", "a{sv}",
                             ({"session_handle_token": ("s", _token())},), timeout)
        self.session = res["session_handle"]
        opts = {"types": ("u", 1), "multiple": ("b", False)}
        if cursor:
            try:
                modes = self.bus.call(PORTAL, PORTAL_PATH, "org.freedesktop.DBus.Properties", "Get", "ss",
                                      (iface, "AvailableCursorModes"), timeout=5)[0]
                if modes & 2:
                    opts["cursor_mode"] = ("u", 2)   # embedded
            except Exception:
                pass
        if ver >= 4:
            opts["persist_mode"] = ("u", 2)
            if restore_token:
                opts["restore_token"] = ("s", restore_token)
        portal_request(self.bus, iface, "SelectSources", "oa{sv}", (self.session, opts), timeout)
        res = portal_request(self.bus, iface, "Start", "osa{sv}", (self.session, "", {}), timeout)
        streams = res.get("streams") or []
        if not streams:
            raise DBusError("portal ScreenCast returned no stream")
        node, props = streams[0]
        self.node = node
        self.position = props.get("position")
        self.size = props.get("size")
        self.restore_token = res.get("restore_token")
        idx = self.bus.call(PORTAL, PORTAL_PATH, iface, "OpenPipeWireRemote", "oa{sv}", (self.session, {}))[0]
        self.fd = self.bus.take_fd(idx)
        log.info(f"ScreenCast: node {node} position={self.position} size={self.size} fd={self.fd}")

    def close(self):
        try:
            self.bus.call(PORTAL, self.session, "org.freedesktop.portal.Session", "Close", timeout=3)
        except Exception:
            pass
        try:
            os.close(self.fd)
        except Exception:
            pass
        self.bus.close()
