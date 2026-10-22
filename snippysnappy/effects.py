# snippysnappy v1.0.0
r"""
Film/audio effect registry and plugin loader.

Built-in video effects (Film > Mode: Effects): Fade, Checkerboard.
More effects - audio or video - can be added by dropping a .py file into the plugins folder:

    ~/.config/snippysnappy/plugins/        (PLUGIN_DIR in your config.py overrides it; PLUGINS_ENABLED = False turns plugins off)

The folder is created on first run. Every *.py file in it (not starting with "_" or ".") is run once when a film
window opens. A file that fails to load is logged and skipped; it never stops the app. Plugins are ordinary Python
with full access to your account - only install ones you trust (same trust level as your config.py).

Minimal plugin (an audio effect; it also shows up in the Audio tool's Type menu):

    from snippysnappy.effects import register_effect, Param

    def build(c):
        # c.p = the layer's parameter values (already validated/clamped), c.start / c.end = seconds in the source,
        # c.t = the ffmpeg time expression to use inside enable=/expressions, c.enable = between(<t>,start,end)
        return f"bass=g={c.p['gain']}:enable='{c.enable}'"

    register_effect(id="bassboost", kind="audio", label="Bass boost", build=build,
                    params=[Param("gain", "Gain (dB)", "int", default=8, lo=0, hi=24)])

For kind="video", build(c) returns either a plain ffmpeg filter chain ("vignette=angle=0.6:enable='...'"), or a full
graph fragment that reads label c.src and writes label c.dst (use c.tmp("a") for extra unique labels):

    return f"[{c.src}]negate=enable='{c.enable}'[{c.dst}]"

Fast path for anything visual (use this - per-pixel ffmpeg expressions are very slow at 1080p): draw the effect
with Pillow and let ffmpeg overlay it. In build(c):

    clip = c.render_clip(draw, w, h)       # draw(i, n, p) -> PIL image (RGBA, w x h), p = 0..1 over the layer
    return c.overlay_clips([{"clip": clip, "w": W, "h": H, "x": "0", "y": "0"}])   # x/y: overlay expressions

c.width / c.height = frame size, c.fps, c.duration; c.workdir() = scratch folder. See the lens flare and explosion
plugins for full examples. c.blend_with_color(expr, color) is the slow per-pixel alternative (A = clip, B = colour).
Param kinds: "int", "float" (lo/hi/step) and "choice" (choices=[("value", "Label"), ...]).
Values reach build() only after being checked against the declared type/range/choices, so they are safe to put
in a filter string. Ids are lowercase letters, digits and underscores; they must be unique.
"""
import importlib.util
import math
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from .logging_setup import log

_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_RESERVED = {"mute", "boost", "fade_in", "fade_out"}      # the built-in audio layer types
_KINDS = ("video", "audio")

COLORS = [("black", "Black"), ("white", "White"), ("gray", "Gray"), ("red", "Red"), ("green", "Green"), ("blue", "Blue")]
DIRECTIONS = [("in", "In"), ("out", "Out")]

_REG = {}
_PLUGINS = {}          # file name -> "ok (n effects)" | "failed: ..."
_loaded = False
_current = "built-in"

_README = """snippysnappy plugins
====================
Put .py files in this folder to add audio/video effects to the Film window
(they are loaded when a film window opens; restart snippysnappy after editing).
A plugin that fails to load is logged (set LOG_LEVEL = "INFO" in config.py to
see which plugins loaded) and skipped. Files starting with _ or . are ignored.
Plugins run with your full permissions - only use ones you trust.

Example (save as bassboost.py):

    from snippysnappy.effects import register_effect, Param

    def build(c):
        return f"bass=g={c.p['gain']}:enable='{c.enable}'"

    register_effect(id="bassboost", kind="audio", label="Bass boost", build=build,
                    params=[Param("gain", "Gain (dB)", "int", default=8, lo=0, hi=24)])

Audio effects appear in the Audio tool's Type menu; video effects in
Film > Mode: Effects. Full API notes: see the docstring of snippysnappy/effects.py.
"""


