# snippysnappy v1.0.5
"""
The tray icon itself: a StatusNotifierItem (org.kde.StatusNotifierItem) with a com.canonical.dbusmenu menu, over the
session bus. This is what XFCE's "Status Notifier" panel plugin, KDE Plasma, GNOME (AppIndicator extension), waybar's
"tray" module, quickshell / ags / hyprpanel system trays and most other bars/panels show. No toolkit or extra package
needed. Left click -> on_activate(); right click -> the dbusmenu (built from the `tree` given to refresh()).

tree = [{"id": "str", "label": "Text", "children": [...], "enabled": True} | {"separator": True}, ...]
"""
import os
from pathlib import Path

from .dbus_mini import DBusError
from .dbus_service import Service
from .logging_setup import log

ITEM_PATH, MENU_PATH = "/StatusNotifierItem", "/MenuBar"
ITEM_IFACE, MENU_IFACE = "org.kde.StatusNotifierItem", "com.canonical.dbusmenu"
WATCHERS = ("org.kde.StatusNotifierWatcher", "org.freedesktop.StatusNotifierWatcher")

_ITEM_XML = ('<interface name="org.kde.StatusNotifierItem">'
             '<method name="ContextMenu"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>'
             '<method name="Activate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>'
             '<method name="SecondaryActivate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>'
             '<method name="Scroll"><arg type="i" direction="in"/><arg type="s" direction="in"/></method>'
             '<property name="Id" type="s" access="read"/><property name="Title" type="s" access="read"/>'
             '<property name="Status" type="s" access="read"/><property name="Menu" type="o" access="read"/>'
             '<property name="IconPixmap" type="a(iiay)" access="read"/>'
             '<signal name="NewIcon"/><signal name="NewStatus"><arg type="s"/></signal></interface>')
_MENU_XML = ('<interface name="com.canonical.dbusmenu">'
             '<method name="GetLayout"><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="as" direction="in"/>'
             '<arg type="u" direction="out"/><arg type="(ia{sv}av)" direction="out"/></method>'
             '<method name="GetGroupProperties"><arg type="ai" direction="in"/><arg type="as" direction="in"/><arg type="a(ia{sv})" direction="out"/></method>'
             '<method name="Event"><arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="in"/><arg type="u" direction="in"/></method>'
             '<method name="AboutToShow"><arg type="i" direction="in"/><arg type="b" direction="out"/></method>'
             '<signal name="LayoutUpdated"><arg type="u"/><arg type="i"/></signal></interface>')


def icon_pixmaps(png_path, sizes=(22, 32, 48, 64)):
    """[(w, h, ARGB32 bytes)] from the program icon - sent to the panel directly, so no icon theme install is needed."""
    out = []
    try:
        from PIL import Image
        src = Image.open(png_path).convert("RGBA")
        for n in sizes:
            im = src.resize((n, n), Image.LANCZOS)
            r, g, b, a = im.split()
            out.append((n, n, Image.merge("RGBA", (a, r, g, b)).tobytes()))
    except Exception:
        log.exception("Tray: could not load the icon for the tray (non-fatal)")
    return out


