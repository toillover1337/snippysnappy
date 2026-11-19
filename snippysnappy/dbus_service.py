# snippysnappy v1.0.5
"""
Tiny D-Bus SERVICE on top of dbus_mini's client (standard library only): own a bus name, export objects with methods and
properties, emit signals, and pump incoming calls from a Tk after() loop (no threads). Used by the tray icon.
"""
import select
import time

from .dbus_mini import Bus, DBusError, _R
from .logging_setup import log


class Service(Bus):
    def __init__(self, timeout=5.0):
        super().__init__(timeout=timeout)
        self.objects = {}      # path -> {"ifaces": {iface: {"methods": {name: (insig, outsig, fn)}, "props": fn() -> {name: (sig, value)}}}}
        self.signal_handlers = []
        self.sock.settimeout(timeout)

    # ---- export
    def export(self, path, iface, methods, props=None, xml=""):
        """methods: {name: (in_sig, out_sig, fn(*args) -> tuple|value|None)}; props: fn() -> {name: (sig, value)}."""
        obj = self.objects.setdefault(path, {"ifaces": {}, "xml": {}})
        obj["ifaces"][iface] = {"methods": methods, "props": props}
        obj["xml"][iface] = xml

    def request_name(self, name):
        r = self.call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "RequestName", "su", (name, 4))
        return r and r[0] in (1, 4)

    def name_has_owner(self, name):
        try:
            return bool(self.call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "NameHasOwner", "s", (name,))[0])
        except Exception:
            return False

    def emit(self, path, iface, member, sig="", args=()):
        self._send(4, [(1, "o", path), (2, "s", iface), (3, "s", member)], sig, args)

    def watch_name_owner(self, name, fn):
        """fn(new_owner) is called when `name` appears (non-empty owner) or disappears (empty)."""
        self.add_match(f"type='signal',sender='org.freedesktop.DBus',interface='org.freedesktop.DBus',member='NameOwnerChanged',arg0='{name}'")
        self.signal_handlers.append(("NameOwnerChanged", name, fn))

    # ---- incoming
    def pump(self, budget=0.0):
        """Handle every message that is waiting (non-blocking). Raises DBusError when the bus connection is gone."""
        deadline = time.monotonic() + budget
        while True:
            while self._incoming:
                self._dispatch(self._incoming.pop(0))
            try:
                ready, _, _ = select.select([self.sock], [], [], 0)
                if not ready and not self._buf:
                    return
                m = self._read_message(time.monotonic() + 0.5)
            except TimeoutError:
                return
            if m["type"] == 1:
                self._dispatch(m)
            elif m["type"] == 4:
                self._signal(m)
            if time.monotonic() > deadline + 0.2:
                return

    def _signal(self, m):
        f = m["fields"]
        if f.get(3) == "NameOwnerChanged" and len(m["body"]) == 3:
            for kind, name, fn in self.signal_handlers:
                if kind == "NameOwnerChanged" and m["body"][0] == name:
                    try:
                        fn(m["body"][2])
                    except Exception:
                        log.exception("Tray: name-owner callback failed (non-fatal)")

    def _reply(self, m, sig="", args=()):
        f = m["fields"]
        self._send(2, [(5, "u", m["serial"]), (6, "s", f.get(7, ""))], sig, args)

    def _error(self, m, name, text):
        f = m["fields"]
        self._send(3, [(4, "s", name), (5, "u", m["serial"]), (6, "s", f.get(7, ""))], "s", (text,))

    def _dispatch(self, m):
        f = m["fields"]
        path, iface, member = f.get(1), f.get(2), f.get(3)
        obj = self.objects.get(path)
        try:
            if iface == "org.freedesktop.DBus.Introspectable" and member == "Introspect" and obj is not None:
                body = "".join(obj["xml"].values())
                self._reply(m, "s", ('<!DOCTYPE node PUBLIC "-//freedesktop//DTD D-BUS Object Introspection 1.0//EN" '
                                     '"http://www.freedesktop.org/standards/dbus/1.0/introspect.dtd"><node>' + body + "</node>",))
                return
            if iface == "org.freedesktop.DBus.Properties" and obj is not None:
                self._properties(m, obj, member)
                return
            if obj is None or iface not in obj["ifaces"] or member not in obj["ifaces"][iface]["methods"]:
                self._error(m, "org.freedesktop.DBus.Error.UnknownMethod", f"{iface}.{member}")
                return
            insig, outsig, fn = obj["ifaces"][iface]["methods"][member]
            res = fn(*m["body"])
            if outsig:
                self._reply(m, outsig, res)      # methods return a tuple of their out-arguments
            else:
                self._reply(m)
        except Exception as e:
            log.exception(f"Tray: D-Bus call {iface}.{member} failed (non-fatal)")
            try:
                self._error(m, "org.freedesktop.DBus.Error.Failed", str(e))
            except Exception:
                pass

    def _properties(self, m, obj, member):
        body = m["body"]
        if member == "GetAll":
            ifc = obj["ifaces"].get(body[0])
            d = ifc["props"]() if ifc and ifc["props"] else {}
            self._reply(m, "a{sv}", (d,))
        elif member == "Get":
            ifc = obj["ifaces"].get(body[0])
            d = ifc["props"]() if ifc and ifc["props"] else {}
            if body[1] not in d:
                self._error(m, "org.freedesktop.DBus.Error.UnknownProperty", body[1])
            else:
                self._reply(m, "v", (d[body[1]],))
        else:
            self._error(m, "org.freedesktop.DBus.Error.PropertyReadOnly", member)


def _split(sig):
    from .dbus_mini import _split_sig
    return _split_sig(sig)