class Param:
    """One adjustable value of an effect. kind: "int" | "float" | "choice"."""

    def __init__(self, key, label=None, kind="int", default=0, lo=0, hi=100, step=1, choices=None):
        if not isinstance(key, str) or not _ID_RE.match(key):
            raise ValueError(f"Param key {key!r} must be lowercase letters/digits/underscores")
        if kind not in ("int", "float", "choice"):
            raise ValueError(f"Param {key!r}: kind must be 'int', 'float' or 'choice'")
        self.key, self.label, self.kind = key, str(label or key), kind
        if kind == "choice":
            ch = []
            for c in (choices or []):
                ch.append((str(c[0]), str(c[1])) if isinstance(c, (tuple, list)) else (str(c), str(c)))
            if not ch:
                raise ValueError(f"Param {key!r}: a 'choice' needs choices=[...]")
            self.choices = ch
            self.default = str(default) if str(default) in [v for v, _l in ch] else ch[0][0]
            self.lo = self.hi = self.step = None
        else:
            self.choices = None
            conv = int if kind == "int" else float
            self.lo, self.hi, self.step = conv(lo), conv(hi), (conv(step) if kind == "int" else float(step))
            if self.lo > self.hi:
                raise ValueError(f"Param {key!r}: lo > hi")
            self.default = self.coerce(default)

    def coerce(self, v):
        """Any stored value -> a valid one (clamped / rounded / replaced by the default)."""
        if self.kind == "choice":
            return str(v) if str(v) in [c[0] for c in self.choices] else self.default
        try:
            x = float(v)
        except (TypeError, ValueError):
            return getattr(self, "default", self.lo)
        if x != x or x in (float("inf"), float("-inf")):
            return getattr(self, "default", self.lo)
        x = max(self.lo, min(self.hi, x))
        return int(round(x)) if self.kind == "int" else round(x, 4)


class Effect:
    def __init__(self, id, kind, label, build, params=(), default_len=1.0, source="built-in"):
        self.id, self.kind, self.label, self.build = id, kind, label, build
        self.params = list(params)
        self.default_len = None if default_len is None else float(default_len)   # None = whole runtime
        self.source = source

    def defaults(self):
        return {p.key: p.default for p in self.params}

    def values(self, stored):
        stored = stored if isinstance(stored, dict) else {}
        return {p.key: p.coerce(stored.get(p.key, p.default)) for p in self.params}


def register_effect(id, kind, label, build, params=(), default_len=1.0):
    """Add an effect. Raises ValueError for a bad definition (the plugin loader logs it and moves on)."""
    if not isinstance(id, str) or not _ID_RE.match(id):
        raise ValueError(f"effect id {id!r}: use lowercase letters, digits and underscores (start with a letter)")
    if kind not in _KINDS:
        raise ValueError(f"effect {id!r}: kind must be 'video' or 'audio'")
    if not callable(build):
        raise ValueError(f"effect {id!r}: build must be a function")
    if id in _RESERVED:
        raise ValueError(f"effect id {id!r} is reserved for a built-in audio layer type")
    if id in _REG:
        raise ValueError(f"effect id {id!r} is already registered by {_REG[id].source}")
    ps = [p if isinstance(p, Param) else Param(*p) for p in params]
    keys = [p.key for p in ps]
    if len(set(keys)) != len(keys):
        raise ValueError(f"effect {id!r}: duplicate parameter keys")
    _REG[id] = Effect(id, kind, str(label or id), build, ps, default_len, _current)
    return _REG[id]


def get(effect_id):
    return _REG.get(effect_id)


def effects(kind=None):
    return [e for e in _REG.values() if kind is None or e.kind == kind]


def plugin_status():
    return dict(_PLUGINS)