class SniTray:
    def __init__(self, title, icon_png, on_activate, on_action):
        self.title, self.icon_png = title, icon_png
        self.on_activate, self.on_action = on_activate, on_action
        self.bus = None
        self.revision = 1
        self.tree = []
        self._nodes = {}        # int id -> node (for dbusmenu)
        self._actions = {}      # int id -> action id string
        self._sig = None
        self._pix = None
        self.registered = False

    # ---- lifecycle
    def start(self):
        self.bus = Service()
        name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        self.bus.request_name(name)
        self.name = name
        self.bus.export(ITEM_PATH, ITEM_IFACE, {
            "ContextMenu": ("ii", "", lambda x, y: None),
            "Activate": ("ii", "", self._activate),
            "SecondaryActivate": ("ii", "", self._activate),
            "Scroll": ("is", "", lambda d, o: None),
        }, props=self._item_props, xml=_ITEM_XML)
        self.bus.export(MENU_PATH, MENU_IFACE, {
            "GetLayout": ("iias", "u(ia{sv}av)", self._get_layout),
            "GetGroupProperties": ("aias", "a(ia{sv})", self._get_group),
            "Event": ("isvu", "", self._event),
            "AboutToShow": ("i", "b", lambda i: (False,)),
        }, props=self._menu_props, xml=_MENU_XML)
        for w in WATCHERS:
            self.bus.watch_name_owner(w, lambda owner: self._register() if owner else setattr(self, "registered", False))
        self._register()

    def _register(self):
        for w in WATCHERS:
            try:
                if not self.bus.name_has_owner(w):
                    continue
                self.bus.call(w, "/StatusNotifierWatcher", w, "RegisterStatusNotifierItem", "s", (self.name,))
                self.registered = True
                log.info(f"Tray: registered with {w}")
                return
            except Exception:
                log.exception(f"Tray: registering with {w} failed (non-fatal)")
        log.info("Tray: no StatusNotifierWatcher on the bus yet (no panel tray) - will register when one appears")

    def pump(self):
        """Call every ~100 ms. Returns False when the bus connection is gone."""
        if self.bus is None:
            return False
        try:
            self.bus.pump()
            return True
        except Exception:
            log.exception("Tray: D-Bus connection lost")
            self.stop()
            return False

    def stop(self):
        b, self.bus = self.bus, None
        if b is not None:
            b.close()
        self.registered = False

    # ---- item
    def _item_props(self):
        if self._pix is None:
            self._pix = icon_pixmaps(self.icon_png)
        return {"Category": ("s", "ApplicationStatus"), "Id": ("s", "snippysnappy"), "Title": ("s", self.title),
                "Status": ("s", "Active"), "WindowId": ("u", 0), "IconName": ("s", ""),
                "IconPixmap": ("a(iiay)", self._pix), "ItemIsMenu": ("b", False), "Menu": ("o", MENU_PATH),
                "ToolTip": ("(sa(iiay)ss)", ("", [], self.title, ""))}

    def _activate(self, x, y):
        self.on_activate()

    # ---- menu
    def _menu_props(self):
        return {"Version": ("u", 3), "TextDirection": ("s", "ltr"), "Status": ("s", "normal"), "IconThemePath": ("as", [])}

    def refresh(self, tree):
        """Give the menu a new content; the panel is told only when something actually changed."""
        sig = repr(tree)
        if sig == self._sig:
            return
        self._sig, self.tree = sig, tree
        self._nodes, self._actions = {0: {"children": tree}}, {}
        self._next = 1
        self._number(tree)
        self.revision += 1
        if self.bus is not None:
            try:
                self.bus.emit(MENU_PATH, MENU_IFACE, "LayoutUpdated", "ui", (self.revision, 0))
            except Exception:
                log.exception("Tray: could not announce the new menu (non-fatal)")

    def _number(self, nodes):
        for n in nodes:
            n["_n"] = self._next
            self._nodes[self._next] = n
            if n.get("id"):
                self._actions[self._next] = n["id"]
            self._next += 1
            if n.get("children"):
                self._number(n["children"])

    def _props_of(self, n):
        if n.get("separator"):
            return {"type": ("s", "separator")}
        d = {"label": ("s", n.get("label", "")), "enabled": ("b", bool(n.get("enabled", True)))}
        if n.get("children"):
            d["children-display"] = ("s", "submenu")
        return d

    def _layout(self, node_id, depth):
        n = self._nodes.get(node_id, {"children": []})
        props = {"children-display": ("s", "submenu")} if node_id == 0 else self._props_of(n)
        kids = []
        if depth != 0:
            for c in n.get("children") or []:
                kids.append(("(ia{sv}av)", self._layout(c["_n"], depth - 1 if depth > 0 else depth)))
        return (node_id, props, kids)

    def _get_layout(self, parent, depth, names):
        return (self.revision, self._layout(parent, depth))

    def _get_group(self, ids, names):
        return ([(i, self._props_of(self._nodes[i])) for i in (ids or list(self._nodes)) if i in self._nodes and i != 0],)

    def _event(self, node_id, event, data, ts):
        if event == "clicked" and node_id in self._actions:
            self.on_action(self._actions[node_id])
