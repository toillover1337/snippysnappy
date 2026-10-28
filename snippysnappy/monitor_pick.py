# snippysnappy v1.0.3
"""Chooses which monitor a new decorated window opens on (see NEW_WINDOW_MONITOR in constants)."""
import importlib

from . import constants
from .logging_setup import log


def _find(name):
    pkg = importlib.import_module(__package__)
    for mod_name in ("capture", None):
        mod = importlib.import_module(f"{__package__}.{mod_name}") if mod_name else pkg
        fn = getattr(mod, name, None)
        if fn is not None:
            return fn
    raise AttributeError(name)


def pick_review_bounds(monitors=None, active=None):
    """Returns (x, y, w, h). 'inactive' (default) picks a monitor other than the one with the pointer; with one monitor, that one."""
    if monitors is None:
        monitors = _find("get_monitors")()
    rects = [(m[1], m[2], m[3], m[4]) for m in monitors]
    if not rects:
        return active
    if active is None:
        try:
            px, py = _find("get_mouse_position")()
            hit = _find("monitor_at_point")(monitors, px, py)
            active = tuple(hit[1:5])
        except Exception:
            log.warning("Monitor pick: could not read the pointer position - treating the first monitor as active")
            active = rects[0]
    active = tuple(active)
    if getattr(constants, "NEW_WINDOW_MONITOR", "inactive") != "inactive":
        return active
    others = [r for r in rects if r != active]
    chosen = others[0] if others else active
    log.info(f"Monitor pick: active monitor {active}, new window goes to {chosen}")
    return chosen