class Ctx:
    """What build() receives."""

    def __init__(self, eff, params, start, end, kind, src=None, dst=None, tag="fx", off=0.0, width=0, height=0, fps=25.0, duration=0.0):
        self.eff = eff
        self.p = eff.values(params)
        self.start, self.end = float(start), float(end)
        self.kind = kind
        self.src, self.dst = src, dst
        self._tag = tag
        self.t = f"(t+{off:.3f})" if off else "t"
        # 2 ms of slack each side: a frame exactly on the boundary (e.g. the last frame) is not lost to rounding
        self.enable = f"between({self.t},{self.start - 0.002:.4f},{self.end + 0.002:.4f})"
        self.duration = end - start
        self.width, self.height, self.fps, self.total = width, height, fps, duration

    def tmp(self, name):
        return f"{self._tag}_{name}"

    def blend_with_color(self, expr, color="black"):
        """Graph: clip (A) blended with a solid colour (B) by per-pixel `expr`, only inside [start, end].
        Simple, but ffmpeg evaluates the expression for every pixel of every frame - very slow at 1080p.
        For anything non-trivial pre-render with render_clip() and overlay_clips() instead (fast)."""
        a, b, c = self.tmp("a"), self.tmp("b"), self.tmp("c")
        return (f"[{self.src}]split[{a}][{b}];"
                f"[{b}]drawbox=x=0:y=0:w=iw:h=ih:color={color}:t=fill[{c}];"
                f"[{a}][{c}]blend=all_expr='{expr}':enable='{self.enable}'[{self.dst}]")

    # ---- fast path: draw the effect with Pillow, overlay it with ffmpeg ----

    def workdir(self):
        """A scratch folder for this effect (removed with the app's temp folder; do not rely on it afterwards)."""
        if getattr(self, "_wd", None) is None:
            d = tempfile.mkdtemp(prefix="fx_")
            if any(ch in d for ch in "\\':,;[]="):          # characters that would need escaping in a filtergraph
                d = tempfile.mkdtemp(prefix="fx_", dir="/tmp")
            self._wd = d
        return self._wd

    def frame_count(self, fps=None):
        fps = float(fps or self.clip_fps())
        return max(2, int(math.ceil(self.duration * fps)) + 1)

    def clip_fps(self):
        return min(max(float(self.fps or 25.0), 1.0), 60.0)

    def render_clip(self, draw, w, h, name="clip"):
        """Write a short lossless RGBA clip of this effect's duration and return its path.
        draw(i, n, p) -> PIL image (any mode; converted to RGBA, size must be w x h); p = 0..1 progress."""
        fps = self.clip_fps()
        n = self.frame_count(fps)
        path = os.path.join(self.workdir(), f"{name}_{self._tag}.mov")
        cmd = ["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{int(w)}x{int(h)}",
               "-r", f"{fps:.4f}", "-i", "-", "-c:v", "png", "-pix_fmt", "rgba", path]
        with tempfile.TemporaryFile() as err:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=err)
            try:
                for i in range(n):
                    im = draw(i, n, i / (n - 1)).convert("RGBA")
                    if im.size != (int(w), int(h)):
                        raise ValueError(f"effect {self.eff.id!r}: draw() returned {im.size}, expected {(int(w), int(h))}")
                    proc.stdin.write(im.tobytes())
                proc.stdin.close()
                rc = proc.wait()
            except BaseException:
                proc.kill()
                proc.wait()
                raise
            if rc != 0:
                err.seek(0)
                raise RuntimeError("ffmpeg could not write the effect clip: " + err.read().decode("utf-8", "replace")[-300:])
        return path

    def overlay_clips(self, layers):
        """Graph fragment: overlay clips (from render_clip) on c.src -> c.dst during [start, end].
        layers: [{"clip": path, "x": "0", "y": "0", "w": None, "h": None, "flags": "bicubic"}, ...]
        x / y are overlay expressions (may use t = time in the film, w/h = overlay size, main_w/main_h);
        w / h scale the clip first. Frames outside the range pass through untouched."""
        parts, cur = [], self.src
        for k, L in enumerate(layers):
            out = self.dst if k == len(layers) - 1 else self.tmp(f"o{k}")
            m = self.tmp(f"m{k}")
            clip = str(L["clip"]).replace("'", "'\\''")
            chain = f"movie='{clip}',setpts=PTS-STARTPTS+{self.start:.4f}/TB"
            if L.get("w") and L.get("h"):
                chain += f",scale={int(L['w'])}:{int(L['h'])}:flags={L.get('flags', 'bicubic')}"
            parts.append(f"{chain}[{m}]")
            parts.append(f"[{cur}][{m}]overlay=x='{L.get('x', '0')}':y='{L.get('y', '0')}':eval=frame:"
                         f"format=auto:enable='{self.enable}'[{out}]")     # the clip's last frame is held; enable= ends the effect
            cur = out
        return ";".join(parts)


def build_video(eff, params, start, end, src, dst, **kw):
    c = Ctx(eff, params, start, end, "video", src=src, dst=dst, tag=dst, **kw)
    frag = eff.build(c)
    if not isinstance(frag, str) or not frag.strip():
        raise ValueError(f"effect {eff.id!r} returned no filter")
    frag = frag.strip()
    if f"[{dst}]" in frag:          # a full graph fragment (it names c.dst); otherwise a plain filter chain
        return frag
    if frag.startswith("[") or ";" in frag:
        raise ValueError(f"effect {eff.id!r}: a graph fragment must write its result to [c.dst] ({dst})")
    return f"[{src}]{frag}[{dst}]"


def build_audio(eff, params, start, end, off=0.0):
    c = Ctx(eff, params, start, end, "audio", off=off)
    frag = eff.build(c)
    if not isinstance(frag, str) or not frag.strip():
        raise ValueError(f"effect {eff.id!r} returned no filter")
    return frag.strip()


# ---------------------------------------------------------------------------
# built-in video effects
# ---------------------------------------------------------------------------

_RGB = {"black": (0, 0, 0), "white": (255, 255, 255), "gray": (128, 128, 128),
        "red": (220, 30, 30), "green": (30, 180, 60), "blue": (40, 80, 220)}


def _build_fade(c):
    from PIL import Image
    rgb = _RGB.get(c.p["color"], (0, 0, 0))
    out = c.eff.id == "vfade_out"

    def draw(i, n, p):
        a = p if out else 1.0 - p               # how much of the colour covers the picture
        return Image.new("RGBA", (16, 16), rgb + (int(round(255 * a)),))

    clip = c.render_clip(draw, 16, 16, "fade")
    return c.overlay_clips([{"clip": clip, "w": c.width, "h": c.height}])


def _build_checker(c):
    from PIL import Image, ImageDraw
    size = int(c.p["size"])
    rgb = _RGB.get(c.p["color"], (0, 0, 0))
    out = c.p["direction"] == "out"
    k = min(12, size)                                       # sprite pixels per square (scaled up with nearest-neighbour)
    nx, ny = int(math.ceil(c.width / size)), int(math.ceil(c.height / size))
    sw, sh = nx * k, ny * k

    def draw(i, n, p):
        if out:
            p = 1.0 - p
        im = Image.new("RGBA", (sw, sh), rgb + (255,))
        dr = ImageDraw.Draw(im)
        for cy in range(ny):
            for cx in range(nx):
                par = (cx + cy) % 2                         # one parity of squares opens first, then the other
                q = max(0.0, min(1.0, 2 * p - par))
                if q <= 0:
                    continue
                if q >= 1:
                    box = (cx * k, cy * k, (cx + 1) * k - 1, (cy + 1) * k - 1)
                else:
                    half = q * k / 2.0
                    mx, my = (cx + 0.5) * k, (cy + 0.5) * k
                    box = (int(round(mx - half)), int(round(my - half)), int(round(mx + half)) - 1, int(round(my + half)) - 1)
                    if box[2] < box[0] or box[3] < box[1]:
                        continue
                dr.rectangle(box, fill=(rgb[0], rgb[1], rgb[2], 0))
        return im

    clip = c.render_clip(draw, sw, sh, "checker")
    return c.overlay_clips([{"clip": clip, "w": nx * size, "h": ny * size, "flags": "neighbor"}])


def _register_builtins():
    global _current
    _current = "built-in"
    for _id, _label in (("vfade_in", "Fade in"), ("vfade_out", "Fade out")):
        register_effect(_id, "video", _label, _build_fade, [Param("color", "Color", "choice", "black", choices=COLORS)])
    register_effect("checker", "video", "Checkerboard", _build_checker, [
        Param("direction", "Direction", "choice", "in", choices=DIRECTIONS),
        Param("size", "Square", "int", 64, lo=8, hi=600, step=8),
        Param("color", "Color", "choice", "black", choices=COLORS),
    ])


_register_builtins()


# ---------------------------------------------------------------------------
# plugins
# ---------------------------------------------------------------------------

def _cfg(name, default):
    try:
        from . import constants
        return getattr(constants, name, default)
    except Exception:
        return default


def plugin_dir():
    d = _cfg("PLUGIN_DIR", None)
    if d:
        return Path(os.path.expanduser(str(d)))
    base = Path(os.environ.get("XDG_CONFIG_HOME") or "~/.config").expanduser()
    return base / "snippysnappy" / "plugins"


def ensure_plugin_dir(path=None):
    """Create the plugins folder (with a short README the first time). Never raises."""
    path = Path(path) if path else plugin_dir()
    try:
        fresh = not path.exists()
        path.mkdir(parents=True, exist_ok=True)
        if fresh and not (path / "README.txt").exists():
            (path / "README.txt").write_text(_README, encoding="utf-8")
        return path
    except OSError as e:
        log.warning(f"Plugins: could not create {path} ({e}) - plugins unavailable")
        return None


def ensure_loaded():
    """Load plugins once per process. Returns True the first time (so callers can refresh their lists)."""
    global _loaded, _current
    if _loaded:
        return False
    _loaded = True
    if not _cfg("PLUGINS_ENABLED", True):
        log.info("Plugins: disabled (PLUGINS_ENABLED = False)")
        return True
    d = ensure_plugin_dir()
    if d is None:
        return True
    try:
        files = sorted(p for p in d.glob("*.py") if p.is_file() and not p.name.startswith(("_", ".")))
    except OSError as e:
        log.warning(f"Plugins: could not list {d} ({e})")
        return True
    from .user_config import untrusted_reason
    for path in files:
        why = untrusted_reason(path)
        if why:
            _PLUGINS[path.name] = f"not loaded: {why}"
            log.warning(f"Plugins: NOT loading {path.name}: {why}")
            continue
        before = len(_REG)
        _current = path.name
        try:
            spec = importlib.util.spec_from_file_location(f"snippysnappy_plugin_{path.stem}", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            n = len(_REG) - before
            _PLUGINS[path.name] = f"ok ({n} effect{'s' if n != 1 else ''})"
            log.info(f"Plugins: loaded {path.name} ({n} effect(s))")
        except BaseException as e:      # SystemExit from a plugin must not kill the app either
            if isinstance(e, KeyboardInterrupt):
                raise
            _PLUGINS[path.name] = f"failed: {e}"
            log.warning(f"Plugins: {path.name} failed to load and was skipped: {type(e).__name__}: {e}")
        finally:
            _current = "built-in"
    return True
