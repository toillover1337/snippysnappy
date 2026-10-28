# snippysnappy v1.0.3
"""
The snippyfilm review/playback window (FilmApp) - a decorated window in
the same visual style as SelectorApp's own decorated windows, for viewing
a just-recorded (or later, opened) snippyfilm. This first version is
deliberately basic, per design: playback, scrubbing, Save/Copy/Cancel,
and the Tabs/Rescale/dimension status row are all real and working;
Edit/Tools are visible but are no-ops for now (they exist here purely so
the eventual layout can be seen/organized against, not because they do
anything yet) - real editing (trim/splice/measure/magnify/pin on the
current frame) is a later, separate phase.

Playback architecture: on open, the WHOLE video is decoded to a sequence
of PNG frames in a temp directory via one ffmpeg call, rather than
streaming frames live during playback. This is a deliberate simplicity
trade for v1 (a longer recording takes a moment to "load" up front) that
also happens to set up every later phase cleanly for free: frame-grab-
while-scrubbing, splicing, and measure/magnify/pin all become "read one
already-decoded PNG off disk" instead of needing their own seek/decode
logic - each frame is just an ordinary image once this runs.
"""
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import uuid
from collections import OrderedDict
from pathlib import Path

from .constants import (
    WINDOW_TITLE_BASE, TOOLBAR_HEIGHT, BUTTON_PADY, MIN_WINDOW_WIDTH,
    MAX_WINDOW_WIDTH, MAX_WINDOW_HEIGHT, SIDE_PANEL_WIDTH_RESERVE,
    MIN_CANVAS_DISPLAY_SIZE, SCROLLBAR_THICKNESS, AUTO_SCALE_TO_FIT, VIEWPORT_BORDER_FUDGE,
)
from .icon import _load_app_icon
from .logging_setup import log, run_logged
from .notify_toast import notify, timestamp
from .dialogs import confirm_discard
from .save_as_dialog import ask_save_as
from .constants import WARN_ON_ACTIVE_CLOSE
from .side_panel import make_scrollable, add_window_row, RowReorder, ordered, move_in_order, inline_rename, window_name, small_button, install_button_tooltips, build_windows_rows
from .constants import PANEL_START_AT_TOP, EXIT_BEHAVIOR
from .coordinator import (
    init_coordinator, _FilmHandle, _register_film, _unregister_film,
    remote_windows_all, request_remote_focus, request_remote_window_close,
    request_remote_window_rename, broadcast_window_roster, request_global_restart, set_restart_handler, remote_window_tab_labels, remote_window_hidden, request_remote_window_hide,
)


FRAME_CACHE_MAX = 48   # decoded display-size frames kept for scrubbing


def probe_video(path):
    """(width, height, fps, total_frames) via ffprobe. fps prefers avg_frame_rate (variable-rate Matroska can report a bogus r_frame_rate like 1000)."""
    out = run_logged(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height,avg_frame_rate,r_frame_rate:format=duration",
            "-of", "json", str(path),
        ],
        capture_output=True, text=True, check=True,
    ).stdout
    info = json.loads(out)
    stream = info["streams"][0]
    width, height = int(stream["width"]), int(stream["height"])

    def _rate(text):
        try:
            num, den = str(text).split("/")
            val = float(num) / float(den)
        except (ValueError, ZeroDivisionError):
            return 0.0
        return val if 1.0 <= val <= 120.0 else 0.0

    fps = _rate(stream.get("avg_frame_rate")) or _rate(stream.get("r_frame_rate")) or 30.0
    if str(stream.get("avg_frame_rate", "")).startswith("1000/") and fps and abs(fps - round(fps)) / fps < 0.02:
        fps = float(round(fps))   # patch 46: millisecond timestamps (wf-recorder on Wayland) read as 1000/33 = 30.3
    try:
        duration = float(info["format"]["duration"])
    except (KeyError, ValueError, TypeError):
        duration = 0.0
    if duration <= 0:
        log.warning(f"Film viewer: ffprobe reported no duration for {path} - treating it as a single frame")
    total = max(1, int(round(duration * fps))) if duration > 0 else 1
    return width, height, fps, total


def _p40_pin_stderr():
    """Append handle for pin processes' stderr (faulthandler output), trimmed once it passes 1 MB. The child gets its own copy of the descriptor."""
    from .tempdir_setup import cache_dir
    path = cache_dir() / "pin-stderr.log"
    try:
        if path.stat().st_size > 1_000_000:
            path.write_bytes(path.read_bytes()[-200_000:])
    except OSError:
        pass
    return open(path, "ab")


def _p40_pin_stage_parent():
    """The temp base folder (~/.cache/snippysnappy/tmp), so leftovers are swept; the video pin moves the folder into its own p<pid> folder."""
    t = Path(tempfile.gettempdir())
    return str(t.parent if t.name.startswith("p") and t.name[1:].isdigit() else t)


class _PlaybackStream:
    """One ffmpeg process decoding raw RGB frames at display size from a start time; a reader thread feeds a small queue. stop() kills it."""

    def __init__(self, path, start_seconds, width, height):
        self.width, self.height = width, height
        self._queue = queue.Queue(maxsize=12)
        self._stop = threading.Event()
        self._done = threading.Event()
        self._proc = subprocess.Popen(
            [
                "ffmpeg", "-v", "error", "-ss", f"{max(0.0, start_seconds):.4f}", "-i", str(path),
                "-vf", f"scale={width}:{height}:flags=bilinear", "-pix_fmt", "rgb24", "-f", "rawvideo", "-",
            ],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        size = self.width * self.height * 3
        try:
            while not self._stop.is_set():
                data = self._proc.stdout.read(size)
                if len(data) < size:
                    break
                while not self._stop.is_set():
                    try:
                        self._queue.put(data, timeout=0.1)
                        break
                    except queue.Full:
                        continue
        except Exception:
            log.exception("Film viewer: playback reader failed")
        finally:
            self._done.set()

    def get(self):
        try:
            return self._queue.get_nowait()
        except queue.Empty:
            return None

    def finished(self):
        return self._done.is_set() and self._queue.empty()

    def stop(self):
        self._stop.set()
        try:
            self._proc.kill()
        except Exception:
            pass
        try:
            self._proc.wait(timeout=1)
        except Exception:
            pass


def show_film_viewer(video_path, monitor_bounds=None):
    """
    Entry point - creates its own fresh Tk root and runs its own
    mainloop (called AFTER the recorder's own floating-control mainloop
    has already returned, never concurrently with it). Blocks until the
    viewer window is closed (Save/Cancel).
    """
    root = tk.Tk(className="snippysnappy-film")
    root.withdraw()
    init_coordinator(root)
    if monitor_bounds is None:
        try:
            from .monitor_pick import pick_review_bounds
            monitor_bounds = pick_review_bounds()
        except Exception:
            log.exception("Film viewer: could not pick a monitor (non-fatal) - using the whole screen")
    app = FilmApp(root, video_path, monitor_bounds)
    root.deiconify()
    root.mainloop()
    return app


class FilmApp:
    def __init__(self, *args, **kwargs):
        self._p33_init(*args, **kwargs)
        try:
            from .leader import typing_focus

            def make(action):
                def handler(event):
                    from . import constants as _c
                    if not getattr(_c, "FILM_SPLICE_VIM", True) or typing_focus(self.root):
                        return None
                    try:
                        if not self._p10_splice_ui():
                            return None
                    except Exception:
                        return None
                    try:
                        if action == "y":
                            self._p32_yank()
                        elif action == "p":
                            self._p31_insert("clip")
                        else:
                            self._p31_insert("copy")
                    except Exception:
                        log.exception(f"Film splice key {action!r} failed (non-fatal)")
                    return "break"
                return handler

            self.root.bind("<y>", make("y"), add="+")
            self.root.bind("<p>", make("p"), add="+")
            self.root.bind("<P>", make("P"), add="+")
        except Exception:
            log.exception("Could not install the splice y/p/P keys in the film window (non-fatal)")
        try:
            from .leader import typing_focus

            def guard(arrows=False):
                from . import constants as _c
                if not getattr(_c, "FILM_SPLICE_VIM", True) or typing_focus(self.root):
                    return False
                try:
                    if not self._p10_splice_ui():
                        return False
                    if arrows:
                        f = self.root.focus_get()
                        if f is not None and f.winfo_class() in ("Scale", "Menubutton", "Listbox"):
                            return False
                except Exception:
                    return False
                return True

            def on_key(fn, needs_visual=False, arrows=False):
                def handler(event):
                    if not guard(arrows):
                        return None
                    if needs_visual and not self._p39_vis_on():
                        return None
                    try:
                        fn()
                    except Exception:
                        log.exception("Film splice key failed (non-fatal)")
                    return "break"
                return handler

            def pending_it(event):
                # in visual mode: i then t selects the whole piece
                if not guard() or not self._p39_vis_on():
                    return None
                if event.keysym == "i":
                    self._p39_pend = "i"
                    return "break"
                if event.keysym == "t" and getattr(self, "_p39_pend", None) == "i":
                    self._p39_pend = None
                    self._p39_whole_piece()
                    return "break"
                self._p39_pend = None
                return None

            def esc():
                if getattr(self, "_p39_vis", False):
                    self._p39_set_vis(False)
                    return True
                return False

            def on_escape(event):
                if not guard() or not getattr(self, "_p39_vis", False):
                    return None
                esc()
                return "break"

            def on_v():
                if self._p39_vis_on():
                    self._p39_set_vis(False)
                else:
                    self._p39_enter_visual()

            b = self.root.bind
            b("<d>", on_key(lambda: self._p39_delete(True)), add="+")
            b("<x>", on_key(lambda: self._p39_delete(False)), add="+")
            b("<v>", on_key(on_v), add="+")
            b("<V>", on_key(lambda: (self._p39_set_vis(True) if not self._p39_vis_on() else None, self._p39_whole_piece())), add="+")
            b("<i>", pending_it, add="+")
            b("<t>", pending_it, add="+")
            for keys, d, big in ((("<h>", "<Left>"), -1, False), (("<l>", "<Right>"), 1, False),
                                 (("<H>", "<Shift-Left>"), -1, True), (("<L>", "<Shift-Right>"), 1, True)):
                for k in keys:
                    b(k, on_key(lambda d=d, big=big: self._p39_move_handle(d, big), needs_visual=True, arrows=k.startswith("<") and len(k) > 4 and k not in ("<h>", "<l>", "<H>", "<L>")), add="+")
            for k in ("<j>", "<k>", "<Up>", "<Down>"):
                b(k, on_key(lambda: self._p39_toggle_handle(), needs_visual=True, arrows=k in ("<Up>", "<Down>")), add="+")
            b("<Escape>", on_escape, add="+")
        except Exception:
            log.exception("Could not install the splice visual/delete keys in the film window (non-fatal)")

    def _init_base(self, root, video_path, monitor_bounds=None, on_closed=None):
        self.root = root
        self._on_closed = on_closed   # called after this window is destroyed (the recorder uses it to end its own hidden root)
        self.video_path = Path(video_path)
        self.frames = []            # list of Path, populated once extraction finishes
        self.frame_index = 0
        self.playing = False
        self._init_lazy_state()
        self._play_job = None
        self._photo = None          # keep a reference - Tk drops the image otherwise
        self.display_scale = 1.0
        self.native_w = self.native_h = 0
        self.fps = 30.0
        self._tmpdir = tempfile.mkdtemp(prefix="snippysnappy-film-frames-")
        self.side_panel_mode = None   # None | "tabs" - see _on_side_panel_choice

        root.title(f"{WINDOW_TITLE_BASE} - {self.video_path.stem}")
        icon = _load_app_icon()
        self._app_icon_image = icon
        if icon is not None:
            root.iconphoto(True, icon)
        root.configure(cursor="crosshair", bg="#1a1a1a")
        root.attributes("-topmost", True)

        if monitor_bounds is not None:
            mx, my, mw, mh = monitor_bounds
        else:
            mx, my = 0, 0
            mw, mh = root.winfo_screenwidth(), root.winfo_screenheight()
        self._monitor_bounds = (mx, my, mw, mh)

        self.main_area = tk.Frame(root, bg="#1a1a1a")
        self.main_area.pack(side="top", fill="both", expand=True)
        # Bottom panel (full width): scrub row on top (scrub bar + time + Re-scale + Tabs), then the status text, then the buttons.
        self.toolbar_container = tk.Frame(root, bg="#222222", height=TOOLBAR_HEIGHT + 30)
        self.toolbar_container.pack(side="bottom", fill="x", before=self.main_area)
        self.toolbar_container.pack_propagate(False)

        self.video_column = tk.Frame(self.main_area, bg="#1a1a1a")
        self.video_column.pack(side="left", fill="both", expand=True)

        self.canvas_frame = tk.Frame(self.video_column, bg="#1a1a1a")
        self.canvas_frame.pack(side="top", fill="both", expand=True)
        self.vbar = self.hbar = None
        if not AUTO_SCALE_TO_FIT:
            self.vbar = tk.Scrollbar(self.canvas_frame, orient="vertical", width=SCROLLBAR_THICKNESS)
            self.hbar = tk.Scrollbar(self.canvas_frame, orient="horizontal", width=SCROLLBAR_THICKNESS)
            self.vbar.pack(side="right", fill="y")
            self.hbar.pack(side="bottom", fill="x")
        self.canvas = tk.Canvas(self.canvas_frame, highlightthickness=0, bg="#000000")
        if not AUTO_SCALE_TO_FIT:
            self.canvas.configure(xscrollcommand=self.hbar.set, yscrollcommand=self.vbar.set)
            self.vbar.config(command=self.canvas.yview)
            self.hbar.config(command=self.canvas.xview)
        self.canvas.pack(side="top", fill="both", expand=True)

        # Scrub bar: lives in video_column.
        self.scrub_frame = tk.Frame(self.toolbar_container, bg="#222222", height=30)
        self.scrub_frame.pack(side="top", fill="x")
        self.scrub_frame.pack_propagate(False)
        self.time_label = tk.Label(self.scrub_frame, text="00:00 / 00:00", fg="white", bg="#222222", font=("", 9))
        self.time_label.pack(side="right", padx=(8, 4))
        self.scrub_canvas = tk.Canvas(self.scrub_frame, height=20, highlightthickness=0, bg="#222222")
        self.scrub_canvas.pack(side="left", fill="both", expand=True)
        self.scrub_canvas.bind("<ButtonPress-1>", self._scrub_press)
        self.scrub_canvas.bind("<ButtonRelease-1>", self._scrub_release)
        self.scrub_canvas.bind("<B1-Motion>", self._on_scrub_click)
        self.scrub_canvas.bind("<Configure>", lambda e: self._draw_scrub_playhead())

        self.side_panel_separator = tk.Frame(self.main_area, width=3, bg="#2a2a2a", relief="ridge", bd=2)
        self.side_panel_container = tk.Frame(self.main_area, bg="#222222")


        self.canvas.create_text(
            10, 10, anchor="nw", fill="#aaaaaa", font=("", 10),
            text="Loading film\u2026", tags=("loading_label",),
        )
        self._build_toolbar()

        self._scrub_right_pad = -1
        root.after(900, self._log_layout)
        install_button_tooltips(root)
        root.bind("<Map>", self._on_map, add="+")
        root.after(50, lambda: setattr(self._handle, "_hide_window", self._hide_to_background))
        self._scrubbing = False
        self.scrub_canvas.pack_configure(padx=(4, 0))
        for w in (root, self.video_column):
            w.bind("<Configure>", self._align_scrub, add="+")
        root.after(300, self._align_scrub)

        threading.Thread(target=self._extract_frames_worker, daemon=True).start()
        root.protocol("WM_DELETE_WINDOW", self._on_window_x)
        self._film_id = uuid.uuid4().hex
        self._handle = _FilmHandle(self._film_id, root, self.video_path.stem, self._discard_now, self._refresh)
        try:
            _register_film(self._handle)
        except Exception:
            log.exception("Film viewer: could not register with the coordinator (non-fatal - other windows' Tabs panels won't list this one)")
        set_restart_handler(lambda: log.info("Film viewer: staying open across a Restart All (only other windows/pins restart)"))

    # ---- frame source: probe once, decode on demand, stream for playback ----
    #
    # Nothing is extracted up front. Scrubbing decodes just the frame you
    # land on (display size, latest request wins, small LRU cache); playback
    # streams one ffmpeg process at display size, dropping frames to stay in
    # sync with the clock. Every later phase (frame grab, measure/magnify/
    # pin on the current frame) goes through get_current_frame_image().

    def _init_lazy_state(self):
        self.total_frames = 0
        self._cache = OrderedDict()
        self._decode_lock = threading.Lock()
        self._decode_busy = False
        self._wanted_index = 0
        self._stream = None
        self._stream_pos = 0
        self._got_first = False
        self._play_t0 = 0.0
        self._play_i0 = 0

    def _on_window_x(self):
        """Decorated X: background the window (recording kept, process alive) unless EXIT_BEHAVIOR is "full_close" - then close but keep the file. Only Cancel / the Windows-panel x discard."""
        self._pause()
        if EXIT_BEHAVIOR == "full_close":
            log.info("Film viewer: decorated X - closing, keeping the film file (full_close)")
            self._close_keep()
        else:
            log.info("Film viewer: decorated X - backgrounding the window (film kept, process stays alive)")
            self._hide_to_background()

    def _hide_to_background(self):
        """Hidden but alive and still listed (Hidden group); showing it again clears the flag."""
        self._pause()
        self.root.withdraw()
        self._handle._hidden = True
        broadcast_window_roster()

    def _on_map(self, event=None):
        if event is not None and event.widget is not self.root:
            return
        if getattr(self._handle, "_hidden", False):
            self._handle._hidden = False
            broadcast_window_roster()

    def _log_layout(self):
        """Diagnostics for the side-panel/toolbar height question: where every piece actually is."""
        try:
            parts = (("root", self.root), ("main_area", self.main_area), ("video_column", self.video_column),
                     ("panel", self.side_panel_container), ("scrub", self.scrub_frame), ("toolbar", self.toolbar_container))
            log.info("Film layout: " + "; ".join(
                f"{n}={w.winfo_width()}x{w.winfo_height()}+{w.winfo_rootx()}+{w.winfo_rooty()}{'' if w.winfo_ismapped() else ' (unmapped)'}"
                for n, w in parts))
        except tk.TclError:
            pass

    def _align_scrub(self, event=None):
        return   # obsolete: Re-scale and Tabs now live in the scrub row itself
        """Scrub bar + time readout end just before the Re-scale button (left padding is fixed); with the side panel open they simply stop at the separator."""
        try:
            btn = getattr(self, "rescale_btn", None)
            right = 8
            if btn is not None and btn.winfo_ismapped():
                col_right = self.video_column.winfo_rootx() + self.video_column.winfo_width()
                right = max(8, col_right - btn.winfo_rootx() + 6)
            if right != self._scrub_right_pad:
                self._scrub_right_pad = right
                self.time_label.pack_configure(padx=(8, right))
        except tk.TclError:
            pass

    def _scrub_press(self, event):
        if self._p25_fx_ui():
            self._p25_state()
            self._p25_before = self._ed5_get("fxlayers")
        if not self._p10_splice_ui():
            if self._p10_splice_ui() and self.total_frames and self._p10_segs:
                i = _p10_seg_at(self._p10_segs, self._ed_t(event.x))
                self._p10_sel = self._p10_segs[i]["id"] if i is not None else None
            self._p2_spress(event)
            if self._p10_splice_ui():
                self._p10_strip_draw()
            return
        if not self.total_frames:
            return
        self._scrubbing = True
        self._p11_drag = None
        self._p11_drop = None
        lay = self._p11_lay_now()
        reg = self._p11_reg
        cur = next((L for L in lay if reg and L["uid"] == reg["pid"]), None)
        if cur is not None:
            xa, xb = self._p11_t_to_x(cur, reg["a"]), self._p11_t_to_x(cur, reg["b"])
            da, db = abs(event.x - xa), abs(event.x - xb)
            if min(da, db) <= 7:
                which = "a" if (da < db or (da == db and event.x < xa)) else "b"
                self._p11_drag = {"kind": "handle", "which": which, "uid": cur["uid"]}
                return
        for L in lay:
            if L["x0"] <= event.x <= L["x1"]:
                self._p11_select(L["uid"])
                if not L.get("ins"):
                    self._p10_seek(self._p11_x_to_t(L, event.x))
                self._p11_drag = {"kind": "piece", "uid": L["uid"], "x0": event.x, "moved": False}
                self._draw_scrub_playhead()
                return

    def _scrub_release(self, event):
        was = bool(getattr(self, "_ed_drag", None))
        self._p11_release(event)
        if not self._p25_fx_ui():
            return
        before = getattr(self, "_p25_before", None)
        self._p25_before = None
        if was and before is not None:
            now = self._ed5_get("fxlayers")
            if now != before:
                self._ed3_acts.append(("set", "fxlayers", before, now))
                self._ed3_redo = []
            self._ed5_refresh(True)

    def _extract_frames_worker(self):
        """Background: probe only (name kept from the old extract-everything version)."""
        try:
            w, h, fps, total = probe_video(self.video_path)
        except Exception:
            log.exception(f"Film viewer: ffprobe failed on {self.video_path}")
            self.root.after(0, lambda: notify("Could not read this film", "ffprobe failed - see the log", blocking=False))
            return
        log.info(f"Film viewer: {self.video_path} is {w}x{h} @ {fps:.2f}fps, ~{total} frame(s)")
        self.root.after(0, lambda: self._on_source_ready(w, h, fps, total))

    def _on_source_ready(self, w, h, fps, total):
        self.native_w, self.native_h, self.fps, self.total_frames = w, h, fps, total
        self.canvas.delete("loading_label")
        self._fit_window_to_video()
        self._show_frame(0)
        self._update_dimension_label()

    def _display_size(self):
        return (
            max(1, int(round(self.native_w * self.display_scale))),
            max(1, int(round(self.native_h * self.display_scale))),
        )

    def _fit_window_to_video(self):
        self._fw_fit()         # that one swaps root.geometry for a spy and removes it again - put the guard back
        self._p33_install_guard()

    def _decode_frame(self, index, width, height, fast=False):
        """Blocking single-frame decode at (width, height); returns a PIL RGB image or None."""
        from PIL import Image
        t = min(index, max(0, self.total_frames - 1)) / self.fps
        out = subprocess.run(
            [
                "ffmpeg", "-v", "error", *(["-noaccurate_seek"] if fast else []), "-ss", f"{t:.4f}", "-i", str(self.video_path), "-frames:v", "1",
                "-vf", f"scale={width}:{height}:flags=bilinear", "-pix_fmt", "rgb24", "-f", "rawvideo", "-",
            ],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True,
        ).stdout
        if len(out) != width * height * 3:
            return None
        return Image.frombytes("RGB", (width, height), out)

    def _cache_put(self, key, img):
        # patch 43: bounded by memory (FILM_FRAME_CACHE_MB) as well as count - 48 full 1080p
        # frames alone were ~300 MB per film window.
        from . import constants as _c
        budget = max(8, int(getattr(_c, "FILM_FRAME_CACHE_MB", 128))) * 1024 * 1024
        self._cache[key] = img
        self._cache.move_to_end(key)
        per = img.width * img.height * len(img.getbands())
        while len(self._cache) > 1 and (len(self._cache) > FRAME_CACHE_MAX or len(self._cache) * per > budget):
            self._cache.popitem(last=False)

    def _request_decode(self, index):
        with self._decode_lock:
            self._wanted_index = index
            if self._decode_busy:
                return
            self._decode_busy = True
        threading.Thread(target=self._decode_worker, daemon=True).start()

    def _decode_worker(self):
        while True:
            with self._decode_lock:
                idx = self._wanted_index
            w, h = self._display_size()
            img = None
            try:
                img = self._decode_frame(idx, w, h, fast=getattr(self, "_scrubbing", False))
            except Exception:
                log.exception(f"Film viewer: could not decode frame {idx}")
            self.root.after(0, lambda i=idx, im=img, ww=w, hh=h: self._on_decoded(i, im, ww, hh))
            with self._decode_lock:
                if self._wanted_index == idx:
                    self._decode_busy = False
                    break

    def _on_decoded(self, index, img, w, h):
        if img is None:
            return
        self._cache_put((index, w, h), img)
        if self.playing:
            return
        if index == self.frame_index and (w, h) == self._display_size():
            self._display_image(img)

    # ---- display ----

    def _display_image(self, img):
        self._ed2_raw = img
        try:
            img = self._ed2_compose(img)
        except Exception:
            log.exception("Film edit: overlay compose failed (non-fatal)")
        self._g_disp = img
        from PIL import ImageTk
        # patch 43: paste into the existing Tk image when the size/mode match (about 40% cheaper per
        # frame than a new PhotoImage + canvas item); raising "frame" keeps the same stacking order
        # the delete + create gave.
        ph = self._photo
        if (ph is not None and getattr(self, "_photo_key", None) == (img.width, img.height, img.mode)
                and self.canvas.find_withtag("frame")):
            ph.paste(img)
            self.canvas.tag_raise("frame")
        else:
            self._photo = ImageTk.PhotoImage(img)
            self._photo_key = (img.width, img.height, img.mode)
            self.canvas.delete("frame")
            self.canvas.create_image(0, 0, anchor="nw", image=self._photo, tags=("frame",))
        self.canvas.configure(scrollregion=(0, 0, img.width, img.height))
        try:
            self.canvas.tag_raise("measure")
            self.canvas.tag_raise("ocr_stroke")
        except tk.TclError:
            pass
        if self._g_active:
            try:
                self.canvas.tag_raise("magnify")
                if self._g_pos is not None:
                    self._g_draw(*self._g_pos)
            except tk.TclError:
                pass
        try:
            if self._g_pin is not None:
                self._h_draw_pin()
                if not self.playing and (self._g_native is None or self._g_native[0] != self.frame_index):
                    self._g_fetch_native()
        except tk.TclError:
            pass
        try:
            self.canvas.tag_raise("ed3ui")
        except tk.TclError:
            pass
        try:
            self._ed5_draw_crop()
        except tk.TclError:
            pass
        try:
            self._ed9_state()
            self._ed3_draw_ui()
            self._ed9_draw_crop_handles()
            self.canvas.tag_raise("ed9tp")
            self.canvas.tag_raise("ed9dim")
        except tk.TclError:
            pass

    def _show_frame(self, *a, **k):
        r = self._show_frame_base(*a, **k)
        try:
            last = getattr(self, "_p17_last", None)
            if last and abs((self.display_scale or 1.0) - getattr(self, "_p17_scale", 0)) > 1e-6:
                if getattr(self, "_ed2_active", False) and getattr(self, "_ed2_tool", None) == "resize":
                    self._p16_draw(*last)
                else:
                    self._p16_clear()
        except Exception:
            pass
        return r

    def _show_frame_base(self, index):
        """Scrub target: the playhead moves immediately; the picture follows as soon as it is decoded (or instantly from the cache)."""
        if not self.total_frames:
            return
        index = max(0, min(index, self.total_frames - 1))
        self.frame_index = index
        self._draw_scrub_playhead()
        w, h = self._display_size()
        cached = self._cache.get((index, w, h))
        if cached is not None:
            self._cache.move_to_end((index, w, h))
            self._display_image(cached)
        else:
            self._request_decode(index)

    def _show_raw_frame(self, data, index, width, height):
        from PIL import Image
        self.frame_index = max(0, min(index, self.total_frames - 1))
        self._display_image(Image.frombytes("RGB", (width, height), data))
        self._draw_scrub_playhead()

    def get_current_frame_image(self):
        img = self.get_current_frame_image_base()
        if img is None:
            return img
        try:
            if getattr(self, "film_edits", None):
                return self._ed2_compose(img)
        except Exception:
            log.exception("Film viewer: could not add overlays to the grabbed frame (non-fatal)")
        return img

    def get_current_frame_image_base(self):
        """
        A fresh PIL Image of the CURRENT frame at native resolution (a
        blocking one-off decode) - the hook later phases (frame grab,
        Measure/Magnify/Pin on the current frame) call into.
        """
        if not self.total_frames:
            return None
        try:
            return self._decode_frame(self.frame_index, self.native_w, self.native_h)
        except Exception:
            log.exception("Film viewer: could not decode the current frame at native size")
            return None

    # ---- scrub bar ----

    @staticmethod
    def _fmt_time(seconds):
        seconds = max(0, int(seconds))
        h, rem = divmod(seconds, 3600)
        m, sec = divmod(rem, 60)
        return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"

    def _update_time_label(self):
        if not self.fps or not self.total_frames:
            return
        total = self.total_frames / self.fps
        cur = self.frame_index / self.fps
        self.time_label.configure(text=f"{self._fmt_time(cur)} / {self._fmt_time(total)}")

    def _draw_scrub_playhead(self):
        self._p30_draw_scrub()
        if not self._p39_vis_on():
            return
        try:
            cv = self.scrub_canvas
            lay = self._p11_lay_now()
            reg = getattr(self, "_p11_reg", None)
            cur = next((L for L in lay if reg and L["uid"] == reg["pid"]), None) if lay else None
            if cur is None:
                return
            mid = max(10, cv.winfo_height() // 2)
            tags = ("scrub", "p11")
            which = getattr(self, "_p31_at", "b")
            x = self._p11_t_to_x(cur, reg[which])
            cv.create_rectangle(x - 3, mid - 11, x + 3, mid + 11, fill="#ff9900", outline="#ffffff", tags=tags)
            xa, xb = self._p11_t_to_x(cur, reg["a"]), self._p11_t_to_x(cur, reg["b"])
            cv.create_rectangle(xa, mid - 9, xb, mid - 6, fill="#ff9900", outline="", tags=tags)
            cv.create_text(10, cv.winfo_height() - 2, text="VISUAL   h/l move   j/k other handle   it whole piece   y d x   Esc", anchor="sw",
                           fill="#ff9900", font=("", 7), tags=tags)
        except tk.TclError:
            pass

    def _draw_scrub_playhead_base(self):
        """Track with the played portion in blue behind a white round handle; inset so the handle is never clipped at either end."""
        self._update_time_label()
        self.scrub_canvas.delete("scrub")
        w_7 = self.scrub_canvas.winfo_width()
        if w_7 <= 1 or not self.total_frames:
            return
        pad = 8
        span = max(1, w_7 - 2 * pad)
        frac = self.frame_index / max(1, self.total_frames - 1)
        px = pad + int(frac * span)
        mid_3 = max(10, self.scrub_canvas.winfo_height() // 2)
        self.scrub_canvas.create_rectangle(pad, mid_3 - 2, w_7 - pad, mid_3 + 2, fill="#444444", outline="", tags=("scrub",))
        self.scrub_canvas.create_rectangle(pad, mid_3 - 2, px, mid_3 + 2, fill="#00b4ff", outline="", tags=("scrub",))
        self.scrub_canvas.create_oval(px - 6, mid_3 - 6, px + 6, mid_3 + 6, fill="white", outline="#888888", tags=("scrub",))

    def _on_scrub_click(self, event):
        ov = self._p30_active()
        if ov is None:
            if not self._p10_splice_ui():
                drag = getattr(self, "_ed_drag", None)
                if drag:
                    layer = self._ed_active()
                    if layer is None:
                        self._ed_drag = None
                        return
                    dur = self._ed_dur()
                    gap_2 = min(max(0.1, 1.0 / max(self.fps, 1.0)), dur)
                    s, e = self._ed_range(layer)
                    t_2 = self._ed_t(event.x)
                    if drag == "start":
                        s = max(0.0, min(t_2, e - gap_2))
                    else:
                        e = min(dur, max(t_2, s + gap_2))
                    layer["start"], layer["end"] = s, e
                    var = self._ed_range_vars.get(layer["id"])
                    if var is not None:
                        var.set(self._ed_range_text(layer))
                    self._draw_scrub_playhead()
                    return
                self._on_scrub_click_base(event)
                return
            d = getattr(self, "_p11_drag", None)
            if not d:
                return
            lay = self._p11_lay_now()
            if d["kind"] == "handle":
                L = next((L for L in lay if L["uid"] == d["uid"]), None)
                if L is None:
                    return
                t = self._p11_x_to_t(L, event.x)
                reg = self._p11_reg
                gap = 0.1
                if d["which"] == "a":
                    reg["a"] = max(L["s"], min(t, reg["b"] - gap))
                else:
                    reg["b"] = min(L["e"], max(t, reg["a"] + gap))
                self._draw_scrub_playhead()
                return
            if abs(event.x - d["x0"]) > 6:
                d["moved"] = True
            if d["moved"]:
                others = [L for L in lay if L["uid"] != d["uid"]]
                self._p11_drop = sum(1 for L in others if (L["x0"] + L["x1"]) / 2 < event.x)
                self._draw_scrub_playhead()
            return
        ov.seek(max(0.0, min(1.0, (event.x - 8) / max(1, self.scrub_canvas.winfo_width() - 16))))

    def _on_scrub_click_base(self, event):
        if not self.total_frames:
            return
        self._pause()
        span = max(1, self.scrub_canvas.winfo_width() - 16)
        frac = max(0.0, min(1.0, (event.x - 8) / span))
        self._show_frame(int(round(frac * (self.total_frames - 1))))

    # ---- play/pause/restart ----

    def _toggle_play(self):
        self._p29_state()
        ov = self._p29_overlay
        if (ov is not None and ov.alive()) or self._p29_preview_tool() or (not self._ed2_active and self._p29_has_fx()):
            return self._p29_play()
        try:
            segs = self._p31_live_segs()
            if segs and not self.playing:
                fps = max(float(self.fps), 1.0)
                t = self.frame_index / fps
                i = _p10_seg_at(segs, t, 0.5 / fps)
                if i is None:
                    j = _p10_next_start(segs, t)
                    self._show_frame(int(round(segs[0 if j is None else j]["start"] * fps)))
                elif i == len(segs) - 1 and t >= segs[i]["end"] - 0.5 / fps:
                    self._show_frame(int(round(segs[0]["start"] * fps)))
        except Exception:
            log.exception("Film splice: could not position playback (non-fatal)")
        if not self.playing and hasattr(self, "_ed5_trim"):
            tv = self._ed5_trim_vals()
            fps_2 = max(float(self.fps), 1.0)
            if tv and self.frame_index / fps_2 >= tv[1] - 0.5 / fps_2:
                self._show_frame(int(round(tv[0] * fps_2)))
        if self.playing:
            self._pause()
        else:
            self._play()

    def _play(self):
        self._play_base()
        if self.playing:
            self._fp_start_audio()

    def _play_base(self):
        if not self.total_frames:
            return
        if self.frame_index >= self.total_frames - 1:
            self.frame_index = 0
        w, h = self._display_size()
        self._play_i0 = self.frame_index
        self._stream_pos = self.frame_index
        self._got_first = False
        self._stream = _PlaybackStream(self.video_path, self.frame_index / self.fps, w, h)
        self.playing = True
        self.play_btn.configure(text="Pause")
        self._advance_frame()

    def _pause(self):
        ov = getattr(self, "_p29_overlay", None)
        if ov is not None and ov.alive() and ov.playing:
            ov.pause()
        self.playing = False
        if self._play_job is not None:
            try:
                self.root.after_cancel(self._play_job)
            except Exception:
                pass
            self._play_job = None
        if self._stream is not None:
            self._stream.stop()
            self._stream = None
        self.play_btn.configure(text="Play")
        self._fp_stop_audio()
        try:
            if self._g_pin is not None and self.total_frames:
                self._g_fetch_native()
        except Exception:
            pass

    def _advance_frame(self):
        self._advance_frame_base()
        try:
            if self.playing and hasattr(self, "_ed5_trim"):
                tv = self._ed5_trim_vals()
                fps_3 = max(float(self.fps), 1.0)
                if tv and self.frame_index / fps_3 >= tv[1] - 0.5 / fps_3:
                    self._pause()
        except Exception:
            log.exception("Film edit: trim-end check failed (non-fatal)")
        try:
            segs = self._p31_live_segs()
            if not segs or not self.playing or self._p10_jumping:
                return
            fps = max(float(self.fps), 1.0)
            t = self.frame_index / fps
            eps = 0.5 / fps
            i = _p10_seg_at(segs, t, eps)
            target = None
            if i is None:
                j = _p10_next_start(segs, t)
                if j is None:
                    self._pause()
                    return
                target = segs[j]["start"]
            elif t >= segs[i]["end"] - eps:
                if i + 1 >= len(segs):
                    self._pause()
                    return
                nxt = segs[i + 1]["start"]
                if abs(nxt - segs[i]["end"]) > 1.0 / fps:
                    target = nxt
            if target is not None:
                self._p10_jumping = True
                self.root.after(0, lambda: self._p10_jump(target))
        except Exception:
            log.exception("Film splice: playback check failed (non-fatal)")

    def _advance_frame_base(self):
        if not self.playing or self._stream is None:
            return
        stream = self._stream
        latest = None
        if not self._got_first:
            data = stream.get()
            if data is not None:
                self._got_first = True
                self._play_t0 = time.monotonic()
                self._stream_pos = self._play_i0
                latest = data
        else:
            target_2 = self._play_i0 + int((time.monotonic() - self._play_t0) * self.fps)
            while self._stream_pos < target_2:
                data = stream.get()
                if data is None:
                    break
                self._stream_pos += 1
                latest = data
        if latest is not None:
            self._show_raw_frame(latest, self._stream_pos, stream.width, stream.height)
        if stream.finished() or self._stream_pos >= self.total_frames - 1:
            self._pause()
            return
        self._play_job = self.root.after(max(1, int(500 / self.fps)), self._advance_frame)

    def _restart(self):
        self._p29_state()
        ov = self._p29_overlay
        if (ov is not None and ov.alive()) or self._p29_preview_tool() or (not self._ed2_active and self._p29_has_fx()):
            return self._p29_restart()
        segs = self._p31_live_segs()
        if segs:
            self._pause()
            self._show_frame(int(round(segs[0]["start"] * max(float(self.fps), 1.0))))
            return
        self._pause()
        tv = self._ed5_trim_vals() if hasattr(self, "_ed5_trim") else None
        self._show_frame(int(round(tv[0] * max(float(self.fps), 1.0))) if tv else 0)

    # ---- Edit/Tools: visible, no-ops for now (layout placeholders) ----

    def _noop_edit(self):
        self._ed2_state()
        if self._ed2_active:
            return
        if getattr(self, "_tl_tools_frame", None) is not None:
            self._t2_back()
        if self.save_row.winfo_children():
            self._collapse_save_row()
        self._pause()
        self._ed2_snap = ([dict(e) for e in self.film_edits], [dict(l) for l in self.film_layers], self._ed_counter, self.film_active_layer_id)
        main_row = self._tl_main_row
        skip = (self.dimension_label, self.scale_label)
        self._ed2_hidden = []
        for w in main_row.winfo_children():
            if w in skip:
                continue
            try:
                self._ed2_hidden.append((w, w.pack_info()))
                w.pack_forget()
            except tk.TclError:
                pass
        self._ed2_active = True
        self._ed2_build_rows()
        log.info("Film viewer: Edit mode entered")

    def _noop_tools(self):
        log.info("Film viewer: Tools clicked (not yet implemented)")
        notify("Not yet implemented", "Film tools (Measure/Magnify/Pin) are a later phase", blocking=False)

    # ---- Save / Copy / Cancel ----

    def _begin_save(self):
        if getattr(self, "_tl_row", None) is not None:
            self._tl_close_row()
        self._fs_begin_save()
        self.root.after(30, self._tl_fit)

    def _begin_rename_save(self):
        self._pause()
        for w in self.save_row.winfo_children():
            w.destroy()
        name_var = tk.StringVar(value=self.video_path.stem)
        entry = tk.Entry(self.save_row, textvariable=name_var, width=28)
        entry.pack(side="left", padx=4)
        entry.focus_set()

        def confirm(event=None):
            name = Path(name_var.get().strip() or self.video_path.stem).name
            if not name.lower().endswith(".mkv"):
                name += ".mkv"
            self._fs_export(_fs_paths.get_dir("films") / name, "mkv", sloppy=False)

        entry.bind("<Return>", confirm)
        tk.Button(self.save_row, text="Confirm", pady=BUTTON_PADY, command=confirm).pack(side="left", padx=4)
        tk.Button(self.save_row, text="Cancel", pady=BUTTON_PADY, command=self._fs_save_buttons).pack(side="left", padx=4)
        self.save_row.pack(side="top", fill="x")

    @staticmethod
    def _convert_command(src, out, ext):
        if ext == "mp4":
            return ["ffmpeg", "-y", "-i", str(src), "-c", "copy", "-movflags", "+faststart", str(out)]
        if ext == "webm":
            return ["ffmpeg", "-y", "-i", str(src), "-c:v", "libvpx-vp9", "-crf", "32", "-b:v", "0", str(out)]
        return ["ffmpeg", "-y", "-i", str(src), "-vf", "fps=15,split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse", "-loop", "0", str(out)]

    def _save_as(self):
        self._pause()
        film_types = [
            ("mkv", "Matroska (.mkv) - original"), ("mp4", "MP4 (.mp4)"),
            ("webm", "WebM (.webm)"), ("gif", "Animated GIF (.gif)"),
        ]

        def do_convert(directory, name, ext):
            self._fs_export(Path(directory) / name, ext, sloppy=False)

        ask_save_as(self.root, "Save film as", film_types, _fs_paths.get_dir("films"), self.video_path.stem, do_convert)

    def _collapse_save_row(self):
        for w in self.save_row.winfo_children():
            w.destroy()
        self.save_row.pack_forget()
        self._tl_snap_mode = False
        try:
            if self._tl_snap_btn is not None:
                self._tl_snap_btn.config(relief="raised")
        except tk.TclError:
            pass
        self.root.after(30, self._tl_fit)

    def _do_copy(self):
        if not self._ed4_dirty():
            return self._fs_do_copy()
        self._pause()
        notify("Rendering edits\u2026", "Baking your edits into the film", blocking=False)
        dest = Path(_ed4_tempfile.mkdtemp(prefix="snippysnappy-bake-"))

        def worker():
            try:
                baked = self._ed4_bake(dest)
                target, _d = _fa_merge_for_export(baked)
            except Exception:
                log.exception("Film viewer: baking edits for copy failed")
                target = None

            def after():
                from .recorder import _copy_file_uri_to_clipboard
                if target is None:
                    notify("Copy failed", "Could not render the edits - see the log", blocking=False)
                    return
                tgt = target
                if self._also_save_var.get():
                    out = _fs_paths.get_dir("sloppyfilms") / f"sloppyfilm-{_fs_stamp()}.mkv"
                    try:
                        _fs_shutil.copy2(tgt, out)
                        tgt = out
                        notify("Sloppy film saved", f"Saved as {out.name}", blocking=False)
                    except Exception:
                        log.exception("Film viewer: Also save on copy failed - copying the rendered file instead")
                if not _copy_file_uri_to_clipboard(tgt):
                    notify("Copy failed", "Could not copy the file - see the log", blocking=False)
                    return
                notify("Snippyfilm copied", "File copied to clipboard", blocking=False)
                if self._also_exit_var.get():
                    self._cleanup_and_close()

            self.root.after(0, after)

        _ed4_threading.Thread(target=worker, daemon=True).start()

    def _confirm_cancel(self):
        self._pause()
        if self._fs_is_temp():
            msg = "Discard this recording? It has not been saved, so this deletes it and cannot be undone."
        else:
            msg = "Close this film? The file on disk is not deleted."
        confirm_discard(self.root, msg, self._discard_now)

    def _discard_now(self):
        if self._fs_is_temp():
            self._film_keep_temp = False
            try:
                self.video_path.unlink(missing_ok=True)
                notify("Snippyfilm Discarded", blocking=False)
            except Exception:
                log.exception(f"Film viewer: failed to delete {self.video_path}")
                notify("Discard failed", "Could not delete the file - see the log", blocking=False)
        else:
            notify("Film closed", "The saved file was not touched", blocking=False)
        self._cleanup_and_close()

    def _redo_capture(self):
        """Discards this take and re-runs the exact invocation that started it (same re-exec idea as the image windows' Redo)."""
        self._pause()
        confirm_discard(self.root, "Discard this recording and record again?", self._redo_now, title="Redo?")

    def _redo_now(self):
        log.info("Film viewer: Redo - discarding this take and re-running the same invocation")
        if self._fs_is_temp():
            _fs_shutil.rmtree(self.video_path.parent, ignore_errors=True)
        shutil.rmtree(self._tmpdir, ignore_errors=True)
        self.root.destroy()
        os.execv(sys.executable, [sys.executable] + sys.argv)

    def _cleanup_and_close(self):
        try:
            self._p29_close()
            d = getattr(self, "_p29_dir", None)
            if d:
                _p29_shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass
        temp_dir = self.video_path.parent if self._fs_is_temp() else None
        keep = getattr(self, "_film_keep_temp", False)
        try:
            self._pause()
        except Exception:
            pass
        if temp_dir is not None and not keep:
            _fs_shutil.rmtree(temp_dir, ignore_errors=True)
        self._pause()
        try:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
        except Exception:
            pass
        try:
            _unregister_film(self._handle)
        except Exception:
            pass
        self.root.destroy()
        if self._on_closed is not None:
            try:
                self._on_closed()
            except Exception:
                log.exception("Film viewer: on_closed callback failed (non-fatal)")

    def _close_keep(self):
        """Closes this window WITHOUT deleting the film (it stays where it auto-saved). Used by the Windows-panel x and remote close requests; Cancel is the only thing that discards."""
        log.info("Film viewer: closing window, keeping the film file")
        self._cleanup_and_close()

    def _confirm_close_keep(self):
        if WARN_ON_ACTIVE_CLOSE:
            from tkinter import messagebox
            if not messagebox.askyesno("Close this window?", "Close this window? The recording stays saved in Snippyfilms.", parent=self.root):
                return
        self._close_keep()

    def _refresh(self):
        # Roster-change callback from the coordinator.
        if self.side_panel_mode == "tabs":
            self._refresh_side_panel()
        result = None
        try:
            self._vl_refresh()
        except Exception:
            log.exception("Film viewer: pins list refresh failed (non-fatal)")
        return result

    # ---- rescale / dimension / tabs status row ----

    def _manual_rescale_to_fit(self):
        self.root.update_idletasks()
        canvas_w, canvas_h = self.canvas.winfo_width(), self.canvas.winfo_height()
        if canvas_w <= 1 or canvas_h <= 1 or not self.native_w or not self.native_h:
            return
        self._pause()
        self.display_scale = min(canvas_w / self.native_w, canvas_h / self.native_h)
        self._show_frame(self.frame_index)
        self._update_dimension_label()

    def _update_dimension_label(self):
        if self.native_w:
            self.dimension_label.configure(text=f"{self.native_w} x {self.native_h}")
            self.scale_label.configure(text=f"Scale: {self.display_scale:.2f}x")

    def _on_side_panel_choice(self, choice):
        self.side_panel_mode = {"None": None, "Layers": "layers", "Tabs": "tabs"}.get(choice)
        self._refresh_side_panel()

    def _refresh_side_panel(self):
        prev = None
        old = getattr(self, "_panel_canvas", None)
        if old is not None:
            try:
                prev = old.yview()[0]
            except tk.TclError:
                prev = None
        self._panel_canvas = None
        for w in self.side_panel_container.winfo_children():
            w.destroy()
        if self.side_panel_mode is None:
            self.side_panel_container.pack_forget()
            self.side_panel_separator.pack_forget()
            return
        self.side_panel_separator.pack(side="left", fill="y")
        self.side_panel_container.config(width=SIDE_PANEL_WIDTH_RESERVE)
        self.side_panel_container.pack_propagate(False)
        self.side_panel_container.pack(side="left", fill="y")
        inner, canvas = make_scrollable(self.side_panel_container)
        self._panel_canvas = canvas
        if self.side_panel_mode == "layers":
            self._ed_build_layers_panel(inner)
        else:
            self._build_film_tabs_panel(inner)
        self.root.update_idletasks()
        same_mode = getattr(self, "_last_panel_mode", None) == self.side_panel_mode
        self._last_panel_mode = self.side_panel_mode
        self.root.after(300, self._log_layout)
        if prev and (same_mode or not PANEL_START_AT_TOP):
            canvas.yview_moveto(prev)

    def _set_tab_label(self, text):
        self._tab_label = text
        self.root.title(f"{WINDOW_TITLE_BASE} - {text}")
        self._refresh_side_panel()

    def _set_own_window_name(self, text):
        self._handle._window_name_override = text
        broadcast_window_roster()
        self._refresh_side_panel()

    def _on_windows_reorder(self, key, to_index):
        keys = list(getattr(self, "_windows_shown_keys", []))
        self._windows_order = move_in_order(keys, key, to_index)
        self._refresh_side_panel()

    def _confirm_restart_all(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("Restart All?")
        dialog.transient(self.root)
        dialog.configure(bg="#222222")
        dialog.resizable(False, False)
        tk.Label(
            dialog, justify="left", fg="white", bg="#222222",
            text=(
                "This restarts every OTHER connected snippysnappy window\n"
                "and pin so they pick up any code changes. This film\n"
                "window stays open."
            ),
        ).pack(anchor="w", padx=16, pady=(14, 8))

        def confirm():
            dialog.destroy()
            request_global_restart()

        btn_row = tk.Frame(dialog, bg="#222222")
        btn_row.pack(padx=16, pady=(0, 14))
        tk.Button(btn_row, text="Restart All", pady=BUTTON_PADY, command=confirm).pack(side="left", padx=(0, 6))
        tk.Button(btn_row, text="Cancel", pady=BUTTON_PADY, command=dialog.destroy).pack(side="left")
        dialog.bind("<Escape>", lambda e: dialog.destroy())
        dialog.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - dialog.winfo_reqwidth()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - dialog.winfo_reqheight()) // 2
        dialog.geometry(f"+{max(0, x)}+{max(0, y)}")
        dialog.grab_set()

    def _open_film_file(self):
        """Opens another film in its own window (a new process, via --open-film)."""
        from tkinter import filedialog
        path = filedialog.askopenfilename(
            parent=self.root, title="Open film", initialdir=str(self.video_path.parent),
            filetypes=[("Video", "*.mkv *.mp4 *.webm *.mov *.avi"), ("All files", "*")],
        )
        if not path:
            return
        entry = Path(__file__).resolve().parent.parent / "snippysnappy.py"
        log.info(f"Film viewer: opening {path} in a new window via {entry} --open-film")
        try:
            subprocess.Popen(
                [sys.executable, str(entry), "--open-film", path],
                stdin=subprocess.DEVNULL, start_new_session=True,
            )
        except Exception:
            log.exception("Film viewer: could not launch a new film window")
            notify("Could not open the film", "See the log", blocking=False)

    def _build_film_tabs_panel(self, parent):
        tk.Label(parent, text="Tabs", fg="white", bg="#222222", font=("", 10, "bold")).pack(anchor="w", padx=6, pady=(6, 4))
        new_row = tk.Frame(parent, bg="#222222")
        new_row.pack(side="top", anchor="w", padx=6, pady=(0, 6))
        tk.Button(new_row, text="+ Open File\u2026", pady=BUTTON_PADY, command=self._open_film_file).pack(side="left")

        tab_name = getattr(self, "_tab_label", None) or self.video_path.stem
        tab_reorder = RowReorder(parent, lambda key, to_index: None)   # one film tab for now; the handle is here for when editing adds more
        add_window_row(
            parent, "\u25b6 " + tab_name, True, lambda: None, None,
            on_rename=lambda cell, btn: inline_rename(cell, btn, tab_name, self._set_tab_label, self._refresh_side_panel),
            reorder=tab_reorder, key="film-tab-0",
        )
        tk.Label(parent, text="(drag \u2261 to reorder \u00b7 \u270e to rename)", fg="#aaaaaa", bg="#222222", justify="left").pack(side="top", anchor="w", padx=6, pady=(4, 6))
        tk.Frame(parent, height=1, bg="#444444").pack(side="top", fill="x", padx=6, pady=(0, 6))

        header = tk.Frame(parent, bg="#222222")
        header.pack(side="top", fill="x", padx=6, pady=(0, 4))
        tk.Label(header, text="Windows", fg="white", bg="#222222", font=("", 10, "bold")).pack(side="left")
        tk.Button(header, text="Restart All", pady=BUTTON_PADY, command=self._confirm_restart_all).pack(side="right")

        own_name = window_name(self._handle)
        entries = [{
            "key": self._film_id, "raw": own_name, "label": "\u25b6 " + own_name, "active": True,
            "focus": lambda: None, "hide": self._hide_to_background, "hidden": False,
            "close": self._confirm_cancel, "rename": self._set_own_window_name,
        }]
        for cid, wid, label, n, kind in remote_windows_all():
            is_film = kind == "film"
            entries.append({
                "key": wid, "raw": label,
                "label": ("\u25b6 " + label) if is_film else (label + (f" +{n - 1} more" if n > 1 else "")),
                "active": False,
                "tip": "\n".join(remote_window_tab_labels(wid) or [label]),
                "focus": lambda c=cid, w=wid: request_remote_focus(c, w),
                "hide": lambda c=cid, w=wid: request_remote_window_hide(c, w),
                "hidden": remote_window_hidden(wid),
                "close": lambda c=cid, w=wid: request_remote_window_close(c, w),
                "rename": lambda text, c=cid, w=wid: request_remote_window_rename(c, w, text),
            })
        log.info("Windows panel (film %s) entries: %s" % (str(self._film_id)[:8], [(str(e["key"])[:8], e["label"], e["active"], e["hidden"]) for e in entries]))
        build_windows_rows(parent, entries, self, self._refresh_side_panel)
        tk.Label(parent, text="(drag \u2261 to reorder \u00b7 \u270e to rename)", fg="#aaaaaa", bg="#222222", justify="left").pack(side="top", anchor="w", padx=6, pady=(6, 6))

    # ---- toolbar ----

    def _build_toolbar(self):
        main_row = tk.Frame(self.toolbar_container, bg="#222222")
        main_row.pack(side="top", fill="x")

        tk.Button(main_row, text="Copy", pady=BUTTON_PADY, command=self._do_copy).pack(side="left", padx=4, pady=4)
        tk.Button(main_row, text="Save", pady=BUTTON_PADY, command=self._begin_save).pack(side="left", padx=4, pady=4)
        tk.Button(main_row, text="Edit", pady=BUTTON_PADY, command=self._noop_edit).pack(side="left", padx=4, pady=4)
        tk.Button(main_row, text="Tools", pady=BUTTON_PADY, command=self._noop_tools).pack(side="left", padx=4, pady=4)
        self.play_btn = tk.Button(main_row, text="Play", pady=BUTTON_PADY, command=self._toggle_play)
        self.play_btn.pack(side="left", padx=4, pady=4)
        tk.Button(main_row, text="Restart", pady=BUTTON_PADY, command=self._restart).pack(side="left", padx=4, pady=4)
        tk.Button(main_row, text="Redo", pady=BUTTON_PADY, command=self._redo_capture).pack(side="left", padx=4, pady=4)
        tk.Button(main_row, text="Cancel", pady=BUTTON_PADY, command=self._confirm_cancel).pack(side="left", padx=4, pady=4)

        # Re-scale and the Tabs selector live in the scrub row, right of the time readout.
        panel_var = tk.StringVar(value="None")
        dropdown = tk.OptionMenu(self.scrub_frame, panel_var, "None", "Layers", "Tabs", command=self._on_side_panel_choice)
        dropdown.config(pady=BUTTON_PADY, width=7)
        dropdown.pack(side="right", padx=(4, 8), pady=1, before=self.time_label)
        self.rescale_btn = tk.Button(self.scrub_frame, text="Re-scale", pady=BUTTON_PADY, command=self._manual_rescale_to_fit)
        self.rescale_btn.pack(side="right", padx=(4, 4), pady=1, before=self.time_label)

        text_row = tk.Frame(self.toolbar_container, bg="#222222")
        text_row.pack(side="top", fill="x", before=main_row)
        # Dimension packed first (rightmost, under the Tabs selector), then
        # Scale (under Re-scale) - same order in image and film windows.
        self.dimension_label = tk.Label(text_row, text="", fg="white", bg="#222222", font=("", 8), pady=0)
        self.dimension_label.pack(side="right", padx=(4, 8))
        self.scale_label = tk.Label(text_row, text="Scale: 1.00x", fg="#aaaaaa", bg="#222222", font=("", 8), pady=0)
        self.scale_label.pack(side="right", padx=(0, 4))

        self.save_row = tk.Frame(self.toolbar_container, bg="#222222")   # packed on demand by _begin_save

    # ---- class attributes that later patches added ----
    _tl_tools_frame = None
    _o_active = False
    _o_busy = False
    _o_running = False
    _o_overlay = None
    _tl_snap_copy_var = None
    _o_width_var = None
    _m_active = False
    _m_kind = "box"
    _m_boxes = []
    _m_ab = None
    _m_handle = None
    _m_creating = False
    _m_start_pt = None
    _m_done = None
    _g_active = False
    _g_native = None
    _g_disp = None
    _g_pos = None
    _g_last = 0.0
    _g_fetching = False
    _g_done = None
    _g_lens = 120
    _g_zoom = 4
    _g_pin = None
    _g_inited = False
    _g_pin_photo = None
    _p_active = False
    _p_pt = None
    _p_done = None
    _p_prop_var = None
    _vp_vars = None
    _vl_btn = None
    _vl_popup = None
    _vl_other = None
    _aud_probed = False

    # ---- folder setup hook for film windows (paths_setup) ----

    # ---- film save/copy parity with the image windows ----

    def _fs_is_temp(self):
        return self.video_path.parent.name.startswith("snippysnappy-record-")

    def _fs_backup(self):
        try:
            dest = _fs_paths.get_dir("sloppyfilms") / f"snippyfilm-og-{_fs_stamp()}.mkv"
            _fs_shutil.copy2(self.video_path, dest)
            log.info(f"Film viewer: BACKUP_ORIGINAL -> {dest}")
        except Exception:
            log.exception("Film viewer: BACKUP_ORIGINAL failed (non-fatal)")

    def _fs_save_buttons_2(self):
        for w in self.save_row.winfo_children():
            w.destroy()
        tk.Button(self.save_row, text="Save", pady=BUTTON_PADY, command=self._begin_rename_save).pack(side="left", padx=4, pady=4)
        tk.Button(self.save_row, text="Save As\u2026", pady=BUTTON_PADY, command=self._save_as).pack(side="left", padx=4, pady=4)
        tk.Button(self.save_row, text="Sloppy Save", pady=BUTTON_PADY, command=self._fs_sloppy_save).pack(side="left", padx=4, pady=4)
        tk.Checkbutton(
            self.save_row, text="Also copy", variable=self._also_copy_var, bg="#222222", fg="white",
            selectcolor="#444444", activebackground="#222222", activeforeground="white",
        ).pack(side="left", padx=4)

    def _fs_begin_save(self):
        self._pause()
        if self.save_row.winfo_children():
            self._collapse_save_row()
            return
        self._fs_save_buttons()
        self.save_row.pack(side="top", fill="x")

    def _fs_sloppy_save(self):
        self._pause()
        self._fs_export(_fs_paths.get_dir("sloppyfilms") / f"sloppyfilm-{_fs_stamp()}.mkv", "mkv", sloppy=True)

    def _fs_export_2(self, out_path, ext, sloppy=False):
        src = self.video_path
        try:
            if out_path.exists() and out_path.resolve() == src.resolve():
                notify("Already saved there", out_path.name, blocking=False)
                return
        except OSError:
            pass
        out_path.parent.mkdir(parents=True, exist_ok=True)
        also_copy = bool(self._also_copy_var.get())
        also_exit = bool(self._also_exit_var.get())
        notify("Saving\u2026", f"Writing {out_path.name}", blocking=False)

        def finished(ok):
            if not ok:
                notify("Save failed", "Could not write the file - see the log", blocking=False)
                return
            notify("Sloppy film saved" if sloppy else "Snippyfilm saved", f"Saved as {out_path.name}", blocking=False)
            log.info(f"Film viewer: {'Sloppy Save' if sloppy else 'Save'} -> {out_path}")
            if also_copy:
                from .recorder import _copy_file_uri_to_clipboard
                if _copy_file_uri_to_clipboard(out_path):
                    notify("Snippyfilm copied", "File copied to clipboard", blocking=False)
            if also_exit:
                self._cleanup_and_close()
            else:
                self._collapse_save_row()

        def worker():
            try:
                src2, merge_dir = _fa_merge_for_export(src)
                if ext == "mkv":
                    _fs_shutil.copy2(src2, out_path)
                else:
                    subprocess.run(self._convert_command(src2, out_path, ext), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
                ok = True
                if merge_dir is not None:
                    shutil.rmtree(merge_dir, ignore_errors=True)
            except Exception:
                log.exception(f"Film viewer: export failed for {out_path}")
                ok = False
            self.root.after(0, lambda: finished(ok))

        _fs_threading.Thread(target=worker, daemon=True).start()

    def _fs_do_copy(self):
        from .recorder import _copy_file_uri_to_clipboard
        self._pause()
        target = self.video_path
        merged, _merge_dir = _fa_merge_for_export(self.video_path)
        if merged != self.video_path:
            target = merged
        if self._also_save_var.get():
            out = _fs_paths.get_dir("sloppyfilms") / f"sloppyfilm-{_fs_stamp()}.mkv"
            try:
                _fs_shutil.copy2(target, out)
                target = out
                notify("Sloppy film saved", f"Saved as {out.name}", blocking=False)
                log.info(f"Film viewer: Also save (on copy) -> {out}")
            except Exception:
                log.exception("Film viewer: Also save on copy failed - copying the temporary file instead")
        if not _copy_file_uri_to_clipboard(target):
            notify("Copy failed", "Could not copy the file - see the log", blocking=False)
            return
        notify("Snippyfilm copied", "File copied to clipboard", blocking=False)
        if target.parent == self.video_path.parent and self._fs_is_temp():
            self._film_keep_temp = True
        if self._also_exit_var.get():
            self._cleanup_and_close()

    # ---- film toolbar layout parity with the image window ----

    def _fs_save_buttons(self):
        for w in self.save_row.winfo_children():
            w.destroy()
        tk.Button(self.save_row, text="Save", pady=BUTTON_PADY, command=self._begin_rename_save).pack(side="left", padx=4, pady=4)
        tk.Button(self.save_row, text="Sloppy Save", pady=BUTTON_PADY, command=self._fs_sloppy_save).pack(side="left", padx=4, pady=4)
        tk.Button(self.save_row, text="Save As\u2026", pady=BUTTON_PADY, command=self._save_as).pack(side="left", padx=4, pady=4)

    # ---- film toolbar: dimension/scale row goes under the buttons, like the image window ----

    # ---- film toolbar: dimension/scale readouts share the button row, right-aligned ----

    # ---- film toolbar: readouts in a flush row directly under the buttons; taller checkboxes ----

    # ---- film toolbar: readouts at the top-right of the button row, directly under Re-scale / Tabs ----

    # ---- film audio playback: speaker overlay, live volume/mute, ffmpeg -> numpy -> pacat ----

    def _fp_gain(self):
        return 0.0 if self._muted else self._vol

    def _fp_stop_audio(self):
        s = getattr(self, "_aud_stream", None)
        self._aud_stream = None
        if s is not None:
            s.stop()

    def _fp_build_overlay(self):
        bg = "#111111"
        self._aud_w = tk.Canvas(self.canvas, width=36, height=34, bg=bg, highlightthickness=0)
        self._aud_tip = tk.Label(
            self.canvas, text="", fg="white", bg=bg, font=("", 10, "bold"),
            highlightthickness=1, highlightbackground="#00b4ff", padx=4, pady=1,
        )
        self._aud_w.bind("<ButtonPress-1>", self._fp_press)
        self._aud_w.bind("<B1-Motion>", self._fp_drag)
        self._aud_w.bind("<ButtonRelease-1>", self._fp_release)
        self._fp_draw()
        self._fp_poll()

    def _fp_draw(self):
        c = self._aud_w
        W = 170 if self._aud_expanded else 36
        c.config(width=W)
        c.delete("all")
        c.create_rectangle(0, 0, W - 1, 33, outline="#333333", fill="#111111")
        silent = self._muted or self._vol <= 0
        c.create_polygon(9, 13, 14, 13, 20, 8, 20, 26, 14, 21, 9, 21, fill="white", outline="white")
        if silent:
            c.create_line(24, 12, 32, 22, fill="#ff5555", width=2)
            c.create_line(32, 12, 24, 22, fill="#ff5555", width=2)
        else:
            c.create_arc(14, 11, 28, 23, start=-50, extent=100, style="arc", outline="white", width=2)
            if self._vol > 0.5:
                c.create_arc(10, 6, 32, 28, start=-50, extent=100, style="arc", outline="white", width=2)
        if self._aud_expanded:
            px = 44 + int(self._vol * 114)
            c.create_rectangle(44, 15, 158, 19, fill="#444444", outline="")
            c.create_rectangle(44, 15, px, 19, fill="#00b4ff", outline="")
            c.create_oval(px - 5, 12, px + 5, 22, fill="white", outline="#888888")

    def _fp_poll_2(self):
        try:
            if not self.root.winfo_exists():
                return
            visible = expanded = False
            if self._aud_dragging:
                visible = expanded = True
            elif self.root.winfo_viewable():
                try:
                    px, py = self.root.winfo_pointerxy()
                    w = self.root.winfo_containing(px, py)
                except Exception:
                    w = None
                if w is not None:
                    name, cname = str(w), str(self.canvas)
                    visible = name == cname or name.startswith(cname + ".")
                    if visible and w is self._aud_w:
                        expanded = (px - self._aud_w.winfo_rootx()) < 36 or self._aud_expanded
            changed = False
            if visible != self._aud_shown:
                self._aud_shown = visible
                if visible:
                    self._aud_w.place(x=10, rely=1.0, y=-10, anchor="sw")
                    tk.Misc.tkraise(self._aud_w)
                else:
                    self._aud_w.place_forget()
                changed = True
            if not visible:
                expanded = False
            if expanded != self._aud_expanded:
                self._aud_expanded = expanded
                changed = True
            if changed:
                self._fp_draw()
            self.root.after(100, self._fp_poll)
        except tk.TclError:
            return
        except Exception:
            log.exception("Film viewer: audio overlay poll failed (non-fatal)")

    def _fp_set_from_x(self, event):
        frac = max(0.0, min(1.0, (event.x - 44) / 114.0))
        self._vol = frac
        if frac > 0:
            self._muted = False
        self._fp_draw()
        cx = event.x_root - self.canvas.winfo_rootx()
        cy = event.y_root - self.canvas.winfo_rooty()
        self._aud_tip.config(text=f"{int(round(frac * 100))}%")
        self._aud_tip.place(x=cx, y=cy - 14, anchor="s")
        self._aud_tip.lift()

    def _fp_press(self, event):
        if event.x < 36:
            self._muted = not self._muted
            self._fp_draw()
            return
        if self._aud_expanded and event.x >= 38:
            self._aud_dragging = True
            self._fp_set_from_x(event)

    def _fp_drag(self, event):
        if self._aud_dragging:
            self._fp_set_from_x(event)

    def _fp_release(self, event):
        self._aud_dragging = False
        self._aud_tip.place_forget()

    # ---- speaker overlay: coordinate-based hover check that can never stop rescheduling ----

    def _fp_poll(self):
        try:
            if not self.root.winfo_exists():
                return
        except tk.TclError:
            return
        try:
            visible = expanded = False
            if self._aud_dragging:
                visible = expanded = True
            elif self.root.winfo_viewable():
                px, py = self.root.winfo_pointerxy()
                cx0, cy0 = self.canvas.winfo_rootx(), self.canvas.winfo_rooty()
                visible = cx0 <= px < cx0 + self.canvas.winfo_width() and cy0 <= py < cy0 + self.canvas.winfo_height()
                if visible and self._aud_shown:
                    ax, ay = self._aud_w.winfo_rootx(), self._aud_w.winfo_rooty()
                    aw, ah = self._aud_w.winfo_width(), self._aud_w.winfo_height()
                    if ax <= px < ax + aw and ay <= py < ay + ah:
                        expanded = self._aud_expanded or (px - ax) < 36
            changed = False
            if visible != self._aud_shown:
                self._aud_shown = visible
                if visible:
                    self._aud_w.place(x=10, rely=1.0, y=-10, anchor="sw")
                    tk.Misc.tkraise(self._aud_w)
                else:
                    self._aud_w.place_forget()
                changed = True
            if not visible:
                expanded = False
            if expanded != self._aud_expanded:
                self._aud_expanded = expanded
                changed = True
            if changed:
                self._fp_draw()
        except Exception:
            if not getattr(self, "_aud_err_logged", False):
                self._aud_err_logged = True
                log.exception("Film viewer: audio overlay poll error (logged once; polling continues)")
        finally:
            try:
                self.root.after(100, self._fp_poll)
            except tk.TclError:
                pass

    # ---- film Tools row + Filmsnap ----

    def _tl_fit_2(self):
        try:
            c = self.toolbar_container
            self.root.update_idletasks()
            if self._tl_base_h is None:
                self._tl_base_h = int(c.cget("height"))
            need = self.scrub_frame.winfo_reqheight() + self._tl_main_row.winfo_reqheight() + 6
            for r in (self._tl_row, self.save_row):
                if r is not None and r.winfo_manager() and r.winfo_children():
                    need += r.winfo_reqheight()
            target = max(self._tl_base_h, need)
            delta = target - int(c.cget("height"))
            if delta:
                c.config(height=target)
                self.root.geometry(f"{self.root.winfo_width()}x{self.root.winfo_height() + delta}")
        except tk.TclError:
            pass
        except Exception:
            log.exception("Film viewer: toolbar fit failed (non-fatal)")

    def _tl_close_row(self):
        if self._tl_snap_mode:
            self._collapse_save_row()
        if self._tl_row is not None:
            self._tl_row.destroy()
            self._tl_row = None
        self._tl_snap_btn = None
        if self._tl_tools_btn is not None:
            self._tl_tools_btn.config(relief="raised")
        self._tl_fit()

    def _tl_snap_buttons_2(self):
        for w in self.save_row.winfo_children():
            w.destroy()
        tk.Button(self.save_row, text="Save", pady=BUTTON_PADY, command=self._tl_snap_rename).pack(side="left", padx=4, pady=4)
        tk.Button(self.save_row, text="Sloppy Save", pady=BUTTON_PADY, command=self._tl_snap_sloppy).pack(side="left", padx=4, pady=4)
        tk.Button(self.save_row, text="Save As\u2026", pady=BUTTON_PADY, command=self._tl_snap_save_as).pack(side="left", padx=4, pady=4)

    def _tl_snap_rename(self):
        for w in self.save_row.winfo_children():
            w.destroy()
        name_var = tk.StringVar(value=f"filmsnap-{_tl_stamp()}")
        entry = tk.Entry(self.save_row, textvariable=name_var, width=28)
        entry.pack(side="left", padx=4)
        entry.focus_set()

        def confirm(event=None):
            name = Path(name_var.get().strip() or f"filmsnap-{_tl_stamp()}").name
            if not name.lower().endswith(".png"):
                name += ".png"
            self._tl_snap_write(_tl_paths.get_dir("snaps") / name, sloppy=False)

        entry.bind("<Return>", confirm)
        tk.Button(self.save_row, text="Confirm", pady=BUTTON_PADY, command=confirm).pack(side="left", padx=4)
        tk.Button(self.save_row, text="Cancel", pady=BUTTON_PADY, command=self._tl_snap_buttons).pack(side="left", padx=4)

    def _tl_snap_sloppy(self):
        self._tl_snap_write(_tl_paths.get_dir("sloppysnaps") / f"sloppyfilmsnap-{_tl_stamp()}.png", sloppy=True)

    def _tl_snap_save_as(self):
        types = [("png", "PNG (.png)"), ("jpg", "JPEG (.jpg)"), ("webp", "WebP (.webp)")]

        def cb(directory, name, ext):
            if not name.lower().endswith("." + ext):
                name += "." + ext
            self._tl_snap_write(Path(directory) / name, sloppy=False)

        ask_save_as(self.root, "Save filmsnap as", types, _tl_paths.get_dir("snaps"), f"filmsnap-{_tl_stamp()}", cb)

    def _tl_snap_write_2(self, path, sloppy=False):
        img = self._tl_img
        if img is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            out = img.convert("RGB") if path.suffix.lower() in (".jpg", ".jpeg") else img
            out.save(path)
        except Exception:
            log.exception(f"Film viewer: Filmsnap save failed for {path}")
            notify("Save failed", "Could not write the file - see the log", blocking=False)
            return
        log.info(f"Film viewer: {'Sloppy ' if sloppy else ''}Filmsnap -> {path}")
        notify("Sloppy filmsnap saved" if sloppy else "Filmsnap saved", f"Saved as {path.name}", blocking=False)
        if self._also_copy_var.get():
            if _tl_copy_image(path):
                notify("Filmsnap copied to clipboard", blocking=False)
        if self._also_exit_var.get():
            self._on_window_x()
        else:
            self._collapse_save_row()

    # ---- Filmsnap: own Also copy, no Also exit / Also save ----

    def _tl_snap_buttons(self):
        for w in self.save_row.winfo_children():
            w.destroy()
        if getattr(self, "_tl_snap_copy_var", None) is None:
            self._tl_snap_copy_var = tk.IntVar(master=self.root, value=_o_default_copy())
        tk.Button(self.save_row, text="Save", pady=BUTTON_PADY, command=self._tl_snap_rename).pack(side="left", padx=4, pady=4)
        tk.Button(self.save_row, text="Sloppy Save", pady=BUTTON_PADY, command=self._tl_snap_sloppy).pack(side="left", padx=4, pady=4)
        tk.Button(self.save_row, text="Save As\u2026", pady=BUTTON_PADY, command=self._tl_snap_save_as).pack(side="left", padx=4, pady=4)
        tk.Checkbutton(
            self.save_row, text="Also copy", variable=self._tl_snap_copy_var, bg="#222222", fg="white",
            selectcolor="#444444", activebackground="#222222", activeforeground="white",
        ).pack(side="left", padx=4)

    def _tl_snap_write(self, path, sloppy=False):
        img = self._tl_img
        if img is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            out = img.convert("RGB") if path.suffix.lower() in (".jpg", ".jpeg") else img
            out.save(path)
        except Exception:
            log.exception(f"Film viewer: Filmsnap save failed for {path}")
            notify("Save failed", "Could not write the file - see the log", blocking=False)
            return
        log.info(f"Film viewer: {'Sloppy ' if sloppy else ''}Filmsnap -> {path}")
        notify("Sloppy filmsnap saved" if sloppy else "Filmsnap saved", f"Saved as {path.name}", blocking=False)
        var = getattr(self, "_tl_snap_copy_var", None)
        if var is not None and var.get():
            if _tl_copy_image(path):
                notify("Filmsnap copied to clipboard", blocking=False)
        self._collapse_save_row()

    # ---- Extract Text ----

    def _o_build_row_2(self):
        for w in self.save_row.winfo_children():
            w.destroy()
        tk.Button(self.save_row, text="Undo", pady=BUTTON_PADY, command=self._o_undo).pack(side="left", padx=4, pady=4)
        tk.Button(self.save_row, text="Redo", pady=BUTTON_PADY, command=self._o_redo_stroke).pack(side="left", padx=4, pady=4)
        tk.Button(self.save_row, text="Cancel", pady=BUTTON_PADY, command=self._o_exit_paint).pack(side="left", padx=4, pady=4)
        tk.Button(self.save_row, text="Extract", pady=BUTTON_PADY, command=self._o_run).pack(side="left", padx=4, pady=4)
        self.save_row.pack(side="top", fill="x")

    def _o_k(self):
        dw = self._display_size()[0]
        return (self._o_img.width / dw) if dw else 1.0

    def _o_seg(self, p0, p1, stroke):
        k = self._o_k()
        sid = self.canvas.create_line(
            p0[0] / k, p0[1] / k, p1[0] / k, p1[1] / k, fill="yellow", width=max(1.0, stroke["width"] / k),
            capstyle=tk.ROUND, joinstyle=tk.ROUND, stipple="gray50", tags=("ocr_stroke",),
        )
        stroke["ids"].append(sid)

    def _o_pt(self, event):
        k = self._o_k()
        x = max(0.0, min(float(self._o_img.width), self.canvas.canvasx(event.x) * k))
        y = max(0.0, min(float(self._o_img.height), self.canvas.canvasy(event.y) * k))
        return (x, y)

    def _o_press_2(self, event):
        if not self._o_active:
            return
        width = getattr(_fs_consts, "OCR_BRUSH_WIDTH", 18)
        self._o_cur = {"points": [self._o_pt(event)], "width": width, "ids": []}

    def _o_drag(self, event):
        cur = self._o_cur
        if not cur:
            return
        p = self._o_pt(event)
        lx, ly = cur["points"][-1]
        if (p[0] - lx) ** 2 + (p[1] - ly) ** 2 < (getattr(_fs_consts, "MIN_POINT_DIST", 3) * self._o_k()) ** 2:
            return
        self._o_seg(cur["points"][-1], p, cur)
        cur["points"].append(p)

    def _o_release(self, event):
        cur = self._o_cur
        if not cur:
            return
        p = self._o_pt(event)
        if p != cur["points"][-1]:
            self._o_seg(cur["points"][-1], p, cur)
            cur["points"].append(p)
        self._o_cur = None
        if len(cur["points"]) > 1:
            self._o_strokes.append(cur)
            self._o_redo = []
        else:
            for sid in cur["ids"]:
                self.canvas.delete(sid)

    def _o_undo(self):
        if not self._o_strokes:
            return
        st = self._o_strokes.pop()
        for sid in st["ids"]:
            self.canvas.delete(sid)
        st["ids"] = []
        self._o_redo.append(st)

    def _o_redo_stroke(self):
        if not self._o_redo:
            return
        st = self._o_redo.pop()
        for a, b in zip(st["points"], st["points"][1:]):
            self._o_seg(a, b, st)
        self._o_strokes.append(st)

    def _o_close_overlay(self):
        ov = getattr(self, "_o_overlay", None)
        self._o_overlay = None
        if ov is not None:
            try:
                ov.destroy()
            except tk.TclError:
                pass

    def _o_run(self):
        if self._o_running or not self._o_active:
            return
        img = self._o_img
        strokes = [list(s["points"]) for s in self._o_strokes]
        widths = [s["width"] for s in self._o_strokes]
        self._o_running = True
        notify("Extracting text\u2026", blocking=False)

        def work():
            text, err = "", False
            try:
                from PIL import Image, ImageDraw
                target = img.convert("RGB")
                if strokes:
                    mask = Image.new("L", target.size, 0)
                    draw = ImageDraw.Draw(mask)
                    pad = getattr(_fs_consts, "OCR_MASK_MIN_PADDING", 20)
                    for pts, width in zip(strokes, widths):
                        r = max(width / 2, pad)
                        xs = [p[0] for p in pts]
                        ys = [p[1] for p in pts]
                        draw.rectangle([min(xs) - r, min(ys) - r, max(xs) + r, max(ys) + r], fill=255)
                    target = Image.composite(target, Image.new("RGB", target.size, "white"), mask)
                pre = _o_find("preprocess_for_ocr")
                ocr = _o_find("ocr_image")
                if ocr is None:
                    raise RuntimeError("ocr_image not found")
                if pre is not None:
                    target = pre(target)
                fd, tmp = tempfile.mkstemp(suffix=".png")
                os.close(fd)
                p = Path(tmp)
                target.save(p)
                try:
                    text = ocr(p)
                finally:
                    p.unlink(missing_ok=True)
            except Exception:
                log.exception("Film viewer: OCR failed")
                err = True
            try:
                self.root.after(0, lambda: self._o_result(text, err))
            except Exception:
                pass

        threading.Thread(target=work, daemon=True).start()

    def _o_result_2(self, text, err):
        self._o_running = False
        if err:
            notify("Extract Text failed", "See the log", blocking=False)
            return
        if not self._o_active:
            return
        log.info(f"Film viewer: OCR result ready ({len(text)} chars)")
        ov = tk.Frame(self.root, bg="#1a1a1a")
        ov.place(relx=0, rely=0, relwidth=1, relheight=1)
        ov.lift()
        self._o_overlay = ov
        tk.Label(ov, text="Extracted Text", fg="white", bg="#1a1a1a", font=("", 12, "bold")).pack(anchor="w", padx=16, pady=(16, 4))
        tw = tk.Text(ov, wrap="word")
        tw.insert("1.0", text if text else "(No text detected.)")
        tw.pack(fill="both", expand=True, padx=16, pady=(0, 8))
        self._o_text = tw
        self._o_copy_var = tk.IntVar(master=self.root, value=_o_default_copy())
        self._o_btns = tk.Frame(ov, bg="#1a1a1a")
        self._o_btns.pack(fill="x", padx=16, pady=16)
        self._o_main_buttons()

    def _o_clear_btns(self):
        for w in self._o_btns.winfo_children():
            w.destroy()

    def _o_copy_text(self):
        fn = _o_find("copy_text_to_clipboard")
        if fn is None:
            notify("Copy failed", "See the log", blocking=False)
            return
        fn(self._o_text.get("1.0", "end-1c"))
        notify("Text copied to clipboard", blocking=False)

    def _o_write_text(self, sloppy, name):
        content = self._o_text.get("1.0", "end-1c")
        if sloppy:
            path = _tl_paths.get_dir("sloppytext") / f"sloppytext-{_tl_stamp()}.txt"
        else:
            name = Path((name or "").strip() or f"snippytext-{_tl_stamp()}").name
            if not name.lower().endswith(".txt"):
                name += ".txt"
            path = _tl_paths.get_dir("text") / name
        try:
            path.write_text(content, encoding="utf-8")
        except Exception:
            log.exception(f"Film viewer: could not save OCR text to {path}")
            notify("Save failed", "Could not write the file - see the log", blocking=False)
            return
        log.info(f"Film viewer: {'Sloppy-saved' if sloppy else 'Saved'} OCR text -> {path} ({len(content)} chars)")
        if self._o_copy_var.get():
            fn = _o_find("copy_text_to_clipboard")
            if fn is not None:
                fn(content)
                notify("Text copied to clipboard", blocking=False)
        notify("Sloppy text saved" if sloppy else "Text saved", f"Saved as {path.name}", blocking=False)
        self._o_exit_paint()

    # ---- tools row: Extract Text now live ----

    def _tl_toggle_tools(self):
        if getattr(self, "_tl_tools_frame", None) is not None:
            self._t2_back()
            return
        if self.save_row.winfo_children():
            self._collapse_save_row()
        main_row = self._tl_main_row
        skip = (self.dimension_label, self.scale_label)
        self._tl_hidden = []
        for w in main_row.winfo_children():
            if w in skip:
                continue
            try:
                self._tl_hidden.append((w, w.pack_info()))
                w.pack_forget()
            except tk.TclError:
                pass
        tf = tk.Frame(main_row, bg="#222222")
        tf.pack(side="left")
        self._tl_tools_frame = tf
        for text, cmd in (
            ("Extract Text", self._o_start),
            ("Filmsnap", self._tl_filmsnap),
            ("Measure", lambda: self._tl_todo("Measure")),
            ("Magnify", lambda: self._tl_todo("Magnify")),
            ("Pin", lambda: self._tl_todo("Pin")),
        ):
            b = tk.Button(tf, text=text, pady=BUTTON_PADY, command=cmd)
            b.pack(side="left", padx=2, pady=4)
            if text == "Filmsnap":
                self._tl_snap_btn = b
        tk.Button(tf, text="Back", pady=BUTTON_PADY, command=self._t2_back).pack(side="left", padx=(10, 2), pady=4)
        self._tl_fit()

    # ---- film layout v3: Play/Restart first, tighter gaps, OCR top row + second row like the image window ----

    def _v3_layout(self):
        try:
            if getattr(self, "_tl_tools_frame", None) is not None:
                return
            row = self.play_btn.master
            kids = {w.cget("text"): w for w in row.winfo_children() if isinstance(w, tk.Button)}
            copy, restart = kids.get("Copy"), kids.get("Restart")
            if copy is None or restart is None:
                return
            self.play_btn.pack_configure(before=copy)
            restart.pack_configure(before=copy)
            for w in (self.play_btn, restart, copy, kids.get("Save"), kids.get("Edit"), kids.get("Tools"), kids.get("Cancel")):
                if w is not None:
                    w.pack_configure(padx=2)
            if kids.get("Redo") is not None:
                kids["Redo"].pack_configure(padx=(10, 2))
        except Exception:
            log.exception("Film viewer: main row layout failed (non-fatal)")

    def _v3_top_ocr(self, on):
        tf = getattr(self, "_tl_tools_frame", None)
        if tf is None:
            self._v3_top = None
            return
        kids = {w.cget("text"): w for w in tf.winfo_children() if isinstance(w, tk.Button)}
        back, ext = kids.get("Back"), kids.get("Extract Text")
        if on:
            if self._v3_top:
                return
            if back is not None:
                back.pack_forget()
            if ext is not None:
                ext.config(relief="sunken")
            btns = []
            for text, cmd, pad in (("Undo", self._o_undo, 2), ("Redo", self._o_redo_stroke, 2), ("Cancel", self._o_exit_paint, (10, 2))):
                b = tk.Button(tf, text=text, pady=BUTTON_PADY, command=cmd)
                b.pack(side="left", padx=pad, pady=4)
                btns.append(b)
            self._v3_top = btns
        else:
            if not self._v3_top:
                return
            for b in self._v3_top:
                b.destroy()
            self._v3_top = None
            if ext is not None:
                ext.config(relief="raised")
            if back is not None:
                back.pack(side="left", padx=(10, 2), pady=4)

    def _o_build_row(self):
        for w in self.save_row.winfo_children():
            w.destroy()
        if getattr(self, "_o_width_var", None) is None:
            self._o_width_var = tk.IntVar(master=self.root, value=int(getattr(_fs_consts, "OCR_BRUSH_WIDTH", 18)))
        r = self.save_row
        tk.Button(r, text="Run OCR", pady=BUTTON_PADY, command=self._o_run).pack(side="left", padx=4, pady=4)
        tk.Label(r, text="Width", fg="white", bg="#222222").pack(side="left", padx=(4, 4))
        tk.Scale(
            r, from_=2, to=100, orient="horizontal", variable=self._o_width_var, showvalue=0, length=80,
            bg="#222222", fg="white", troughcolor="#444444", highlightthickness=0,
        ).pack(side="left")
        tk.Label(r, textvariable=self._o_width_var, fg="white", bg="#222222", width=3, anchor="w").pack(side="left", padx=(4, 10))
        tk.Label(r, text="Optionally paint a region to extract", fg="#888888", bg="#222222").pack(side="left")
        r.pack(side="top", fill="x")
        self._v3_top_ocr(True)

    def _o_press(self, event):
        if not self._o_active:
            return
        self._o_cur = {"points": [self._o_pt(event)], "width": int(self._o_width_var.get()), "ids": []}

    def _o_exit_paint(self):
        self._v3_top_ocr(False)
        self._o_close_overlay()
        if self._o_active:
            for ev in ("<ButtonPress-1>", "<B1-Motion>", "<ButtonRelease-1>"):
                self.canvas.unbind(ev)
            self.canvas.delete("ocr_stroke")
        self._o_active = False
        self._o_strokes = []
        self._o_redo = []
        self._o_cur = None
        self._o_img = None
        self._collapse_save_row()

    # ---- OCR review layout fixes + Measure ----

    def _m_k(self):
        try:
            dw = self._display_size()[0]
            return (self.native_w / dw) if dw and self.native_w else 1.0
        except Exception:
            return 1.0

    def _m_xy(self, event):
        return self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)

    def _m_label(self, box):
        x1, y1, x2, y2 = box["coords"]
        k = self._m_k()
        if box["kind"] == "line":
            return f"{_m_math.hypot(x2 - x1, y2 - y1) * k:.0f} px"
        return f"{int(abs(x2 - x1) * k)} x {int(abs(y2 - y1) * k)}"

    def _m_hit(self, x, y):
        hs = getattr(_fs_consts, "HANDLE_SIZE", 8)
        for i, box in enumerate(self._m_boxes):
            for hid, name in box["handle_ids"]:
                c = self.canvas.coords(hid)
                if not c:
                    continue
                if abs(x - (c[0] + hs)) <= hs * 1.5 and abs(y - (c[1] + hs)) <= hs * 1.5:
                    return i, name
        return None, None

    def _m_handles(self, idx):
        box = self._m_boxes[idx]
        for hid, _n in box["handle_ids"]:
            self.canvas.delete(hid)
        box["handle_ids"] = []
        x1, y1, x2, y2 = box["coords"]
        hs = getattr(_fs_consts, "HANDLE_SIZE", 8)
        if box["kind"] == "line":
            pts = {"start": (x1, y1), "end": (x2, y2)}
        else:
            pts = {"nw": (x1, y1), "ne": (x2, y1), "sw": (x1, y2), "se": (x2, y2)}
        for name, (hx, hy) in pts.items():
            hid = self.canvas.create_rectangle(hx - hs, hy - hs, hx + hs, hy + hs, fill=box["color"], outline="white", tags=("measure",))
            box["handle_ids"].append((hid, name))

    def _m_press(self, event):
        if not self._m_active:
            return
        cx, cy = self._m_xy(event)
        i, name = self._m_hit(cx, cy)
        if i is not None:
            self._m_ab, self._m_handle, self._m_creating = i, name, False
            return
        cols = getattr(_fs_consts, "MEASURE_COLORS", ["#ff4040", "#40ff40", "#4080ff", "#ffff40", "#ff40ff", "#40ffff"])
        color = cols[len(self._m_boxes) % len(cols)]
        if self._m_kind == "line":
            rid = self.canvas.create_line(cx, cy, cx, cy, fill=color, width=2, tags=("measure",))
        else:
            rid = self.canvas.create_rectangle(cx, cy, cx, cy, outline=color, width=2, dash=(4, 2), tags=("measure",))
        self._m_boxes.append({"coords": (cx, cy, cx, cy), "color": color, "rect_id": rid, "handle_ids": [], "label": None, "kind": self._m_kind})
        self._m_ab = len(self._m_boxes) - 1
        self._m_handle = None
        self._m_creating = True
        self._m_start_pt = (cx, cy)
        self._m_build_row()

    def _m_drag(self, event):
        if self._m_ab is None:
            return
        cx, cy = self._m_xy(event)
        box = self._m_boxes[self._m_ab]
        if self._m_creating:
            x1, y1 = self._m_start_pt
            x2, y2 = cx, cy
        else:
            x1, y1, x2, y2 = box["coords"]
            n = self._m_handle
            if n in ("nw", "start"):
                x1, y1 = cx, cy
            elif n == "ne":
                x2, y1 = cx, cy
            elif n == "sw":
                x1, y2 = cx, cy
            elif n in ("se", "end"):
                x2, y2 = cx, cy
        box["coords"] = (x1, y1, x2, y2)
        self.canvas.coords(box["rect_id"], x1, y1, x2, y2)
        self._m_handles(self._m_ab)
        if box.get("label") is not None:
            try:
                box["label"].config(text=self._m_label(box))
            except tk.TclError:
                pass

    def _m_release(self, event):
        self._m_ab = None
        self._m_handle = None
        self._m_creating = False

    def _m_build_row(self):
        r = self.save_row
        for w in r.winfo_children():
            w.destroy()
        for text, kind in (("Boxes", "box"), ("Lines", "line")):
            tk.Button(
                r, text=text, pady=BUTTON_PADY, relief=("sunken" if self._m_kind == kind else "raised"),
                command=lambda k=kind: self._m_set_kind(k),
            ).pack(side="left", padx=2, pady=4)
        tk.Button(r, text="Clear", pady=BUTTON_PADY, command=self._m_clear_all).pack(side="left", padx=2, pady=4)
        for i, box in enumerate(self._m_boxes):
            lbl = tk.Label(r, text=self._m_label(box), fg=box["color"], bg="#222222")
            lbl.pack(side="left", padx=(8, 2))
            box["label"] = lbl
            tk.Button(r, text="\u00d7", width=2, command=lambda i=i: self._m_clear_box(i)).pack(side="left", padx=(0, 8))
        r.pack(side="top", fill="x")
        self._tl_fit()

    def _m_set_kind(self, kind):
        self._m_kind = kind
        self._m_build_row()

    def _m_clear_box(self, idx):
        if idx >= len(self._m_boxes):
            return
        box = self._m_boxes.pop(idx)
        self.canvas.delete(box["rect_id"])
        for hid, _n in box["handle_ids"]:
            self.canvas.delete(hid)
        self._m_build_row()

    def _m_clear_all(self):
        self.canvas.delete("measure")
        self._m_boxes = []
        self._m_build_row()

    def _m_top(self, on):
        tf = getattr(self, "_tl_tools_frame", None)
        if tf is None:
            self._m_done = None
            return
        kids = {w.cget("text"): w for w in tf.winfo_children() if isinstance(w, tk.Button)}
        back, mea = kids.get("Back"), kids.get("Measure")
        if on:
            if self._m_done is not None:
                return
            if back is not None:
                back.pack_forget()
            if mea is not None:
                mea.config(relief="sunken")
            b = tk.Button(tf, text="Done", pady=BUTTON_PADY, command=self._m_exit)
            b.pack(side="left", padx=(10, 2), pady=4)
            self._m_done = b
        else:
            if self._m_done is None:
                return
            self._m_done.destroy()
            self._m_done = None
            if mea is not None:
                mea.config(relief="raised")
            if back is not None:
                back.pack(side="left", padx=(10, 2), pady=4)

    def _m_start(self):
        if self._m_active:
            return
        if self._o_active:
            self._o_exit_paint()
        if self._tl_snap_mode or self.save_row.winfo_children():
            self._collapse_save_row()
        self._pause()
        self._m_active = True
        self._m_kind = "box"
        self._m_boxes = []
        self._m_ab = None
        self._m_handle = None
        self._m_creating = False
        self.canvas.bind("<ButtonPress-1>", self._m_press)
        self.canvas.bind("<B1-Motion>", self._m_drag)
        self.canvas.bind("<ButtonRelease-1>", self._m_release)
        self._m_top(True)
        self._m_build_row()

    def _m_exit(self):
        if not self._m_active:
            return
        self._m_active = False
        for ev in ("<ButtonPress-1>", "<B1-Motion>", "<ButtonRelease-1>"):
            self.canvas.unbind(ev)
        self.canvas.delete("measure")
        self._m_boxes = []
        self._m_top(False)
        self._collapse_save_row()

    # ---- Magnify ----

    def _g_source(self):
        nat = self._g_native
        if nat is not None and nat[0] == self.frame_index and not self.playing:
            return nat[1]
        return self._g_disp

    def _g_draw_2(self, cx, cy):
        from PIL import Image, ImageDraw, ImageTk
        src = self._g_source()
        if src is None:
            return
        dw = self._display_size()[0] or src.width
        s = src.width / dw
        L = int(self._g_lens)
        Z = max(1, int(self._g_zoom))
        half = L / Z / 2 * s
        box = tuple(int(round(v)) for v in (cx * s - half, cy * s - half, cx * s + half, cy * s + half))
        patch = src.crop(box).convert("RGB").resize((L, L), Image.BICUBIC)
        mask = Image.new("L", (L, L), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, L - 1, L - 1), fill=255)
        lens = patch.convert("RGBA")
        lens.putalpha(mask)
        ImageDraw.Draw(lens).ellipse((0, 0, L - 1, L - 1), outline=(0, 180, 255, 255), width=2)
        self._g_photo = ImageTk.PhotoImage(lens)
        self.canvas.delete("magnify")
        self.canvas.create_image(cx, cy, image=self._g_photo, tags=("magnify",))
        self.canvas.create_oval(cx - 5, cy - 5, cx + 5, cy + 5, outline="white", tags=("magnify",))

    def _g_fetch_native_2(self):
        if self._g_fetching:
            return
        self._g_fetching = True
        idx = self.frame_index

        def work():
            img = None
            try:
                img = self.get_current_frame_image()
            except Exception:
                log.exception("Film viewer: Magnify native frame grab failed")

            def done():
                self._g_fetching = False
                if img is not None:
                    self._g_native = (idx, img)
                if self._g_active and self._g_pos is not None:
                    try:
                        self._g_draw(*self._g_pos)
                    except Exception:
                        pass

            try:
                self.root.after(0, done)
            except Exception:
                pass

        threading.Thread(target=work, daemon=True).start()

    def _g_motion(self, event):
        cx, cy = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        self._g_pos = (cx, cy)
        now = time.monotonic()
        if now - self._g_last < 0.03:
            return
        self._g_last = now
        if not self.playing and (self._g_native is None or self._g_native[0] != self.frame_index):
            self._g_fetch_native()
        self._g_draw(cx, cy)

    def _g_leave(self, event=None):
        self._g_pos = None
        self.canvas.delete("magnify")

    def _g_build_row_2(self):
        r = self.save_row
        for w in r.winfo_children():
            w.destroy()

        def slider(label, var, lo, hi, attr):
            tk.Label(r, text=label, fg="white", bg="#222222").pack(side="left", padx=(8, 4))
            tk.Scale(
                r, from_=lo, to=hi, orient="horizontal", variable=var, showvalue=0, length=100,
                bg="#222222", fg="white", troughcolor="#444444", highlightthickness=0,
                command=lambda v, a=attr: setattr(self, a, int(float(v))),
            ).pack(side="left")
            tk.Label(r, textvariable=var, fg="white", bg="#222222", width=3, anchor="w").pack(side="left", padx=(4, 4))

        slider("Lens size", self._g_lens_var, 40, 300, "_g_lens")
        slider("Zoom", self._g_zoom_var, 2, 16, "_g_zoom")
        r.pack(side="top", fill="x")
        self._tl_fit()

    def _g_top(self, on):
        tf = getattr(self, "_tl_tools_frame", None)
        if tf is None:
            self._g_done = None
            return
        kids = {w.cget("text"): w for w in tf.winfo_children() if isinstance(w, tk.Button)}
        back, mag = kids.get("Back"), kids.get("Magnify")
        if on:
            if self._g_done is not None:
                return
            if back is not None:
                back.pack_forget()
            if mag is not None:
                mag.config(relief="sunken")
            b = tk.Button(tf, text="Done", pady=BUTTON_PADY, command=self._g_exit)
            b.pack(side="left", padx=(10, 2), pady=4)
            self._g_done = b
        else:
            if self._g_done is None:
                return
            self._g_done.destroy()
            self._g_done = None
            if mag is not None:
                mag.config(relief="raised")
            if back is not None:
                back.pack(side="left", padx=(10, 2), pady=4)

    def _g_start_2(self):
        if self._g_active:
            return
        if self._m_active:
            self._m_exit()
        if self._o_active:
            self._o_exit_paint()
        if self._tl_snap_mode or self.save_row.winfo_children():
            self._collapse_save_row()
        self._g_active = True
        self._g_lens = int(getattr(_fs_consts, "MAGNIFIER_LENS_SIZE_DEFAULT", 120))
        self._g_zoom = int(getattr(_fs_consts, "MAGNIFIER_LEVEL_DEFAULT", 4))
        self._g_lens_var = tk.IntVar(master=self.root, value=self._g_lens)
        self._g_zoom_var = tk.IntVar(master=self.root, value=self._g_zoom)
        self._g_native = None
        self._g_pos = None
        self.canvas.bind("<Motion>", self._g_motion)
        self.canvas.bind("<Leave>", self._g_leave)
        self.canvas.configure(cursor="none")
        self._g_top(True)
        self._g_build_row()

    def _g_exit_2(self):
        if not self._g_active:
            return
        self._g_active = False
        self.canvas.unbind("<Motion>")
        self.canvas.unbind("<Leave>")
        self.canvas.delete("magnify")
        self.canvas.configure(cursor="crosshair")
        self._g_native = None
        self._g_top(False)
        self._collapse_save_row()

    # ---- OCR row height lock, Magnify pin, Pin tool ----

    def _o_result(self, text, err):
        self._o_result_2(text, err)
        try:
            if getattr(self, "_o_overlay", None) is not None and getattr(self, "_o_text", None) is not None:
                self._o_text.config(height=4)
                self._o_btns.pack_forget()
                self._o_btns.pack(side="bottom", fill="x", padx=16, pady=16, before=self._o_text)
        except tk.TclError:
            pass
        result = None
        b = getattr(self, "_o_btns", None)
        if b is not None and getattr(self, "_o_overlay", None) is not None:
            _k_fit(b, True)
        return result

    # ---- Magnify: pinned lens ----

    def _h_lens(self, cx, cy):
        from PIL import Image, ImageDraw
        src = self._g_source()
        if src is None:
            return None
        dw = self._display_size()[0] or src.width
        s = src.width / dw
        L = int(self._g_lens)
        Z = max(1, int(self._g_zoom))
        half = L / Z / 2 * s
        box = tuple(int(round(v)) for v in (cx * s - half, cy * s - half, cx * s + half, cy * s + half))
        patch = src.crop(box).convert("RGB").resize((L, L), Image.BICUBIC)
        mask = Image.new("L", (L, L), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, L - 1, L - 1), fill=255)
        lens = patch.convert("RGBA")
        lens.putalpha(mask)
        ImageDraw.Draw(lens).ellipse((0, 0, L - 1, L - 1), outline=(0, 180, 255, 255), width=2)
        return lens

    def _g_draw(self, cx, cy):
        from PIL import ImageTk
        self.canvas.delete("magnify")
        if self._g_pin is None:
            lens = self._h_lens(cx, cy)
            if lens is not None:
                self._g_photo = ImageTk.PhotoImage(lens)
                self.canvas.create_image(cx, cy, image=self._g_photo, tags=("magnify",))
        self.canvas.create_oval(cx - 5, cy - 5, cx + 5, cy + 5, outline="white", tags=("magnify",))

    def _h_draw_pin(self):
        from PIL import ImageTk
        self.canvas.delete("magpin")
        if self._g_pin is None:
            return
        k = self._m_k() or 1.0
        cx, cy = self._g_pin[0] / k, self._g_pin[1] / k
        lens = self._h_lens(cx, cy)
        if lens is None:
            return
        self._g_pin_photo = ImageTk.PhotoImage(lens)
        self.canvas.create_image(cx, cy, image=self._g_pin_photo, tags=("magpin",))
        self.canvas.tag_raise("magpin")
        self.canvas.tag_raise("magnify")

    def _g_fetch_native(self):
        if self._g_fetching:
            return
        self._g_fetching = True
        idx = self.frame_index

        def work():
            img = None
            try:
                img = self.get_current_frame_image()
            except Exception:
                log.exception("Film viewer: Magnify native frame grab failed")

            def done():
                self._g_fetching = False
                if img is not None:
                    self._g_native = (idx, img)
                try:
                    if self._g_pin is not None:
                        self._h_draw_pin()
                    if self._g_active and self._g_pos is not None:
                        self._g_draw(*self._g_pos)
                    if (self._g_active or self._g_pin is not None) and not self.playing and idx != self.frame_index:
                        self._g_fetch_native()
                except tk.TclError:
                    pass

            try:
                self.root.after(0, done)
            except Exception:
                pass

        threading.Thread(target=work, daemon=True).start()

    def _h_rclick(self, event):
        if not self._g_active:
            return
        cx, cy = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        k = self._m_k() or 1.0
        if self._g_pin is None:
            self._g_pin = (cx * k, cy * k)
            self._g_pos = (cx, cy)
            self._h_draw_pin()
            self._g_draw(cx, cy)
            if not self.playing and (self._g_native is None or self._g_native[0] != self.frame_index):
                self._g_fetch_native()
        else:
            px, py = self._g_pin[0] / k, self._g_pin[1] / k
            if _m_math.hypot(cx - px, cy - py) <= self._g_lens / 2:
                self._g_pin = None
                self.canvas.delete("magpin")
                self._g_draw(cx, cy)

    def _h_reset(self):
        self._g_pin = None
        self.canvas.delete("magpin")
        self._g_lens = int(getattr(_fs_consts, "MAGNIFIER_LENS_SIZE_DEFAULT", 120))
        self._g_zoom = int(getattr(_fs_consts, "MAGNIFIER_LEVEL_DEFAULT", 4))
        self._g_lens_var.set(self._g_lens)
        self._g_zoom_var.set(self._g_zoom)
        if self._g_pos is not None:
            self._g_draw(*self._g_pos)

    def _g_build_row(self):
        r = self.save_row
        for w in r.winfo_children():
            w.destroy()
        tk.Button(r, text="Reset", pady=BUTTON_PADY, command=self._h_reset).pack(side="left", padx=(4, 6), pady=(10, 4))

        def slider(label, var, lo, hi, attr):
            tk.Label(r, text=label, fg="white", bg="#222222").pack(side="left", padx=(8, 4), pady=(10, 0))
            tk.Scale(
                r, from_=lo, to=hi, orient="horizontal", variable=var, showvalue=0, length=100,
                bg="#222222", fg="white", troughcolor="#444444", highlightthickness=0,
                command=lambda v, a=attr: setattr(self, a, int(float(v))),
            ).pack(side="left", pady=(6, 0))
            tk.Label(r, textvariable=var, fg="white", bg="#222222", width=3, anchor="w").pack(side="left", padx=(4, 4), pady=(10, 0))

        slider("Lens size", self._g_lens_var, 40, 300, "_g_lens")
        slider("Zoom", self._g_zoom_var, 2, 16, "_g_zoom")
        tk.Label(r, text="Right-click: pin / grab the lens", fg="#888888", bg="#222222").pack(side="left", padx=(10, 2), pady=(10, 0))
        r.pack(side="top", fill="x")
        self._tl_fit()

    def _g_start(self):
        if self._g_active:
            return
        before = (self._g_lens, self._g_zoom)
        self._g_start_2()
        if self._g_inited:
            self._g_lens, self._g_zoom = before
            self._g_lens_var.set(before[0])
            self._g_zoom_var.set(before[1])
        self._g_inited = True
        self.canvas.bind("<Button-3>", self._h_rclick)

    def _g_exit(self):
        was = self._g_active
        self._g_exit_2()
        if was:
            self.canvas.unbind("<Button-3>")

    # ---- Pin tool ----

    def _p_dim(self, x, y, text):
        self.canvas.delete("pin_dim")
        t = self.canvas.create_text(x, y - 14, text=text, fill="white", font=("", 10, "bold"), anchor="s", tags=("pin_dim",))
        bb = self.canvas.bbox(t)
        if bb:
            r = self.canvas.create_rectangle(bb[0] - 4, bb[1] - 4, bb[2] + 4, bb[3] + 4, fill="#111111", outline="#00b4ff", tags=("pin_dim",))
            self.canvas.tag_lower(r, t)

    def _p_press(self, event):
        if not self._p_active:
            return
        self._p_pt = (self.canvas.canvasx(event.x), self.canvas.canvasy(event.y))
        self.canvas.delete("pin_sel")
        x, y = self._p_pt
        self.canvas.create_rectangle(x, y, x, y, outline="#00e0a0", width=2, dash=(4, 2), tags=("pin_sel",))

    def _p_drag(self, event):
        if self._p_pt is None:
            return
        cx, cy = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        sx, sy = self._p_pt
        self.canvas.coords("pin_sel", sx, sy, cx, cy)
        k = self._m_k() or 1.0
        self._p_dim(cx, cy, f"{round(abs(cx - sx) * k)} x {round(abs(cy - sy) * k)}")

    def _p_release_2(self, event):
        if self._p_pt is None:
            return
        sx, sy = self._p_pt
        self._p_pt = None
        cx, cy = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        self.canvas.delete("pin_sel")
        self.canvas.delete("pin_dim")
        if abs(cx - sx) < 6 or abs(cy - sy) < 6:
            return
        img = self.get_current_frame_image()
        if img is None:
            notify("Pin failed", "Could not grab the current frame - see the log", blocking=False)
            return
        k = self._m_k() or 1.0
        iw, ih = img.size
        box = (
            max(0, min(iw, int(round(min(sx, cx) * k)))), max(0, min(ih, int(round(min(sy, cy) * k)))),
            max(0, min(iw, int(round(max(sx, cx) * k)))), max(0, min(ih, int(round(max(sy, cy) * k)))),
        )
        if box[2] - box[0] < 4 or box[3] - box[1] < 4:
            return
        self._p_launch(img, box)

    def _p_launch_2(self, img, box):
        from ._paths import INSTALL_DIR
        fd, tmp = tempfile.mkstemp(suffix=".png", prefix="snippysnappy-pin-")
        os.close(fd)
        try:
            img.convert("RGB").crop(box).save(tmp)
            env = dict(os.environ)
            pp = env.get("PYTHONPATH")
            env["PYTHONPATH"] = str(INSTALL_DIR) + (os.pathsep + pp if pp else "")
            pin_id = str(uuid.uuid4()); self.__dict__.setdefault("_owned_pin_ids", set()).add(pin_id)
            subprocess.Popen(
                [sys.executable, "-X", "faulthandler", "-m", "snippysnappy", "--pin-view", tmp, "--pin-id", pin_id,
                 "--pin-proportional", "1" if self._p_prop_var.get() else "0"],
                start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=_p40_pin_stderr(), env=env,
            )
            log.info(f"Film viewer: Pin launched for {box[2] - box[0]}x{box[3] - box[1]} region ({tmp}, pin_id={pin_id})")
        except Exception:
            log.exception("Film viewer: could not launch the pin window")
            try:
                Path(tmp).unlink(missing_ok=True)
            except Exception:
                pass
            notify("Pin failed", "See the log", blocking=False)

    def _p_build_row_2(self):
        r = self.save_row
        for w in r.winfo_children():
            w.destroy()
        tk.Label(r, text="Drag a region to pin it to the desktop", fg="#aaaaaa", bg="#222222").pack(side="left", padx=(8, 8), pady=(10, 4))
        tk.Checkbutton(
            r, text="Proportional resize", variable=self._p_prop_var, bg="#222222", fg="white",
            selectcolor="#444444", activebackground="#222222", activeforeground="white",
        ).pack(side="left", padx=4, pady=(6, 0))
        r.pack(side="top", fill="x")
        self._tl_fit()

    def _p_top(self, on):
        tf = getattr(self, "_tl_tools_frame", None)
        if tf is None:
            self._p_done = None
            return
        kids = {w.cget("text"): w for w in tf.winfo_children() if isinstance(w, tk.Button)}
        back, pin = kids.get("Back"), kids.get("Pin")
        if on:
            if self._p_done is not None:
                return
            if back is not None:
                back.pack_forget()
            if pin is not None:
                pin.config(relief="sunken")
            b = tk.Button(tf, text="Done", pady=BUTTON_PADY, command=self._p_exit)
            b.pack(side="left", padx=(10, 2), pady=4)
            self._p_done = b
        else:
            if self._p_done is None:
                return
            self._p_done.destroy()
            self._p_done = None
            if pin is not None:
                pin.config(relief="raised")
            if back is not None:
                back.pack(side="left", padx=(10, 2), pady=4)

    def _p_start(self):
        if self._p_active:
            return
        if self._m_active:
            self._m_exit()
        if self._g_active:
            self._g_exit()
        if self._o_active:
            self._o_exit_paint()
        if self._tl_snap_mode or self.save_row.winfo_children():
            self._collapse_save_row()
        self._pause()
        self._p_active = True
        self._p_pt = None
        if self._p_prop_var is None:
            self._p_prop_var = tk.IntVar(master=self.root, value=int(bool(getattr(_fs_consts, "DEFAULT_PROPORTIONAL_RESIZE", True))))
        self.canvas.bind("<ButtonPress-1>", self._p_press)
        self.canvas.bind("<B1-Motion>", self._p_drag)
        self.canvas.bind("<ButtonRelease-1>", self._p_release)
        self._p_top(True)
        self._p_build_row()

    def _tl_todo(self, name):
        if name == "Pin":
            self._p_start()
            return
        if self._p_active:
            self._p_exit()
        if name == "Magnify":
            self._g_start()
            return
        if self._g_active:
            self._g_exit()
        if name == "Measure":
            self._m_start()
            return
        log.info(f"Film viewer: {name} clicked (not yet implemented)")
        notify("Not yet implemented", f"{name} is the next Tools step", blocking=False)

    def _tl_filmsnap(self):
        if self._p_active:
            self._p_exit()
        if self._g_active:
            self._g_exit()
        if self._m_active:
            self._m_exit()
        if self._o_active:
            self._o_exit_paint()
        if self._tl_snap_mode:
            self._collapse_save_row()
            return
        self._pause()
        if self.save_row.winfo_children():
            self._collapse_save_row()

        def done(img):
            if img is None:
                notify("Filmsnap failed", "Could not grab the current frame - see the log", blocking=False)
                return
            self._tl_img = img
            self._tl_snap_mode = True
            try:
                self._tl_snap_btn.config(relief="sunken")
            except Exception:
                pass
            self._tl_snap_buttons()
            self.save_row.pack(side="top", fill="x")
            self._tl_fit()

        def work():
            img = None
            try:
                img = self.get_current_frame_image()
            except Exception:
                log.exception("Film viewer: Filmsnap frame grab failed")
            try:
                self.root.after(0, lambda: done(img))
            except Exception:
                pass

        threading.Thread(target=work, daemon=True).start()

    def _t2_back(self):
        if self._p_active:
            self._p_exit()
        if self._g_active:
            self._g_exit()
        if self._m_active:
            self._m_exit()
        if self._o_active:
            self._o_exit_paint()
        _t2_back(self)
        self._v3_layout()

    def _o_start(self):
        if self._p_active:
            self._p_exit()
        if self._g_active:
            self._g_exit()
        if self._m_active:
            self._m_exit()
        if self._o_active or self._o_busy:
            return
        if self._tl_snap_mode:
            self._collapse_save_row()
        self._pause()
        self._o_busy = True

        def done(img):
            self._o_busy = False
            if img is None:
                notify("Extract Text failed", "Could not grab the current frame - see the log", blocking=False)
                return
            if getattr(self, "_tl_tools_frame", None) is None:
                return
            self._o_img = img
            self._o_strokes = []
            self._o_redo = []
            self._o_cur = None
            self._o_active = True
            self.canvas.bind("<ButtonPress-1>", self._o_press)
            self.canvas.bind("<B1-Motion>", self._o_drag)
            self.canvas.bind("<ButtonRelease-1>", self._o_release)
            self._o_build_row()
            self._tl_fit()

        def work():
            img = None
            try:
                img = self.get_current_frame_image()
            except Exception:
                log.exception("Film viewer: Extract Text frame grab failed")
            try:
                self.root.after(0, lambda: done(img))
            except Exception:
                pass

        threading.Thread(target=work, daemon=True).start()

    # ---- OCR review button gaps, Pin row layout ----

    def _o_main_buttons(self):
        self._o_clear_btns()
        b_4 = self._o_btns
        tk.Button(b_4, text="Copy Text", pady=BUTTON_PADY, command=self._o_copy_text).pack(side="left", padx=4)
        tk.Button(b_4, text="Save Text", pady=BUTTON_PADY, command=self._o_save_options).pack(side="left", padx=4)
        tk.Button(b_4, text="Back", pady=BUTTON_PADY, command=self._o_close_overlay).pack(side="left", padx=4)
        tk.Button(b_4, text="Close", pady=BUTTON_PADY, command=self._o_exit_paint).pack(side="left", padx=4)
        result_2 = None
        b_5 = getattr(self, "_o_btns", None)
        if b_5 is not None and getattr(self, "_o_overlay", None) is not None:
            _k_fit(b_5, False)
        result = result_2
        try:
            b = getattr(self, "_o_btns", None)
            if b is not None and getattr(self, "_o_overlay", None) is not None:
                _q_walk(b)
        except tk.TclError:
            pass
        return result

    def _o_save_options(self):
        self._o_clear_btns()
        b_3 = self._o_btns
        tk.Checkbutton(
            b_3, text="Also copy", variable=self._o_copy_var, bg="#1a1a1a", fg="white", selectcolor="#444444",
            activebackground="#1a1a1a", activeforeground="white",
        ).pack(side="left", padx=(0, 8))
        tk.Button(b_3, text="Save", pady=BUTTON_PADY, command=self._o_save_name).pack(side="left", padx=4)
        tk.Button(b_3, text="Sloppy Save", pady=BUTTON_PADY, command=lambda: self._o_write_text(True, None)).pack(side="left", padx=4)
        tk.Button(b_3, text="Back", pady=BUTTON_PADY, command=self._o_main_buttons).pack(side="left", padx=4)
        try:
            for w in self._o_btns.winfo_children():
                if isinstance(w, tk.Checkbutton):
                    w.pack_forget()
                    w.pack(side="right", padx=(8, 0))
        except tk.TclError:
            pass
        result_3 = None
        b_6 = getattr(self, "_o_btns", None)
        if b_6 is not None and getattr(self, "_o_overlay", None) is not None:
            _k_fit(b_6, False)
        result = result_3
        try:
            b = getattr(self, "_o_btns", None)
            if b is not None and getattr(self, "_o_overlay", None) is not None:
                _q_walk(b)
        except tk.TclError:
            pass
        return result

    def _o_save_name(self):
        self._o_clear_btns()
        b_2 = self._o_btns
        name_var = tk.StringVar(value=f"snippytext-{_tl_stamp()}")
        entry = tk.Entry(b_2, textvariable=name_var, width=28)
        entry.pack(side="left", padx=4)
        entry.focus_set()
        entry.bind("<Return>", lambda e: self._o_write_text(False, name_var.get()))
        tk.Button(b_2, text="Confirm", pady=BUTTON_PADY, command=lambda: self._o_write_text(False, name_var.get())).pack(side="left", padx=4)
        tk.Button(b_2, text="Cancel", pady=BUTTON_PADY, command=self._o_save_options).pack(side="left", padx=4)
        result_4 = None
        b_7 = getattr(self, "_o_btns", None)
        if b_7 is not None and getattr(self, "_o_overlay", None) is not None:
            _k_fit(b_7, False)
        result = result_4
        try:
            b = getattr(self, "_o_btns", None)
            if b is not None and getattr(self, "_o_overlay", None) is not None:
                _q_walk(b)
        except tk.TclError:
            pass
        return result

    # ---- video pin: pre-pin options + launcher ----

    def _vp_launch(self, img, box):
        v_2 = self._vp_vars
        if v_2 is None or not v_2["video"].get() or not self.total_frames:
            return self._p_launch_2(img, box)
        x0, y0, x1, y1 = box
        x0 -= x0 % 2
        y0 -= y0 % 2
        w = (x1 - x0) // 2 * 2
        h = (y1 - y0) // 2 * 2
        if w < 4 or h < 4:
            return
        src = Path(self.video_path)
        fps, frames = float(self.fps), int(self.total_frames)
        opts = {"volume": int(v_2["vol"].get()), "loop": bool(v_2["loop"].get()), "autoplay": bool(v_2["autoplay"].get())}
        notify("Pinning\u2026", "Preparing the video pin", blocking=False)
        prop_flag = "1" if self._p_prop_var.get() else "0"   # patch 43: Tk variables are read on the Tk thread only

        def work():
            stage = None
            try:
                from ._paths import INSTALL_DIR
                stage = Path(tempfile.mkdtemp(prefix="pinclips-", dir=_p40_pin_stage_parent()))
                clip = stage / ("clip" + src.suffix)
                try:
                    os.link(src, clip)          # _p40: same filesystem -> instant, no second copy on disk
                except OSError:
                    shutil.copy2(src, clip)
                first = self._decode_frame(0, self.native_w, self.native_h)
                if first is None:
                    raise RuntimeError("could not decode the first frame")
                fd, tmp = tempfile.mkstemp(suffix=".png", prefix="snippysnappy-pin-")
                os.close(fd)
                first.convert("RGB").crop((x0, y0, x0 + w, y0 + h)).save(tmp)
                spec = dict(opts, clip=str(clip), stage=str(stage), box=[x0, y0, w, h], fps=fps, frames=frames, tracks=_fa_audio_count(clip))
                env = dict(os.environ)
                pp = env.get("PYTHONPATH")
                env["PYTHONPATH"] = str(INSTALL_DIR) + (os.pathsep + pp if pp else "")
                env["SNIPPYSNAPPY_PIN_VIDEO"] = _vp_json.dumps(spec)
                pin_id = str(uuid.uuid4()); self.__dict__.setdefault("_owned_pin_ids", set()).add(pin_id)
                subprocess.Popen(
                    [sys.executable, "-X", "faulthandler", "-m", "snippysnappy", "--pin-view", tmp, "--pin-id", pin_id,
                     "--pin-proportional", prop_flag],
                    start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=_p40_pin_stderr(), env=env,
                )
                log.info(f"Film viewer: video pin launched ({w}x{h}, {frames} frames, clip {clip}, pin_id={pin_id})")
            except Exception:
                log.exception("Film viewer: could not launch the video pin")
                if stage is not None:
                    shutil.rmtree(stage, ignore_errors=True)
                try:
                    self.root.after(0, lambda: notify("Pin failed", "See the log", blocking=False))
                except Exception:
                    pass

        threading.Thread(target=work, daemon=True).start()

    # ---- film Pin row: pins list, probe gate ----

    def _vl_entries(self):
        entries = [(p.window_id, p._document_label(), False, None) for p in (_vl_get("_LOCAL_PINS") or [])]
        owned = self.__dict__.get("_owned_pin_ids", set())
        show = bool(self._vl_other.get()) if self._vl_other is not None else False
        rp = _vl_get("remote_pins")
        if rp is not None:
            for cid, pid, label in rp():
                if pid in owned or show:
                    entries.append((pid, label, True, cid))
        return entries

    def _vl_refresh(self):
        mb = self._vl_btn
        try:
            if mb is None or not mb.winfo_exists():
                return
            mb.configure(text=f"Pins ({len(self._vl_entries())})")
            pop = self._vl_popup
            if pop is not None and pop.winfo_exists():
                self._vl_render()
        except tk.TclError:
            pass

    def _vl_close_popup(self):
        pop = self._vl_popup
        self._vl_popup = None
        if pop is not None:
            try:
                pop.destroy()
            except Exception:
                pass

    def _vl_toggle(self):
        pop = self._vl_popup
        if pop is not None and pop.winfo_exists():
            self._vl_close_popup()
            return
        pop = tk.Toplevel(self.root, bg="#1c1c1c", highlightthickness=1, highlightbackground="#444444")
        pop.overrideredirect(True)
        pop.attributes("-topmost", True)
        pop.geometry(f"+{self._vl_btn.winfo_rootx()}+{self._vl_btn.winfo_rooty() + self._vl_btn.winfo_height()}")
        self._vl_popup = pop
        self._vl_render()
        pop.bind("<Escape>", lambda e: self._vl_close_popup())
        try:
            pop.focus_force()
        except Exception:
            pass

    def _vl_render_2(self):
        pop = self._vl_popup
        if pop is None or not pop.winfo_exists():
            return
        for w in pop.winfo_children():
            w.destroy()
        owned = self.__dict__.get("_owned_pin_ids", set())
        kw = dict(bg="#1c1c1c", fg="white", anchor="w", padx=10, pady=6, font=("", 9))
        entries = self._vl_entries()
        if not entries:
            tk.Label(pop, text="(no pins open)", **dict(kw, fg="#888888")).pack(fill="x")
        for pid, label, remote, cid in entries:
            text = label if pid in owned or not remote else f"{label} (other window)"
            row = tk.Label(pop, text=text, cursor="hand2", **kw)
            row.pack(fill="x")
            row.bind("<Enter>", lambda e, r=row: r.configure(bg="#3a3a3a"))
            row.bind("<Leave>", lambda e, r=row: r.configure(bg="#1c1c1c"))
            row.bind("<Button-1>", lambda e, p=pid, rm=remote, c=cid: (self._vl_focus(p, rm, c), self._vl_close_popup()))
            row.bind("<Button-3>", lambda e, p=pid, rm=remote, c=cid: self._vl_close_pin(p, rm, c))
        tk.Label(pop, text="Left-click: focus \u00b7 Right-click: close", bg="#1c1c1c", fg="#777777", anchor="w", padx=10, pady=4, font=("", 8)).pack(fill="x")

    def _vl_focus(self, pid, remote, cid):
        coord = _vl_get("_coord")
        if remote:
            if not coord or coord.get("role") is None:
                notify("Can't reach that pin", "The coordinator isn't active - see snippysnappy.log", blocking=False)
                return
            _vl_get("request_remote_focus")(cid, pid)
            return
        pin = _vl_get("_find_local_pin")(pid)
        if pin is None:
            self._vl_refresh()
            return
        _vl_get("focus_local_window")(pin)

    def _vl_close_pin(self, pid, remote, cid):
        coord = _vl_get("_coord")
        if remote:
            if not coord or coord.get("role") is None:
                notify("Can't reach that pin", "The coordinator isn't active - see snippysnappy.log", blocking=False)
                return
            _vl_get("request_remote_pin_close")(cid, pid)
            self.root.after(600, self._vl_refresh)
            return
        pin = _vl_get("_find_local_pin")(pid)
        if pin is not None:
            pin._cleanup()
        self._vl_refresh()

    def _vl_clear(self):
        owned = self.__dict__.get("_owned_pin_ids", set())
        for pid, _l, remote, cid in [e for e in self._vl_entries() if e[0] in owned]:
            self._vl_close_pin(pid, remote, cid)

    def _p_build_row(self):
        r = self.save_row
        for w in r.winfo_children():
            w.destroy()
        m = self.root
        if self._vp_vars is None:
            self._vp_vars = {
                "video": tk.IntVar(master=m, value=1), "autoplay": tk.IntVar(master=m, value=1), "loop": tk.IntVar(master=m, value=0),
                "vol": tk.IntVar(master=m, value=int(getattr(_fs_consts, "FILM_DEFAULT_VOLUME", 100))),
            }
        if self._vl_other is None:
            self._vl_other = tk.IntVar(master=m, value=0)
        v = self._vp_vars
        cb = dict(bg="#222222", fg="white", selectcolor="#444444", activebackground="#222222", activeforeground="white")
        self._vl_close_popup()
        self._vl_btn = tk.Button(r, text="Pins (0)", pady=BUTTON_PADY, command=self._vl_toggle)
        self._vl_btn.pack(side="left", padx=2, pady=4)
        tk.Button(r, text="Clear", pady=BUTTON_PADY, command=self._vl_clear).pack(side="left", padx=2, pady=4)
        tk.Checkbutton(r, text="Video", variable=v["video"], **cb).pack(side="left", padx=(8, 2), pady=(6, 0))
        tk.Checkbutton(r, text="Autoplay", variable=v["autoplay"], **cb).pack(side="left", padx=2, pady=(6, 0))
        tk.Checkbutton(r, text="Loop", variable=v["loop"], **cb).pack(side="left", padx=2, pady=(6, 0))
        tk.Label(r, text="Volume", fg="white", bg="#222222").pack(side="left", padx=(8, 4), pady=(10, 0))
        tk.Scale(
            r, from_=0, to=100, orient="horizontal", variable=v["vol"], showvalue=0, length=80,
            bg="#222222", fg="white", troughcolor="#444444", highlightthickness=0,
        ).pack(side="left", pady=(6, 0))
        tk.Label(r, textvariable=v["vol"], fg="white", bg="#222222", width=3, anchor="w").pack(side="left", padx=(4, 4), pady=(10, 0))
        tk.Checkbutton(r, text="Proportional", variable=self._p_prop_var, **cb).pack(side="right", padx=(2, 8), pady=(6, 0))
        tk.Checkbutton(r, text="Other windows", variable=self._vl_other, command=self._vl_refresh, **cb).pack(side="right", padx=(4, 2), pady=(6, 0))
        r.pack(side="top", fill="x")
        self._tl_fit()
        self._vl_refresh()

    def _p_release(self, event):
        self._p_release_2(event)
        for ms in (800, 2500, 5000):
            self.root.after(ms, self._vl_refresh)

    def _p_exit(self):
        self._vl_close_popup()
        if not self._p_active:
            return
        self._p_active = False
        for ev in ("<ButtonPress-1>", "<B1-Motion>", "<ButtonRelease-1>"):
            self.canvas.unbind(ev)
        self.canvas.delete("pin_sel")
        self.canvas.delete("pin_dim")
        self._p_top(False)
        self._collapse_save_row()

    def _p_launch(self, img, box):
        v = self._vp_vars
        if v is not None and v["video"].get() and not self._aud_probed:
            notify("Still checking audio", "Try the pin again in a moment", blocking=False)
            return
        self._vp_launch(img, box)

    # ---- film window width guard ----

    def _fw_fit(self):
        rec = {}
        root = self.root
        orig = root.geometry

        def spy(*a, **k):
            if a and isinstance(a[0], str):
                rec["g"] = a[0]
            return orig(*a, **k)

        root.geometry = spy
        try:
            # Same sizing rules as SelectorApp._recalculate_display: 1.0x with scrollbars unless AUTO_SCALE_TO_FIT.
            mx, my, mw, mh = self._monitor_bounds
            max_w = max(MIN_WINDOW_WIDTH, min(MAX_WINDOW_WIDTH, mw))
            scrub_h = self.scrub_frame.winfo_reqheight()
            max_canvas_h = max(MIN_CANVAS_DISPLAY_SIZE, min(MAX_WINDOW_HEIGHT, mh) - TOOLBAR_HEIGHT - scrub_h)
            nw, nh = self.native_w, self.native_h
            if max(nw, nh) < MIN_CANVAS_DISPLAY_SIZE:
                self.display_scale = MIN_CANVAS_DISPLAY_SIZE / max(1, max(nw, nh))
            elif AUTO_SCALE_TO_FIT:
                self.display_scale = min(max_w / nw, max_canvas_h / nh)
            else:
                self.display_scale = 1.0
            disp_w = max(1, int(nw * self.display_scale))
            disp_h = max(1, int(nh * self.display_scale))
            pad = 0 if AUTO_SCALE_TO_FIT else SCROLLBAR_THICKNESS + VIEWPORT_BORDER_FUDGE
            win_w = max(MIN_WINDOW_WIDTH, min(disp_w + pad, max_w)) + SIDE_PANEL_WIDTH_RESERVE
            canvas_h = min(disp_h + pad, max_canvas_h)
            win_h = canvas_h + TOOLBAR_HEIGHT + scrub_h
            x = mx + max((mw - win_w) // 2, 0)
            y = my + max((mh - win_h) // 2, 0)
            self.root.geometry(f"{win_w}x{win_h}+{x}+{y}")
        finally:
            try:
                del root.geometry
            except AttributeError:
                pass
        m = _fw_re.match(r"(\d+)x(\d+)([+-]-?\d+)([+-]-?\d+)", rec.get("g", ""))
        if not m:
            return
        w, h, px, py = int(m.group(1)), int(m.group(2)), m.group(3), m.group(4)
        try:
            root.update_idletasks()
            floor = max(self.play_btn.master.winfo_reqwidth() + 16, 300)
            root.minsize(floor, 200)
            if w < floor:
                w = floor
                root.geometry(f"{w}x{h}{px}{py}")
        except tk.TclError:
            return
        self._fw_expected = (w, h, px, py)
        for ms in (250, 700, 1500, 3000):
            root.after(ms, self._fw_check)

    def _fw_check(self):
        exp = getattr(self, "_fw_expected", None)
        try:
            if exp is None or not self.root.winfo_exists():
                return
            w, h, px, py = exp
            if self.root.winfo_width() < w - 2:
                log.warning(f"Film viewer: window came up at {self.root.winfo_width()}x{self.root.winfo_height()}, expected {w}x{h} - re-asserting")
                self.root.geometry(f"{w}x{h}{px}{py}")
        except tk.TclError:
            pass

    # ---- Ctrl+scroll zoom and middle-drag pan ----

    def _z_scroll(self, direction, event):
        if not self.total_frames or not self.native_w:
            return
        old = self.display_scale
        step = float(getattr(_fs_consts, "CTRL_SCROLL_ZOOM_STEP", 0.1))
        cap = min(20.0, 6000.0 / max(self.native_w, self.native_h))
        new = max(0.05, min(cap, old * (1 + step * direction)))
        if abs(new - old) < 1e-4:
            return
        was = self.playing
        if was:
            self._pause()
        cx, cy = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        r = new / old
        self.display_scale = new
        w, h = self._display_size()
        self.canvas.configure(scrollregion=(0, 0, w, h))
        self._show_frame(self.frame_index)
        try:
            self.canvas.xview_moveto(max(0.0, (cx * r - event.x) / max(1, w)))
            self.canvas.yview_moveto(max(0.0, (cy * r - event.y) / max(1, h)))
        except tk.TclError:
            pass
        self._update_dimension_label()
        if was:
            self._play()

    def _z_pan_start(self, event):
        self.canvas.scan_mark(event.x, event.y)

    def _z_pan_drag_2(self, event):
        self.canvas.scan_dragto(event.x, event.y, gain=1)

    # ---- film Also checkboxes: default font, like the image toolbar ----

    def _cf_init(self, *args, **kwargs):
        self._init_base(*args, **kwargs)
        try:
            from . import paths_setup as _paths_setup
            self.root.after(400, lambda: _paths_setup.startup(self))
        except Exception:
            log.exception("folder setup hook for film window failed (non-fatal)")
        try:
            c_2 = _fs_consts
            self._also_copy_var = tk.IntVar(master=self.root, value=int(bool(getattr(c_2, "DEFAULT_COPY_ON_SAVE", True))))
            self._also_exit_var = tk.IntVar(master=self.root, value=int(bool(getattr(c_2, "DEFAULT_ALSO_EXIT", True))))
            self._also_save_var = tk.IntVar(master=self.root, value=int(bool(getattr(c_2, "DEFAULT_ALSO_SAVE", False))))
            self._film_keep_temp = False
            row_2 = self.dimension_label.master
            opts_2 = dict(bg="#222222", fg="white", selectcolor="#444444", activebackground="#222222", activeforeground="white", font=("", 8))
            tk.Checkbutton(row_2, text="Also exit", variable=self._also_exit_var, **opts_2).pack(side="left", padx=(8, 0))
            tk.Checkbutton(row_2, text="Also save", variable=self._also_save_var, **opts_2).pack(side="left", padx=(4, 0))
            if getattr(c_2, "BACKUP_ORIGINAL", False) and self._fs_is_temp():
                self.root.after(800, lambda: _fs_threading.Thread(target=self._fs_backup, daemon=True).start())
        except Exception:
            log.exception("Film viewer: save-option setup failed (non-fatal)")
        try:
            main_row_2 = self.play_btn.master
            text_row_2 = self.dimension_label.master
            for w_2 in list(text_row_2.winfo_children()):
                if isinstance(w_2, tk.Checkbutton):
                    w_2.destroy()
            box = tk.Frame(main_row_2, bg="#222222")
            box.pack(side="left", padx=(8, 4))
            opts = dict(bg="#222222", fg="white", selectcolor="#444444", activebackground="#222222", activeforeground="white")
            tk.Checkbutton(box, text="Also save", variable=self._also_save_var, **opts).pack(side="left", padx=(0, 8))
            tk.Checkbutton(box, text="Also copy", variable=self._also_copy_var, **opts).pack(side="left")
            tk.Checkbutton(box, text="Also exit", variable=self._also_exit_var, **opts).pack(side="left", padx=(8, 0))
            self.dimension_label.config(font="TkDefaultFont", pady=1)
            self.scale_label.config(font="TkDefaultFont", pady=1)
            ctrls = [self.rescale_btn] + [w_2 for w_2 in self.scrub_frame.winfo_children() if isinstance(w_2, tk.OptionMenu)]
            for w_2 in ctrls:
                if w_2 is not None:
                    w_2.pack_configure(pady=4)
            self.root.update_idletasks()
            need = max(w_2.winfo_reqheight() for w_2 in ctrls if w_2 is not None) + 8
            diff = need - 30
            if diff > 0:
                self.scrub_frame.config(height=need)
                self.toolbar_container.config(height=int(self.toolbar_container.cget("height")) + diff)
        except Exception:
            log.exception("Film viewer: layout parity setup failed (non-fatal)")
        try:
            main_row_3 = self.play_btn.master
            text_row_3 = self.dimension_label.master
            text_row_3.pack_forget()
            text_row_3.pack(side="top", fill="x", after=main_row_3)
        except Exception:
            log.exception("Film viewer: could not move the text row under the buttons (non-fatal)")
        try:
            main_row_4 = self.play_btn.master
            old_row_2 = self.dimension_label.master
            self.root.update_idletasks()
            row_h_2 = old_row_2.winfo_reqheight()
            dim_text_2 = self.dimension_label.cget("text")
            scale_text_2 = self.scale_label.cget("text")
            self.dimension_label = tk.Label(main_row_4, text=dim_text_2, fg="white", bg="#222222")
            self.dimension_label.pack(side="right", padx=(4, 8))
            self.scale_label = tk.Label(main_row_4, text=scale_text_2, fg="#aaaaaa", bg="#222222")
            self.scale_label.pack(side="right", padx=(0, 4))
            old_row_2.destroy()
            for w_3 in main_row_4.winfo_children():
                if isinstance(w_3, tk.Frame):
                    for cb_2 in w_3.winfo_children():
                        if isinstance(cb_2, tk.Checkbutton):
                            cb_2.config(font=("", 8), padx=2, pady=0)
            new_h = max(60, int(self.toolbar_container.cget("height")) - row_h_2)
            self.toolbar_container.config(height=new_h)
        except Exception:
            log.exception("Film viewer: could not move the readouts onto the button row (non-fatal)")
        try:
            main_row_5 = self.play_btn.master
            dim_text_3 = self.dimension_label.cget("text")
            scale_text_3 = self.scale_label.cget("text")
            self.dimension_label.destroy()
            self.scale_label.destroy()
            text_row = tk.Frame(self.toolbar_container, bg="#222222")
            text_row.pack(side="top", fill="x", after=main_row_5)
            self.dimension_label = tk.Label(text_row, text=dim_text_3, fg="white", bg="#222222", pady=0)
            self.dimension_label.pack(side="right", padx=(4, 8))
            self.scale_label = tk.Label(text_row, text=scale_text_3, fg="#aaaaaa", bg="#222222", pady=0)
            self.scale_label.pack(side="right", padx=(0, 4))
            for w_4 in main_row_5.winfo_children():
                if isinstance(w_4, tk.Frame):
                    for cb_3 in w_4.winfo_children():
                        if isinstance(cb_3, tk.Checkbutton):
                            cb_3.config(font=("", 8), padx=2, pady=4)
            self.root.update_idletasks()
            self.toolbar_container.config(height=int(self.toolbar_container.cget("height")) + text_row.winfo_reqheight())
        except Exception:
            log.exception("Film viewer: flush readout row setup failed (non-fatal)")
        try:
            main_row = self.play_btn.master
            old_row = self.dimension_label.master
            dim_text = self.dimension_label.cget("text")
            scale_text = self.scale_label.cget("text")
            self.root.update_idletasks()
            row_h = old_row.winfo_reqheight()
            old_row.destroy()
            self.dimension_label = tk.Label(main_row, text=dim_text, fg="white", bg="#222222", pady=0)
            self.dimension_label.pack(side="right", anchor="n", padx=(4, 8))
            self.scale_label = tk.Label(main_row, text=scale_text, fg="#aaaaaa", bg="#222222", pady=0)
            self.scale_label.pack(side="right", anchor="n", padx=(0, 4))
            self.toolbar_container.config(height=max(60, int(self.toolbar_container.cget("height")) - row_h))
        except Exception:
            log.exception("Film viewer: could not place the readouts under Re-scale/Tabs (non-fatal)")
        try:
            v = getattr(_fp_consts, "FILM_DEFAULT_VOLUME", 100)
            self._vol = max(0.0, min(1.0, float(v) / 100.0))
            self._muted = False
            self._aud_stream = None
            self._aud_tracks = 0
            self._aud_shown = self._aud_expanded = self._aud_dragging = False
            self.root.bind("<Destroy>", lambda e: self._fp_stop_audio() if e.widget is self.root else None, add="+")

            def on_probe(n):
                self._aud_tracks = n
                self._aud_probed = True
                if n > 0:
                    self._fp_build_overlay()

            def probe():
                n = _fa_audio_count(self.video_path)
                try:
                    self.root.after(0, lambda: on_probe(n))
                except Exception:
                    pass

            threading.Thread(target=probe, daemon=True).start()
        except Exception:
            log.exception("Film viewer: audio playback setup failed (non-fatal)")
        self._tl_row = None
        self._tl_snap_mode = False
        self._tl_snap_btn = None
        self._tl_tools_btn = None
        self._tl_img = None
        self._tl_base_h = None
        try:
            self._tl_main_row = self.play_btn.master
            for w_5 in self._tl_main_row.winfo_children():
                if isinstance(w_5, tk.Button) and w_5.cget("text") == "Tools":
                    w_5.config(command=self._tl_toggle_tools)
                    self._tl_tools_btn = w_5
        except Exception:
            log.exception("Film viewer: could not hook the Tools button (non-fatal)")
        self._v3_top = None
        self._v3_layout()
        try:
            c = self.canvas
            c.bind("<Control-Button-4>", lambda e: self._z_scroll(1, e))
            c.bind("<Control-Button-5>", lambda e: self._z_scroll(-1, e))
            c.bind("<Control-MouseWheel>", lambda e: self._z_scroll(1 if e.delta > 0 else -1, e))
            c.bind("<ButtonPress-2>", self._z_pan_start)
            c.bind("<B2-Motion>", self._z_pan_drag)
        except Exception:
            log.exception("Film viewer: zoom/pan setup failed (non-fatal)")
        def apply():
            try:
                row = self.play_btn.master
                for w in row.winfo_children():
                    if isinstance(w, tk.Frame):
                        for cb in w.winfo_children():
                            if isinstance(cb, tk.Checkbutton):
                                cb.config(font="TkDefaultFont", padx=2, pady=2)
            except tk.TclError:
                pass
        self.root.after(50, apply)

    # ---- film Pins list: per-entry X ----

    def _vl_render(self):
        pop = self._vl_popup
        if pop is None or not pop.winfo_exists():
            return
        for w in pop.winfo_children():
            w.destroy()
        owned = self.__dict__.get("_owned_pin_ids", set())
        bg, hover = "#1c1c1c", "#3a3a3a"
        entries = self._vl_entries()
        if not entries:
            tk.Label(pop, text="(no pins open)", bg=bg, fg="#888888", anchor="w", padx=10, pady=6, font=("", 9)).pack(fill="x")
        for pid, label, remote, cid in entries:
            text = label if pid in owned or not remote else f"{label} (other window)"
            row = tk.Frame(pop, bg=bg)
            row.pack(fill="x")
            name = tk.Label(row, text=text, bg=bg, fg="white", anchor="w", padx=10, pady=6, font=("", 9), cursor="hand2")
            name.pack(side="left", fill="x", expand=True)
            xbtn = tk.Label(row, text="\u2715", bg=bg, fg="#aaaaaa", padx=8, pady=6, font=("", 9), cursor="hand2")
            xbtn.pack(side="right")

            def enter(_e, r=row, n=name, x=xbtn):
                for w in (r, n, x):
                    w.configure(bg=hover)

            def leave(_e, r=row, n=name, x=xbtn):
                for w in (r, n, x):
                    w.configure(bg=bg)

            def do_focus(_e, p=pid, rm=remote, c=cid):
                self._vl_focus(p, rm, c)
                self._vl_close_popup()

            def do_close(_e, p=pid, rm=remote, c=cid):
                self._vl_close_pin(p, rm, c)
                self.root.after(600, self._vl_refresh)
                return "break"

            name.bind("<Enter>", enter)
            name.bind("<Leave>", leave)
            name.bind("<Button-1>", do_focus)
            name.bind("<Button-3>", do_close)
            xbtn.bind("<Enter>", lambda _e, x=xbtn: x.configure(fg="#ff5555"))
            xbtn.bind("<Leave>", lambda _e, x=xbtn: x.configure(fg="#aaaaaa"))
            xbtn.bind("<Button-1>", do_close)
        tk.Label(pop, text="Left-click: focus \u00b7 \u2715 / right-click: close", bg=bg, fg="#777777", anchor="w", padx=10, pady=4, font=("", 8)).pack(fill="x")

    # ---- film edit model + orange range bar (_ed_*) ----

    def _ed_state(self):
        if not hasattr(self, "film_layers"):
            self.film_layers = []
            self._ed_counter = 0
            self.film_active_layer_id = None
            self._ed_drag = None
            self._ed_range_vars = {}
        return self.film_layers

    def _ed_dur(self):
        if not self.total_frames or not self.fps:
            return 0.0
        return max(0, self.total_frames - 1) / self.fps

    def _ed_find(self, lid):
        for layer in self._ed_state():
            if layer["id"] == lid:
                return layer
        return None

    def _ed_range(self, layer):
        dur = self._ed_dur()
        s, e = layer.get("start"), layer.get("end")
        s = 0.0 if s is None else max(0.0, min(float(s), dur))
        e = dur if e is None else max(0.0, min(float(e), dur))
        return s, e

    def _ed_range_text(self, layer):
        if layer.get("start") is None and layer.get("end") is None:
            return "whole runtime"
        s, e = self._ed_range(layer)
        return f"{_ed_fmt(s)} - {_ed_fmt(e)}"

    def _ed_x(self, t):
        span = max(1, self.scrub_canvas.winfo_width() - 16)
        dur = self._ed_dur() or 1.0
        return 8 + (t / dur) * span

    def _ed_t(self, x):
        span = max(1, self.scrub_canvas.winfo_width() - 16)
        frac = max(0.0, min(1.0, (x - 8) / span))
        return frac * self._ed_dur()

    def _ed_new_layer(self, name):
        self._ed_state()
        self._ed_counter += 1
        return {"id": f"flayer{self._ed_counter}", "name": name, "kind": "strokes", "start": None, "end": None}

    def _ed_reset_range(self, layer):
        layer["start"] = None
        layer["end"] = None
        var = self._ed_range_vars.get(layer["id"])
        if var is not None:
            var.set(self._ed_range_text(layer))
        self._draw_scrub_playhead()

    def _ed_add_test_layer(self):
        layer = self._ed_new_layer("")
        layer["name"] = f"Test {self._ed_counter}"
        self.film_layers.append(layer)
        self._ed_select(layer["id"])

    def _ed_build_layers_panel_2(self, inner):
        self._ed_state()
        self._ed_range_vars = {}
        bg = "#222222"
        tk.Label(inner, text="Layers", fg="white", bg=bg, font=("", 10, "bold")).pack(side="top", anchor="w", padx=6, pady=(6, 4))
        full_row = tk.Frame(inner, bg=bg)
        full_row.pack(side="top", fill="x", padx=6, pady=(0, 6))
        tk.Button(
            full_row, text="Full", pady=BUTTON_PADY,
            relief=("sunken" if self.film_active_layer_id is None else "raised"),
            command=lambda: self._ed_select(None),
        ).pack(side="left")
        for layer in reversed(self.film_layers):
            active = layer["id"] == self.film_active_layer_id
            cbg = "#333333" if active else bg
            cell = tk.Frame(inner, bg=cbg, highlightthickness=1, highlightbackground="#555555")
            cell.pack(side="top", fill="x", padx=4, pady=2)
            tk.Button(
                cell, text=layer["name"], pady=BUTTON_PADY, anchor="w",
                relief=("sunken" if active else "raised"),
                command=lambda lid=layer["id"]: self._ed_select(lid),
            ).pack(side="top", fill="x", padx=2, pady=2)
            var = tk.StringVar(value=self._ed_range_text(layer))
            self._ed_range_vars[layer["id"]] = var
            tk.Label(cell, textvariable=var, fg="#ff9900", bg=cbg, anchor="w").pack(side="top", fill="x", padx=6)
            if active:
                tk.Button(
                    cell, text="Whole runtime", pady=BUTTON_PADY,
                    command=lambda l=layer: self._ed_reset_range(l),
                ).pack(side="top", fill="x", padx=2, pady=(2, 4))
        tk.Frame(inner, height=1, bg="#444444").pack(side="top", fill="x", padx=6, pady=(8, 6))
        tk.Button(inner, text="+ Test layer (temporary)", pady=BUTTON_PADY, command=self._ed_add_test_layer).pack(side="top", fill="x", padx=6, pady=(0, 6))

    def _ed_release(self, event):
        if getattr(self, "_ed_drag", None):
            self._ed_drag = None
            self._draw_scrub_playhead()
            return
        # Dragging shows nearest-keyframe previews (fast); letting go decodes the exact frame.
        self._scrubbing = False
        if self.total_frames:
            self._cache.clear()
            self._show_frame(self.frame_index)

    # ---- film edit tools: Draw / Shape / Text overlays (_ed2_*) ----

    def _ed2_state(self):
        self._ed_state()
        if not hasattr(self, "film_edits"):
            self.film_edits = []
            self._ed2_redo = []
            self._ed2_active = False
            self._ed2_cache = {}
            self._ed2_raw = None
            self._ed2_tool = "pen"
            self._ed2_hidden = []
            self._ed2_rows = []
            self._ed2_saved_binds = {}
            self._ed2_snap = None
            self._ed2_drag = None
            self._ed2_tmp = []
            self._ed2_color = None
            self._ed2_width = None
            self._ed2_pen_kind = None
            self._ed2_shape_kind = None
            self._ed2_fill = None
        return self.film_edits

    def _ed2_has_edits(self):
        return bool(self._ed2_state())

    def _ed2_patch_2(self, el, scale):
        from PIL import Image
        key = (el["eid"], round(scale, 4))
        p = self._ed2_cache.get(key)
        if p is None:
            base = self._ed2_native_patch(el)
            if abs(scale - 1.0) < 1e-3:
                p = base
            else:
                p = base.resize((max(1, int(round(base.width * scale))), max(1, int(round(base.height * scale)))), Image.LANCZOS)
            if len(self._ed2_cache) > 300:
                self._ed2_cache.clear()
            self._ed2_cache[key] = p
        return p

    def _ed2_compose_2(self, img):
        self._ed2_state()
        if not self.film_edits:
            return img
        fps = max(float(self.fps), 1.0)
        t = self.frame_index / fps
        tol = 0.5 / fps
        scale = img.width / max(1, self.native_w)
        order = {l["id"]: i for i, l in enumerate(self.film_layers)}
        out = None
        for el in sorted(self.film_edits, key=lambda e: order.get(e.get("layer_id"), 0)):
            layer = self._ed_find(el.get("layer_id"))
            if layer is None:
                continue
            s, e = self._ed_range(layer)
            if t < s - tol or t > e + tol:
                continue
            patch = self._ed2_patch(el, scale)
            if out is None:
                out = img.convert("RGBA")
            out.paste(patch, (int(round(el["origin"][0] * scale)), int(round(el["origin"][1] * scale))), patch)
        return out.convert("RGB") if out is not None else img

    def _ed2_redisplay(self):
        raw = getattr(self, "_ed2_raw", None)
        if raw is not None:
            self._display_image(raw)
        elif self.total_frames:
            self._show_frame(self.frame_index)

    def _ed2_target_layer_2(self, tool):
        layer = self._ed_active()
        if layer is not None:
            return layer["id"]
        gk = {"pen": "Draw", "highlight": "Draw", "shape": "Shapes", "text": "Text"}.get(tool, "Draw")
        for l in self.film_layers:
            if l.get("group_key") == gk:
                return l["id"]
        l = self._ed_new_layer(gk)
        l["group_key"] = gk
        self.film_layers.append(l)
        log.info(f"Film layer '{gk}' created (first {tool} element)")
        return l["id"]

    def _ed2_undo_2(self):
        self._ed2_state()
        if not self.film_edits:
            return
        self._ed2_redo.append(self.film_edits.pop())
        self._ed2_redisplay()

    def _ed2_redo_do_2(self):
        self._ed2_state()
        if not self._ed2_redo:
            return
        self.film_edits.append(self._ed2_redo.pop())
        self._ed2_redisplay()

    # ---- canvas interaction ----

    def _ed2_xy(self, event):
        cx = self.canvas.canvasx(event.x)
        cy = self.canvas.canvasy(event.y)
        s = self.display_scale or 1.0
        nx = max(0.0, min(float(self.native_w), cx / s))
        ny = max(0.0, min(float(self.native_h), cy / s))
        return nx, ny, cx, cy

    def _ed2_clear_tmp(self):
        for item in self._ed2_tmp:
            try:
                self.canvas.delete(item)
            except tk.TclError:
                pass
        self._ed2_tmp = []

    def _ed2_text_dialog(self, box):
        from .constants import TEXT_FONT_FAMILIES, TEXT_FONT_FAMILY_DEFAULT, TEXT_FONT_SIZE_DEFAULT
        x1, y1, w, h = box
        dlg = tk.Toplevel(self.root)
        dlg.title("Film text")
        dlg.transient(self.root)
        dlg.configure(bg="#222222")
        txt = tk.Text(dlg, width=36, height=5, wrap="word")
        txt.pack(padx=10, pady=(10, 6))
        txt.focus_set()
        row = tk.Frame(dlg, bg="#222222")
        row.pack(padx=10, pady=(0, 6), fill="x")
        size_var = tk.IntVar(value=int(TEXT_FONT_SIZE_DEFAULT))
        tk.Label(row, text="Size", fg="white", bg="#222222").pack(side="left")
        tk.Spinbox(row, from_=6, to=400, width=4, textvariable=size_var).pack(side="left", padx=(2, 8))
        fam_var = tk.StringVar(value=TEXT_FONT_FAMILY_DEFAULT)
        tk.OptionMenu(row, fam_var, *list(TEXT_FONT_FAMILIES.keys())).pack(side="left", padx=(0, 8))
        bgs = {"No background": None, "White": "white", "Black": "black", "White + border": "white_border", "Black + border": "black_border"}
        bg_var = tk.StringVar(value="No background")
        tk.OptionMenu(row, bg_var, *bgs.keys()).pack(side="left")
        result = {"ok": False}

        def ok():
            result.update(ok=True, text=txt.get("1.0", "end-1c"), size=size_var.get(), fam=fam_var.get(), bg=bgs.get(bg_var.get()))
            dlg.destroy()

        btns = tk.Frame(dlg, bg="#222222")
        btns.pack(padx=10, pady=(0, 10))
        tk.Button(btns, text="Add text", command=ok).pack(side="left", padx=(0, 6))
        tk.Button(btns, text="Cancel", command=dlg.destroy).pack(side="left")
        dlg.update_idletasks()
        try:
            px = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - dlg.winfo_reqwidth()) // 2)
            py = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - dlg.winfo_reqheight()) // 2)
            dlg.geometry(f"+{px}+{py}")
        except tk.TclError:
            pass
        dlg.grab_set()
        dlg.wait_window()
        if not result["ok"] or not result["text"].strip():
            return
        el = {
            "mode": "text", "text": result["text"], "color": self._ed2_color, "font_family": result["fam"],
            "font_size": int(result["size"]), "text_align": "left", "bg_fill": result["bg"],
            "origin": (x1, y1), "w": w, "h": h,
        }
        self._ed2_add(el, "text")

    # ---- edit row ----

    def _ed2_defaults(self):
        from . import constants as C
        pal = list(getattr(C, "PALETTE", ["#FF0000"]))
        if self._ed2_color is None:
            self._ed2_color = pal[0]
        if self._ed2_width is None:
            self._ed2_width = int(getattr(C, "EDIT_PEN_WIDTH", 4))
        if self._ed2_pen_kind is None:
            pm = str(getattr(C, "DEFAULT_PEN_MODE", "pen"))
            self._ed2_pen_kind = pm if pm in ("pen", "highlight") else "pen"
        if self._ed2_shape_kind is None:
            sm = str(getattr(C, "DEFAULT_SHAPE_MODE", "box"))
            self._ed2_shape_kind = sm if sm in ("line", "box", "ellipse") else "box"

    def _ed2_set(self, name, value):
        setattr(self, name, value)
        self._ed2_build_rows()

    def _ed2_set_tool_2(self, tool):
        self._ed2_tool = tool
        self._ed2_build_rows()

    def _ed2_swatches(self, parent, colors, current, attr, allow_none=False):
        if allow_none:
            tk.Button(parent, text="None", pady=BUTTON_PADY, relief=("sunken" if current is None else "raised"),
                      command=lambda: self._ed2_set(attr, None)).pack(side="left", padx=1, pady=2)
        for c in colors:
            tk.Button(parent, bg=c, activebackground=c, width=2, relief=("sunken" if c == current else "raised"),
                      command=lambda c=c: self._ed2_set(attr, c)).pack(side="left", padx=1, pady=2)

    def _ed2_build_rows_2(self):
        from . import constants as C
        self._ed2_defaults()
        for w in self._ed2_rows:
            try:
                w.destroy()
            except tk.TclError:
                pass
        self._ed2_rows = []
        main_row = self._tl_main_row
        bg = "#222222"
        ef = tk.Frame(main_row, bg=bg)
        ef.pack(side="left")
        self._ed2_rows.append(ef)
        for label, tool in (("Draw", "pen"), ("Shape", "shape"), ("Text", "text")):
            tk.Button(ef, text=label, pady=BUTTON_PADY, relief=("sunken" if self._ed2_tool == tool else "raised"),
                      command=lambda t=tool: self._ed2_set_tool(t)).pack(side="left", padx=2, pady=4)
        tk.Button(ef, text="Undo", pady=BUTTON_PADY, command=self._ed2_undo).pack(side="left", padx=2, pady=4)
        tk.Button(ef, text="Redo", pady=BUTTON_PADY, command=self._ed2_redo_do).pack(side="left", padx=2, pady=4)
        tk.Button(ef, text="Done", pady=BUTTON_PADY, command=lambda: self._ed2_exit(False)).pack(side="left", padx=(10, 2), pady=4)
        tk.Button(ef, text="Cancel", pady=BUTTON_PADY, command=lambda: self._ed2_exit(True)).pack(side="left", padx=2, pady=4)

        opts = tk.Frame(main_row.master, bg=bg)
        try:
            opts.pack(side="top", fill="x", after=main_row)
        except tk.TclError:
            opts.pack(side="top", fill="x")
        self._ed2_rows.append(opts)
        pal = list(getattr(C, "PALETTE", ["#FF0000"]))
        fpal = list(getattr(C, "FILL_PALETTE", pal + ["#FFFFFF"]))
        tool = self._ed2_tool
        if tool == "pen":
            kv = tk.StringVar(value="Highlighter" if self._ed2_pen_kind == "highlight" else "Pen")
            tk.OptionMenu(opts, kv, "Pen", "Highlighter", command=lambda v: self._ed2_set("_ed2_pen_kind", "highlight" if v == "Highlighter" else "pen")).pack(side="left", padx=2)
            self._ed2_swatches(opts, pal, self._ed2_color, "_ed2_color")
        elif tool == "shape":
            names = {"line": "Line", "box": "Box", "ellipse": "Ellipse"}
            kv = tk.StringVar(value=names[self._ed2_shape_kind])
            tk.OptionMenu(opts, kv, "Line", "Box", "Ellipse", command=lambda v: self._ed2_set("_ed2_shape_kind", v.lower() if v != "Box" else "box")).pack(side="left", padx=2)
            tk.Label(opts, text="Border", fg="white", bg=bg).pack(side="left", padx=(6, 2))
            self._ed2_swatches(opts, pal, self._ed2_color, "_ed2_color")
            tk.Label(opts, text="Fill", fg="white", bg=bg).pack(side="left", padx=(6, 2))
            self._ed2_swatches(opts, fpal, self._ed2_fill, "_ed2_fill", allow_none=True)
        else:
            self._ed2_swatches(opts, pal, self._ed2_color, "_ed2_color")
            tk.Label(opts, text="drag a box (or click), then type", fg="#aaaaaa", bg=bg).pack(side="left", padx=8)
        if tool in ("pen", "shape"):
            tk.Label(opts, text="Width", fg="white", bg=bg).pack(side="left", padx=(8, 2))
            wv = tk.IntVar(value=int(self._ed2_width))

            def on_w(*_a, v=wv):
                try:
                    self._ed2_width = max(1, int(v.get()))
                except (tk.TclError, ValueError):
                    pass

            sp = tk.Spinbox(opts, from_=1, to=80, width=3, textvariable=wv, command=on_w)
            sp.pack(side="left")
            sp.bind("<KeyRelease>", on_w)
        self._ed2_bind_canvas(True)
        try:
            self._tl_fit()
        except Exception:
            log.exception("Film edit: _tl_fit failed (non-fatal)")

    # ---- film edit: select / move / resize / rotate, undo stack (_ed3_*) ----

    def _ed3_state(self):
        self._ed2_state()
        if not hasattr(self, "_ed3_sel"):
            from . import constants as C
            self._ed3_sel = None
            self._ed3_acts = []
            self._ed3_redo = []
            self._ed3_drag = None
            self._ed3_keybound = False
            self._ed3_prop = tk.IntVar(master=self.root, value=int(bool(getattr(C, "DEFAULT_PROPORTIONAL_RESIZE", False))))

    def _ed3_el(self, eid):
        if eid is None:
            return None
        for el in self.film_edits:
            if el.get("eid") == eid:
                return el
        return None

    def _ed3_visible(self, el):
        layer = self._ed_find(el.get("layer_id"))
        if layer is None:
            return False
        fps = max(float(self.fps), 1.0)
        t = self.frame_index / fps
        tol = 0.5 / fps
        s, e = self._ed_range(layer)
        return s - tol <= t <= e + tol

    def _ed3_pt(self, event):
        s = self.display_scale or 1.0
        cx = self.canvas.canvasx(event.x)
        cy = self.canvas.canvasy(event.y)
        return cx / s, cy / s, cx, cy

    def _ed3_handles(self, el):
        ox, oy = el["origin"]
        w, h = el["w"], el["h"]
        cx, cy = ox + w / 2, oy + h / 2
        th = _ed3_math.radians(el.get("rotation", 0.0))
        c, s = _ed3_math.cos(th), _ed3_math.sin(th)
        gap = 24 / max(self.display_scale or 1.0, 0.01)
        offs = {
            "nw": (-w / 2, -h / 2), "ne": (w / 2, -h / 2), "sw": (-w / 2, h / 2), "se": (w / 2, h / 2),
            "n": (0, -h / 2), "s": (0, h / 2), "e": (w / 2, 0), "w": (-w / 2, 0),
            "rotate": (0, -h / 2 - gap),
        }
        return {n: (cx + dx * c - dy * s, cy + dx * s + dy * c) for n, (dx, dy) in offs.items()}

    def _ed3_hit_handle(self, el, cx, cy):
        s = self.display_scale or 1.0
        for name, (px, py) in self._ed3_handles(el).items():
            if abs(cx - px * s) <= 12 and abs(cy - py * s) <= 12:
                return name
        return None

    def _ed3_hit(self, el, x, y):
        ox, oy = el["origin"]
        w, h = el["w"], el["h"]
        th = _ed3_math.radians(el.get("rotation", 0.0))
        c, s = _ed3_math.cos(th), _ed3_math.sin(th)
        dx, dy = x - (ox + w / 2), y - (oy + h / 2)
        lx = dx * c + dy * s
        ly = -dx * s + dy * c
        tol = 8 / max(self.display_scale or 1.0, 0.01)
        if el.get("mode") == "shape" and el.get("shape_type") == "line":
            fx1, fy1 = el.get("start_frac", (0.0, 0.0))
            fx2, fy2 = el.get("end_frac", (1.0, 1.0))
            ax, ay = fx1 * w - w / 2, fy1 * h - h / 2
            bx, by = fx2 * w - w / 2, fy2 * h - h / 2
            vx, vy = bx - ax, by - ay
            L2 = vx * vx + vy * vy
            u = 0.0 if L2 == 0 else max(0.0, min(1.0, ((lx - ax) * vx + (ly - ay) * vy) / L2))
            dist = _ed3_math.hypot(lx - (ax + u * vx), ly - (ay + u * vy))
            return dist <= tol + el.get("width", 4) / 2
        return abs(lx) <= w / 2 + tol and abs(ly) <= h / 2 + tol

    def _ed3_draw_ui_2(self):
        try:
            self.canvas.delete("ed3ui")
        except tk.TclError:
            return
        el = self._ed3_el(self._ed3_sel)
        if el is None or not self._ed2_active or self._ed2_tool != "select":
            return
        s = self.display_scale or 1.0
        pos = self._ed3_handles(el)
        pts = []
        for n in ("nw", "ne", "se", "sw"):
            pts += [pos[n][0] * s, pos[n][1] * s]
        tg = ("ed3ui",)
        self.canvas.create_polygon(pts, outline="#00b4ff", fill="", width=2, dash=(5, 3), tags=tg)
        tmx = (pos["nw"][0] + pos["ne"][0]) / 2 * s
        tmy = (pos["nw"][1] + pos["ne"][1]) / 2 * s
        self.canvas.create_line(tmx, tmy, pos["rotate"][0] * s, pos["rotate"][1] * s, fill="#33cc33", tags=tg)
        for name, (px, py) in pos.items():
            col = "#33cc33" if name == "rotate" else "#ffaa00"
            self.canvas.create_oval(px * s - 6, py * s - 6, px * s + 6, py * s + 6, fill=col, outline="white", tags=tg)
        self.canvas.tag_raise("ed3ui")

    def _ed3_select_el(self, el):
        self._ed3_sel = el["eid"] if el is not None else None
        if el is not None and el.get("layer_id") != self.film_active_layer_id:
            self._ed_select(el["layer_id"])
        self._ed3_draw_ui()

    def _ed3_begin(self, el, mode, x, y):
        before = dict(el)
        self._ed3_lift(el)
        self._ed3_drag = {
            "el": el, "mode": mode, "start": (x, y), "before": before,
            "o": tuple(el["origin"]), "w": el["w"], "h": el["h"], "rot": el.get("rotation", 0.0),
        }

    def _ed3_delete(self):
        self._ed3_state()
        el = self._ed3_el(self._ed3_sel)
        if el is None:
            return
        for i, e in enumerate(self.film_edits):
            if e is el:
                self.film_edits.pop(i)
                self._ed3_acts.append(("del", el, i))
                self._ed3_redo = []
                break
        self._ed3_sel = None
        self._after_edit_change()

    def _ed2_add(self, el, tool):
        self._ed3_state()
        import uuid
        el["layer_id"] = self._ed2_target_layer(tool)
        el["eid"] = uuid.uuid4().hex
        el.setdefault("rotation", 0.0)
        self.film_edits.append(el)
        self._ed2_redo = []
        if self.side_panel_mode == "layers":
            self._refresh_side_panel()
        self._ed2_redisplay()
        log.info(f"Film edit: {el.get('mode')} added to layer {el['layer_id']} ({len(self.film_edits)} elements)")
        self._ed3_acts.append(("add", self.film_edits[-1]))
        self._ed3_redo = []

    def _ed3_key_2(self, event):
        if not (self._ed2_active and self._ed2_tool == "select"):
            return
        try:
            if isinstance(self.root.focus_get(), (tk.Entry, tk.Text, tk.Spinbox)):
                return
        except (tk.TclError, KeyError):
            pass
        self._ed3_delete()

    # ---- film edit: bake edits into the exported film (_ed4_*) ----

    def _ed4_dirty(self):
        return bool(self._ed4_flags())

    def _ed4_bake_2(self, dest_dir):
        from PIL import Image
        src = Path(self.video_path)
        out = Path(dest_dir) / f"{src.stem}-edited.mkv"
        order = {l["id"]: i for i, l in enumerate(self.film_layers)}
        els = [e for e in sorted(self.film_edits, key=lambda e: order.get(e.get("layer_id"), 0)) if self._ed_find(e.get("layer_id")) is not None]
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(src)]
        chain = []
        last = "0:v"
        for n, el in enumerate(els):
            patch = self._ed2_native_patch(el)
            rot = el.get("rotation", 0.0)
            if abs(rot) > 0.01:
                patch = patch.rotate(-rot, expand=True, resample=Image.BICUBIC)
            cx = el["origin"][0] + el["w"] / 2
            cy = el["origin"][1] + el["h"] / 2
            x = int(round(cx - patch.width / 2))
            y = int(round(cy - patch.height / 2))
            png = Path(dest_dir) / f"ov{n}.png"
            patch.save(png)
            cmd += ["-i", str(png)]
            layer = self._ed_find(el["layer_id"])
            en = ""
            if layer.get("start") is not None or layer.get("end") is not None:
                s, e = self._ed_range(layer)
                en = f":enable='between(t,{s:.3f},{e:.3f})'"
            chain.append(f"[{last}][{n + 1}:v]overlay=x={x}:y={y}:format=auto{en}[v{n}]")
            last = f"v{n}"
        chain.append(f"[{last}]pad=ceil(iw/2)*2:ceil(ih/2)*2[vout]")
        cmd += ["-filter_complex", ";".join(chain), "-map", "[vout]", "-map", "0:a?",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k", str(out)]
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if r.returncode != 0:
            raise RuntimeError("ffmpeg bake failed: " + r.stderr.decode("utf-8", "replace")[-800:])
        log.info(f"Film viewer: baked {len(els)} element(s) -> {out}")
        return out

    def _fs_export(self, out_path, ext, sloppy=False):
        if not self._ed4_dirty():
            return self._fs_export_2(out_path, ext, sloppy)
        self._pause()
        notify("Rendering edits\u2026", "Baking your edits into the film", blocking=False)
        dest = Path(_ed4_tempfile.mkdtemp(prefix="snippysnappy-bake-"))
        _ed4_atexit.register(_ed4_shutil.rmtree, str(dest), True)

        def worker():
            try:
                baked = self._ed4_bake(dest)
            except Exception:
                log.exception("Film viewer: baking edits failed")
                baked = None

            def after():
                if baked is None:
                    notify("Save failed", "Could not render the edits - see the log", blocking=False)
                    return
                orig_path = self.video_path
                self.video_path = baked
                try:
                    self._fs_export_2(out_path, ext, sloppy)
                finally:
                    self.video_path = orig_path

            self.root.after(0, after)

        _ed4_threading.Thread(target=worker, daemon=True).start()

    # ---- film edit: trim / crop / output scale / audio edits, baked on export (_ed5_*) ----

    def _ed5_state(self):
        self._ed3_state()
        if not hasattr(self, "_ed5_trim"):
            self._ed5_trim = {"id": "__trim__", "name": "Trim", "start": None, "end": None}
            self._ed5_crop = None
            self._ed5_scale = 1.0
            self._ed5_audio = []
            self._ed5_snap = None
            self._ed5_before = None
            self._ed5_cd = None

    def _ed5_trim_vals_2(self):
        self._ed5_state()
        t = self._ed5_trim
        if t["start"] is None and t["end"] is None:
            return None
        dur = self._ed_dur()
        s, e = self._ed_range(t)
        if s <= 0.05 and e >= dur - 0.05:
            return None
        return (s, e)

    def _ed5_audio_list(self, n):
        self._ed5_state()
        lst = self._ed5_audio
        while len(lst) < n:
            lst.append({"mute": False, "vol": 1.0})
        return lst

    def _ed5_audio_dirty_2(self):
        return any(a.get("mute") or abs(float(a.get("vol", 1.0)) - 1.0) > 0.001 for a in self._ed5_audio)

    def _ed5_scale_active(self):
        sc = self._ed5_scale
        return sc != 1.0 and not (sc == "orig" and not self._ed5_crop)

    def _ed5_set(self, attr, new, rebuild=True):
        self._ed5_state()
        old = self._ed5_get(attr)
        if old == new:
            return
        self._ed5_apply(attr, new)
        self._ed3_acts.append(("set", attr, old, new))
        self._ed3_redo = []
        self._ed5_refresh(rebuild)

    def _ed5_undo(self):
        self._ed5_state()
        if self._ed3_acts and self._ed3_acts[-1][0] == "set":
            act = self._ed3_acts.pop()
            self._ed5_apply(act[1], act[2])
            self._ed3_redo.append(act)
            self._ed5_refresh(True)
            return
        self._ed3_state()
        if not self._ed3_acts:
            return
        act_2 = self._ed3_acts.pop()
        if act_2[0] == "add":
            self._ed3_remove(act_2[1])
        elif act_2[0] == "xf":
            act_2[1].clear()
            act_2[1].update(act_2[2])
        elif act_2[0] == "del":
            self.film_edits.insert(min(act_2[2], len(self.film_edits)), act_2[1])
        self._ed3_redo.append(act_2)
        self._after_edit_change()

    def _ed5_redo_do(self):
        self._ed5_state()
        if self._ed3_redo and self._ed3_redo[-1][0] == "set":
            act = self._ed3_redo.pop()
            self._ed5_apply(act[1], act[3])
            self._ed3_acts.append(act)
            self._ed5_refresh(True)
            return
        self._ed3_state()
        if not self._ed3_redo:
            return
        act_3 = self._ed3_redo.pop()
        if act_3[0] == "add":
            self.film_edits.append(act_3[1])
        elif act_3[0] == "xf":
            act_3[1].clear()
            act_3[1].update(act_3[3])
        elif act_3[0] == "del":
            self._ed3_remove(act_3[1])
        self._ed3_acts.append(act_3)
        self._after_edit_change()

    # ---- trim uses the orange bar ----

    def _ed5_trim_here(self, which):
        fps = max(float(self.fps), 1.0)
        t = self.frame_index / fps
        s, e = self._ed_range(self._ed5_trim)
        gap = 0.1
        if which == "start":
            s = max(0.0, min(t, e - gap))
        else:
            e = min(self._ed_dur(), max(t, s + gap))
        self._ed5_set("trim", (s, e))

    # ---- crop tool ----

    def _ed5_draw_crop_2(self):
        self.canvas.delete("ed5crop")
        crop_2 = getattr(self, "_ed5_crop", None)
        if not crop_2:
            return
        s_9 = self.display_scale or 1.0
        x_2, y_2, w_12, h_4 = crop_2
        W_2, H_2 = self.native_w * s_9, self.native_h * s_9
        x0_6, y0_6, x1_6, y1_6 = x_2 * s_9, y_2 * s_9, (x_2 + w_12) * s_9, (y_2 + h_4) * s_9
        tg_2 = ("ed5crop",)
        for r in ((0, 0, W_2, y0_6), (0, y1_6, W_2, H_2), (0, y0_6, x0_6, y1_6), (x1_6, y0_6, W_2, y1_6)):
            self.canvas.create_rectangle(*r, fill="black", stipple="gray50", outline="", tags=tg_2)
        self.canvas.create_rectangle(x0_6, y0_6, x1_6, y1_6, outline="#00e5ff", width=2, tags=tg_2)
        self.canvas.tag_raise("ed3ui")

    # ---- the third edit row and per-tool option rows ----

    def _ed5_toggle_tool(self, tool):
        self._ed2_set_tool("select" if self._ed2_tool == tool else tool)

    def _ed5_opts_trim(self, opts):
        bg = "#222222"
        tv = self._ed5_trim_vals()
        txt = "whole runtime" if tv is None else f"{_ed_fmt(tv[0])} - {_ed_fmt(tv[1])}"
        tk.Label(opts, text="Keep:", fg="white", bg=bg).pack(side="left", padx=(6, 2))
        tk.Label(opts, text=txt, fg="#ff9900", bg=bg).pack(side="left", padx=(0, 8))
        tk.Button(opts, text="Set start here", pady=BUTTON_PADY, command=lambda: self._ed5_trim_here("start")).pack(side="left", padx=2)
        tk.Button(opts, text="Set end here", pady=BUTTON_PADY, command=lambda: self._ed5_trim_here("end")).pack(side="left", padx=2)
        tk.Button(opts, text="Reset", pady=BUTTON_PADY, command=lambda: self._ed5_set("trim", (None, None))).pack(side="left", padx=2)
        tk.Label(opts, text="or drag the orange handles on the scrub bar", fg="#aaaaaa", bg=bg).pack(side="left", padx=8)

    def _ed5_opts_crop(self, opts):
        bg = "#222222"
        c = self._ed5_crop
        txt = "none" if not c else f"{c[2]} x {c[3]} at {c[0]},{c[1]}"
        tk.Label(opts, text="Crop:", fg="white", bg=bg).pack(side="left", padx=(6, 2))
        tk.Label(opts, text=txt, fg="#00e5ff", bg=bg).pack(side="left", padx=(0, 8))
        tk.Button(opts, text="Clear crop", pady=BUTTON_PADY, command=lambda: self._ed5_set("crop", None)).pack(side="left", padx=2)
        tk.Label(opts, text="Output size", fg="white", bg=bg).pack(side="left", padx=(10, 2))
        labels = [l for l, _v in _ED5_SCALES]
        cur = [l for l, v in _ED5_SCALES if v == self._ed5_scale]
        var = tk.StringVar(master=self.root, value=cur[0] if cur else "100%")
        mp = dict(_ED5_SCALES)
        tk.OptionMenu(opts, var, *labels, command=lambda v: self._ed5_set("scale", mp[v])).pack(side="left", padx=2)
        tk.Label(opts, text="drag a rectangle on the video", fg="#aaaaaa", bg=bg).pack(side="left", padx=8)

    def _ed5_aud_commit(self, i, mv, vv):
        try:
            vol = max(0, min(300, int(vv.get()))) / 100.0
        except (tk.TclError, ValueError):
            return
        n = max(i + 1, int(getattr(self, "_aud_tracks", 0) or 0))
        self._ed5_audio_list(n)
        new = self._ed5_get("audio")
        new[i] = {"mute": bool(mv.get()), "vol": vol}
        self._ed5_set("audio", new, rebuild=False)

    def _ed5_opts_audio(self, opts):
        bg = "#222222"
        n = int(getattr(self, "_aud_tracks", 0) or 0)
        if n <= 0:
            tk.Label(opts, text="No audio tracks found (or still checking)", fg="#aaaaaa", bg=bg).pack(side="left", padx=8)
            return
        lst = self._ed5_audio_list(n)
        ck = dict(bg=bg, fg="white", selectcolor="#444444", activebackground=bg, activeforeground="white")
        for i in range(n):
            item = lst[i]
            mv = tk.IntVar(master=self.root, value=int(bool(item["mute"])))
            vv = tk.IntVar(master=self.root, value=int(round(float(item["vol"]) * 100)))
            tk.Label(opts, text=self._p1_track_label(i), fg="white", bg=bg).pack(side="left", padx=(8, 2))
            tk.Checkbutton(opts, text="Mute", variable=mv, command=lambda i=i, mv=mv, vv=vv: self._ed5_aud_commit(i, mv, vv), **ck).pack(side="left")
            sp = tk.Spinbox(opts, from_=0, to=300, increment=5, width=4, textvariable=vv,
                            command=lambda i=i, mv=mv, vv=vv: self._ed5_aud_commit(i, mv, vv))
            sp.pack(side="left")
            sp.bind("<KeyRelease>", lambda e, i=i, mv=mv, vv=vv: self._ed5_aud_commit(i, mv, vv))
            tk.Label(opts, text="%", fg="white", bg=bg).pack(side="left")
        tk.Label(opts, text="applied on Save / Copy", fg="#aaaaaa", bg=bg).pack(side="left", padx=8)

    def _tl_fit(self):
        try:
            c = self.toolbar_container
            self.root.update_idletasks()
            if self._tl_base_h is None:
                self._tl_base_h = int(c.cget("height"))
            need = self.scrub_frame.winfo_reqheight() + self._tl_main_row.winfo_reqheight() + 6
            for r in (self._tl_row, self.save_row):
                if r is not None and r.winfo_manager() and r.winfo_children():
                    need += r.winfo_reqheight()
            for r in getattr(self, "_ed2_rows", [])[1:]:
                try:
                    if r.winfo_exists() and r.winfo_manager():
                        need += r.winfo_reqheight()
                except tk.TclError:
                    pass
            target = max(self._tl_base_h, need)
            delta = target - int(c.cget("height"))
            if delta:
                c.config(height=target)
                self.root.geometry(f"{self.root.winfo_width()}x{self.root.winfo_height() + delta}")
        except tk.TclError:
            pass
        except Exception:
            log.exception("Film viewer: toolbar fit failed (non-fatal)")

    # ---- Cancel restores trim/crop/scale/audio too ----

    # ---- preview playback respects the trim ----

    # ---- re-encode flags and the bake ----

    # ---- film edit: audio edits audible in preview (_ed6_*) ----

    # ---- film Layers panel parity, ghost, new-layer-on-edit, split/merge, restyled range bar (_ed8_*) ----

    def _ed8_state_2(self):
        self._ed2_state()
        if getattr(self, "_ed8_ready", False):
            return
        from . import constants as C
        self.new_layer_on_edit_default = bool(getattr(C, "DEFAULT_NEW_LAYER_ON_EDIT", True))
        self._layer_popup_silenced = bool(getattr(C, "SILENCE_NEW_LAYER_POPUP", False))
        self._layer_confirm_enabled = bool(getattr(C, "CONFIRM_LAYER_CHOICE", True))
        lm = getattr(C, "NEW_LAYER_ON_EDIT_MODE", None)
        if lm is not None:
            lm = str(lm).strip().lower()
            if lm == "ask":
                self.new_layer_on_edit_default = True
                self._layer_popup_silenced = False
                self._layer_confirm_enabled = True
            elif lm == "current":
                self.new_layer_on_edit_default = False
            elif lm == "new":
                self.new_layer_on_edit_default = True
                self._layer_popup_silenced = True
        try:
            g = float(getattr(C, "GHOST_LAYER_OPACITY", 0.3))
        except (TypeError, ValueError):
            g = 0.3
        if g > 1.0:
            g = g / 100.0
        self.ghost_layer_opacity = max(0.0, min(1.0, g))
        self._ed8_split = None
        self._ed8_ready = True

    def _ed8_label(self, layer):
        depth = -1
        for i, l in enumerate(self.film_layers):
            if l is layer:
                depth = i
                break
        count = sum(1 for e in self.film_edits if e.get("layer_id") == layer["id"])
        return f"{depth}: {layer['name']} ({count})"

    def _ed8_refresh_all(self):
        if self.side_panel_mode == "layers":
            self._refresh_side_panel()
        self._ed2_redisplay()
        self._draw_scrub_playhead()

    def _ed8_set_ghost(self, val):
        self._ed8_state()
        self.ghost_layer_opacity = max(0, min(100, int(float(val)))) / 100.0
        if self.film_active_layer_id is not None:
            self._ed2_redisplay()

    def _ed8_move(self, frm, to):
        n = len(self.film_layers)
        if frm == to or not (0 <= frm < n):
            return
        layer = self.film_layers.pop(frm)
        to = max(0, min(n - 1, to))
        self.film_layers.insert(to, layer)
        self._ed8_refresh_all()

    def _ed8_merge(self, source_id, target_id):
        if source_id == target_id:
            return
        src, tgt = self._ed_find(source_id), self._ed_find(target_id)
        if src is None or tgt is None:
            return
        dur = self._ed_dur()
        s1, e1 = self._ed_range(src)
        s2, e2 = self._ed_range(tgt)
        s, e = min(s1, s2), max(e1, e2)
        for el in self.film_edits:
            if el.get("layer_id") == source_id:
                el["layer_id"] = target_id
        self.film_layers = [l for l in self.film_layers if l["id"] != source_id]
        if s <= 0.05 and e >= dur - 0.05:
            tgt["start"] = tgt["end"] = None
        else:
            tgt["start"], tgt["end"] = s, e
        if self.film_active_layer_id == source_id:
            self.film_active_layer_id = target_id
        self._ed3_state()
        self._ed3_acts, self._ed3_redo = [], []
        log.info(f"Film layer '{src['name']}' merged into '{tgt['name']}'")
        self._ed8_refresh_all()

    def _ed8_new_layer_btn(self):
        self._ed8_state()
        layer = self._ed_new_layer("")
        names = {l["name"] for l in self.film_layers}
        n = self._ed_counter
        while f"Layer {n}" in names:
            n += 1
        layer["name"] = f"Layer {n}"
        self.film_layers.append(layer)
        log.info(f"Film layer '{layer['name']}' created from the New Layer button")
        self._ed_select(layer["id"])

    def _ed8_reset_active(self):
        layer = self._ed_find(self.film_active_layer_id) if self.film_active_layer_id else None
        if layer is None:
            return
        self._ed_reset_range(layer)
        self._ed2_redisplay()

    def _ed8_rename(self, layer, name_row, name_btn):
        name_btn.pack_forget()
        var = tk.StringVar(master=self.root, value=layer["name"])
        ent = tk.Entry(name_row, textvariable=var)
        ent.pack(side="left", fill="x", expand=True, padx=2, pady=2)
        ent.focus_set()
        ent.select_range(0, "end")
        done = {"v": False}

        def commit(event=None, cancel=False):
            if done["v"]:
                return
            done["v"] = True
            if not cancel:
                new = var.get().strip()
                if new:
                    layer["name"] = new
            self.root.after_idle(self._ed8_refresh_all)

        ent.bind("<Return>", commit)
        ent.bind("<FocusOut>", commit)
        ent.bind("<Escape>", lambda e: commit(cancel=True))

    def _ed_build_layers_panel(self, inner):
        from .side_panel import small_button, attach_tooltip, style_menu_like_small_button
        self._ed8_state()
        self._ed_range_vars = {}
        bg = "#222222"
        tk.Label(inner, text="Layers", fg="white", bg=bg, font=("", 10, "bold")).pack(side="top", anchor="w", padx=6, pady=(6, 4))
        full_row = tk.Frame(inner, bg=bg)
        full_row.pack(side="top", fill="x", padx=6, pady=(0, 6))
        tk.Button(
            full_row, text="Full", pady=BUTTON_PADY,
            relief=("sunken" if self.film_active_layer_id is None else "raised"),
            command=lambda: self._ed_select(None),
        ).pack(side="left")
        ghost_var = tk.IntVar(master=self.root, value=int(round(self.ghost_layer_opacity * 100)))
        tk.Label(full_row, textvariable=ghost_var, fg="white", bg=bg, width=3, anchor="w").pack(side="right")
        tk.Scale(
            full_row, from_=0, to=100, orient="horizontal", length=50, showvalue=False,
            bg=bg, fg="white", troughcolor="#444444", highlightthickness=0,
            variable=ghost_var, command=self._ed8_set_ghost,
        ).pack(side="right")
        tk.Label(full_row, text="Ghost:", fg="#aaaaaa", bg=bg).pack(side="right", padx=(6, 2))

        from .side_panel import RowReorder
        n_layers = len(self.film_layers)
        reorder = RowReorder(inner, lambda key, to_display, n=n_layers: self._ed8_move(key, (n - 1) - to_display))
        for index in range(n_layers - 1, -1, -1):
            layer = self.film_layers[index]
            active = layer["id"] == self.film_active_layer_id
            cbg = "#333333" if active else bg
            cell = tk.Frame(inner, bg=cbg, highlightthickness=1, highlightbackground="#555555")
            cell.pack(side="top", fill="x", padx=4, pady=2)
            name_row = tk.Frame(cell, bg=cbg)
            name_row.pack(side="top", fill="x")
            handle = tk.Label(name_row, text="\u2261", fg="#888888", bg=cbg, width=2, cursor="fleur")
            handle.pack(side="left", padx=(2, 0), pady=2)
            name_btn = tk.Button(
                name_row, text=self._ed8_label(layer), pady=BUTTON_PADY, anchor="w",
                relief=("sunken" if active else "raised"),
                command=lambda lid=layer["id"]: self._ed_select(lid),
            )
            small_button(name_row, "\u270e", lambda l=layer, r=name_row, b=name_btn: self._ed8_rename(l, r, b)).pack(side="right", padx=(0, 2), pady=2)
            name_btn.pack(side="left", fill="x", expand=True, padx=2, pady=2)
            attach_tooltip(name_btn, self._ed8_label(layer))
            reorder.add(index, cell, handle, self._ed8_label(layer))

            handle.update_idletasks()
            indent = handle.winfo_reqwidth() + 4
            var = tk.StringVar(master=self.root, value=self._ed_range_text(layer))
            self._ed_range_vars[layer["id"]] = var
            tk.Label(cell, textvariable=var, fg="#ff9900", bg=cbg, anchor="w").pack(side="top", fill="x", padx=(indent, 2))
            controls_row = tk.Frame(cell, bg=cbg)
            controls_row.pack(side="top", fill="x", padx=(indent, 2), pady=(0, 2))
            if active:
                small_button(controls_row, "Split", self._ed8_enter_split, width=0).pack(side="left", fill="x", expand=True, padx=(0, 2))
            others = [l for l in self.film_layers if l["id"] != layer["id"]]
            if others:
                merge_var = tk.StringVar(master=self.root, value="Merge\u2026")
                choices = {l["name"]: l["id"] for l in others}
                dropdown = tk.OptionMenu(
                    controls_row, merge_var, *choices.keys(),
                    command=lambda name, sid=layer["id"], choices=choices: self._ed8_merge(sid, choices[name]),
                )
                style_menu_like_small_button(dropdown)
                dropdown.pack(side="left", fill="x", expand=True, padx=(0, 2))

        tk.Label(inner, text="(drag \u2261 to reorder \u00b7 \u270e to rename)", fg="#aaaaaa", bg=bg, justify="left").pack(side="top", anchor="w", padx=6, pady=(6, 6))
        tk.Frame(inner, height=1, bg="#444444").pack(side="top", fill="x", padx=6, pady=(0, 6))
        tk.Button(inner, text="+ New Layer", pady=BUTTON_PADY, command=self._ed8_new_layer_btn).pack(side="top", fill="x", padx=6, pady=(0, 6))
        nl_var = tk.IntVar(master=self.root, value=int(self.new_layer_on_edit_default))
        nlrow = tk.Frame(inner, bg=bg)
        nlrow.pack(side="top", fill="x", padx=6, pady=(0, 8))
        tk.Checkbutton(
            nlrow, text="New layer on edit", variable=nl_var,
            command=lambda: setattr(self, "new_layer_on_edit_default", bool(nl_var.get())),
            bg=bg, fg="white", selectcolor="#444444", activebackground=bg, activeforeground="white",
        ).pack(side="left")
        rb = small_button(nlrow, "\u21ba", self._ed8_reset_active)
        rb.pack(side="left", padx=(8, 0))
        attach_tooltip(rb, "Reset the selected layer's time range to the whole runtime")
        rb._tooltip_force = True

    # ---- where new elements land: same rules as the image side ----

    def _ed2_target_layer(self, tool):
        self._ed8_state()
        from . import constants as C
        natural = getattr(C, "LAYER_MODE_GROUPS", {}).get(tool, "Draw")
        layer = self._ed_find(self.film_active_layer_id) if self.film_active_layer_id else None
        if layer is not None:
            join = True
            if layer.get("group_key") != natural:
                if not self.new_layer_on_edit_default:
                    join = True
                elif self._layer_popup_silenced or not self._layer_confirm_enabled:
                    join = False
                else:
                    from .selector_app import SelectorApp
                    join = SelectorApp._ask_new_layer_or_current(self, tool, layer) == "current"
            if join:
                return layer["id"]
        for l in self.film_layers:
            if l.get("group_key") == natural:
                return l["id"]
        l = self._ed_new_layer(natural)
        l["group_key"] = natural
        self.film_layers.append(l)
        log.info(f"Film layer '{natural}' created (first {tool} element)")
        return l["id"]

    # ---- selecting a layer re-dims the preview ----

    def _ed_select(self, lid):
        self._ed_state()
        self.film_active_layer_id = lid
        log.info(f"Film layer selected: {lid}")
        self._refresh_side_panel()
        self._draw_scrub_playhead()
        self._ed2_redisplay()

    # ---- split ----

    def _ed8_next_split_name(self, base):
        root = base
        parts = base.rsplit(" ", 1)
        if len(parts) == 2 and parts[1].isdigit():
            root = parts[0]
        names = {l["name"] for l in self.film_layers}
        n = 1
        while f"{root} {n}" in names:
            n += 1
        return f"{root} {n}"

    def _ed8_enter_split(self):
        self._ed8_state()
        layer = self._ed_find(self.film_active_layer_id) if self.film_active_layer_id else None
        if layer is None:
            notify("Can't split", "Select a layer first (pick one from the Layers panel), then Split.", blocking=False)
            return
        if not self._ed2_active:
            self._ed2_enter()
        self._ed8_split = {"lid": layer["id"], "start": None, "scr": None}
        log.info(f"Film split mode entered for layer '{layer['name']}'")
        notify("Split", "Drag a rectangle over the elements to split out", blocking=False)

    def _ed8_finish_split(self, rect):
        sp = self._ed8_split
        self._ed8_split = None
        layer = self._ed_find(sp["lid"])
        if layer is None:
            return
        x1, y1, x2, y2 = rect
        caught = []
        for el in self.film_edits:
            if el.get("layer_id") != layer["id"]:
                continue
            ox, oy = el["origin"]
            bx2, by2 = ox + el["w"], oy + el["h"]
            if not (bx2 < x1 or ox > x2 or by2 < y1 or oy > y2):
                caught.append(el)
        if not caught:
            notify("Nothing to split", "No elements of this layer were inside the selection.", blocking=False)
            return
        new = self._ed_new_layer(self._ed8_next_split_name(layer["name"]))
        new["start"], new["end"] = layer.get("start"), layer.get("end")
        self.film_layers.append(new)
        for el in caught:
            el["layer_id"] = new["id"]
        self.film_active_layer_id = new["id"]
        self._ed3_state()
        self._ed3_acts, self._ed3_redo = [], []
        log.info(f"Film split: {len(caught)} element(s) moved from '{layer['name']}' into '{new['name']}'")
        self._ed8_refresh_all()

    # ---- preview compose with ghosting ----

    def _ed2_compose(self, img):
        from PIL import Image
        self._ed8_state()
        if not self.film_edits:
            return img
        fps = max(float(self.fps), 1.0)
        t = self.frame_index / fps
        tol = 0.5 / fps
        scale = img.width / max(1, self.native_w)
        order = {l["id"]: i for i, l in enumerate(self.film_layers)}
        act = self.film_active_layer_id
        ghost = self.ghost_layer_opacity
        out = None
        for el in sorted(self.film_edits, key=lambda e: order.get(e.get("layer_id"), 0)):
            layer = self._ed_find(el.get("layer_id"))
            if layer is None:
                continue
            s, e = self._ed_range(layer)
            if t < s - tol or t > e + tol:
                continue
            faded = act is not None and el.get("layer_id") != act
            if faded and ghost <= 0.0:
                continue
            patch = self._ed2_patch(el, scale)
            if faded and ghost < 1.0:
                patch = patch.copy()
                patch.putalpha(patch.getchannel("A").point(lambda v, g=ghost: int(v * g)))
            rot = el.get("rotation", 0.0)
            if abs(rot) > 0.01:
                patch = patch.rotate(-rot, expand=True, resample=Image.BICUBIC)
            cx = (el["origin"][0] + el["w"] / 2) * scale
            cy = (el["origin"][1] + el["h"] / 2) * scale
            if out is None:
                # patch 43: paste straight onto an RGB copy (same pixels as the RGBA round trip, ~2x faster)
                out = img.copy() if img.mode == "RGB" else img.convert("RGBA")
            out.paste(patch, (int(round(cx - patch.width / 2)), int(round(cy - patch.height / 2))), patch)
        if out is None:
            return img
        return out if out.mode == "RGB" else out.convert("RGB")

    # ---- restyled range bar ----

    def _ed8_draw(self):
        self._draw_scrub_playhead_base()
        try:
            layer = self._ed_active()
            cv_3 = self.scrub_canvas
            if layer is None or cv_3.winfo_width() <= 1 or not self.total_frames:
                return
            oval = None
            for item in cv_3.find_withtag("scrub"):
                kind = cv_3.type(item)
                if kind == "rectangle":
                    fill = cv_3.itemcget(item, "fill")
                    if fill == "#444444":
                        cv_3.itemconfigure(item, fill="#2e2e2e")
                    elif fill == "#00b4ff":
                        cv_3.itemconfigure(item, fill="#07587a")
                elif kind == "oval":
                    oval = item
            s_5, e_2 = self._ed_range(layer)
            xs, xe = self._ed_x(s_5), self._ed_x(e_2)
            mid_4 = max(10, cv_3.winfo_height() // 2)
            w = cv_3.winfo_width()
            tags = ("scrub", "edrange")
            cv_3.create_rectangle(xs, mid_4 - 4, xe, mid_4 + 4, fill="#ff9900", outline="", tags=tags)
            drag = getattr(self, "_ed_drag", None)
            for which, x in (("start", xs), ("end", xe)):
                _ed8_handle(cv_3, x, mid_4, drag == which, tags)
            if xs < 50:
                sx, sa = xs + 10, "w"
            else:
                sx, sa = xs - 10, "e"
            if xe > w - 50:
                ex, ea = xe - 10, "e"
            else:
                ex, ea = xe + 10, "w"
            for x, anchor, tt in ((sx, sa, s_5), (ex, ea, e_2)):
                txt = cv_3.create_text(x, mid_4, text=_ed_fmt(tt), anchor=anchor, fill="white", font=("", 8, "bold"), tags=tags)
                box = cv_3.bbox(txt)
                if box:
                    bgr = cv_3.create_rectangle(box[0] - 3, box[1] - 1, box[2] + 3, box[3] + 1, fill="#111111", outline="#00b4ff", tags=tags)
                    cv_3.tag_lower(bgr, txt)
            if oval is not None:
                cv_3.tag_raise(oval)
        except tk.TclError:
            pass

    # ---- film edit UI rebuild: two-row toolbar, Mode dropdowns, crop handles, in-canvas Text (_ed9_*) ----

    # Toolbar builders shared with the image window. selector_app is imported lazily
    # (it is the bigger module and is not needed to open a film window).
    def _pack_dropdown(self, *args, **kwargs):
        from .selector_app import SelectorApp
        return SelectorApp._pack_dropdown(self, *args, **kwargs)

    def _pack_color_swatches(self, *args, **kwargs):
        from .selector_app import SelectorApp
        return SelectorApp._pack_color_swatches(self, *args, **kwargs)

    def _pack_labeled_slider(self, *args, **kwargs):
        from .selector_app import SelectorApp
        return SelectorApp._pack_labeled_slider(self, *args, **kwargs)

    def _pack_fill_dropdown(self, *args, **kwargs):
        from .selector_app import SelectorApp
        return SelectorApp._pack_fill_dropdown(self, *args, **kwargs)

    def _build_fill_swatch(self, *args, **kwargs):
        from .selector_app import SelectorApp
        return SelectorApp._build_fill_swatch(self, *args, **kwargs)

    @property
    def _FILL_SWATCH_COLORS(self):
        from .selector_app import SelectorApp
        return SelectorApp._FILL_SWATCH_COLORS

    def _ed8_state(self):
        was = getattr(self, "_ed8_ready", False)
        self._ed8_state_2()
        if not was:
            from . import constants as C
            if not hasattr(C, "GHOST_LAYER_OPACITY"):
                self.ghost_layer_opacity = 0.15

    def _ed9_state_2(self):
        self._ed3_state()
        self._ed5_state()
        if getattr(self, "_ed9_ready", False):
            return
        from . import constants as C
        self._ed9_ready = True
        self._ed9_g = None
        self._ed9_cd = None
        self._ed9_tw = None
        self._ed9_tw_box = None
        self._ed9_tw_edit = None
        self._ed9_tick_job = None
        self._ed9_tp_sig = None
        self._ed9_photo = None
        pm_2 = str(getattr(C, "DEFAULT_PEN_MODE", "pen"))
        self._ed9_pen_mode = pm_2 if pm_2 in ("select", "pen", "highlight") else "pen"
        sm = str(getattr(C, "DEFAULT_SHAPE_MODE", "box"))
        self._ed9_shape_mode = sm if sm in ("select", "line", "box", "ellipse") else "box"
        tm = str(getattr(C, "DEFAULT_TEXT_MODE", "write"))
        self._ed9_text_mode = tm if tm in ("select", "write") else "write"
        self._ed9_hl_width = int(getattr(C, "EDIT_HIGHLIGHT_WIDTH", 22))
        self._ed9_font = getattr(C, "TEXT_FONT_FAMILY_DEFAULT", "Serif")
        self._ed9_size = int(getattr(C, "TEXT_FONT_SIZE_DEFAULT", 24))
        self._ed9_fill_t = "transparent"
        self._ed9_align = "left"

    def _ed9_editable(self, el):
        if not self._ed3_visible(el):
            return False
        a = self.film_active_layer_id
        return a is None or el.get("layer_id") == a

    def _ed9_grab_ok(self, tool):
        if tool in ("snip", "shape", "text"):
            return True
        if tool == "pen":
            return self._ed9_pen_mode == "select"
        return False

    # ---- overlays ----

    def _ed9_dim(self, cx, cy, text):
        cv = self.canvas
        cv.delete("ed9dim")
        t = cv.create_text(cx, cy - 14, text=text, fill="white", font=("", 10, "bold"), anchor="s", tags=("ed9dim",))
        bb = cv.bbox(t)
        if bb:
            r = cv.create_rectangle(bb[0] - 4, bb[1] - 4, bb[2] + 4, bb[3] + 4, fill="#111111", outline="#00b4ff", tags=("ed9dim",))
            cv.tag_lower(r, t)
        cv.tag_raise("ed9dim")

    def _ed9_clear_dim(self):
        try:
            self.canvas.delete("ed9dim")
        except tk.TclError:
            pass

    def _ed9_clear_g(self):
        self._ed9_g = None
        self._ed9_cd = None
        self._ed2_clear_tmp()
        self._ed9_clear_dim()

    def _ed3_draw_ui(self):
        cv = self.canvas
        try:
            cv.delete("ed3ui")
        except tk.TclError:
            return
        if not self._ed2_active or self._ed9_tw is not None:
            return
        tool = self._ed2_tool
        if tool not in ("snip", "pen", "text", "shape") or not self._ed9_grab_ok(tool):
            return
        s = self.display_scale or 1.0
        hs = int(getattr(_fs_consts, "HANDLE_SIZE", 8))
        tg = ("ed3ui",)
        for el in self._ed9_cands(tool):
            pos = self._ed3_handles(el)
            pts = []
            for n in ("nw", "ne", "se", "sw"):
                pts += [pos[n][0] * s, pos[n][1] * s]
            col = "#33ff33" if el.get("eid") == self._ed3_sel else "#00b4ff"
            cv.create_polygon(pts, outline=col, fill="", width=2, dash=(5, 3), tags=tg)
            tmx = (pos["nw"][0] + pos["ne"][0]) / 2 * s
            tmy = (pos["nw"][1] + pos["ne"][1]) / 2 * s
            cv.create_line(tmx, tmy, pos["rotate"][0] * s, pos["rotate"][1] * s, fill="#33cc33", tags=tg)
            for name, (px, py) in pos.items():
                c = "#33cc33" if name == "rotate" else "#ffaa00"
                cv.create_oval(px * s - hs, py * s - hs, px * s + hs, py * s + hs, fill=c, outline="white", tags=tg)
        cv.tag_raise("ed3ui")

    def _ed9_crop_rect(self):
        c = self._ed5_crop
        if c:
            return (c[0], c[1], c[0] + c[2], c[1] + c[3])
        return (0, 0, self.native_w, self.native_h)

    def _ed9_crop_corners(self):
        x1, y1, x2, y2 = self._ed9_crop_rect()
        s = self.display_scale or 1.0
        return {"nw": (x1 * s, y1 * s), "ne": (x2 * s, y1 * s), "sw": (x1 * s, y2 * s), "se": (x2 * s, y2 * s)}

    def _ed9_draw_crop_handles(self):
        cv = self.canvas
        try:
            cv.delete("ed9crop")
        except tk.TclError:
            return
        if not self._ed9_crop_on():
            return
        hs = int(getattr(_fs_consts, "HANDLE_SIZE", 8))
        for _n, (hx, hy) in self._ed9_crop_corners().items():
            cv.create_rectangle(hx - hs, hy - hs, hx + hs, hy + hs, fill="#00b4ff", outline="white", tags=("ed9crop",))
        cv.tag_raise("ed9crop")

    def _ed9_hit_crop(self, cx, cy):
        if not self._ed9_crop_on():
            return None
        hs = int(getattr(_fs_consts, "HANDLE_SIZE", 8))
        for name, (hx, hy) in self._ed9_crop_corners().items():
            if abs(cx - hx) <= hs * 1.5 and abs(cy - hy) <= hs * 1.5:
                return name
        return None

    # ---- selecting / transforming ----

    def _ed9_try_grab(self, event, tool):
        x, y, cx, cy = self._ed3_pt(event)
        cands = list(reversed(self._ed9_cands(tool)))
        for el in cands:
            h = self._ed3_hit_handle(el, cx, cy)
            if h:
                self._ed3_sel = el["eid"]
                self._ed3_begin(el, h, x, y)
                return True
        for el in cands:
            if self._ed3_hit(el, x, y):
                self._ed3_sel = el["eid"]
                self._ed3_begin(el, "move", x, y)
                self._ed3_draw_ui()
                return True
        return False

    def _ed9_xf_move(self, event):
        d = self._ed3_drag
        x, y, cx, cy = self._ed3_pt(event)
        el, mode = d["el"], d["mode"]
        sx, sy = d["start"]
        if mode == "move":
            el["origin"] = (d["o"][0] + (x - sx), d["o"][1] + (y - sy))
        elif mode == "rotate":
            mx = d["o"][0] + d["w"] / 2
            my = d["o"][1] + d["h"] / 2
            a0 = _ed3_math.degrees(_ed3_math.atan2(sy - my, sx - mx))
            a1 = _ed3_math.degrees(_ed3_math.atan2(y - my, x - mx))
            el["rotation"] = d["rot"] + (a1 - a0)
        else:
            aw, ah = _ED3_AXES.get(mode, (True, True))
            w0, h0 = d["w"], d["h"]
            mx, my = d["o"][0] + w0 / 2, d["o"][1] + h0 / 2
            th = _ed3_math.radians(d["rot"])
            c, s = _ed3_math.cos(th), _ed3_math.sin(th)
            ldx = (x - mx) * c + (y - my) * s
            ldy = -(x - mx) * s + (y - my) * c
            if mode in _ED3_CORNERS and self._ed3_prop.get():
                half = _ed3_math.hypot(w0 / 2, h0 / 2)
                f = _ed3_math.hypot(ldx, ldy) / half if half > 0 else 1.0
                f = max(f, 4 / max(1e-6, min(w0, h0)))
                nw, nh = w0 * f, h0 * f
                ncx, ncy = mx, my
            else:
                from .resize_math import anchored_resize
                nw, nh, ncx, ncy = anchored_resize(mode, w0, h0, mx, my, c, s, ldx, ldy, aw, ah)
            el["w"], el["h"] = nw, nh
            el["origin"] = (ncx - nw / 2, ncy - nh / 2)
        self._ed2_redisplay()
        if mode == "rotate":
            self._ed9_dim(cx, cy, f"{el.get('rotation', 0.0):.0f} deg")
        else:
            self._ed9_dim(cx, cy, f"{round(el['w'])} x {round(el['h'])}")

    def _ed9_xf_release(self):
        d = self._ed3_drag
        self._ed3_drag = None
        self._ed9_clear_dim()
        if not d:
            return
        el = d["el"]
        same = (tuple(el["origin"]) == d["o"] and el["w"] == d["w"] and el["h"] == d["h"]
                and el.get("rotation", 0.0) == d["rot"])
        if same:
            el.clear()
            el.update(d["before"])
        else:
            self._ed3_acts.append(("xf", el, d["before"], dict(el)))
            self._ed3_redo = []
        self._ed2_redisplay()

    def _ed9_crop_move_2(self, event):
        cd = self._ed9_cd
        nx, ny, cx, cy = self._ed2_xy(event)
        fx, fy = cd["fixed"]
        x1o, y1o, x2o, y2o = cd["r0"]
        if self._ed3_prop.get():
            w0, h0 = max(1.0, x2o - x1o), max(1.0, y2o - y1o)
            sx = 1 if cd["name"] in ("ne", "se") else -1
            sy = 1 if cd["name"] in ("sw", "se") else -1
            f = max(abs(nx - fx) / w0, abs(ny - fy) / h0, 16.0 / min(w0, h0))
            maxw = (self.native_w - fx) if sx > 0 else fx
            maxh = (self.native_h - fy) if sy > 0 else fy
            f = min(f, maxw / w0, maxh / h0)
            nx, ny = fx + sx * w0 * f, fy + sy * h0 * f
        xa, xb = sorted((fx, nx))
        ya, yb = sorted((fy, ny))
        w = int(xb - xa) // 2 * 2
        h = int(yb - ya) // 2 * 2
        if w < 16 or h < 16:
            return
        self._ed5_crop = (int(xa), int(ya), w, h)
        self._ed5_draw_crop()
        self._ed9_draw_crop_handles()
        self._ed9_dim(cx, cy, f"{w} x {h}")

    def _ed9_crop_release(self):
        cd = self._ed9_cd
        self._ed9_cd = None
        self._ed9_clear_dim()
        new = self._ed5_crop
        self._ed5_crop = cd["orig"]
        if new is not None and new[0] <= 0 and new[1] <= 0 and new[2] >= self.native_w - 2 and new[3] >= self.native_h - 2:
            new = None
        self._ed5_set("crop", new)
        self._ed5_draw_crop()
        self._ed9_draw_crop_handles()

    # ---- canvas handlers ----

    # ---- in-canvas Text ----

    def _ed9_text_el(self, content, box):
        x1, y1, x2, y2 = box
        return {
            "mode": "text", "text": content, "color": self._ed2_color, "font_family": self._ed9_font,
            "font_size": int(self._ed9_size), "text_align": self._ed9_align, "bg_fill": self._ed9_fill_t,
            "origin": (x1, y1), "w": x2 - x1, "h": y2 - y1,
        }

    def _ed9_tp_tick(self):
        if self._ed9_tw is None:
            return
        try:
            self._ed9_tp_render()
        except Exception:
            log.exception("Film text: preview render failed (non-fatal)")
        self._ed9_tick_job = self.canvas.after(120, self._ed9_tp_tick)

    def _ed9_tp_render_2(self):
        from PIL import Image, ImageTk
        from .selector_app import SelectorApp as SA
        tw = self._ed9_tw
        if tw is None:
            return
        x1, y1, x2, y2 = self._ed9_tw_box
        w = max(1, int(round(x2 - x1)))
        h = max(1, int(round(y2 - y1)))
        s = self.display_scale or 1.0
        content = tw.get("1.0", "end-1c")
        ins = len(tw.get("1.0", "insert"))
        phase = int(time.monotonic() * 2) % 2
        sig = (content, ins, phase, self._ed9_font, self._ed9_size, self._ed2_color, self._ed9_align, self._ed9_fill_t, (w, h), round(s, 4))
        if sig == self._ed9_tp_sig:
            return
        self._ed9_tp_sig = sig
        st = self._ed9_text_el(content, (x1, y1, x1 + w, y1 + h))
        patch = SA._render_element_local_patch(None, st, w, h).convert("RGBA")
        before = content[:ins]
        st2 = dict(st)
        st2["bg_fill"] = "transparent"
        st2["runs"] = [{"text": before, "color": None, "font": None, "size": None},
                       {"text": "|", "color": "#fe0001", "font": None, "size": None}]
        bb = None
        try:
            bb = _ed9_marker_bbox(SA._render_element_local_patch(None, st2, w, h).convert("RGBA"))
        except Exception:
            bb = None
        pw, ph = max(1, int(w * s)), max(1, int(h * s))
        shown = patch.resize((pw, ph), Image.LANCZOS) if (pw, ph) != patch.size else patch
        self._ed9_photo = ImageTk.PhotoImage(shown, master=self.canvas)
        cv = self.canvas
        cv.delete("ed9tp")
        cx1, cy1 = x1 * s, y1 * s
        cv.create_image(cx1, cy1, anchor="nw", image=self._ed9_photo, tags=("ed9tp",))
        cv.create_rectangle(cx1, cy1, cx1 + pw, cy1 + ph, outline="#00b4ff", width=2, dash=(5, 3), tags=("ed9tp",))
        if phase == 0:
            if bb:
                lx = cx1 + (bb[0] + bb[2]) / 2.0 * s
                cv.create_line(lx, cy1 + bb[1] * s, lx, cy1 + bb[3] * s, fill=self._ed2_color, width=2, tags=("ed9tp",))
            else:
                cv.create_line(cx1 + 4 * s, cy1 + 2 * s, cx1 + 4 * s, cy1 + (2 + self._ed9_size) * s, fill=self._ed2_color, width=2, tags=("ed9tp",))
        cv.tag_raise("ed9tp")

    def _ed9_tw_teardown(self):
        tw = self._ed9_tw
        self._ed9_tw = None
        self._ed9_tw_box = None
        self._ed9_tw_edit = None
        if self._ed9_tick_job is not None:
            try:
                self.canvas.after_cancel(self._ed9_tick_job)
            except Exception:
                pass
            self._ed9_tick_job = None
        try:
            self.canvas.delete("ed9tp")
        except tk.TclError:
            pass
        self._ed9_photo = None
        if tw is not None:
            try:
                tw.destroy()
            except tk.TclError:
                pass

    def _ed9_text_confirm(self):
        tw = self._ed9_tw
        if tw is None:
            return
        content = tw.get("1.0", "end-1c")
        box = self._ed9_tw_box
        edit = self._ed9_tw_edit
        self._ed9_tw_teardown()
        if edit is not None:
            el, idx, before = edit
            if not content.strip():
                self._ed3_acts.append(("del", el, idx))
                self._ed3_redo = []
            else:
                el.update(self._ed9_text_el(content, box))
                el.pop("runs", None)
                self.film_edits.insert(min(idx, len(self.film_edits)), el)
                self._ed3_acts.append(("xf", el, before, dict(el)))
                self._ed3_redo = []
        elif content.strip():
            self._ed2_add(self._ed9_text_el(content, box), "text")
        try:
            self.canvas.focus_set()
        except tk.TclError:
            pass
        self._after_edit_change()

    def _ed9_text_cancel(self):
        if self._ed9_tw is None:
            return
        edit = self._ed9_tw_edit
        self._ed9_tw_teardown()
        if edit is not None:
            el, idx, _before = edit
            self.film_edits.insert(min(idx, len(self.film_edits)), el)
        try:
            self.canvas.focus_set()
        except tk.TclError:
            pass
        self._after_edit_change()

    def _ed9_dbl(self, event):
        self._ed9_state()
        if not self._ed2_active or self._ed2_tool != "text" or self._ed9_text_mode != "select" or self._ed9_tw is not None:
            return
        x, y, cx, cy = self._ed3_pt(event)
        for el in reversed(self._ed9_cands("text")):
            if self._ed3_hit(el, x, y):
                idx = 0
                for i, e in enumerate(self.film_edits):
                    if e is el:
                        idx = i
                        break
                before = dict(el)
                self.film_edits.pop(idx)
                self._ed2_color = el.get("color", self._ed2_color)
                self._ed9_font = el.get("font_family", self._ed9_font)
                self._ed9_size = int(el.get("font_size", self._ed9_size))
                self._ed9_fill_t = el.get("bg_fill") or "transparent"
                self._ed9_align = el.get("text_align", "left")
                self._ed3_sel = None
                self._ed2_build_rows()
                self._ed2_redisplay()
                self._ed9_text_begin((el["origin"][0], el["origin"][1], el["origin"][0] + el["w"], el["origin"][1] + el["h"]), edit=(el, idx, before))
                return

    def _ed9_text_apply(self):
        tw = self._ed9_tw
        if tw is None:
            return
        from . import constants as C
        s = self.display_scale or 1.0
        try:
            tkf = C.TEXT_FONT_FAMILIES.get(self._ed9_font, {}).get("tk", "serif")
            tw.config(font=(tkf, max(10, int(self._ed9_size * 0.6 * s))), fg=self._ed2_color, insertbackground=self._ed2_color)
        except tk.TclError:
            pass
        self._ed9_tp_sig = None
        self.canvas.after(60, lambda t=tw: t.focus_set() if self._ed9_tw is t else None)

    # ---- tool / mode / option setters ----

    def _ed9_pick_color(self, c):
        self._ed2_color = c
        self._ed9_text_apply()
        self._ed2_build_rows()

    def _ed9_set_fill(self, c):
        self._ed2_fill = c
        self._ed2_build_rows()

    def _ed9_set_font(self, v):
        self._ed9_font = v
        self._ed9_text_apply()
        self._ed2_build_rows()

    def _ed9_set_size(self, val):
        try:
            self._ed9_size = max(6, min(400, int(float(val))))
        except (TypeError, ValueError):
            return
        self._ed9_text_apply()

    def _ed9_set_text_fill(self, v):
        self._ed9_fill_t = v
        self._ed9_tp_sig = None
        self._ed2_build_rows()
        self._ed9_text_apply()

    def _ed9_set_align(self, v):
        self._ed9_align = v
        self._ed9_tp_sig = None
        self._ed2_build_rows()
        self._ed9_text_apply()

    # ---- option rows ----

    def _ed9_opts_shape(self, o):
        from . import constants as C
        pal = list(getattr(C, "PALETTE", ["#FF0000"]))
        fpal = list(getattr(C, "FILL_PALETTE", pal + ["#FFFFFF"]))
        opts = [("select", "Select"), ("line", "Line"), ("box", "Box"), ("ellipse", "Ellipse")]
        self._pack_dropdown(o, "Mode", opts, self._ed9_shape_mode, lambda v: self._ed9_set_mode("shape", v), width=9)
        tk.Label(o, text="Border", fg="white", bg="#222222").pack(side="left", padx=(8, 2))
        self._pack_color_swatches(o, pal, self._ed2_color, self._ed9_pick_color)
        tk.Frame(o, width=8, bg="#222222").pack(side="left")
        var = tk.IntVar(master=self.root, value=int(self._ed2_width))
        self._pack_labeled_slider(o, "Width", var, 1, 30, lambda v: setattr(self, "_ed2_width", int(float(v))))
        tk.Label(o, text="Fill", fg="white", bg="#222222").pack(side="left", padx=2)
        self._pack_color_swatches(o, fpal, self._ed2_fill, self._ed9_set_fill,
                                  none_button=("None", self._ed2_fill is None, lambda: self._ed9_set_fill(None)))

    def _ed9_opts_text(self, o):
        from . import constants as C
        bg = "#222222"
        self._pack_dropdown(o, "Mode", [("select", "Select"), ("write", "Write")], self._ed9_text_mode,
                            lambda v: self._ed9_set_mode("text", v), width=7)
        self._pack_color_swatches(o, list(getattr(C, "PALETTE", ["#FF0000"])), self._ed2_color, self._ed9_pick_color)
        fills = [("transparent", "None"), ("white", "White"), ("black", "Black"),
                 ("white_border", "White/Black"), ("black_border", "Black/White")]
        self._pack_fill_dropdown(o, "Fill", fills, self._ed9_fill_t, self._ed9_set_text_fill)
        fams = list(C.TEXT_FONT_FAMILIES.keys())
        fm = self._pack_dropdown(o, "Font", [(n, n) for n in fams], self._ed9_font, self._ed9_set_font, width=7)
        try:
            inner = fm["menu"]
            for i, n in enumerate(fams):
                inner.entryconfig(i, font=(C.TEXT_FONT_FAMILIES[n]["tk"], 11))
        except tk.TclError:
            pass
        tk.Label(o, text="Size", fg="white", bg=bg).pack(side="left", padx=(8, 2))
        size_var = tk.StringVar(master=self.root, value=str(self._ed9_size))

        def commit(*_a):
            self._ed9_set_size(size_var.get())
            size_var.set(str(self._ed9_size))
            return "break"

        def step(d):
            try:
                cur = int(float(size_var.get()))
            except ValueError:
                cur = self._ed9_size
            size_var.set(str(max(6, min(400, cur + d))))
            commit()

        p1 = tk.Button(o, text="X", pady=BUTTON_PADY)
        p1.pack()
        p1.update_idletasks()
        sh = p1.winfo_reqheight()
        p1.destroy()
        p2 = tk.Button(o, text="\u25b2", width=2, pady=0)
        p2.pack()
        p2.update_idletasks()
        sw = max(12, p2.winfo_reqwidth() // 3 + 3)
        p2.destroy()
        ef = tk.Frame(o, width=32, height=sh, bg=bg)
        ef.pack_propagate(False)
        ef.pack(side="left", padx=(2, 0), pady=4)
        ent = tk.Entry(ef, textvariable=size_var, justify="center")
        ent.pack(fill="both", expand=True)
        ent.bind("<Return>", commit)
        ent.bind("<FocusOut>", commit)
        ent.bind("<Up>", lambda e: step(2))
        ent.bind("<Down>", lambda e: step(-2))
        sf = tk.Frame(o, width=sw, height=sh, bg=bg)
        sf.pack_propagate(False)
        sf.pack(side="left", padx=(1, 2), pady=4)
        uh = sh // 2
        uf = tk.Frame(sf, height=uh, bg=bg)
        uf.pack_propagate(False)
        uf.pack(side="top", fill="both")
        df = tk.Frame(sf, height=sh - uh, bg=bg)
        df.pack_propagate(False)
        df.pack(side="top", fill="both")
        tk.Button(uf, text="\u25b2", padx=0, pady=0, command=lambda: step(2)).pack(fill="both", expand=True)
        tk.Button(df, text="\u25bc", padx=0, pady=0, command=lambda: step(-2)).pack(fill="both", expand=True)

        self._pack_dropdown(o, "Align", [("left", "Left"), ("center", "Center"), ("right", "Right")],
                            self._ed9_align, self._ed9_set_align, width=6)

    def _ed9_opts_resize_2(self, o):
        bg = "#222222"
        self._pack_dropdown(o, "Output size", [(v, l) for l, v in _ED5_SCALES], self._ed5_scale,
                            lambda v: self._ed5_set("scale", v), width=14)
        ow, oh = self._ed9_out_dims()
        tk.Label(o, text=f"Output: {ow} x {oh}", fg="#00e5ff", bg=bg).pack(side="left", padx=8)
        if self._ed5_crop:
            c = self._ed5_crop
            tk.Label(o, text=f"Crop: {c[2]} x {c[3]}", fg="#00e5ff", bg=bg).pack(side="left", padx=4)
            tk.Button(o, text="Clear crop", pady=BUTTON_PADY, command=lambda: self._ed5_set("crop", None)).pack(side="left", padx=4, pady=4)
        tk.Label(o, text="baked into the saved film; the window zoom stays view-only", fg="#aaaaaa", bg=bg).pack(side="left", padx=8)

    def _ed2_bind_canvas(self, on):
        seqs = (("<ButtonPress-1>", self._ed2_press), ("<B1-Motion>", self._ed2_drag_move), ("<ButtonRelease-1>", self._ed2_release))
        if on:
            if not self._ed2_saved_binds:
                for seq, _f in seqs:
                    self._ed2_saved_binds[seq] = self.canvas.bind(seq)
            for seq, f in seqs:
                self.canvas.bind(seq, f)
            try:
                self.canvas.configure(cursor="crosshair")
            except tk.TclError:
                pass
        else:
            for seq, script in self._ed2_saved_binds.items():
                if script:
                    self.canvas.bind(seq, script)
                else:
                    self.canvas.unbind(seq)
            self._ed2_saved_binds = {}
            try:
                self.canvas.configure(cursor="")
            except tk.TclError:
                pass
        try:
            if on:
                self.canvas.bind("<Double-Button-1>", self._ed9_dbl)
            else:
                self.canvas.unbind("<Double-Button-1>")
        except tk.TclError:
            pass

    def _after_edit_change(self):
        try:
            self._ed2_cache.clear()
        except Exception:
            pass
        if self.side_panel_mode == "layers":
            self._refresh_side_panel()
        self._ed2_redisplay()
        if self._ed3_el(self._ed3_sel) is None:
            self._ed3_sel = None
        self._ed3_draw_ui()

    def _ed3_key(self, event):
        if not self._ed2_active or self._ed3_sel is None:
            return
        try:
            if isinstance(self.root.focus_get(), (tk.Entry, tk.Text, tk.Spinbox)):
                return
        except (tk.TclError, KeyError):
            pass
        self._ed3_delete()


    # ---- patch 1: audio first, spacing, track labels, snip modes (_p1_*) ----

    def _p1_track_labels(self):
        labs = getattr(self, "_p1_labels", None)
        if labs is not None:
            return labs
        labs = []
        try:
            r = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream_tags=title", "-of", "json", str(self.video_path)],
                capture_output=True, text=True, check=True)
            for st in json.loads(r.stdout).get("streams", []):
                tags = {str(k).lower(): v for k, v in (st.get("tags") or {}).items()}
                labs.append(str(tags.get("title", "")).strip())
        except Exception:
            log.exception("Film edit: could not read audio track titles (non-fatal)")
        self._p1_labels = labs
        return labs

    def _p1_track_label(self, i):
        labs = self._p1_track_labels()
        t = labs[i] if i < len(labs) else ""
        return (t[:1].upper() + t[1:]) if t else f"Track {i + 1}"

    def _p1_snip_mode(self):
        m = getattr(self, "_p1_smode", None)
        if m is None:
            from . import constants as C
            m = str(getattr(C, "DEFAULT_SNIP_MODE", "select"))
            if m not in ("select", "box", "ellipse", "freeform"):
                m = "select"
            self._p1_smode = m
        return m

    def _ed9_mode(self, tool):
        if tool == "snip":
            return self._p1_snip_mode()
        if tool == "snip":
            return "select"
        if tool == "pen":
            return self._ed9_pen_mode
        if tool == "shape":
            return self._ed9_shape_mode
        if tool == "text":
            return self._ed9_text_mode
        return None

    def _ed9_set_mode(self, tool, mode):
        if tool == "snip":
            self._p1_smode = mode
            self._ed9_text_confirm()
            self._ed9_clear_g()
            self._ed3_sel = None
            self._ed2_build_rows()
            self._ed2_redisplay()
            return
        _ed9_set_mode(self, tool, mode)

    def _ed9_opts_snip(self, o):
        self._pack_dropdown(o, "Mode", [("select", "Select"), ("box", "Box"), ("ellipse", "Ellipse"), ("freeform", "Free-form")],
                            self._p1_snip_mode(), lambda v: self._ed9_set_mode("snip", v), width=9)
        tk.Label(o, text="Box/Ellipse/Free-form: drag to copy that part of the frame into a new object | Select: move, resize, rotate, crop",
                 fg="#aaaaaa", bg="#222222").pack(side="left", padx=(10, 0))
        if self._ed5_crop:
            tk.Button(o, text="Clear crop", pady=BUTTON_PADY, command=lambda: self._ed5_set("crop", None)).pack(side="left", padx=6, pady=4)

    def _p1_press(self, event):
        self._ed9_state()
        if self._ed2_tool == "snip" and self._p1_snip_mode() != "select" and not getattr(self, "_ed8_split", None):
            if self._ed9_tw is not None:
                self._ed9_text_confirm()
            nx_14, ny_14, cx_16, cy_16 = self._ed2_xy(event)
            self._ed2_clear_tmp()
            if self._ed9_try_grab(event, "snip"):
                return
            self._ed9_g = {"kind": "snip", "start": (nx_14, ny_14), "scr": (cx_16, cy_16), "pts": [(nx_14, ny_14)], "spts": [(cx_16, cy_16)]}
            return
        self._ed9_state()
        if getattr(self, "_ed8_split", None):
            sp = getattr(self, "_ed8_split", None)
            if sp:
                nx_8, ny_8, cx_10, cy_10 = self._ed2_xy(event)
                sp["start"], sp["scr"] = (nx_8, ny_8), (cx_10, cy_10)
                self._ed2_clear_tmp()
                return
            tool_4 = self._ed2_tool
            if tool_4 in ("trim", "audio"):
                return
            if tool_4 == "crop":
                self._ed5_state()
                self._ed2_clear_tmp()
                nx_5, ny_5, cx_7, cy_7 = self._ed2_xy(event)
                self._ed5_cd = {"start": (nx_5, ny_5), "scr": (cx_7, cy_7)}
                return
            if self._ed2_tool != "select":
                nx_2, ny_2, cx_2, cy_2 = self._ed2_xy(event)
                self._ed2_clear_tmp()
                tool_2 = self._ed2_tool
                if tool_2 == "pen":
                    self._ed2_drag = {"pts": [(nx_2, ny_2)], "last": (cx_2, cy_2)}
                else:
                    self._ed2_drag = {"start": (nx_2, ny_2), "scr": (cx_2, cy_2)}
                return
            self._ed3_state()
            x, y, cx_4, cy_4 = self._ed3_pt(event)
            sel = self._ed3_el(self._ed3_sel)
            if sel is not None:
                handle = self._ed3_hit_handle(sel, cx_4, cy_4)
                if handle:
                    self._ed3_begin(sel, handle, x, y)
                    return
            order = {l["id"]: i for i, l in enumerate(self.film_layers)}
            hit = None
            for el in reversed(sorted(self.film_edits, key=lambda e: order.get(e.get("layer_id"), 0))):
                if self._ed3_visible(el) and self._ed3_hit(el, x, y):
                    hit = el
                    break
            self._ed3_select_el(hit)
            if hit is not None:
                self._ed3_begin(hit, "move", x, y)
            return
        if self._ed9_tw is not None:
            self._ed9_text_confirm()
        tool = self._ed2_tool
        if tool not in ("snip", "pen", "text", "shape"):
            return
        mode = self._ed9_mode(tool)
        nx_11, ny_11, cx_13, cy_13 = self._ed2_xy(event)
        self._ed2_clear_tmp()
        if self._ed9_grab_ok(tool) and self._ed9_try_grab(event, tool):
            return
        if mode == "select":
            self._ed3_sel = None
            self._ed3_draw_ui()
            name_2 = self._ed9_hit_crop(cx_13, cy_13)
            if name_2:
                x1_5, y1_5, x2_2, y2_2 = self._ed9_crop_rect()
                opp_2 = {"nw": (x2_2, y2_2), "ne": (x1_5, y2_2), "sw": (x2_2, y1_5), "se": (x1_5, y1_5)}[name_2]
                self._ed9_cd = {"name": name_2, "orig": self._ed5_crop, "fixed": opp_2, "r0": (x1_5, y1_5, x2_2, y2_2)}
            return
        if tool == "pen":
            self._ed9_g = {"kind": "pen", "pts": [(nx_11, ny_11)], "last": (cx_13, cy_13)}
        elif tool == "shape":
            self._ed9_g = {"kind": "shape", "start": (nx_11, ny_11), "scr": (cx_13, cy_13)}
        elif tool == "text":
            self._ed9_g = {"kind": "text", "start": (nx_11, ny_11), "scr": (cx_13, cy_13)}

    def _p1_drag(self, event):
        g_4 = getattr(self, "_ed9_g", None)
        if g_4 and g_4.get("kind") == "snip" and not self._ed3_drag:
            nx_15, ny_15, cx_17, cy_17 = self._ed2_xy(event)
            self._ed2_clear_tmp()
            mode = self._p1_snip_mode()
            x0, y0 = g_4["scr"]
            if mode == "freeform":
                g_4["pts"].append((nx_15, ny_15))
                g_4["spts"].append((cx_17, cy_17))
                flat = [v for p in g_4["spts"] for v in p]
                if len(flat) >= 4:
                    self._ed2_tmp.append(self.canvas.create_line(*flat, fill="#00b4ff", width=2, dash=(4, 2)))
                xs = [p[0] for p in g_4["pts"]]
                ys = [p[1] for p in g_4["pts"]]
                self._ed9_dim(cx_17, cy_17, f"{round(max(xs) - min(xs))} x {round(max(ys) - min(ys))}")
                return
            if mode == "ellipse":
                self._ed2_tmp.append(self.canvas.create_oval(x0, y0, cx_17, cy_17, outline="#00b4ff", width=2, dash=(4, 2)))
            else:
                self._ed2_tmp.append(self.canvas.create_rectangle(x0, y0, cx_17, cy_17, outline="#00b4ff", width=2, dash=(4, 2)))
            sx_7, sy_7 = g_4["start"]
            self._ed9_dim(cx_17, cy_17, f"{round(abs(nx_15 - sx_7))} x {round(abs(ny_15 - sy_7))}")
            return
        if getattr(self, "_ed8_split", None):
            sp = getattr(self, "_ed8_split", None)
            if sp:
                if not sp.get("scr"):
                    return
                nx_9, ny_9, cx_11, cy_11 = self._ed2_xy(event)
                self._ed2_clear_tmp()
                x0_4, y0_4 = sp["scr"]
                self._ed2_tmp.append(self.canvas.create_rectangle(x0_4, y0_4, cx_11, cy_11, outline="#00b4ff", dash=(4, 3), width=2))
                return
            tool = self._ed2_tool
            if tool in ("trim", "audio"):
                return
            if tool == "crop":
                d_6 = self._ed5_cd
                if not d_6:
                    return
                nx_6, ny_6, cx_8, cy_8 = self._ed2_xy(event)
                self._ed2_clear_tmp()
                x0_3, y0_3 = d_6["scr"]
                self._ed2_tmp.append(self.canvas.create_rectangle(x0_3, y0_3, cx_8, cy_8, outline="#00e5ff", dash=(4, 3), width=2))
                return
            if self._ed2_tool != "select":
                d_2 = self._ed2_drag
                if not d_2:
                    return
                nx_3, ny_3, cx_3, cy_3 = self._ed2_xy(event)
                s_2 = self.display_scale or 1.0
                if self._ed2_tool == "pen":
                    px_2, py_2 = d_2["pts"][-1]
                    if abs(nx_3 - px_2) < 1 and abs(ny_3 - py_2) < 1:
                        return
                    d_2["pts"].append((nx_3, ny_3))
                    lx_2, ly_2 = d_2["last"]
                    self._ed2_tmp.append(self.canvas.create_line(lx_2, ly_2, cx_3, cy_3, fill=self._ed2_color, width=max(1, self._ed2_width * s_2), capstyle="round"))
                    d_2["last"] = (cx_3, cy_3)
                    return
                self._ed2_clear_tmp()
                x0_2, y0_2 = d_2["scr"]
                if self._ed2_tool == "shape":
                    kind_2 = self._ed2_shape_kind
                    if kind_2 == "line":
                        item = self.canvas.create_line(x0_2, y0_2, cx_3, cy_3, fill=self._ed2_color, width=max(1, self._ed2_width * s_2))
                    elif kind_2 == "ellipse":
                        item = self.canvas.create_oval(x0_2, y0_2, cx_3, cy_3, outline=self._ed2_color, width=max(1, self._ed2_width * s_2))
                    else:
                        item = self.canvas.create_rectangle(x0_2, y0_2, cx_3, cy_3, outline=self._ed2_color, width=max(1, self._ed2_width * s_2))
                else:
                    item = self.canvas.create_rectangle(x0_2, y0_2, cx_3, cy_3, outline="#00b4ff", dash=(4, 3), width=2)
                self._ed2_tmp.append(item)
                return
            d_4 = getattr(self, "_ed3_drag", None)
            if not d_4:
                return
            x, y, cx_5, cy_5 = self._ed3_pt(event)
            el, mode_2 = d_4["el"], d_4["mode"]
            sx_4, sy_4 = d_4["start"]
            if mode_2 == "move":
                el["origin"] = (d_4["o"][0] + (x - sx_4), d_4["o"][1] + (y - sy_4))
            elif mode_2 == "rotate":
                mx = d_4["o"][0] + d_4["w"] / 2
                my = d_4["o"][1] + d_4["h"] / 2
                a0 = _ed3_math.degrees(_ed3_math.atan2(sy_4 - my, sx_4 - mx))
                a1 = _ed3_math.degrees(_ed3_math.atan2(y - my, x - mx))
                el["rotation"] = d_4["rot"] + (a1 - a0)
            else:
                aw, ah = _ED3_AXES.get(mode_2, (True, True))
                w0_2, h0_2 = d_4["w"], d_4["h"]
                mx, my = d_4["o"][0] + w0_2 / 2, d_4["o"][1] + h0_2 / 2
                th = _ed3_math.radians(d_4["rot"])
                c, s_3 = _ed3_math.cos(th), _ed3_math.sin(th)
                ldx = (x - mx) * c + (y - my) * s_3
                ldy = -(x - mx) * s_3 + (y - my) * c
                if mode_2 in _ED3_CORNERS and self._ed3_prop.get():
                    half = _ed3_math.hypot(w0_2 / 2, h0_2 / 2)
                    f_2 = _ed3_math.hypot(ldx, ldy) / half if half > 0 else 1.0
                    f_2 = max(f_2, 4 / max(1e-6, min(w0_2, h0_2)))
                    nw, nh = w0_2 * f_2, h0_2 * f_2
                    ncx, ncy = mx, my
                else:
                    from .resize_math import anchored_resize
                    nw, nh, ncx, ncy = anchored_resize(mode_2, w0_2, h0_2, mx, my, c, s_3, ldx, ldy, aw, ah)
                el["w"], el["h"] = nw, nh
                el["origin"] = (ncx - nw / 2, ncy - nh / 2)
            self._ed2_redisplay()
            self._ed3_draw_ui()
            return
        if self._ed3_drag:
            return self._ed9_xf_move(event)
        if self._ed9_cd:
            return self._ed9_crop_move(event)
        g_2 = self._ed9_g
        if not g_2:
            return
        nx_12, ny_12, cx_14, cy_14 = self._ed2_xy(event)
        s_6 = self.display_scale or 1.0
        if g_2["kind"] == "pen":
            px_3, py_3 = g_2["pts"][-1]
            if abs(nx_12 - px_3) < 1 and abs(ny_12 - py_3) < 1:
                return
            g_2["pts"].append((nx_12, ny_12))
            lx_3, ly_3 = g_2["last"]
            hl = self._ed9_pen_mode == "highlight"
            wd = self._ed9_hl_width if hl else self._ed2_width
            kw = {"stipple": "gray50"} if hl else {}
            self._ed2_tmp.append(self.canvas.create_line(lx_3, ly_3, cx_14, cy_14, fill=self._ed2_color, width=max(1, wd * s_6), capstyle="round", **kw))
            g_2["last"] = (cx_14, cy_14)
            return
        self._ed2_clear_tmp()
        x0_5, y0_5 = g_2["scr"]
        if g_2["kind"] == "shape":
            kind = self._ed9_shape_mode
            wd = max(1, self._ed2_width * s_6)
            fill = self._ed2_fill or ""
            if kind == "line":
                self._ed2_tmp.append(self.canvas.create_line(x0_5, y0_5, cx_14, cy_14, fill=self._ed2_color, width=wd, capstyle="round"))
            elif kind == "ellipse":
                self._ed2_tmp.append(self.canvas.create_oval(x0_5, y0_5, cx_14, cy_14, outline=self._ed2_color, width=wd, fill=fill))
            else:
                self._ed2_tmp.append(self.canvas.create_rectangle(x0_5, y0_5, cx_14, cy_14, outline=self._ed2_color, width=wd, fill=fill))
        self._ed2_tmp.append(self.canvas.create_rectangle(x0_5, y0_5, cx_14, cy_14, outline="#00b4ff", width=2, dash=(5, 3)))
        sx_6, sy_6 = g_2["start"]
        self._ed9_dim(cx_14, cy_14, f"{round(abs(nx_12 - sx_6))} x {round(abs(ny_12 - sy_6))}")

    def _p1_release(self, event):
        g_5 = getattr(self, "_ed9_g", None)
        if g_5 and g_5.get("kind") == "snip" and not self._ed3_drag:
            self._ed9_g = None
            self._ed2_clear_tmp()
            self._ed9_clear_dim()
            nx, ny, cx, cy = self._ed2_xy(event)
            mode = self._p1_snip_mode()
            if mode == "freeform":
                pts = g_5["pts"]
                if len(pts) < 3:
                    return
            else:
                pts = [g_5["start"], (nx, ny)]
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            box = (min(xs), min(ys), max(xs), max(ys))
            if box[2] - box[0] < 4 or box[3] - box[1] < 4:
                return
            self._p1_make_snip(mode, box, g_5["pts"] if mode == "freeform" else None)
            return
        if getattr(self, "_ed8_split", None):
            sp = getattr(self, "_ed8_split", None)
            if sp:
                self._ed2_clear_tmp()
                if not sp.get("start"):
                    self._ed8_split = None
                    return
                nx_10, ny_10, cx_12, cy_12 = self._ed2_xy(event)
                sx_5, sy_5 = sp["start"]
                self._ed8_finish_split((min(sx_5, nx_10), min(sy_5, ny_10), max(sx_5, nx_10), max(sy_5, ny_10)))
                return
            tool = self._ed2_tool
            if tool in ("trim", "audio"):
                return
            if tool == "crop":
                d_7 = self._ed5_cd
                self._ed5_cd = None
                self._ed2_clear_tmp()
                if not d_7:
                    return
                nx_7, ny_7, cx_9, cy_9 = self._ed2_xy(event)
                sx_3, sy_3 = d_7["start"]
                x1_3, y1_3 = max(0.0, min(sx_3, nx_7)), max(0.0, min(sy_3, ny_7))
                x2, y2 = min(float(self.native_w), max(sx_3, nx_7)), min(float(self.native_h), max(sy_3, ny_7))
                w_8 = int(x2 - x1_3) // 2 * 2
                h_3 = int(y2 - y1_3) // 2 * 2
                if w_8 < 16 or h_3 < 16:
                    return
                self._ed5_set("crop", (int(x1_3), int(y1_3), w_8, h_3))
                return
            if self._ed2_tool != "select":
                d_3 = self._ed2_drag
                self._ed2_drag = None
                if not d_3:
                    return
                nx_4, ny_4, cx_6, cy_6 = self._ed2_xy(event)
                self._ed2_clear_tmp()
                tool_3 = self._ed2_tool
                if tool_3 == "pen":
                    pts_2 = d_3["pts"]
                    if len(pts_2) > 1 or True:
                        width_2 = int(self._ed2_width)
                        pad_2 = width_2 / 2 + 1
                        xs_2 = [p_2[0] for p_2 in pts_2]
                        ys_2 = [p_2[1] for p_2 in pts_2]
                        x1_2, y1_2 = min(xs_2) - pad_2, min(ys_2) - pad_2
                        el_2 = {
                            "mode": self._ed2_pen_kind, "points": pts_2, "color": self._ed2_color, "width": width_2,
                            "origin": (x1_2, y1_2), "w": max(xs_2) - min(xs_2) + 2 * pad_2, "h": max(ys_2) - min(ys_2) + 2 * pad_2,
                        }
                        self._ed2_add(el_2, self._ed2_pen_kind)
                    return
                sx_2, sy_2 = d_3["start"]
                s_4 = self.display_scale or 1.0
                x1_2, y1_2 = min(sx_2, nx_4), min(sy_2, ny_4)
                w_6 = max(4.0, abs(nx_4 - sx_2))
                h_2 = max(4.0, abs(ny_4 - sy_2))
                tiny_2 = abs(nx_4 - sx_2) * s_4 < 4 and abs(ny_4 - sy_2) * s_4 < 4
                if tool_3 == "shape":
                    kind_3 = self._ed2_shape_kind
                    if kind_3 != "line" and tiny_2:
                        return
                    if kind_3 == "line" and tiny_2:
                        return
                    el_2 = {
                        "mode": "shape", "shape_type": kind_3, "origin": (x1_2, y1_2), "w": w_6, "h": h_2,
                        "start_frac": ((sx_2 - x1_2) / w_6, (sy_2 - y1_2) / h_2), "end_frac": ((nx_4 - x1_2) / w_6, (ny_4 - y1_2) / h_2),
                        "color": self._ed2_color, "width": int(self._ed2_width),
                        "fill": (self._ed2_fill if kind_3 != "line" else None), "segment_ids": [],
                    }
                    self._ed2_add(el_2, "shape")
                    return
                if tiny_2:
                    w_6, h_2 = 240.0, 60.0
                    x1_2, y1_2 = min(sx_2, self.native_w - w_6), min(sy_2, self.native_h - h_2)
                    x1_2, y1_2 = max(0.0, x1_2), max(0.0, y1_2)
                self._ed2_text_dialog((x1_2, y1_2, w_6, h_2))
                return
            d_5 = getattr(self, "_ed3_drag", None)
            self._ed3_drag = None
            if not d_5:
                return
            el_3 = d_5["el"]
            same = (tuple(el_3["origin"]) == d_5["o"] and el_3["w"] == d_5["w"] and el_3["h"] == d_5["h"]
                    and el_3.get("rotation", 0.0) == d_5["rot"])
            if same:
                el_3.clear()
                el_3.update(d_5["before"])
            else:
                self._ed3_acts.append(("xf", el_3, d_5["before"], dict(el_3)))
                self._ed3_redo = []
            self._ed2_redisplay()
            self._ed3_draw_ui()
            return
        if self._ed3_drag:
            return self._ed9_xf_release()
        if self._ed9_cd:
            return self._ed9_crop_release()
        g_3 = self._ed9_g
        self._ed9_g = None
        self._ed2_clear_tmp()
        self._ed9_clear_dim()
        if not g_3:
            return
        nx_13, ny_13, cx_15, cy_15 = self._ed2_xy(event)
        s = self.display_scale or 1.0
        if g_3["kind"] == "pen":
            pts_3 = g_3["pts"]
            if len(pts_3) < 2:
                return
            hl = self._ed9_pen_mode == "highlight"
            width = int(self._ed9_hl_width if hl else self._ed2_width)
            pad = width / 2 + 1
            xs_3 = [p_3[0] for p_3 in pts_3]
            ys_3 = [p_3[1] for p_3 in pts_3]
            el_4 = {
                "mode": "highlight" if hl else "pen", "points": pts_3, "color": self._ed2_color, "width": width,
                "origin": (min(xs_3) - pad, min(ys_3) - pad), "w": max(xs_3) - min(xs_3) + 2 * pad, "h": max(ys_3) - min(ys_3) + 2 * pad,
            }
            self._ed2_add(el_4, el_4["mode"])
            return
        sx, sy = g_3["start"]
        x1_4, y1_4 = min(sx, nx_13), min(sy, ny_13)
        w = max(4.0, abs(nx_13 - sx))
        h = max(4.0, abs(ny_13 - sy))
        tiny = abs(nx_13 - sx) * s < 4 and abs(ny_13 - sy) * s < 4
        if g_3["kind"] == "shape":
            if tiny:
                return
            kind = self._ed9_shape_mode
            el_4 = {
                "mode": "shape", "shape_type": kind, "origin": (x1_4, y1_4), "w": w, "h": h,
                "start_frac": ((sx - x1_4) / w, (sy - y1_4) / h), "end_frac": ((nx_13 - x1_4) / w, (ny_13 - y1_4) / h),
                "color": self._ed2_color, "width": int(self._ed2_width),
                "fill": (self._ed2_fill if kind != "line" else None), "segment_ids": [],
            }
            self._ed2_add(el_4, "shape")
            return
        if abs(nx_13 - sx) * s < 10 or abs(ny_13 - sy) * s < 10:
            return
        self._ed9_text_begin((x1_4, y1_4, x1_4 + w, y1_4 + h))

    def _p1_make_snip_2(self, shape, box, pts):
        from PIL import Image, ImageDraw
        self._pause()
        img_2 = self.get_current_frame_image_base()
        if img_2 is None:
            notify("Snip failed", "Could not grab the current frame - see the log", blocking=False)
            return
        iw, ih = img_2.size
        x1, y1, x2, y2 = box
        bx1_2, by1_2 = max(0, int(x1)), max(0, int(y1))
        bx2, by2 = min(iw, int(round(x2))), min(ih, int(round(y2)))
        bw, bh = bx2 - bx1_2, by2 - by1_2
        if bw < 4 or bh < 4:
            return
        crop = img_2.convert("RGBA").crop((bx1_2, by1_2, bx2, by2))
        mask = Image.new("L", (bw, bh), 0)
        d = ImageDraw.Draw(mask)
        if shape == "ellipse":
            d.ellipse([0, 0, bw - 1, bh - 1], fill=255)
        elif shape == "freeform":
            d.polygon([(px - bx1_2, py - by1_2) for px, py in pts], fill=255)
        else:
            d.rectangle([0, 0, bw - 1, bh - 1], fill=255)
        crop.putalpha(mask)
        ox = min(bx1_2 + 20, max(0, iw - bw))
        oy = min(by1_2 + 20, max(0, ih - bh))
        el_5 = {"mode": "snip", "shape": shape, "extracted_image": crop, "base_w": bw, "base_h": bh,
              "origin": (float(ox), float(oy)), "w": float(bw), "h": float(bh), "rotation": 0.0,
              "orig_bounds": None, "segment_ids": []}
        self._ed2_add(el_5, "snip")
        self._ed3_sel = el_5["eid"]
        self._ed3_draw_ui()
        notify("Snip copied", "New object placed over the video - drag it where you want it", blocking=False)

    # ---- patch 2: audio layers (fade in / fade out / mute / volume boost), preview + bake (_p2_*) ----

    def _p2_state(self):
        self._ed5_state()
        if not hasattr(self, "_p2_layers"):
            self._p2_layers = []
            self._p2_sel = None
            self._p2_counter = 0
            self._p2_newtype = "fade_in"
            self._p2_snap = None
            self._p2_before = None

    def _p2_find(self, lid):
        self._p2_state()
        for l in self._p2_layers:
            if l["id"] == lid:
                return l
        return None

    def _p2_default_range_2(self, typ):
        if typ in ("mute", "boost"):
            return None, None
        dur = self._ed_dur()
        d = min(1.0, dur)
        t = self.frame_index / max(float(self.fps), 1.0)
        s = max(0.0, min(t, dur - d))
        return s, s + d

    def _p2_unique_name_2(self, typ, ignore=None):
        names = {l["name"] for l in self._p2_layers if l["id"] != ignore}
        n = 1
        while f"{_P2_NAMES[typ]} {n}" in names:
            n += 1
        return f"{_P2_NAMES[typ]} {n}"

    def _p2_make_2(self, typ):
        self._p2_state()
        self._p2_counter += 1
        s, e = self._p2_default_range(typ)
        return {"id": f"alayer{self._p2_counter}", "name": self._p2_unique_name(typ), "type": typ,
                "start": s, "end": e, "track": None, "amount": 150}

    def _ed5_audio_dirty(self):
        self._p2_state()
        return bool(self._p2_layers) or self._ed5_audio_dirty_2()

    def _p2_audio_tool(self):
        return bool(getattr(self, "_ed2_active", False)) and getattr(self, "_ed2_tool", None) == "audio"

    def _p2_spress(self, event):
        if self._p2_audio_tool():
            self._p2_state()
            self._p2_before = self._ed5_get("alayers")
        if getattr(self, "_ed2_active", False) and self._ed2_tool == "trim":
            self._ed5_state()
            self._ed5_before = self._ed5_get("trim")
        layer = self._ed_active()
        if layer is not None and self.total_frames:
            s, e = self._ed_range(layer)
            ds = abs(event.x - self._ed_x(s))
            de = abs(event.x - self._ed_x(e))
            if min(ds, de) <= 7:
                if ds < de:
                    self._ed_drag = "start"
                elif de < ds:
                    self._ed_drag = "end"
                else:
                    self._ed_drag = "start" if event.x < self._ed_x(s) else "end"
                return
        self._ed_drag = None
        self._scrubbing = True
        self._on_scrub_click(event)

    def _p2_draw(self):
        self._ed8_draw()
        try:
            if not self._p2_audio_tool() or not self.total_frames:
                return
            self._p2_state()
            cv_4 = self.scrub_canvas
            if cv_4.winfo_width() <= 1:
                return
            mid_5 = max(10, cv_4.winfo_height() // 2)
            for l in self._p2_layers:
                if l["id"] == self._p2_sel:
                    continue
                s_7, e = self._ed_range(l)
                cv_4.create_rectangle(self._ed_x(s_7), mid_5 + 9, self._ed_x(e), mid_5 + 12, fill="#8a5a1a", outline="", tags=("scrub", "p2dim"))
        except tk.TclError:
            pass

    def _p2_commit_edit(self, rebuild=True, **kw):
        sel = self._p2_find(self._p2_sel)
        if sel is None:
            return
        new = self._ed5_get("alayers")
        for l in new:
            if l["id"] == sel["id"]:
                l.update(kw)
        self._ed5_set("alayers", new, rebuild=rebuild)

    def _p2_pick(self, v):
        self._p2_state()
        if v == "__new__":
            l = self._p2_make(self._p2_newtype)
            self._p2_sel = l["id"]
            self._ed5_set("alayers", self._ed5_get("alayers") + [l])
            return
        self._p2_sel = v
        self._ed5_refresh(True)

    def _p2_set_type(self, v):
        self._p2_state()
        sel = self._p2_find(self._p2_sel)
        if sel is None:
            self._p2_newtype = v
            self._ed5_refresh(True)
            return
        kw = {"type": v}
        if v in ("fade_in", "fade_out") and sel["start"] is None and sel["end"] is None:
            kw["start"], kw["end"] = self._p2_default_range(v)
        if sel["name"].startswith(_P2_NAMES[sel["type"]] + " "):
            kw["name"] = self._p2_unique_name(v, ignore=sel["id"])
        self._p2_newtype = v
        self._p2_commit_edit(**kw)

    def _p2_set_track(self, v):
        self._p2_commit_edit(track=(None if v == "all" else int(v)))

    def _p2_reset(self):
        sel = self._p2_find(self._p2_sel)
        if sel is None:
            return
        s, e = self._p2_default_range(sel["type"])
        self._p2_commit_edit(start=s, end=e, amount=150)

    def _p2_delete(self):
        sel = self._p2_find(self._p2_sel)
        if sel is None:
            return
        new = [l for l in self._ed5_get("alayers") if l["id"] != sel["id"]]
        self._p2_sel = None
        self._ed5_set("alayers", new)

    def _p2_filter(self, l, off):
        s, e = self._ed_range(l)
        t = f"(t+{off:.3f})" if off else "t"
        typ = l["type"]
        if typ == "mute":
            return f"volume=0:enable='between({t},{s:.3f},{e:.3f})'"
        if typ == "boost":
            g = max(0.0, float(l.get("amount", 150))) / 100.0
            return f"volume={g:.3f}:enable='between({t},{s:.3f},{e:.3f})'"
        d = max(0.05, e - s)
        ramp = f"({t}-{s:.3f})/{d:.3f}"
        if typ == "fade_out":
            ramp = f"1-{ramp}"
        return f"volume='if(lt({t},{s:.3f}),1,if(gt({t},{e:.3f}),1,{ramp}))':eval=frame"

    def _p2_audio_chain_2(self, n_aud, off=0.0):
        self._p2_state()
        lst = self._ed5_audio_list(n_aud)
        parts, kept = [], []
        for i in range(n_aud):
            a = lst[i]
            if a.get("mute"):
                continue
            fl = []
            vol = float(a.get("vol", 1.0))
            if abs(vol - 1.0) > 0.001:
                fl.append(f"volume={vol:.3f}")
            fl += [self._p2_filter(l, off) for l in self._p2_layers if l.get("track") == i]
            parts.append(f"[0:a:{i}]{','.join(fl) if fl else 'anull'}[a{i}]")
            kept.append(f"a{i}")
        if not kept:
            return parts, None
        if len(kept) > 1:
            parts.append("".join(f"[{k}]" for k in kept) + f"amix=inputs={len(kept)}:duration=longest[amix]")
            cur = "amix"
        else:
            cur = kept[0]
        allfl = [self._p2_filter(l, off) for l in self._p2_layers if l.get("track") is None]
        if allfl:
            parts.append(f"[{cur}]{','.join(allfl)}[agl]")
            cur = "agl"
        return parts, cur

    def _fp_start_audio(self):
        self._p2_state()
        if not self._p2_layers:
            if not hasattr(self, "_ed5_audio") or not self._ed5_audio_dirty():
                self._fp_stop_audio()
                if not getattr(self, "_aud_tracks", 0):
                    return
                try:
                    self._aud_stream = _FilmAudio(self.video_path, self._play_i0 / max(self.fps, 1.0), self._aud_tracks, self._fp_gain)
                except Exception:
                    log.exception("Film viewer: could not start audio playback (non-fatal; is pacat installed?)")
                    self._aud_stream = None
                return
            self._fp_stop_audio()
            if not getattr(self, "_aud_tracks", 0):
                return
            try:
                self._aud_stream = _FilmAudioEd(self.video_path, self._play_i0 / max(self.fps, 1.0),
                                                self._aud_tracks, self._fp_gain, [dict(a) for a in self._ed5_audio])
            except Exception:
                log.exception("Film viewer: could not start edited audio playback (non-fatal)")
                self._aud_stream = None
            return
        self._fp_stop_audio()
        n = int(getattr(self, "_aud_tracks", 0) or 0)
        if not n:
            return
        try:
            off = self._play_i0 / max(self.fps, 1.0)
            parts, cur = self._p2_audio_chain(n, off)
            self._aud_stream = _P2AudioPlay(self.video_path, off, parts, cur, self._fp_gain)
        except Exception:
            log.exception("Film viewer: could not start audio playback with layers (non-fatal)")
            self._aud_stream = None

    # ---- patch 3: audio layers consolidated - one layer type with track checkboxes, volume, fade effect (_p3_*) ----

    def _p2_unique_name(self, typ=None, ignore=None):
        names = {l["name"] for l in self._p2_layers if l["id"] != ignore}
        n = 1
        while f"Layer {n}" in names:
            n += 1
        return f"Layer {n}"

    def _p2_make(self, typ=None):
        self._p2_state()
        self._p2_counter += 1
        return {"id": f"alayer{self._p2_counter}", "name": self._p3_unique_name(), "start": None, "end": None,
                "tracks": None, "vol": 100, "fx": "none"}

    def _p3_set_fx_2(self, v):
        sel = self._p2_find(self._p2_sel)
        if sel is None:
            return
        kw = {"fx": v}
        if v != "none" and sel["start"] is None and sel["end"] is None:
            kw["start"], kw["end"] = self._p3_default_range(v)
        self._p2_commit_edit(**kw)

    def _p3_reset(self):
        sel = self._p2_find(self._p2_sel)
        if sel is None:
            return
        s, e = self._p3_default_range(sel.get("fx", "none"))
        self._p2_commit_edit(start=s, end=e, vol=100)

    def _p3_toggle_track(self, vars_):
        on = [i for i, v in enumerate(vars_) if v.get()]
        self._p2_commit_edit(tracks=(None if len(on) == len(vars_) else on))

    def _p2_audio_chain(self, n_aud, off=0.0):
        self._p2_state()
        lst = self._ed5_audio_list(n_aud)
        parts, kept = [], []
        for i in range(n_aud):
            a = lst[i]
            if a.get("mute"):
                continue
            fl = []
            vol = float(a.get("vol", 1.0))
            if abs(vol - 1.0) > 0.001:
                fl.append(f"volume={vol:.3f}")
            fl += [self._p3_filter(l, off) for l in self._p2_layers if l.get("tracks") is not None and i in l["tracks"]]
            parts.append(f"[0:a:{i}]{','.join(fl) if fl else 'anull'}[a{i}]")
            kept.append(f"a{i}")
        if not kept:
            return parts, None
        if len(kept) > 1:
            parts.append("".join(f"[{k}]" for k in kept) + f"amix=inputs={len(kept)}:duration=longest[amix]")
            cur = "amix"
        else:
            cur = kept[0]
        allfl = [self._p3_filter(l, off) for l in self._p2_layers if l.get("tracks") is None]
        if allfl:
            parts.append(f"[{cur}]{','.join(allfl)}[agl]")
            cur = "agl"
        return parts, cur

    # ---- patch 4: audio layer picker with + button and None placeholder, Effect None (_p4_*) ----

    def _p4_add(self):
        self._p2_state()
        l = self._p3_make()
        self._p2_sel = l["id"]
        self._ed5_set("alayers", self._ed5_get("alayers") + [l])

    def _p4_pick(self, v):
        self._p2_state()
        self._p2_sel = None if v == "__none__" else v
        self._ed5_refresh(True)

    def _p4_opts_audio(self, o):
        self._p2_state()
        bg = "#222222"
        n = int(getattr(self, "_aud_tracks", 0) or 0)
        if n <= 0:
            tk.Label(o, text="No audio tracks found (or still checking)", fg="#aaaaaa", bg=bg).pack(side="left", padx=8)
            return
        from .side_panel import small_button, attach_tooltip
        sel_2 = self._p2_find(self._p2_sel)
        tk.Label(o, text="Layer", fg="white", bg=bg).pack(side="left", padx=(2, 2))
        plus = tk.Button(o, text="+", padx=4, pady=BUTTON_PADY, command=self._p4_add)
        plus.pack(side="left", padx=(2, 0), pady=4)
        attach_tooltip(plus, "New audio layer")
        plus._tooltip_force = True
        opts = [("__none__", "None")] + [(l["id"], l["name"]) for l in self._p2_layers]
        by_label = {lbl: val for val, lbl in opts}
        var = tk.StringVar(master=self.root, value=(sel_2["name"] if sel_2 else "None"))
        menu = tk.OptionMenu(o, var, *[lbl for _v, lbl in opts], command=lambda lbl: self._p4_pick(by_label[lbl]))
        menu.config(width=10, pady=BUTTON_PADY)
        menu.pack(side="left", padx=(0, 2), pady=4)
        if sel_2 is None:
            return
        rb = small_button(o, "↺", self._p3_reset)
        rb.pack(side="left", padx=(4, 6), pady=4)
        attach_tooltip(rb, "Reset this layer's range and volume")
        rb._tooltip_force = True
        ck = dict(bg=bg, fg="white", selectcolor="#444444", activebackground=bg, activeforeground="white")
        tr = sel_2.get("tracks")
        tvars = [tk.IntVar(master=self.root, value=int(tr is None or i in tr)) for i in range(n)]
        for i in range(n):
            tk.Checkbutton(o, text=self._p1_track_label(i), variable=tvars[i],
                           command=lambda tv=tvars: self._p3_toggle_track(tv), **ck).pack(side="left", padx=(4, 0))
        av = tk.IntVar(master=self.root, value=int(sel_2.get("vol", 100)))

        def commit(av=av):
            try:
                v = max(0, min(400, int(av.get())))
            except (tk.TclError, ValueError):
                return
            self._p2_commit_edit(rebuild=False, vol=v)

        tk.Label(o, text="Vol", fg="white", bg=bg).pack(side="left", padx=(10, 2))
        sp = tk.Spinbox(o, from_=0, to=400, increment=5, width=4, textvariable=av, command=commit)
        sp.pack(side="left")
        sp.bind("<KeyRelease>", lambda e: commit())
        tk.Label(o, text="%", fg="white", bg=bg).pack(side="left")
        self._pack_dropdown(o, "Effect", list(_P4_FX), sel_2.get("fx", "none"), self._p3_set_fx, width=8)
        tk.Button(o, text="Delete", pady=BUTTON_PADY, command=self._p2_delete).pack(side="left", padx=(10, 2), pady=4)

    # ---- patch 5: + button matches the layer selector, double-click cancels a dropdown (_p5_*) ----

    def _p5_cancel_menu(self, mb, event=None):
        try:
            m = mb.nametowidget(mb.cget("menu"))
        except (tk.TclError, KeyError):
            return
        if event is not None and event.widget is m:
            px, py = mb.winfo_pointerxy()
            x0, y0 = mb.winfo_rootx(), mb.winfo_rooty()
            if not (x0 <= px < x0 + mb.winfo_width() and y0 <= py < y0 + mb.winfo_height()):
                return
        try:
            mb.tk.call("tk::MenuUnpost", str(m))
        except tk.TclError:
            try:
                m.unpost()
            except tk.TclError:
                pass
        return "break"

    def _p5_bind_menus(self, parent):
        for w in parent.winfo_children():
            if isinstance(w, tk.Menubutton) and not getattr(w, "_p5_bound", False):
                w._p5_bound = True
                w.bind("<Double-Button-1>", lambda e, w=w: self._p5_cancel_menu(w), add="+")
                try:
                    m = w.nametowidget(w.cget("menu"))
                    m.bind("<Double-Button-1>", lambda e, w=w: self._p5_cancel_menu(w, e), add="+")
                except (tk.TclError, KeyError):
                    pass
            self._p5_bind_menus(w)

    # ---- patch 6: film Draw gets Spray Can and Eraser, Width/Spread split (_p6_*) ----

    def _ed9_state(self):
        first = not getattr(self, "_p6_ready", False)
        self._ed9_state_2()
        if first:
            from . import constants as C
            self._p6_ready = True
            self._p6_spray_w = max(1, int(getattr(C, "SPRAY_DOT_WIDTH_DEFAULT", 2)))
            pm = str(getattr(C, "DEFAULT_PEN_MODE", "pen"))
            if pm in ("select", "pen", "spray", "highlight", "eraser"):
                self._ed9_pen_mode = pm

    def _ed2_native_patch(self, el):
        if el.get("mode") == "spray":
            from PIL import Image
            from .selector_app import SelectorApp as SA
            w = max(1, int(round(el["w"])))
            h = max(1, int(round(el["h"])))
            patch = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            SA._bake_spray_stroke(SA, patch, {"dots": el["dots"], "color": el["color"]}, el["origin"][0], el["origin"][1])
            return patch
        from PIL import Image, ImageDraw, ImageColor
        mode = el.get("mode")
        w_14 = max(1, int(round(el["w"])))
        h_5 = max(1, int(round(el["h"])))
        if mode in ("pen", "highlight"):
            from .constants import EDIT_HIGHLIGHT_ALPHA
            patch_2 = Image.new("RGBA", (w_14, h_5), (0, 0, 0, 0))
            d = ImageDraw.Draw(patch_2)
            ox, oy = el["origin"]
            pts = [(x - ox, y - oy) for x, y in el["points"]]
            width = max(1, int(el.get("width", 4)))
            rgb = ImageColor.getrgb(el["color"])[:3]
            fill = rgb + ((EDIT_HIGHLIGHT_ALPHA if mode == "highlight" else 255),)
            if len(pts) == 1:
                px, py = pts[0]
                r = width / 2
                d.ellipse([px - r, py - r, px + r, py + r], fill=fill)
            else:
                d.line(pts, fill=fill, width=width, joint="curve")
            return patch_2
        from .selector_app import SelectorApp
        return SelectorApp._render_element_local_patch(None, el, w_14, h_5)

    def _ed9_family(self, el):
        if el.get("mode") == "spray":
            return "pen"
        if el.get("lifted") or el.get("mode") in ("pen", "highlight"):
            return "pen"
        return el.get("mode")

    def _ed3_lift(self, el):
        if el.get("mode") == "spray":
            el["extracted_image"] = self._ed2_native_patch(el)
            el["mode"] = "snip"
            el["lifted"] = True
            el.pop("dots", None)
            return
        if el.get("mode") in ("pen", "highlight"):
            patch = self._ed2_native_patch(el)
            el["extracted_image"] = patch
            el["mode"] = "snip"
            el["lifted"] = True
            el.pop("points", None)

    def _p6_burst(self, g, nx, ny):
        from . import constants as C
        s = self.display_scale or 1.0
        spread = max(2, int(self._ed2_width))
        wmul = max(1, int(self._p6_spray_w)) / 2.0
        lo, hi = getattr(C, "SPRAY_DOT_RADIUS_RANGE", (0.5, 1.5))
        for _ in range(int(getattr(C, "SPRAY_DOTS_PER_BURST", 8))):
            ang = _p6_random.uniform(0, 2 * _p6_math.pi)
            dist = _p6_random.uniform(0, spread)
            dx = nx + _p6_math.cos(ang) * dist
            dy = ny + _p6_math.sin(ang) * dist
            r = _p6_random.uniform(lo, hi) * wmul
            g["dots"].append((dx, dy, r))
            rs = max(0.5, r * s)
            self._ed2_tmp.append(self.canvas.create_oval(dx * s - rs, dy * s - rs, dx * s + rs, dy * s + rs,
                                                          fill=self._ed2_color, outline=""))
        g["lastburst"] = (nx, ny)

    def _p6_apply_erase(self, pts, width):
        from PIL import Image, ImageDraw, ImageChops
        self._ed9_state()
        subs = []
        for el in list(self._ed9_cands("pen")):
            try:
                patch = self._ed2_native_patch(el)
            except Exception:
                log.exception("Film edit: eraser could not render an element (skipped)")
                continue
            rot = float(el.get("rotation", 0.0) or 0.0)
            P = patch.rotate(-rot, expand=True, resample=Image.BICUBIC) if abs(rot) > 0.01 else patch
            cx = el["origin"][0] + el["w"] / 2
            cy = el["origin"][1] + el["h"] / 2
            px = int(round(cx - P.width / 2))
            py = int(round(cy - P.height / 2))
            mask = Image.new("L", P.size, 0)
            d = ImageDraw.Draw(mask)
            sp = [(x - px, y - py) for x, y in pts]
            r = width / 2
            if len(sp) == 1:
                d.ellipse([sp[0][0] - r, sp[0][1] - r, sp[0][0] + r, sp[0][1] + r], fill=255)
            else:
                d.line(sp, fill=255, width=width, joint="curve")
                for qx, qy in (sp[0], sp[-1]):
                    d.ellipse([qx - r, qy - r, qx + r, qy + r], fill=255)
            a = P.getchannel("A")
            if ImageChops.multiply(a, mask).getbbox() is None:
                continue
            newa = ImageChops.multiply(a, ImageChops.invert(mask))
            before = dict(el)
            idx = next(i for i, e in enumerate(self.film_edits) if e is el)
            bb = newa.getbbox()
            if bb is None:
                self._ed3_remove(el)
                subs.append(("del", el, idx))
                continue
            P2 = P.copy()
            P2.putalpha(newa)
            P2 = P2.crop(bb)
            after = {"eid": before["eid"], "layer_id": before.get("layer_id"), "mode": "snip", "shape": "box", "lifted": True,
                     "extracted_image": P2, "base_w": P2.width, "base_h": P2.height,
                     "origin": (float(px + bb[0]), float(py + bb[1])), "w": float(P2.width), "h": float(P2.height),
                     "rotation": 0.0, "orig_bounds": None, "orig_freeform_points": None, "segment_ids": []}
            el.clear()
            el.update(after)
            subs.append(("xf", el, before, dict(after)))
        if not subs:
            return
        self._ed3_acts.append(subs[0] if len(subs) == 1 else ("multi", subs))
        self._ed3_redo = []
        self._ed3_sel = None
        self._after_edit_change()
        self._ed2_redisplay()

    def _ed2_undo(self):
        self._ed3_state()
        if self._ed3_acts and self._ed3_acts[-1][0] == "multi":
            act = self._ed3_acts.pop()
            subs = act[1]
            for sub in reversed(subs):
                self._ed3_acts.append(sub)
                self._ed5_undo()
            del self._ed3_redo[-len(subs):]
            self._ed3_redo.append(act)
            return
        self._ed5_undo()

    def _ed2_redo_do(self):
        self._ed3_state()
        if self._ed3_redo and self._ed3_redo[-1][0] == "multi":
            act = self._ed3_redo.pop()
            subs = act[1]
            for sub in subs:
                self._ed3_redo.append(sub)
                self._ed5_redo_do()
            del self._ed3_acts[-len(subs):]
            self._ed3_acts.append(act)
            return
        self._ed5_redo_do()

    def _ed9_opts_pen(self, o):
        from . import constants as C
        self._ed9_state()
        opts = [("select", "Select"), ("pen", "Pen"), ("spray", "Spray Can"), ("highlight", "Highlighter"), ("eraser", "Eraser")]
        self._pack_dropdown(o, "Mode", opts, self._ed9_pen_mode, lambda v: self._ed9_set_mode("pen", v), width=9)
        mode = self._ed9_pen_mode
        if mode != "eraser":
            self._pack_color_swatches(o, list(getattr(C, "PALETTE", ["#FF0000"])), self._ed2_color, self._ed9_pick_color)
        if mode == "highlight":
            var = tk.IntVar(master=self.root, value=int(self._ed9_hl_width))
            self._pack_labeled_slider(o, "Width", var, 4, 60, lambda v: setattr(self, "_ed9_hl_width", int(float(v))))
            return
        if mode == "spray":
            wv = tk.IntVar(master=self.root, value=int(self._p6_spray_w))
            self._pack_labeled_slider(o, "Width", wv, 1, 20, lambda v: setattr(self, "_p6_spray_w", int(float(v))))
            sv = tk.IntVar(master=self.root, value=int(self._ed2_width))
            self._pack_labeled_slider(o, "Spread", sv, 1, 30, lambda v: setattr(self, "_ed2_width", int(float(v))))
            return
        var = tk.IntVar(master=self.root, value=int(self._ed2_width))
        self._pack_labeled_slider(o, "Width", var, 1, 30, lambda v: setattr(self, "_ed2_width", int(float(v))))

    # ---- vim keys in the film Text tool ----

    def _p7_refresh(self):
        self._ed9_tp_sig = None
        try:
            self._ed9_tp_render()
        except Exception:
            log.exception("Film text: preview refresh failed (non-fatal)")

    def _p7_enter_insert(self):
        self._p7_mode = "insert"
        tw = self._ed9_tw
        if tw is not None:
            tw.tag_remove("sel", "1.0", "end")
        self._p7_refresh()

    def _p7_enter_normal(self):
        self._p7_mode = "normal"
        tw = self._ed9_tw
        if tw is not None:
            tw.tag_remove("sel", "1.0", "end")
        self._p7_refresh()

    def _p7_enter_visual(self, linewise):
        tw = self._ed9_tw
        if tw is None:
            return
        self._p7_mode = "visual_line" if linewise else "visual"
        self._p7_anchor = tw.index("insert")
        self._p7_update_visual()

    def _p7_update_visual(self):
        tw = self._ed9_tw
        if tw is None or getattr(self, "_p7_anchor", None) is None:
            return
        tw.tag_remove("sel", "1.0", "end")
        cur = tw.index("insert")
        anchor = self._p7_anchor
        if self._p7_mode == "visual_line":
            a_line, c_line = int(anchor.split(".")[0]), int(cur.split(".")[0])
            lo, hi = sorted((a_line, c_line))
            tw.tag_add("sel", f"{lo}.0", f"{hi}.end+1c")
        elif tw.compare(anchor, "<=", cur):
            tw.tag_add("sel", anchor, f"{cur}+1c")
        else:
            tw.tag_add("sel", cur, f"{anchor}+1c")
        self._p7_refresh()

    def _p7_yank(self):
        tw = self._ed9_tw
        if tw is None:
            return
        if self._p7_mode in ("visual", "visual_line"):
            try:
                _P7_REG["text"] = tw.get("sel.first", "sel.last")
            except tk.TclError:
                _P7_REG["text"] = ""
            self._p7_enter_normal()
        else:
            _P7_REG["text"] = tw.get("1.0", "end-1c")

    def _p7_delete(self):
        tw = self._ed9_tw
        if tw is None:
            return
        if self._p7_mode in ("visual", "visual_line"):
            try:
                _P7_REG["text"] = tw.get("sel.first", "sel.last")
                tw.delete("sel.first", "sel.last")
            except tk.TclError:
                pass
            self._p7_enter_normal()
        else:
            self._ed9_text_cancel()

    def _p7_paste(self):
        tw = self._ed9_tw
        if tw is None:
            return
        if not _P7_REG["text"]:
            notify("Nothing to paste", "Yank some text first (v to select, y to copy)", blocking=False)
            return
        tw.insert("insert", _P7_REG["text"])
        if self._p7_mode in ("visual", "visual_line"):
            self._p7_enter_normal()
        else:
            self._p7_refresh()

    def _p7_move(self, keysym):
        tw = self._ed9_tw
        if tw is None:
            return
        pos = tw.index("insert")
        line, col = (int(v) for v in pos.split("."))
        if keysym == "h":
            if col > 0:
                tw.mark_set("insert", f"{pos}-1c")
        elif keysym == "l":
            if tw.compare(pos, "<", "insert lineend"):
                tw.mark_set("insert", f"{pos}+1c")
        else:
            delta = 1 if keysym == "j" else -1
            try:
                tw.mark_set("insert", tw.tk.call("tk::TextUpDownLine", str(tw), delta))
            except tk.TclError:
                last_line = int(tw.index("end-1c").split(".")[0])
                if 1 <= line + delta <= last_line:
                    tw.mark_set("insert", f"insert {'+' if delta > 0 else '-'}1 lines")
        if self._p7_mode in ("visual", "visual_line"):
            self._p7_update_visual()
        else:
            self._p7_refresh()

    def _p7_escape(self, event=None):
        if getattr(self, "_p7_mode", "insert") == "insert":
            self._p7_enter_normal()
        else:
            self._ed9_text_cancel()
        return "break"

    def _p7_on_key(self, event):
        mode = getattr(self, "_p7_mode", "insert")
        if mode == "insert":
            return None
        k = event.keysym
        if k == "i":
            self._p7_enter_insert()
        elif k == "v":
            self._p7_enter_visual(False)
        elif k == "V":
            self._p7_enter_visual(True)
        elif k == "y":
            self._p7_yank()
        elif k == "d":
            self._p7_delete()
        elif k == "p":
            self._p7_paste()
        elif k in ("h", "j", "k", "l"):
            self._p7_move(k)
        elif k in _P7_NAV:
            if mode in ("visual", "visual_line"):
                self.root.after_idle(self._p7_update_visual)
            else:
                self.root.after_idle(self._p7_refresh)
            return None
        elif event.char:
            pass
        else:
            return None
        return "break"

    def _ed9_text_begin(self, box, edit=None):
        self._p7_mode = "insert"
        self._p7_anchor = None
        from . import constants as C
        self._ed9_text_confirm()
        x1, y1, x2, y2 = box
        s = self.display_scale or 1.0
        W = max(1, int((x2 - x1) * s))
        H = max(1, int((y2 - y1) * s))
        tkf = C.TEXT_FONT_FAMILIES.get(self._ed9_font, {}).get("tk", "serif")
        tw_2 = tk.Text(
            self.canvas, wrap="word", bd=0, highlightthickness=0,
            font=(tkf, max(10, int(self._ed9_size * 0.6 * s))), fg=self._ed2_color,
            insertbackground=self._ed2_color, exportselection=False,
        )
        if edit is not None:
            tw_2.insert("1.0", edit[0].get("text", ""))
            tw_2.mark_set("insert", "end-1c")
        tw_2.place(x=-(W + 300), y=0, width=W, height=H)
        tw_2.bind("<Return>", lambda e: (self._ed9_text_confirm(), "break")[1])
        tw_2.bind("<Shift-Return>", lambda e: None)
        tw_2.bind("<Escape>", lambda e: (self._ed9_text_cancel(), "break")[1])
        self._ed9_tw = tw_2
        self._ed9_tw_box = box
        self._ed9_tw_edit = edit
        self._ed9_tp_sig = None
        tw_2.focus_set()
        self._ed9_tp_tick()
        tw = self._ed9_tw
        if tw is not None:
            tw.bind("<Escape>", self._p7_escape)
            tw.bind("<Key>", self._p7_on_key)

    def _ed9_tp_render(self):
        from PIL import Image, ImageTk
        from .selector_app import SelectorApp as SA
        tw = self._ed9_tw
        if tw is None:
            return
        x1, y1, x2, y2 = self._ed9_tw_box
        w = max(1, int(round(x2 - x1)))
        h = max(1, int(round(y2 - y1)))
        s = self.display_scale or 1.0
        content = tw.get("1.0", "end-1c")
        ins = len(tw.get("1.0", "insert"))
        mode = getattr(self, "_p7_mode", "insert")
        sel = None
        if mode in ("visual", "visual_line"):
            try:
                sel = (len(tw.get("1.0", "sel.first")), len(tw.get("1.0", "sel.last")))
            except tk.TclError:
                sel = None
        phase = int(time.monotonic() * 2) % 2 if mode == "insert" else 0
        sig = (content, ins, phase, mode, sel, self._ed9_font, self._ed9_size, self._ed2_color, self._ed9_align, self._ed9_fill_t, (w, h), round(s, 4))
        if sig == self._ed9_tp_sig:
            return
        self._ed9_tp_sig = sig
        st = self._ed9_text_el(content, (x1, y1, x1 + w, y1 + h))
        patch = SA._render_element_local_patch(None, st, w, h).convert("RGBA")

        def runs(a, b):
            return [{"text": content[:a], "color": None, "font": None, "size": None},
                    {"text": content[a:b], "color": "#fe0001", "font": None, "size": None},
                    {"text": content[b:], "color": None, "font": None, "size": None}]

        def probe(a, b):
            st2 = dict(st)
            st2["bg_fill"] = "transparent"
            st2["runs"] = runs(a, b)
            return SA._render_element_local_patch(None, st2, w, h).convert("RGBA")

        caret = None
        try:
            st3 = dict(st)
            st3["bg_fill"] = "transparent"
            st3["runs"] = [{"text": content[:ins], "color": None, "font": None, "size": None},
                           {"text": "|", "color": "#fe0001", "font": None, "size": None}]
            caret = _ed9_marker_bbox(SA._render_element_local_patch(None, st3, w, h).convert("RGBA"))
        except Exception:
            caret = None
        block = None
        bands = []
        try:
            if mode == "normal":
                bb = None
                if ins < len(content) and content[ins] != "\n":
                    bb = _ed9_marker_bbox(probe(ins, ins + 1))
                if bb:
                    block = (bb[0] - 1, bb[1], bb[2] + 1, bb[3])
                elif caret:
                    cw = max(4, int(self._ed9_size * 0.5))
                    cxm = (caret[0] + caret[2]) / 2.0
                    block = (cxm, caret[1], cxm + cw, caret[3])
            elif mode in ("visual", "visual_line") and sel and sel[1] > sel[0]:
                mask = _p7_marker_mask(probe(sel[0], sel[1]))
                _rm = mask.resize((1, h), Image.BOX)
                rows = list(getattr(_rm, "get_flattened_data", _rm.getdata)())   # patch 45: getdata() removed in Pillow 14
                y = 0
                while y < h:
                    if rows[y] > 0:
                        y0 = y
                        while y < h and rows[y] > 0:
                            y += 1
                        box = mask.crop((0, y0, w, y)).getbbox()
                        if box:
                            bands.append((box[0] - 1, y0, box[2] + 1, y))
                    else:
                        y += 1
        except Exception:
            log.exception("Film text: could not mark the vim cursor/selection (non-fatal)")
        pw, ph = max(1, int(w * s)), max(1, int(h * s))
        shown = patch.resize((pw, ph), Image.LANCZOS) if (pw, ph) != patch.size else patch
        self._ed9_photo = ImageTk.PhotoImage(shown, master=self.canvas)
        cv = self.canvas
        cv.delete("ed9tp")
        cx1, cy1 = x1 * s, y1 * s
        cv.create_image(cx1, cy1, anchor="nw", image=self._ed9_photo, tags=("ed9tp",))
        cv.create_rectangle(cx1, cy1, cx1 + pw, cy1 + ph, outline="#00b4ff", width=2, dash=(5, 3), tags=("ed9tp",))
        for bx0, by0, bx1, by1 in bands:
            cv.create_rectangle(cx1 + bx0 * s, cy1 + by0 * s, cx1 + bx1 * s, cy1 + by1 * s,
                                fill="#3399ff", stipple="gray50", outline="", tags=("ed9tp",))
        if block:
            cv.create_rectangle(cx1 + block[0] * s, cy1 + block[1] * s, cx1 + block[2] * s, cy1 + block[3] * s,
                                fill="#cccccc", stipple="gray50", outline="#cccccc", tags=("ed9tp",))
        if mode == "insert" and phase == 0:
            if caret:
                lx = cx1 + (caret[0] + caret[2]) / 2.0 * s
                cv.create_line(lx, cy1 + caret[1] * s, lx, cy1 + caret[3] * s, fill=self._ed2_color, width=2, tags=("ed9tp",))
            else:
                cv.create_line(cx1 + 4 * s, cy1 + 2 * s, cx1 + 4 * s, cy1 + (2 + self._ed9_size) * s, fill=self._ed2_color, width=2, tags=("ed9tp",))
        cv.tag_raise("ed9tp")

    # ---- film snip: copy (default) or cut-with-hole ----

    def _p1_make_snip(self, shape, box, pts):
        from . import constants as C
        from PIL import Image, ImageColor
        n0 = len(self.film_edits)
        self._p1_make_snip_2(shape, box, pts)
        if str(getattr(C, "FILM_SNIP_MODE", "copy")).lower() != "cut":
            return
        if len(self.film_edits) != n0 + 1:
            return
        el = self.film_edits[-1]
        if el.get("mode") != "snip":
            return
        img = el["extracted_image"]
        bx1 = max(0, int(box[0]))
        by1 = max(0, int(box[1]))
        el["origin"] = (float(bx1), float(by1))
        color = str(getattr(C, "FILM_SNIP_HOLE_COLOR", "#000000"))
        try:
            rgb = ImageColor.getrgb(color)[:3]
        except ValueError:
            rgb = (0, 0, 0)
        fill = Image.new("RGBA", img.size, rgb + (255,))
        fill.putalpha(img.getchannel("A"))
        import uuid
        hole = {"mode": "snip", "shape": el.get("shape", "box"), "extracted_image": fill, "base_w": img.width, "base_h": img.height,
                "origin": (float(bx1), float(by1)), "w": float(img.width), "h": float(img.height), "rotation": 0.0,
                "orig_bounds": None, "segment_ids": [], "layer_id": el.get("layer_id"), "eid": uuid.uuid4().hex}
        idx = next(i for i, e in enumerate(self.film_edits) if e is el)
        self.film_edits.insert(idx, hole)
        acts = self._ed3_acts
        if acts and acts[-1][0] == "add" and acts[-1][1] is el:
            acts.pop()
        acts.append(("multi", [("add", hole), ("add", el)]))
        self._ed3_redo = []
        self._ed3_sel = el["eid"]
        self._after_edit_change()
        self._ed3_draw_ui()

    # ---- patch 8: snip handles for snip objects in every Snip mode, all objects only in Snip Select; Proportional by the readouts (_p8_*) ----

    def _p8_select_all(self):
        from . import constants as C
        return self._p1_snip_mode() == "select" and bool(getattr(C, "SNIP_SELECT_ALL_OBJECTS", True))

    def _ed9_cands(self, tool):
        order = {l["id"]: i for i, l in enumerate(self.film_layers)}
        out_2 = [e_3 for e_3 in self.film_edits if self._ed9_editable(e_3) and (tool == "snip" or self._ed9_family(e_3) == tool)]
        out_2.sort(key=lambda e_3: order.get(e_3.get("layer_id"), 0))
        out = out_2
        if tool == "snip" and not self._p8_select_all():
            out = [e for e in out if e.get("mode") == "snip"]
        return out

    # ---- patch 9: Proportional checkbox directly under the dimension readout, right-aligned, original styling (_p9_*) ----

    # ---- Proportional checkbox: a little gap under the dimension readout, default (image-mode) font ----

    # ---- splice model: ordered list of kept pieces, in SOURCE seconds, in OUTPUT order ----

    def _p10_state(self):
        self._ed5_state()
        if not hasattr(self, "_p10_segs"):
            self._p10_segs = None
            self._p10_mode = "trim"
            self._p10_sel = None
            self._p10_counter = 0
            self._p10_snap = None
            self._p10_strip = None
            self._p10_blocks = []
            self._p10_dragi = None
            self._p10_jumping = False

    def _p10_active_segs_2(self):
        self._p10_state()
        segs = self._p10_segs
        if not segs or _p10_trivial(segs, self._ed_dur()):
            return None
        return segs

    def _p10_splice_ui(self):
        self._p10_state()
        return bool(getattr(self, "_ed2_active", False)) and getattr(self, "_ed2_tool", None) == "trim" and self._p10_mode == "splice"

    def _p10_seed_2(self):
        self._p10_state()
        if self._p10_segs:
            return
        dur = self._ed_dur()
        s, e = 0.0, dur
        t = self._ed5_trim
        if t["start"] is not None or t["end"] is not None:
            s, e = self._ed_range(t)
        self._p10_counter += 1
        self._p10_segs = [{"id": self._p10_counter, "start": float(s), "end": float(e)}]
        self._p10_sel = None

    def _ed5_trim_vals(self):
        if self._p10_active_segs():
            return None
        return self._ed5_trim_vals_2()

    # ---- editing actions ----

    def _p10_set(self, new):
        self._ed5_set("splice", new)

    def _p10_set_mode(self, v):
        self._p10_state()
        self._p10_mode = v
        if v == "splice":
            self._p10_seed()
        self._ed5_refresh(True)

    def _p10_cut_here(self):
        self._p10_seed()
        fps = max(float(self.fps), 1.0)
        t = self.frame_index / fps
        segs = [dict(s) for s in self._p10_segs]
        for i, sg in enumerate(segs):
            if sg["start"] + 0.1 <= t <= sg["end"] - 0.1:
                self._p10_counter += 1
                right = {"id": self._p10_counter, "start": t, "end": sg["end"]}
                sg["end"] = t
                segs.insert(i + 1, right)
                self._p10_sel = right["id"]
                self._p10_set(segs)
                return
        notify("Can't cut here", "Put the playhead inside a kept piece, at least 0.1s from its edges", blocking=False)

    def _p10_sel_index(self):
        for i, s in enumerate(self._p10_segs or []):
            if s["id"] == self._p10_sel:
                return i
        return None

    def _p10_delete(self):
        i = self._p10_sel_index()
        if i is None:
            notify("Nothing selected", "Click a piece on the scrub bar or in the strip first", blocking=False)
            return
        if len(self._p10_segs) <= 1:
            notify("Can't delete", "At least one piece has to stay", blocking=False)
            return
        segs = [dict(s) for s in self._p10_segs]
        del segs[i]
        self._p10_sel = segs[min(i, len(segs) - 1)]["id"]
        self._p10_set(segs)

    def _p10_move(self, delta):
        i = self._p10_sel_index()
        if i is None:
            notify("Nothing selected", "Click a piece on the scrub bar or in the strip first", blocking=False)
            return
        j = i + delta
        if not 0 <= j < len(self._p10_segs):
            return
        segs = [dict(s) for s in self._p10_segs]
        segs[i], segs[j] = segs[j], segs[i]
        self._p10_set(segs)

    def _p10_reorder(self, i, to):
        segs = [dict(s) for s in self._p10_segs]
        item = segs.pop(i)
        segs.insert(to, item)
        if segs != self._p10_segs:
            self._p10_set(segs)

    def _p10_reset(self):
        self._p10_state()
        self._p10_sel = None
        self._p10_set(None)
        if self._p10_mode == "splice":
            self._p10_seed()
            self._ed5_refresh(True)

    def _p10_seek(self, t):
        fps = max(float(self.fps), 1.0)
        self._pause()
        self._show_frame(int(round(t * fps)))

    # ---- option rows: Mode dropdown (Trim | Splice) + the splice controls and the output strip ----

    def _p10_strip_draw(self, drop=None):
        cv = getattr(self, "_p10_strip", None)
        if cv is None or not self._p10_segs:
            return
        try:
            if not cv.winfo_exists():
                return
            cv.delete("all")
            segs = self._p10_segs
            h = int(float(cv.cget("height")))
            n = len(segs)
            total = max(0.001, _p10_total(segs))
            gap = 3
            avail = max(60, cv.winfo_width() - 4 - gap * (n - 1))
            raw = [max(14.0, avail * (s["end"] - s["start"]) / total) for s in segs]
            k = min(1.0, avail / max(1.0, sum(raw)))
            x = 2.0
            self._p10_blocks = []
            for i, sg in enumerate(segs):
                w = raw[i] * k
                sel = sg["id"] == self._p10_sel
                cv.create_rectangle(x, 2, x + w, h - 2, fill=_P10_COLORS[i % len(_P10_COLORS)],
                                    outline=("#ff9900" if sel else "#222222"), width=(3 if sel else 1))
                d = sg["end"] - sg["start"]
                label = f"{i + 1}" if w < 60 else f"{i + 1}  {_ed_fmt(sg['start'])}-{_ed_fmt(sg['end'])}  ({d:.1f}s)" if w > 190 else f"{i + 1}  {d:.1f}s"
                cv.create_text(x + w / 2, h / 2, text=label, fill="white", font=("", 8))
                self._p10_blocks.append((x, x + w))
                x += w + gap
            if drop is not None and self._p10_blocks:
                idx = max(0, min(len(self._p10_blocks), drop))
                dx = self._p10_blocks[idx][0] - 2 if idx < len(self._p10_blocks) else self._p10_blocks[-1][1] + 2
                cv.create_line(dx, 0, dx, h, fill="#ffffff", width=2)
        except tk.TclError:
            pass

    def _p10_block_at(self, x):
        for i, (a, b) in enumerate(self._p10_blocks):
            if a <= x <= b:
                return i
        return None

    def _p10_strip_press(self, event):
        i = self._p10_block_at(event.x)
        self._p10_dragi = i
        self._p10_dragx = event.x
        self._p10_dragmoved = False
        if i is None:
            return
        sg = self._p10_segs[i]
        self._p10_sel = sg["id"]
        self._p10_seek(sg["start"])
        self._p10_strip_draw()
        self._draw_scrub_playhead()

    def _p10_strip_motion(self, event):
        if self._p10_dragi is None:
            return
        if abs(event.x - self._p10_dragx) > 6:
            self._p10_dragmoved = True
        if self._p10_dragmoved:
            self._p10_strip_draw(drop=self._p10_drop_index(event.x))

    def _p10_drop_index(self, x):
        return sum(1 for (a, b) in self._p10_blocks if (a + b) / 2 < x)

    def _p10_strip_release(self, event):
        i = self._p10_dragi
        self._p10_dragi = None
        if i is None or not self._p10_dragmoved:
            return
        drop = self._p10_drop_index(event.x)
        to = drop - 1 if drop > i else drop
        to = max(0, min(len(self._p10_segs) - 1, to))
        if to != i:
            self._p10_reorder(i, to)
        else:
            self._p10_strip_draw()

    # ---- scrub bar: dim the removed parts, mark the cut lines, number the pieces ----

    # ---- preview playback: skip removed parts and follow the new order ----

    def _p10_jump(self, start):
        self._p10_jumping = False
        try:
            self._pause()
            self._show_frame(int(round(start * max(float(self.fps), 1.0))))
            self._toggle_play()
        except Exception:
            log.exception("Film splice: playback jump failed (non-fatal)")

    # ---- export: bake everything on the full timeline first, then cut the pieces out and join them in order ----

    # ---- patch 11: Trim = handles + Reset only; Splice = handles select a region, Cut makes it a piece, drag pieces along the bar (_p11_*) ----

    def _p11_vis(self):
        # visible pieces: the splice pieces clipped to the trim window, in output order
        segs = self._p10_segs or []
        tv = self._ed5_trim_vals_2()
        out = []
        for ui, sg in enumerate(segs):
            s, e = sg["start"], sg["end"]
            if tv and not sg.get("ins"):
                s, e = max(s, tv[0]), min(e, tv[1])
            if e - s > 0.05:
                out.append({"uid": sg["id"], "uidx": ui, "s": s, "e": e, "ins": sg.get("ins")})
        return out

    def _p10_active_segs(self):
        self._p10_state()
        segs = self._p10_segs
        if not segs or _p10_trivial(segs, self._ed_dur()):
            return None
        vis = self._p11_vis()
        if not vis:
            return None
        return [{"id": v["uid"], "start": v["s"], "end": v["e"], "ins": v.get("ins")} for v in vis]

    def _p10_seed(self):
        self._p10_state()
        if self._p10_segs:
            return
        self._p10_counter += 1
        self._p10_segs = [{"id": self._p10_counter, "start": 0.0, "end": float(self._ed_dur())}]

    def _p11_ensure_region(self):
        self._p11_seed()
        vis = self._p11_vis()
        reg = getattr(self, "_p11_reg", None)
        piece = None
        if reg:
            piece = next((v for v in vis if v["uid"] == reg["pid"]), None)
        if piece is None:
            if not vis:
                self._p11_reg = None
                return vis
            piece = next((v for v in vis if v["uid"] == self._p10_sel), vis[0])
            self._p11_reg = {"pid": piece["uid"], "a": piece["s"], "b": piece["e"]}
            self._p10_sel = piece["uid"]
            return vis
        reg["a"] = max(piece["s"], min(reg["a"], piece["e"]))
        reg["b"] = max(piece["s"], min(reg["b"], piece["e"]))
        if reg["b"] - reg["a"] < 0.05:
            reg["a"], reg["b"] = piece["s"], piece["e"]
        self._p10_sel = piece["uid"]
        return vis

    def _p11_select(self, uid):
        vis = self._p11_vis()
        piece = next((v for v in vis if v["uid"] == uid), None)
        if piece is not None:
            self._p11_reg = {"pid": uid, "a": piece["s"], "b": piece["e"]}
            self._p10_sel = uid

    def _p11_layout(self, vis, w):
        pad = 8
        span = max(1, w - 2 * pad)
        total = max(0.001, sum(v["e"] - v["s"] for v in vis))
        x = float(pad)
        lay = []
        for v in vis:
            wd = span * (v["e"] - v["s"]) / total
            lay.append(dict(v, x0=x, x1=x + wd))
            x += wd
        return lay

    def _p11_t_to_x(self, L, t):
        d = max(0.0001, L["e"] - L["s"])
        return L["x0"] + (max(L["s"], min(t, L["e"])) - L["s"]) / d * (L["x1"] - L["x0"])

    def _p11_x_to_t(self, L, x):
        d = L["x1"] - L["x0"]
        if d <= 0:
            return L["s"]
        return L["s"] + max(0.0, min(1.0, (x - L["x0"]) / d)) * (L["e"] - L["s"])

    def _p11_lay_now(self):
        w = self.scrub_canvas.winfo_width()
        return self._p11_layout(self._p11_ensure_region(), w)

    # ---- mouse: handles, piece select / drag ----

    def _p11_release(self, event):
        if not self._p10_splice_ui():
            was_8 = bool(getattr(self, "_ed_drag", None))
            was_3 = bool(getattr(self, "_ed_drag", None))
            was_range_drag = bool(getattr(self, "_ed_drag", None))
            self._ed_release(event)
            if was_range_drag and self._ed2_state():
                self._ed2_redisplay()
            if was_3 and getattr(self, "_ed2_active", False) and self._ed2_tool == "trim" and self._ed5_before is not None:
                now_2 = self._ed5_get("trim")
                if now_2 != self._ed5_before:
                    self._ed3_acts.append(("set", "trim", self._ed5_before, now_2))
                    self._ed3_redo = []
                self._ed5_before = None
                self._ed5_refresh(True)
            if not self._p2_audio_tool():
                return
            before_2 = getattr(self, "_p2_before", None)
            self._p2_before = None
            if was_8 and before_2 is not None:
                now_3 = self._ed5_get("alayers")
                if now_3 != before_2:
                    self._ed3_acts.append(("set", "alayers", before_2, now_3))
                    self._ed3_redo = []
                self._ed5_refresh(True)
            return
        self._scrubbing = False
        d = getattr(self, "_p11_drag", None)
        drop = getattr(self, "_p11_drop", None)
        self._p11_drag = None
        self._p11_drop = None
        if d and d["kind"] == "piece" and d.get("moved") and drop is not None:
            self._p11_move_piece(d["uid"], drop)
        else:
            self._draw_scrub_playhead()

    def _p11_move_piece(self, uid, drop):
        vis = self._p11_vis()
        others = [v["uid"] for v in vis if v["uid"] != uid]
        segs = [dict(s) for s in self._p10_segs]
        item = next((s for s in segs if s["id"] == uid), None)
        if item is None:
            return
        segs.remove(item)
        if not others:
            return
        if drop < len(others):
            pos = next(i for i, s in enumerate(segs) if s["id"] == others[drop])
        else:
            pos = next(i for i, s in enumerate(segs) if s["id"] == others[-1]) + 1
        segs.insert(pos, item)
        if segs != self._p10_segs:
            self._p10_set(segs)
        else:
            self._draw_scrub_playhead()

    # ---- actions ----

    def _p11_split(self, remove):
        self._p11_ensure_region()
        reg = self._p11_reg
        segs = [dict(s) for s in self._p10_segs]
        idx = next((i for i, s in enumerate(segs) if s["id"] == (reg or {}).get("pid")), None)
        if idx is None:
            return
        P = segs[idx]
        a, b = reg["a"], reg["b"]
        vis = self._p11_vis()
        piece = next(v for v in vis if v["uid"] == P["id"])
        whole = a <= piece["s"] + 0.05 and b >= piece["e"] - 0.05
        if not remove and whole:
            notify("Nothing to cut", "Drag a handle inward to mark the part you want, then Cut", blocking=False)
            return
        new = []
        left = right = mid = None
        if a - P["start"] > 0.05:
            self._p10_counter += 1
            left = dict(P, id=self._p10_counter, start=P["start"], end=a)
        self._p10_counter += 1
        mid = dict(P, id=self._p10_counter, start=a, end=b)
        if P["end"] - b > 0.05:
            self._p10_counter += 1
            right = dict(P, id=self._p10_counter, start=b, end=P["end"])
        new = [x for x in (left, None if remove else mid, right) if x is not None]
        result = segs[:idx] + new + segs[idx + 1:]
        tv = self._ed5_trim_vals_2()
        seen = 0
        for s in result:
            lo, hi = s["start"], s["end"]
            if tv and not s.get("ins"):
                lo, hi = max(lo, tv[0]), min(hi, tv[1])
            if hi - lo > 0.05:
                seen += 1
        if seen == 0:
            notify("Can't delete", "At least one piece has to stay", blocking=False)
            return
        keep = (mid["id"] if not remove else ((right or left) or {}).get("id"))
        self._p11_reg = None
        self._p10_sel = keep
        self._p10_set(result)
        if keep is not None:
            self._p11_select(keep)
        self._draw_scrub_playhead()

    def _p11_cut(self):
        self._p11_split(False)

    def _p11_delete(self):
        self._p11_split(True)

    def _p11_reset(self):
        self._p10_state()
        self._p11_reg = None
        self._p10_sel = None
        self._p10_set(None)
        if self._p10_splice_ui():
            self._p11_seed()
            self._ed5_refresh(True)

    def _p11_trim_reset(self):
        self._ed5_set("trim", (None, None))

    # ---- patch 13: film Resize matches the image Resize (Full Capture / Background, Scale, W x H, Color, corner handles) (_p13_*) ----

    def _p13_state(self):
        self._ed5_state()
        if not hasattr(self, "_p13_target"):
            from . import constants as C
            self._p13_target = "capture"
            self._p13_out = None
            c = str(getattr(C, "FILM_RESIZE_BG_DEFAULT", "black")).lower()
            self._p13_bg = c if c in ("black", "white") else "black"
            self._p13_cd = None
            self._p13_snap = None
            self._p13_scale_var = None

    def _ed9_out_dims(self):
        self._p13_state()
        if self._p13_out:
            return self._p13_out
        c = self._ed5_crop
        bw, bh = (c[2], c[3]) if c else (self.native_w, self.native_h)
        sc = self._ed5_scale
        k = (self.native_w / float(bw) if c else 1.0) if sc == "orig" else float(sc)
        return int(bw * k) // 2 * 2, int(bh * k) // 2 * 2

    def _p13_canvas_dims(self):
        c = self._ed5_crop
        return (int(c[2]), int(c[3])) if c else (int(self.native_w), int(self.native_h))

    def _p13_set_canvas(self, x, y, w, h):
        x, y = max(0, int(x)), max(0, int(y))
        w, h = _p13_even(w), _p13_even(h)
        new = (x, y, w, h)
        if x <= 0 and y <= 0 and w == int(self.native_w) // 2 * 2 and h == int(self.native_h) // 2 * 2:
            new = None
        self._ed5_set("crop", new)

    def _p13_pick_target(self, t):
        self._p13_state()
        self._p13_target = t
        self._ed5_refresh(True)

    def _p13_current_dims(self):
        if self._p13_target == "capture":
            return self._ed9_out_dims()
        return self._p13_canvas_dims()

    def _p13_apply_scale(self, f):
        w, h = self._p13_current_dims()
        self._p13_apply_size(w * float(f), h * float(f))

    def _p13_apply_dims(self, ws, hs):
        try:
            w, h = int(float(ws)), int(float(hs))
        except ValueError:
            notify("Resize", "Enter whole numbers for W and H", blocking=False)
            return
        self._p13_apply_size(w, h)

    def _p13_set_bg(self, c):
        self._ed5_set("bgc", c)

    # ---- corner handles in the Resize tool ----

    def _p13_resize_on(self):
        return bool(getattr(self, "_ed2_active", False)) and getattr(self, "_ed2_tool", None) == "resize" and self._ed9_tw is None

    def _ed9_crop_on(self):
        if self._p13_resize_on():
            return True
        if not self._ed2_active or self._ed9_tw is not None:
            return False
        t = self._ed2_tool
        return t in ("snip", "pen", "text", "shape") and self._ed9_mode(t) == "select"

    def _p13_move(self, event):
        if not self._p13_resize_on():
            g = getattr(self, "_ed9_g", None)
            if g and g.get("kind") in ("spray", "erase") and not getattr(self, "_ed8_split", None):
                from . import constants as C
                nx_16, ny_16, cx_19, cy_19 = self._ed2_xy(event)
                s_8 = self.display_scale or 1.0
                if g["kind"] == "spray":
                    lx, ly = g["lastburst"]
                    md = float(getattr(C, "SPRAY_MIN_BURST_DIST", 4)) / s_8
                    if (nx_16 - lx) ** 2 + (ny_16 - ly) ** 2 >= md * md:
                        self._p6_burst(g, nx_16, ny_16)
                else:
                    px, py = g["pts"][-1]
                    if abs(nx_16 - px) < 1 and abs(ny_16 - py) < 1:
                        return
                    g["pts"].append((nx_16, ny_16))
                    lx, ly = g["last"]
                    self._ed2_tmp.append(self.canvas.create_line(lx, ly, cx_19, cy_19, fill="#ffffff", width=max(1, self._ed2_width * s_8),
                                                                  capstyle="round", stipple="gray50"))
                    g["last"] = (cx_19, cy_19)
                return
            self._p1_drag(event)
            return
        d = self._p13_cd
        if not d:
            return
        s = self.display_scale or 1.0
        cx, cy = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        nx, ny = cx / s, cy / s
        fx, fy = d["fixed"]
        x1o, y1o, x2o, y2o = d["r0"]
        w0, h0 = max(1.0, x2o - x1o), max(1.0, y2o - y1o)
        sx = 1 if d["name"] in ("ne", "se") else -1
        sy = 1 if d["name"] in ("sw", "se") else -1
        bgm = self._p13_target == "background"
        if self._ed3_prop.get():
            f = max(abs(nx - fx) / w0, abs(ny - fy) / h0, 16.0 / min(w0, h0))
            if bgm:
                if sx < 0:
                    f = min(f, fx / w0)
                if sy < 0:
                    f = min(f, fy / h0)
            nx, ny = fx + sx * w0 * f, fy + sy * h0 * f
        if bgm:
            xa, xb = sorted((fx, nx))
            ya, yb = sorted((fy, ny))
            xa, ya = max(0.0, xa), max(0.0, ya)
            w, h = int(xb - xa) // 2 * 2, int(yb - ya) // 2 * 2
            if w < 16 or h < 16:
                return
            d["new"] = (int(xa), int(ya), w, h)
            self._ed5_crop = d["new"]
            self._ed5_draw_crop()
            self._ed9_draw_crop_handles()
        else:
            ow, oh = d["out0"]
            w = _p13_even(ow * abs(nx - fx) / w0)
            h = _p13_even(oh * abs(ny - fy) / h0)
            d["new"] = (w, h)
        self._ed9_dim(cx, cy, f"{d['new'][2] if bgm else d['new'][0]} x {d['new'][3] if bgm else d['new'][1]}")

    def _p13_release(self, event):
        if not self._p13_resize_on():
            g = getattr(self, "_ed9_g", None)
            if g and g.get("kind") in ("spray", "erase") and not getattr(self, "_ed8_split", None) and not self._ed3_drag:
                self._ed9_g = None
                self._ed2_clear_tmp()
                if g["kind"] == "spray":
                    dots = g["dots"]
                    if not dots:
                        return
                    x0 = min(x - r_3 for x, y, r_3 in dots) - 1.0
                    y0 = min(y - r_3 for x, y, r_3 in dots) - 1.0
                    x1 = max(x + r_3 for x, y, r_3 in dots) + 1.0
                    y1 = max(y + r_3 for x, y, r_3 in dots) + 1.0
                    el = {"mode": "spray", "dots": dots, "color": self._ed2_color, "origin": (x0, y0),
                          "w": max(2.0, x1 - x0), "h": max(2.0, y1 - y0)}
                    self._ed2_add(el, "pen")
                else:
                    self._p6_apply_erase(g["pts"], max(1, int(self._ed2_width)))
                return
            self._p1_release(event)
            return
        d = self._p13_cd
        self._p13_cd = None
        self._ed9_clear_dim()
        if not d:
            return
        new = d["new"]
        if self._p13_target == "background":
            self._ed5_crop = d["orig"]
            if new is not None:
                self._p13_set_canvas(*new)
        elif new is not None and tuple(new) != tuple(d["out0"]):
            self._ed5_set("pout", tuple(new))
        self._ed5_draw_crop()
        self._ed9_draw_crop_handles()

    def _ed9_crop_move(self, event):
        self._ed9_crop_move_2(event)
        c = self._ed5_crop
        if c and (c[0] < 0 or c[1] < 0):
            self._ed5_crop = (max(0, c[0]), max(0, c[1]), c[2], c[3])
            self._ed5_draw_crop()
            self._ed9_draw_crop_handles()

    # ---- preview: show the fill color where the canvas grows past the video ----

    def _ed5_draw_crop(self):
        self._ed5_draw_crop_2()
        try:
            self._p13_state()
            crop = self._ed5_crop
            if not crop:
                return
            s = self.display_scale or 1.0
            x, y, w, h = crop
            W, H = self.native_w * s, self.native_h * s
            x0, y0, x1, y1 = x * s, y * s, (x + w) * s, (y + h) * s
            tg = ("ed5crop",)
            if x1 > W:
                self.canvas.create_rectangle(W, y0, x1, y1, fill=self._p13_bg, outline="", tags=tg)
            if y1 > H:
                self.canvas.create_rectangle(x0, H, x1, y1, fill=self._p13_bg, outline="", tags=tg)
            self.canvas.tag_raise("ed3ui")
            self.canvas.tag_raise("ed9crop")
        except tk.TclError:
            pass

    # ---- patch 15: film Resize row reordered (Region, Color, Scale, dimensions), no Proportional box, no rebuild jitter ----

    def _ed2_build_rows(self):
        self._ed9_state()
        self._ed2_defaults()
        for w_9 in self._ed2_rows:
            try:
                w_9.destroy()
            except tk.TclError:
                pass
        self._ed2_rows = []
        main_row = self._tl_main_row
        container_2 = main_row.master
        bg_2 = "#222222"
        tool = self._ed2_tool
        ef = tk.Frame(main_row, bg=bg_2)
        ef.pack(side="left")
        for label, t in (("Audio", "audio"), ("Film", "trim"), ("Snip", "snip"), ("Draw", "pen"),
                         ("Text", "text"), ("Shape", "shape"), ("Resize", "resize"), ("Preview", "preview")):
            tk.Button(ef, text=label, pady=BUTTON_PADY, relief=("sunken" if tool == t else "raised"),
                      command=lambda t=t: self._ed9_set_tool(t)).pack(side="left", padx=2, pady=4)
        tk.Button(ef, text="Undo", pady=BUTTON_PADY, command=lambda: self._ed2_undo()).pack(side="left", padx=2, pady=4)
        tk.Button(ef, text="Redo", pady=BUTTON_PADY, command=lambda: self._ed2_redo_do()).pack(side="left", padx=2, pady=4)
        tk.Button(ef, text="Done", pady=BUTTON_PADY, command=lambda: self._ed2_exit(False)).pack(side="left", padx=(10, 2), pady=4)
        tk.Button(ef, text="Cancel", pady=BUTTON_PADY, command=lambda: self._ed2_exit(True)).pack(side="left", padx=2, pady=4)
        opts_3 = tk.Frame(container_2, bg=bg_2)
        try:
            opts_3.pack(side="top", fill="x", after=main_row)
        except tk.TclError:
            opts_3.pack(side="top", fill="x")
        self._ed2_rows = [ef, opts_3]
        if True:
            tk.Checkbutton(opts_3, text="Proportional", variable=self._ed3_prop, bg=bg_2, fg="white",
                           selectcolor="#444444", activebackground=bg_2, activeforeground="white").pack(side="right", padx=(2, 8), pady=4)
        getattr(self, "_ed9_opts_" + tool)(opts_3)
        sel = self._ed3_el(self._ed3_sel)
        if sel is not None and not (tool == "snip" or self._ed9_family(sel) == tool):
            self._ed3_sel = None
        self._ed2_bind_canvas(True)
        try:
            self._tl_fit()
            self.root.update_idletasks()
            need = max(ef.winfo_reqwidth() + self.dimension_label.winfo_reqwidth() + self.scale_label.winfo_reqwidth() + 40,
                       opts_3.winfo_reqwidth() + 20)
            if need > self.root.winfo_width():
                self.root.geometry(f"{need}x{self.root.winfo_height()}")
        except Exception:
            log.exception("Film edit: toolbar fit failed (non-fatal)")
        self._ed3_draw_ui()
        self._ed9_draw_crop_handles()
        try:
            opts_4 = self._ed2_rows[1]
            kids_2 = opts_4.winfo_children()
            if kids_2:
                sp = tk.Frame(opts_4, height=5, bg="#222222")
                sp.pack(side="top", fill="x", before=kids_2[0])
            self._tl_fit()
        except Exception:
            log.exception("Film edit: row spacing failed (non-fatal)")
        try:
            opts = self._ed2_rows[1]
            kids = opts.winfo_children()
            for i, w_10 in enumerate(kids):
                if isinstance(w_10, tk.Button) and getattr(w_10, "_tooltip_force", False) and w_10.cget("text") == "+":
                    menu = next((k_2 for k_2 in kids[i + 1:] if isinstance(k_2, tk.Menubutton)), None)
                    if menu is not None:
                        w_10.config(highlightthickness=int(float(menu.cget("highlightthickness"))), bd=int(float(menu.cget("bd"))),
                                 relief="raised", anchor="center")
                        w_10.pack_configure(fill="y")
                        menu.pack_configure(fill="y")
                    break
            for r in self._ed2_rows:
                self._p5_bind_menus(r)
            self._tl_fit()
        except Exception:
            log.exception("Film edit: dropdown tweaks failed (non-fatal)")
        try:
            def walk(w_11):
                for k in w_11.winfo_children():
                    yield k
                    yield from walk(k)
            for host_2 in (self._ed2_rows[1], self._tl_main_row):
                for w_11 in list(walk(host_2)):
                    if isinstance(w_11, tk.Checkbutton) and w_11.cget("text") == "Proportional":
                        w_11.destroy()
            bg = "#222222"
            container = self._tl_main_row.master
            cb = tk.Checkbutton(container, text="Proportional", variable=self._ed3_prop, bg=bg, fg="white",
                                selectcolor="#444444", activebackground=bg, activeforeground="white")
            cb.place(in_=self.dimension_label, relx=1.0, rely=1.0, anchor="ne", x=0, y=0)
            cb.lift()
            self._ed2_rows.append(cb)
            self._tl_fit()
        except Exception:
            log.exception("Film edit: could not place the Proportional checkbox (non-fatal)")
        try:
            for w_13 in self._tl_main_row.master.winfo_children():
                if isinstance(w_13, tk.Checkbutton) and w_13.cget("text") == "Proportional":
                    w_13.config(font="TkDefaultFont")
                    w_13.place_configure(y=4)
        except Exception:
            log.exception("Film edit: Proportional checkbox tweak failed (non-fatal)")
        try:
            if getattr(self, "_ed2_tool", None) == "resize":
                keep = []
                for w in list(self._ed2_rows):
                    if isinstance(w, tk.Checkbutton) and w.cget("text") == "Proportional":
                        w.destroy()
                    else:
                        keep.append(w)
                self._ed2_rows = keep
                for host in (self._tl_main_row.master, self._tl_main_row):
                    for w in host.winfo_children():
                        if isinstance(w, tk.Checkbutton) and w.cget("text") == "Proportional":
                            w.destroy()
        except Exception:
            log.exception("Film edit: could not hide the Proportional checkbox in Resize (non-fatal)")

    def _p15_move(self, event):
        if self._p13_resize_on():
            v = self._ed3_prop.get()
            self._ed3_prop.set(0)
            try:
                return self._p13_move(event)
            finally:
                self._ed3_prop.set(v)
        return self._p13_move(event)

    # ---- patch 16: film Resize previews the new size (dashed outline + stippled cut/extension) before Apply ----

    def _p16_prev_dims(self, ws, hs):
        try:
            w, h = int(float(ws)), int(float(hs))
            if w < 1 or h < 1:
                raise ValueError
        except (ValueError, TypeError):
            self._p16_clear()
            return
        self._p16_draw(w, h)

    def _p16_prev_scale(self, v):
        try:
            f = float(v)
        except (ValueError, TypeError):
            return
        cw, ch = self._p13_current_dims()
        self._p16_draw(cw * f, ch * f)

    def _ed5_refresh(self, rebuild=True):
        self._p16_clear()
        if (rebuild and getattr(self, "_ed2_active", False) and getattr(self, "_ed2_tool", None) == "resize"
                and len(getattr(self, "_ed2_rows", [])) >= 2 and self._ed9_tw is None):
            try:
                opts = self._ed2_rows[1]
                for w in list(opts.winfo_children()):
                    w.destroy()
                self._ed9_opts_resize(opts)
                self.root.update_idletasks()
                need = opts.winfo_reqwidth() + 20
                if need > self.root.winfo_width():
                    self.root.geometry(f"{need}x{self.root.winfo_height()}")
                self._draw_scrub_playhead()
                self._ed2_redisplay()
                self._ed9_draw_crop_handles()
                return
            except Exception:
                log.exception("Film edit: in-place Resize row refresh failed; doing a full rebuild")
        try:
            if rebuild and self._ed2_active:
                self._ed2_build_rows()
            self._draw_scrub_playhead()
            self._ed2_redisplay()
        except Exception:
            log.exception("Film edit: refresh failed (non-fatal)")

    def _p13_apply_size(self, w, h):
        self._p16_clear()
        self._p13_state()
        w, h = _p13_even(w), _p13_even(h)
        if self._p13_target == "capture":
            self._ed5_set("pout", (w, h))
        else:
            c = self._ed5_crop
            self._p13_set_canvas(c[0] if c else 0, c[1] if c else 0, w, h)

    def _ed2_press(self, event):
        if self._p13_resize_on():
            self._p16_clear()
        if not self._p13_resize_on():
            self._p1_press(event)
            try:
                if getattr(self, "_ed8_split", None):
                    return
                g = self._ed9_g
                if self._ed2_tool == "pen" and g and g.get("kind") == "pen" and self._ed9_pen_mode in ("spray", "eraser"):
                    nx, ny, cx_18, cy_18 = self._ed2_xy(event)
                    if self._ed9_pen_mode == "spray":
                        g["kind"] = "spray"
                        g["dots"] = []
                        self._p6_burst(g, nx, ny)
                    else:
                        g["kind"] = "erase"
            except Exception:
                log.exception("Film edit: spray/eraser press failed (non-fatal)")
            return
        self._p13_state()
        self._p13_cd = None
        cx, cy = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        name = self._ed9_hit_crop(cx, cy)
        if name:
            x1, y1, x2, y2 = self._ed9_crop_rect()
            opp = {"nw": (x2, y2), "ne": (x1, y2), "sw": (x2, y1), "se": (x1, y1)}[name]
            self._p13_cd = {"name": name, "fixed": opp, "r0": (x1, y1, x2, y2), "orig": self._ed5_crop,
                            "out0": self._ed9_out_dims(), "new": None}

    def _ed9_opts_resize(self, o):
        self._p13_state()
        bg = "#222222"
        self._pack_dropdown(o, "Region", [("capture", "Full Capture"), ("background", "Background")],
                            self._p13_target, lambda v: self._p13_pick_target(v), width=12)
        if self._p13_target == "background":
            self._pack_dropdown(o, "Color", [("white", "White"), ("black", "Black")],
                                self._p13_bg, lambda v: self._p13_set_bg(v), width=6)
        tk.Label(o, text="Scale", fg="white", bg=bg).pack(side="left", padx=(8, 2))
        var = tk.DoubleVar(master=self.root, value=1.0)
        self._p13_scale_var = var
        tk.Scale(o, from_=0.1, to=4.0, resolution=0.1, orient="horizontal", length=70, showvalue=False, bg=bg, fg="white",
                 troughcolor="#444444", highlightthickness=0, variable=var, command=lambda v: self._p16_prev_scale(v)).pack(side="left", padx=2, pady=4)
        tk.Label(o, textvariable=var, fg="white", bg=bg, width=3, anchor="w").pack(side="left", padx=(0, 2))
        tk.Button(o, text="Apply Scale", pady=BUTTON_PADY, command=lambda: self._p13_apply_scale(var.get())).pack(side="left", padx=2, pady=4)
        cw, ch = self._p13_current_dims()
        wv = tk.StringVar(master=self.root, value=str(cw))
        hv = tk.StringVar(master=self.root, value=str(ch))
        _pd = lambda *_a: self._p16_prev_dims(wv.get(), hv.get())
        wv.trace_add("write", _pd)
        hv.trace_add("write", _pd)
        tk.Label(o, text="W", fg="white", bg=bg).pack(side="left", padx=(14, 2))
        we = tk.Entry(o, textvariable=wv, width=6)
        we.pack(side="left", padx=2)
        tk.Label(o, text="H", fg="white", bg=bg).pack(side="left", padx=2)
        he = tk.Entry(o, textvariable=hv, width=6)
        he.pack(side="left", padx=2)
        go = lambda *_a: (self._p13_apply_dims(wv.get(), hv.get()), "break")[1]
        we.bind("<Return>", go)
        he.bind("<Return>", go)
        tk.Button(o, text="Apply Dimensions", pady=BUTTON_PADY, command=go).pack(side="left", padx=2, pady=4)

    # ---- patch 17: film Resize preview follows zoom ----

    def _p16_draw(self, w, h):
        _p17_prev_draw(self, w, h)
        self._p17_last = (w, h)
        self._p17_scale = self.display_scale

    def _p16_clear(self):
        self._p17_last = None
        _p17_prev_clear(self)

    # ---- patch 23: leader key, Space = play/pause, Effects placeholder (_p23_leader) ----

    def _p11_set_mode(self, v):
        return _p11_set_mode(self, v)

    # ---- patch 25: film Effects mode (layers of video effects) + plugin effects for audio/video (_p25_fx) ----

    def _p25_state(self):
        self._ed5_state()
        if not hasattr(self, "_p25_layers"):
            self._p25_layers = []
            self._p25_sel = None
            self._p25_counter = 0
            self._p25_newtype = None
            self._p25_snap = None
            self._p25_before = None

    def _p25_fx_ui(self):
        self._p10_state()
        return (bool(getattr(self, "_ed2_active", False)) and getattr(self, "_ed2_tool", None) == "trim"
                and self._p10_mode == "effects")

    def _p25_find(self, lid):
        self._p25_state()
        for l in self._p25_layers:
            if l["id"] == lid:
                return l
        return None

    def _p25_range_for(self, eff):
        if eff is None or eff.default_len is None:
            return None, None
        dur = self._ed_dur()
        d = min(float(eff.default_len), dur)
        t = self.frame_index / max(float(self.fps), 1.0)
        s = max(0.0, min(t, dur - d))
        return s, s + d

    def _p25_unique_name(self, label, ignore=None):
        names = {l["name"] for l in self._p25_layers if l["id"] != ignore}
        n = 1
        while f"{label} {n}" in names:
            n += 1
        return f"{label} {n}"

    def _p25_make(self, typ):
        self._p25_state()
        eff = _fx.get(typ)
        self._p25_counter += 1
        s, e = self._p25_range_for(eff)
        return {"id": f"fxlayer{self._p25_counter}", "name": self._p25_unique_name(eff.label if eff else typ),
                "type": typ, "start": s, "end": e, "params": eff.defaults() if eff else {}}

    def _ed5_get(self, attr):
        if attr == "fxlayers":
            self._p25_state()
            return _p25_copy(self._p25_layers)
        if attr == "pout":
            self._p13_state()
            return self._p13_out
        if attr == "bgc":
            self._p13_state()
            return self._p13_bg
        if attr == "splice":
            self._p10_state()
            return None if self._p10_segs is None else [dict(s) for s in self._p10_segs]
        if attr == "alayers":
            self._p2_state()
            return [dict(l) for l in self._p2_layers]
        if attr == "trim":
            return (self._ed5_trim["start"], self._ed5_trim["end"])
        if attr == "crop":
            return self._ed5_crop
        if attr == "scale":
            return self._ed5_scale
        return [dict(a) for a in self._ed5_audio]

    def _ed5_apply(self, attr, val):
        if attr == "fxlayers":
            self._p25_state()
            self._p25_layers = _p25_copy(val)
            return
        if attr == "pout":
            self._p13_state()
            self._p13_out = None if val is None else (int(val[0]), int(val[1]))
            return
        if attr == "bgc":
            self._p13_state()
            self._p13_bg = val
            return
        if attr == "splice":
            self._p10_state()
            self._p10_segs = None if val is None else [dict(s) for s in val]
            if self._p10_sel is not None and not any(s["id"] == self._p10_sel for s in (self._p10_segs or [])):
                self._p10_sel = None
            return
        if attr == "alayers":
            self._p2_state()
            self._p2_layers = [dict(l) for l in val]
            return
        if attr == "trim":
            self._ed5_trim["start"], self._ed5_trim["end"] = val
        elif attr == "crop":
            self._ed5_crop = val
        elif attr == "scale":
            self._ed5_scale = val
        elif attr == "audio":
            self._ed5_audio = [dict(a) for a in val]

    def _ed5_summary(self):
        parts = []
        tv = self._ed5_trim_vals()
        if tv:
            parts.append(f"trim {_ed_fmt(tv[0])}-{_ed_fmt(tv[1])}")
        if self._ed5_crop:
            parts.append(f"crop {self._ed5_crop[2]}x{self._ed5_crop[3]}")
        if self._ed5_scale_active():
            label = [l for l, v in _ED5_SCALES if v == self._ed5_scale]
            parts.append("size " + (label[0] if label else "custom"))
        if self._ed5_audio_dirty():
            parts.append("audio edited")
        s = ("Applied: " + ", ".join(parts)) if parts else ""
        self._p25_state()
        n = len(self._p25_layers)
        if not n:
            return s
        extra = f"{n} effect{'s' if n != 1 else ''}"
        return (s + ", " + extra) if s else ("Applied: " + extra)

    # ---- the selected effect's range uses the orange handles on the scrub bar (same as audio layers) ----

    def _ed_active(self):
        if self._p25_fx_ui():
            self._p25_state()
            return self._p25_find(self._p25_sel)
        if self._p10_splice_ui():
            return None
        if getattr(self, "_ed2_active", False) and getattr(self, "_ed2_tool", None) == "audio":
            self._p2_state()
            return self._p2_find(self._p2_sel)
        if getattr(self, "_ed2_active", False) and getattr(self, "_ed2_tool", None) == "trim":
            self._ed5_state()
            return self._ed5_trim
        self._ed_state()
        if self.film_active_layer_id is None:
            return None
        return self._ed_find(self.film_active_layer_id)

    # ---- editing actions ----

    def _p25_commit(self, rebuild=True, **kw):
        sel = self._p25_find(self._p25_sel)
        if sel is None:
            return
        new = self._ed5_get("fxlayers")
        for l in new:
            if l["id"] == sel["id"]:
                l.update(kw)
        self._ed5_set("fxlayers", new, rebuild=rebuild)

    def _p25_pick(self, v):
        self._p25_state()
        if v == "__new__":
            types = self._p25_type_options()
            cur = self._p25_find(self._p25_sel)
            typ = cur["type"] if cur is not None else (self._p25_newtype or (types[0][0] if types else None))
            if typ is None:
                return
            l = self._p25_make(typ)
            self._p25_sel = l["id"]
            self._ed5_set("fxlayers", self._ed5_get("fxlayers") + [l])
            if not getattr(self, "_p25_told", False):
                self._p25_told = True
                notify("Effects", "Effects are applied when you save or copy the film", blocking=False)
            return
        self._p25_sel = v
        self._ed5_refresh(True)

    def _p25_set_type(self, v):
        self._p25_state()
        sel = self._p25_find(self._p25_sel)
        self._p25_newtype = v
        if sel is None:
            self._p25_pick("__new__")     # choosing a Type with no layer selected makes the layer
            return
        eff = _fx.get(v)
        kw = {"type": v, "params": eff.defaults() if eff else {}}
        if sel["start"] is None and sel["end"] is None:
            kw["start"], kw["end"] = self._p25_range_for(eff)
        old = _fx.get(sel["type"])
        if old is not None and sel["name"].startswith(old.label + " ") and eff is not None:
            kw["name"] = self._p25_unique_name(eff.label, ignore=sel["id"])
        self._p25_commit(**kw)

    def _p25_set_param(self, key, val, rebuild=False):
        sel = self._p25_find(self._p25_sel)
        if sel is None:
            return
        params = dict(sel.get("params") or {})
        params[key] = val
        self._p25_commit(rebuild=rebuild, params=params)

    def _p25_reset(self):
        sel = self._p25_find(self._p25_sel)
        if sel is None:
            return
        eff = _fx.get(sel["type"])
        s, e = self._p25_range_for(eff)
        self._p25_commit(start=s, end=e, params=eff.defaults() if eff else {})

    def _p25_delete(self):
        sel = self._p25_find(self._p25_sel)
        if sel is None:
            return
        new = [l for l in self._ed5_get("fxlayers") if l["id"] != sel["id"]]
        self._p25_sel = None
        self._ed5_set("fxlayers", new)

    def _p25_type_options(self):
        return [(e.id, e.label) for e in _fx.effects("video")]

    def _p25_param_widgets(self, o, eff, values, on_change):
        """Generic controls for an effect's declared parameters. on_change(key, value, rebuild)."""
        bg = "#222222"
        for pr in eff.params:
            v = values[pr.key]
            if pr.kind == "choice":
                self._pack_dropdown(o, pr.label, list(pr.choices), v,
                                    lambda val, k=pr.key: on_change(k, val, False), width=max(5, max(len(c[1]) for c in pr.choices)))
                continue
            var = tk.StringVar(master=self.root, value=str(v))

            def commit(*_a, pr=pr, var=var):
                try:
                    val = pr.coerce(float(var.get()))
                except (tk.TclError, ValueError):
                    return
                on_change(pr.key, val, False)

            tk.Label(o, text=pr.label, fg="white", bg=bg).pack(side="left", padx=(8, 2))
            sp = tk.Spinbox(o, from_=pr.lo, to=pr.hi, increment=pr.step, width=5, textvariable=var, command=commit)
            sp.pack(side="left")
            sp.bind("<KeyRelease>", commit)

    def _p25_opts_effects(self, o):
        self._p25_state()
        bg = "#222222"
        self._pack_dropdown(o, "Mode", [("trim", "Trim"), ("splice", "Splice"), ("effects", "Effects")], "effects", self._p11_set_mode, width=7)
        types = self._p25_type_options()
        if not types:
            tk.Label(o, text="No video effects available", fg="#aaaaaa", bg=bg).pack(side="left", padx=8)
            return
        from .side_panel import small_button, attach_tooltip
        sel = self._p25_find(self._p25_sel)
        typ = sel["type"] if sel else (self._p25_newtype or types[0][0])
        if typ not in dict(types):
            types = types + [(typ, f"{typ} (missing)")]
        self._pack_dropdown(o, "Type", types, typ, self._p25_set_type, width=max(9, max(len(t[1]) for t in types)))
        layer_opts = [(l["id"], l["name"]) for l in self._p25_layers] + [("__new__", "+ New layer")]
        self._pack_dropdown(o, "Layer", layer_opts, sel["id"] if sel else "__new__", self._p25_pick, width=14)
        if sel is None:
            tk.Label(o, text="choose a Type, then + New layer", fg="#aaaaaa", bg=bg).pack(side="left", padx=8)
            return
        eff = _fx.get(sel["type"])
        if eff is not None:
            self._p25_param_widgets(o, eff, eff.values(sel.get("params")), self._p25_set_param)
        rb = small_button(o, "↺", self._p25_reset)
        rb.pack(side="left", padx=(8, 2), pady=4)
        attach_tooltip(rb, "Reset this layer's range and settings")
        rb._tooltip_force = True
        tk.Button(o, text="Delete", pady=BUTTON_PADY, command=self._p25_delete).pack(side="left", padx=4, pady=4)

    def _p25_opts_trim(self, o):
        self._p10_state()
        if self._p10_mode == "effects":
            return self._p25_opts_effects(o)
        self._p10_state()
        self._pack_dropdown(o, "Mode", [("trim", "Trim"), ("splice", "Splice"), ("effects", "Effects")], self._p10_mode, self._p11_set_mode, width=7)
        if self._p10_mode == "splice":
            self._p11_seed()
            tk.Button(o, text="Cut", pady=BUTTON_PADY, command=self._p11_cut).pack(side="left", padx=2, pady=4)
            tk.Button(o, text="Delete", pady=BUTTON_PADY, command=self._p11_delete).pack(side="left", padx=2, pady=4)
            tk.Button(o, text="Reset", pady=BUTTON_PADY, command=self._p11_reset).pack(side="left", padx=2, pady=4)
        else:
            tk.Button(o, text="Reset", pady=BUTTON_PADY, command=self._p11_trim_reset).pack(side="left", padx=2, pady=4)

    # ---- export: video effects go in after the text/shape overlays, before crop/scale/trim (source-time ranges) ----

    def _p25_video_chain(self, chain, last):
        self._p25_state()
        fps = float(self.fps or 25.0)
        pw, ph = int(self.native_w), int(self.native_h)      # the stream at this point (padded by Resize > Background)
        crop = getattr(self, "_ed5_crop", None)
        if crop:
            pw, ph = max(pw, crop[0] + crop[2]), max(ph, crop[1] + crop[3])
        for i, l in enumerate(self._p25_layers):
            eff = _fx.get(l["type"])
            if eff is None or eff.kind != "video":
                log.warning(f"Film effects: skipping layer {l.get('name')!r} (effect {l['type']!r} is not available)")
                continue
            s, e = self._ed_range(l)
            if e - s < 0.01:
                continue
            dst = f"fx{i}"
            try:
                frag = _fx.build_video(eff, l.get("params"), s, e, last, dst, width=pw, height=ph,
                                       fps=fps, duration=self._ed_dur())
            except Exception:
                log.exception(f"Film effects: layer {l.get('name')!r} ({eff.id}) could not be built - skipped")
                continue
            chain.append(frag)
            last = dst
        return last

    # ---- plugin effects for the Audio tool: they join the layer's Effect menu (None / Fade in / Fade out / ...) ----

    def _p2_default_range(self, fx):
        e = _p25_audio_eff(fx)
        if e is not None:
            return self._p25_range_for(e)
        if fx not in ("fade_in", "fade_out"):
            return None, None
        dur = self._ed_dur()
        d = min(1.0, dur)
        t = self.frame_index / max(float(self.fps), 1.0)
        s = max(0.0, min(t, dur - d))
        return s, s + d

    def _p3_set_fx(self, v):
        sel = self._p2_find(self._p2_sel)
        if sel is None:
            return
        kw = {"fx": v}
        e = _p25_audio_eff(v)
        if e is not None:
            kw["params"] = e.defaults()
        if v != "none" and sel["start"] is None and sel["end"] is None:
            kw["start"], kw["end"] = self._p3_default_range(v)
        self._p2_commit_edit(**kw)

    def _p3_filter(self, l, off):
        e = _p25_audio_eff(l.get("fx"))
        if e is None:
            s_10, e_4 = self._ed_range(l)
            t_3 = f"(t+{off:.3f})" if off else "t"
            g_6 = max(0.0, float(l.get("vol", 100))) / 100.0
            fx = l.get("fx", "none")
            if fx == "none":
                return f"volume={g_6:.3f}:enable='between({t_3},{s_10:.3f},{e_4:.3f})'"
            d = max(0.05, e_4 - s_10)
            ramp = f"({t_3}-{s_10:.3f})/{d:.3f}"
            if fx == "fade_out":
                ramp = f"(1-{ramp})"
            return f"volume='if(lt({t_3},{s_10:.3f}),1,if(gt({t_3},{e_4:.3f}),1,{g_6:.3f}*{ramp}))':eval=frame"
            return
        s, en = self._ed_range(l)
        t = f"(t+{off:.3f})" if off else "t"
        g = max(0.0, float(l.get("vol", 100))) / 100.0
        parts = []
        if abs(g - 1.0) > 0.001:
            parts.append(f"volume={g:.3f}:enable='between({t},{s:.3f},{en:.3f})'")
        try:
            parts.append(_fx.build_audio(e, l.get("params"), s, en, off))
        except Exception:
            log.exception(f"Film effects: audio layer {l.get('name')!r} ({e.id}) could not be built - skipped")
        return ",".join(parts) or "anull"

    def _p25_audio_param(self, key, val, rebuild=False):
        sel = self._p2_find(self._p2_sel)
        if sel is None:
            return
        params = dict(sel.get("params") or {})
        params[key] = val
        self._p2_commit_edit(rebuild=False, params=params)

    def _ed9_opts_audio(self, o):
        self._p4_opts_audio(o)
        try:
            self._p2_state()
            sel = self._p2_find(self._p2_sel)
            e_5 = _p25_audio_eff(sel.get("fx")) if sel else None
            if e_5 is None or not e_5.params:
                return
            delete = next((w for w in o.winfo_children() if isinstance(w, tk.Button) and w.cget("text") == "Delete"), None)
            self._p25_param_widgets(o, e_5, e_5.values(sel.get("params")), self._p25_audio_param)
            if delete is not None:
                delete.pack_forget()
                delete.pack(side="left", padx=(10, 2), pady=4)    # keep Delete last
        except Exception:
            log.exception("Film effects: could not add the plugin effect's settings to the Audio row (non-fatal)")

    # ---- patch 28: Play / Restart return to the front of the main row after Edit (_p28_exit_layout) ----

    # ---- patch 29: Preview tool + effects preview (_p29_preview) ----

    def _p29_state(self):
        if not hasattr(self, "_p29_overlay"):
            self._p29_overlay = None
            self._p29_sig = None
            self._p29_file = None
            self._p29_dir = None
            self._p29_busy = False
            self._p29_fast = False
            self._p29_btn = None

    def _p29_venc(self):
        """Encoder settings for the bake: normal, or fast/small while rendering a preview."""
        if getattr(self, "_p29_fast", False):
            return ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "30"]
        return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18"]

    def _p29_scale_post(self):
        if getattr(self, "_p29_fast", False):
            return ["scale='min(960,iw)':-2:flags=bilinear"]
        return []

    def _p29_signature(self):
        self._p25_state()
        self._p10_state()
        parts = []
        for name in ("film_edits", "film_layers", "_ed5_trim", "_ed5_crop", "_ed5_scale", "_ed5_audio",
                     "_p10_segs", "_p25_layers", "_p13_out", "_p13_bg"):
            v = getattr(self, name, None)
            if name == "_ed5_audio" and v and all((not a.get("mute")) and a.get("vol", 1.0) == 1.0 for a in v):
                v = []            # the per-track list is filled in lazily; all-default means the same as empty
            parts.append(repr(v))
        return "|".join(parts)

    def _p29_stale(self):
        """True when the edits changed since the file being shown was rendered."""
        return bool(self._ed4_flags()) and self._p29_signature() != self._p29_sig

    def _p29_has_fx_2(self):
        self._p25_state()
        return any((_fx.get(l["type"]) is not None and _fx.get(l["type"]).kind == "video") for l in self._p25_layers)

    def _p29_open(self, restart=False):
        """Show the preview overlay and play (rendering the edits first when they changed)."""
        self._p29_state()
        self._pause()
        ov = self._p29_overlay
        if ov is None or not ov.alive():
            ov = self._p29_overlay = _P29Overlay(self)
            self.root.update_idletasks()
        if self._p29_busy:
            return
        if not self._ed4_flags():            # nothing to bake: play the film itself
            ov.load(self.video_path)
            ov.play(0)
            return

        sig = self._p29_signature()

        def go(path):
            o = self._p29_overlay
            if o is None or not o.alive():
                return
            if o.path != str(path):
                o.load(path)
            o.play(0 if restart or o.pos >= o.total - 1 else None)

        if sig == self._p29_sig and self._p29_file and os.path.exists(self._p29_file):
            go(self._p29_file)
            return
        self._p29_busy = True
        ov.message("Rendering preview…")
        ov.path = None
        old_dir = self._p29_dir
        dest = Path(tempfile.mkdtemp(prefix="snippysnappy-preview-"))
        _p29_atexit.register(_p29_shutil.rmtree, str(dest), True)
        self._p29_fast = True

        def worker():
            out = None
            try:
                out = self._ed4_bake(dest)
            except Exception:
                log.exception("Film preview: rendering the edits failed")
            finally:
                self._p29_fast = False

            def after():
                self._p29_busy = False
                if old_dir:
                    _p29_shutil.rmtree(old_dir, ignore_errors=True)
                if out is None:
                    notify("Preview failed", "Could not render the edits - see the log", blocking=False)
                    _p29_shutil.rmtree(str(dest), ignore_errors=True)
                    self._p29_dir = self._p29_file = self._p29_sig = None
                    self._p29_close()
                    return
                self._p29_dir, self._p29_file, self._p29_sig = str(dest), str(out), sig
                go(out)

            try:
                self.root.after(0, after)
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def _p29_play(self):
        """Play / pause the preview (the Preview tool's Play button, Space, the leader)."""
        self._p29_state()
        ov = self._p29_overlay
        if ov is not None and ov.alive() and ov.path and (ov.playing or not self._p29_stale()):
            ov.toggle()
            return
        self._p29_open()

    def _p29_restart(self):
        self._p29_state()
        ov = self._p29_overlay
        if ov is not None and ov.alive() and ov.path and not self._p29_stale():
            ov.restart()
            return
        self._p29_open(restart=True)

    def _p29_sync_buttons(self):
        b = getattr(self, "_p29_btn", None)
        if b is None:
            return
        try:
            ov = self._p29_overlay
            b.config(text=("Pause" if (ov is not None and ov.alive() and ov.playing) else "Play"))
        except tk.TclError:
            self._p29_btn = None

    def _ed9_opts_preview(self, o):
        self._p29_state()
        bg = "#222222"
        self._p29_btn = tk.Button(o, text="Play", pady=BUTTON_PADY, command=self._p29_play)
        self._p29_btn.pack(side="left", padx=2, pady=4)
        tk.Button(o, text="Restart", pady=BUTTON_PADY, command=self._p29_restart).pack(side="left", padx=2, pady=4)
        tk.Label(o, text="plays your edits as they are now (low-resolution render; nothing is changed)", fg="#aaaaaa", bg=bg).pack(side="left", padx=8)
        self._p29_sync_buttons()

    def _p29_preview_tool(self):
        return bool(getattr(self, "_ed2_active", False)) and getattr(self, "_ed2_tool", None) == "preview"

    def _ed2_set_tool(self, tool):
        before = getattr(self, "_ed2_tool", None)
        if tool != "audio":
            self._p2_state()
            self._p2_sel = None
        _ed9_set_tool(self, tool)
        if before == "preview" and tool != "preview":
            self._p29_close()

    def _ed9_set_tool(self, tool):
        before = getattr(self, "_ed2_tool", None)
        _ed9_set_tool(self, tool)
        if before == "preview" and tool != "preview":
            self._p29_close()

    def _ed2_enter(self):
        self._p29_close()
        self._p25_state()
        was = self._ed2_active
        self._p13_state()
        was_11 = self._ed2_active
        self._p10_state()
        was_9 = self._ed2_active
        self._p2_state()
        was_6 = self._ed2_active
        self._ed9_state()
        if not self._ed2_active:
            self._ed9_tool_init = True
            self._ed2_tool = "audio"
        self._ed9_state()
        if not self._ed2_active and not getattr(self, "_ed9_tool_init", False):
            self._ed9_tool_init = True
            self._ed2_tool = "snip"
        self._ed5_state()
        was_4 = self._ed2_active
        self._ed3_state()
        was_2 = self._ed2_active
        self._noop_edit()
        if not was_2:
            self._ed3_acts, self._ed3_redo, self._ed3_sel = [], [], None
        if not self._ed3_keybound:
            self._ed3_keybound = True
            self.root.bind("<Delete>", self._ed3_key, add="+")
        if not was_4 and self._ed2_active:
            self._ed5_snap = (self._ed5_get("trim"), self._ed5_get("crop"), self._ed5_get("scale"), self._ed5_get("audio"))
        if not was_6 and self._ed2_active:
            self._p2_snap = self._ed5_get("alayers")
        if not was_9 and self._ed2_active:
            self._p10_snap = self._ed5_get("splice")
        if not was_11 and self._ed2_active:
            self._p13_snap = (self._p13_out, self._p13_bg)
        if not was and self._ed2_active:
            self._p25_snap = self._ed5_get("fxlayers")

    def _ed2_exit(self, cancel):
        if self._ed2_active:
            self._p29_close()
        was = self._ed2_active
        self._p25_state()
        was_13 = self._ed2_active
        self._p13_state()
        was_12 = self._ed2_active
        self._p10_state()
        was_10 = self._ed2_active
        self._p2_state()
        was_7 = self._ed2_active
        self._ed9_state()
        if self._ed9_tw is not None:
            if cancel:
                self._ed9_text_cancel()
            else:
                self._ed9_text_confirm()
        self._ed9_clear_g()
        self._ed5_state()
        was_5 = self._ed2_active
        self._ed3_state()
        _ed2_exit(self, cancel)
        self._ed3_acts, self._ed3_redo, self._ed3_sel, self._ed3_drag = [], [], None, None
        try:
            self.canvas.delete("ed3ui")
        except tk.TclError:
            pass
        if was_5 and cancel and self._ed5_snap:
            t, c, s, a = self._ed5_snap
            self._ed5_apply("trim", t)
            self._ed5_apply("crop", c)
            self._ed5_apply("scale", s)
            self._ed5_apply("audio", a)
            self._ed5_refresh(False)
        self._ed5_snap = None
        try:
            self.canvas.delete("ed9crop")
            self.canvas.delete("ed3ui")
        except tk.TclError:
            pass
        if was_7 and cancel and self._p2_snap is not None:
            self._ed5_apply("alayers", self._p2_snap)
            self._p2_sel = None
            self._ed5_refresh(False)
        self._p2_snap = None
        if was_10 and cancel:
            self._ed5_apply("splice", self._p10_snap)
            self._p10_sel = None
            self._ed5_refresh(False)
        self._p10_snap = None
        self._p10_strip = None
        if was_12 and cancel and self._p13_snap is not None:
            self._p13_out, self._p13_bg = self._p13_snap
            self._ed5_refresh(False)
        self._p13_snap = None
        if was_13 and cancel and self._p25_snap is not None:
            self._ed5_apply("fxlayers", self._p25_snap)
            self._p25_sel = None
            self._ed5_refresh(False)
        self._p25_snap = None
        if was:
            try:
                self._v3_layout()     # the Edit-exit re-packs the hidden buttons at the end; this puts Play/Restart first again
            except Exception:
                log.exception("Film viewer: could not restore the main row order (non-fatal)")

    # ---- patch 30: preview uses the normal scrub bar; "r" restarts (_p30_preview_scrub) ----

    def _p30_draw_scrub(self):
        ov = self._p30_active()
        if ov is None:
            if not self._p10_splice_ui():
                self._p2_draw()
                try:
                    self._p10_state()
                    segs = self._p10_segs
                    if not segs or not getattr(self, "_ed2_active", False) or not self.total_frames:
                        return
                    cv_6 = self.scrub_canvas
                    if cv_6.winfo_width() <= 1:
                        return
                    dur = self._ed_dur()
                    mid_8 = max(10, cv_6.winfo_height() // 2)
                    ui = self._p10_splice_ui()
                    tags_3 = ("scrub", "p10")
                    cur_3 = 0.0
                    gaps = []
                    for sg in sorted(segs, key=lambda s: s["start"]):
                        if sg["start"] > cur_3 + 0.01:
                            gaps.append((cur_3, sg["start"]))
                        cur_3 = max(cur_3, sg["end"])
                    if cur_3 < dur - 0.01:
                        gaps.append((cur_3, dur))
                    for a, b in gaps:
                        cv_6.create_rectangle(self._ed_x(a), mid_8 - 10, self._ed_x(b), mid_8 + 10, fill="#000000", stipple="gray50", outline="#aa3333", tags=tags_3)
                    if ui:
                        for i, sg in enumerate(segs):
                            xs, xe = self._ed_x(sg["start"]), self._ed_x(sg["end"])
                            sel_3 = sg["id"] == self._p10_sel
                            if sel_3:
                                cv_6.create_rectangle(xs, mid_8 - 11, xe, mid_8 + 11, outline="#ff9900", width=2, tags=tags_3)
                            for x_4 in (xs, xe):
                                cv_6.create_line(x_4, mid_8 - 12, x_4, mid_8 + 12, fill="#ffffff", dash=(2, 2), tags=tags_3)
                            if xe - xs > 14:
                                cv_6.create_text((xs + xe) / 2, mid_8 - 15, text=str(i + 1), fill="#ffb347" if sel_3 else "#cccccc", font=("", 7), tags=tags_3)
                    for item in cv_6.find_withtag("scrub"):
                        if cv_6.type(item) == "oval":
                            cv_6.tag_raise(item)
                except tk.TclError:
                    pass
                return
            try:
                self._update_time_label()
                cv_5 = self.scrub_canvas
                cv_5.delete("scrub")
                w_15 = cv_5.winfo_width()
                if w_15 <= 1 or not self.total_frames:
                    return
                lay_2 = self._p11_lay_now()
                if not lay_2:
                    return
                pad_3 = 8
                mid_7 = max(10, cv_5.winfo_height() // 2)
                tags_2 = ("scrub", "p11")
                t = self.frame_index / max(float(self.fps), 1.0)
                eps = 0.5 / max(float(self.fps), 1.0)
                px_4 = None
                for L_2 in lay_2:
                    if L_2["s"] - eps <= t <= L_2["e"] + eps and not L_2.get("ins"):
                        px_4 = self._p11_t_to_x(L_2, t)
                        break
                if px_4 is None:
                    nxt = [L_2 for L_2 in lay_2 if L_2["s"] > t and not L_2.get("ins")]
                    px_4 = (min(nxt, key=lambda L_2: L_2["s"])["x0"] if nxt else lay_2[-1]["x1"])
                for L_2 in lay_2:
                    cv_5.create_rectangle(L_2["x0"], mid_7 - 2, L_2["x1"], mid_7 + 2, fill=("#5c5c5c" if L_2.get("ins") else "#444444"), outline="", tags=tags_2)
                cv_5.create_rectangle(pad_3, mid_7 - 2, px_4, mid_7 + 2, fill="#00b4ff", outline="", tags=tags_2)
                for L_2 in lay_2[:-1]:
                    cv_5.create_rectangle(L_2["x1"] - 1, mid_7 - 2, L_2["x1"] + 1, mid_7 + 2, fill="#222222", outline="", tags=tags_2)
                sel = next((L_2 for L_2 in lay_2 if L_2["uid"] == self._p10_sel), None)
                if sel is not None:
                    cv_5.create_line(sel["x0"] + 1, mid_7 + 6, sel["x1"] - 1, mid_7 + 6, fill="#aaaaaa", tags=tags_2)
                reg_2 = self._p11_reg
                cur_2 = next((L_2 for L_2 in lay_2 if L_2["uid"] == reg_2["pid"]), None) if reg_2 else None
                if cur_2 is not None:
                    xa_2, xb_2 = self._p11_t_to_x(cur_2, reg_2["a"]), self._p11_t_to_x(cur_2, reg_2["b"])
                    cv_5.create_line(xa_2, mid_7 - 7, xb_2, mid_7 - 7, fill="#aaaaaa", tags=tags_2)
                    for x_3 in (xa_2, xb_2):
                        cv_5.create_line(x_3, mid_7 - 7, x_3, mid_7 + 4, fill="#ffffff", width=2, tags=tags_2)
                drop = getattr(self, "_p11_drop", None)
                d = getattr(self, "_p11_drag", None)
                if d and d.get("kind") == "piece" and d.get("moved") and drop is not None:
                    others = [L_2 for L_2 in lay_2 if L_2["uid"] != d["uid"]]
                    if others:
                        dx = others[drop]["x0"] if drop < len(others) else others[-1]["x1"]
                        cv_5.create_line(dx, mid_7 - 9, dx, mid_7 + 9, fill="#ffffff", width=2, tags=tags_2)
                cv_5.create_oval(px_4 - 6, mid_7 - 6, px_4 + 6, mid_7 + 6, fill="white", outline="#888888", tags=tags_2)
            except tk.TclError:
                pass
            return
        try:
            self.time_label.configure(text=f"{self._fmt_time(ov.pos / ov.fps)} / {self._fmt_time(ov.total / ov.fps)}")
            cv_2 = self.scrub_canvas
            cv_2.delete("scrub")
            w = cv_2.winfo_width()
            if w <= 1:
                return
            pad = 8
            px = pad + int(ov.pos / max(1, ov.total - 1) * max(1, w - 2 * pad))
            mid_2 = max(10, cv_2.winfo_height() // 2)
            cv_2.create_rectangle(pad, mid_2 - 2, w - pad, mid_2 + 2, fill="#444444", outline="", tags=("scrub",))
            cv_2.create_rectangle(pad, mid_2 - 2, px, mid_2 + 2, fill="#00b4ff", outline="", tags=("scrub",))
            cv_2.create_oval(px - 6, mid_2 - 6, px + 6, mid_2 + 6, fill="white", outline="#888888", tags=("scrub",))
        except tk.TclError:
            pass

    def _p29_close(self):
        self._p29_state()
        ov, self._p29_overlay = self._p29_overlay, None
        if ov is not None:
            ov.close()
        self._p29_sync_buttons()
        try:
            self._draw_scrub_playhead()
        except Exception:
            pass

    # ---- patch 31: splice insertions - another film, a still image, a black/white gap, or a copy of a region (_p31_*) ----

    def _p31_ins_pieces(self):
        return [s for s in (self._p10_segs or []) if s.get("ins")]

    def _p31_live_segs(self):
        """The source pieces only - what the live player can jump between. Inserted pieces show in Preview / on export."""
        segs = self._p10_active_segs()
        if not segs:
            return None
        src = [s for s in segs if not s.get("ins")]
        if not src or (len(src) == 1 and _p10_trivial(src, self._ed_dur())):
            return None
        return src

    def _p31_ask_duration(self, title, default=3.0):
        from tkinter import simpledialog
        v = simpledialog.askfloat(title, "Length in seconds:", parent=self.root, initialvalue=default, minvalue=0.1, maxvalue=3600.0)
        return None if v is None else float(v)

    def _p31_make_spec(self, what):
        """(ins-dict, length) for what = file | still | black | white | copy, or None when cancelled / not possible."""
        from tkinter import filedialog
        start_dir = str(Path(self.video_path).parent)
        if what == "file":
            path = filedialog.askopenfilename(
                parent=self.root, title="Insert a film", initialdir=start_dir,
                filetypes=[("Video", "*.mkv *.mp4 *.webm *.mov *.avi"), ("All files", "*")])
            if not path:
                return None
            try:
                w, h, fps, total = probe_video(path)
                dur = total / max(float(fps), 1.0)
            except Exception:
                log.exception("Film splice: could not read the film to insert")
                notify("Can't insert", "That file could not be read as a film", blocking=False)
                return None
            return {"kind": "file", "path": str(path)}, float(dur)
        if what == "still":
            path = filedialog.askopenfilename(
                parent=self.root, title="Insert a still image", initialdir=start_dir,
                filetypes=[("Images", "*.png *.jpg *.jpeg *.webp *.bmp *.gif"), ("All files", "*")])
            if not path:
                return None
            try:
                from PIL import Image
                Image.open(path).verify()
            except Exception:
                log.exception("Film splice: could not read the image to insert")
                notify("Can't insert", "That file could not be read as an image", blocking=False)
                return None
            d = self._p31_ask_duration("Still image", 3.0)
            return None if d is None else ({"kind": "still", "path": str(path)}, d)
        if what in ("black", "white"):
            d = self._p31_ask_duration("White gap" if what == "white" else "Black gap", 2.0)
            return None if d is None else ({"kind": "blank", "color": what}, d)
        return None

    def _p31_insert(self, what):
        """Insert at the chosen scrub handle of the selected piece (splitting the piece there when the handle is inside it)."""
        self._p10_state()
        vis = self._p11_ensure_region()
        reg = getattr(self, "_p11_reg", None)
        if not vis or not reg:
            notify("Can't insert", "Select a piece first", blocking=False)
            return
        segs = [dict(s) for s in self._p10_segs]
        idx = next((i for i, s in enumerate(segs) if s["id"] == reg["pid"]), None)
        if idx is None:
            return
        P = segs[idx]
        piece = next(v for v in vis if v["uid"] == P["id"])
        at = getattr(self, "_p31_at", "a")
        t = reg[at]
        if what == "clip":
            spec, start, end = self._p32_clip_for_insert()
            if spec is None:
                return
        elif what == "copy":
            a, b = reg["a"], reg["b"]
            if b - a < 0.05:
                return
            spec = P.get("ins")
            start, end = a, b
        else:
            made = self._p31_make_spec(what)
            if made is None:
                return
            spec, dur = made
            start, end = 0.0, float(dur)
        self._p10_counter += 1
        new = dict({"id": self._p10_counter, "start": start, "end": end}, **({"ins": spec} if spec else {}))
        if t <= piece["s"] + 0.05:
            result = segs[:idx] + [new] + segs[idx:]
        elif t >= piece["e"] - 0.05:
            result = segs[:idx + 1] + [new] + segs[idx + 1:]
        else:
            self._p10_counter += 1
            left = dict(P, id=self._p10_counter, end=t)
            self._p10_counter += 1
            right = dict(P, id=self._p10_counter, start=t)
            result = segs[:idx] + [left, new, right] + segs[idx + 1:]
        self._p11_reg = None
        self._p10_set(result)
        self._p11_select(new["id"])
        self._draw_scrub_playhead()
        notify("Inserted", "Press Play in the Preview tool to see it (the live player skips inserted pieces)", blocking=False)

    def _p31_state_ok(self):
        self._p10_state()
        if not hasattr(self, "_p31_at"):
            self._p31_at = "a"

    def _p31_set_at(self, v):
        self._p31_at = "b" if v == "b" else "a"

    def _p31_menu_2(self, btn):
        m = tk.Menu(self.root, tearoff=0)
        for label, what in (("Film file…", "file"), ("Still image…", "still"), ("Black gap…", "black"),
                            ("White gap…", "white"), ("Copy of the marked region", "copy")):
            m.add_command(label=label, command=lambda w=what: self._p31_insert(w))
        try:
            m.tk_popup(btn.winfo_rootx(), btn.winfo_rooty() + btn.winfo_height())
        finally:
            m.grab_release()

    def _ed9_opts_trim(self, o):
        self._p25_opts_trim(o)
        if self._p10_mode != "splice":
            return
        self._p31_state_ok()
        b = tk.Button(o, text="Insert ▾", pady=BUTTON_PADY)
        b.configure(command=lambda b=b: self._p31_menu(b))
        b.pack(side="left", padx=2, pady=4)
        self._pack_dropdown(o, "At", [("a", "Left handle"), ("b", "Right handle")], self._p31_at, self._p31_set_at, width=11)

    def _ed4_flags(self):
        self._ed5_state()
        flags_2 = []
        if getattr(self, "film_edits", None):
            flags_2.append("overlays")
        flags_3 = flags_2
        if self._ed5_trim_vals() is not None:
            flags_3.append("trim")
        if self._ed5_crop:
            flags_3.append("crop")
        if self._ed5_scale_active():
            flags_3.append("scale")
        if self._ed5_audio_dirty():
            flags_3.append("audio")
        flags_4 = flags_3
        if self._p10_active_segs():
            flags_4.append("splice")
        flags_5 = flags_4
        self._p13_state()
        if self._p13_out:
            flags_5.append("resize")
        flags_6 = flags_5
        self._p25_state()
        if any((_fx.get(l["type"]) is not None) for l in self._p25_layers):
            flags_6.append("effects")
        flags = flags_6
        self._p10_state()
        if self._p31_ins_pieces() and self._p10_active_segs() and "splice" not in flags:
            flags.append("splice")
        return flags

    # ---- export: bake the source pieces as before, then append / interleave the inserted ones ----

    def _ed4_bake(self, dest_dir):
        segs = self._p10_active_segs()
        if not segs or not any(s.get("ins") for s in segs):
            segs_2 = self._p10_active_segs()
            if not segs_2:
                return self._p13_bake(dest_dir)
            segs_2 = [dict(s_11) for s_11 in segs_2]
            mid_dir_2 = Path(dest_dir) / "pass1"
            mid_dir_2.mkdir(parents=True, exist_ok=True)
            mid_6 = self._p13_bake(mid_dir_2)
            src_2 = Path(self.video_path)
            out_3 = Path(dest_dir) / f"{src_2.stem}-edited.mkv"
            audio = _fa_audio_count(mid_6) > 0
            cmd_2 = ["ffmpeg", "-y", "-v", "error"]
            parts_2, ins_2 = [], ""
            for i_2, sg_2 in enumerate(segs_2):
                cmd_2 += ["-ss", f"{sg_2['start']:.3f}", "-t", f"{sg_2['end'] - sg_2['start']:.3f}", "-i", str(mid_6)]
                parts_2.append(f"[{i_2}:v:0]setpts=PTS-STARTPTS[v{i_2}]")
                if audio:
                    parts_2.append(f"[{i_2}:a:0]asetpts=PTS-STARTPTS[a{i_2}]")
                    ins_2 += f"[v{i_2}][a{i_2}]"
                else:
                    ins_2 += f"[v{i_2}]"
            parts_2.append(f"{ins_2}concat=n={len(segs_2)}:v=1:a={1 if audio else 0}[vout]" + ("[aout]" if audio else ""))
            cmd_2 += ["-filter_complex", ";".join(parts_2), "-map", "[vout]"]
            if audio:
                cmd_2 += ["-map", "[aout]", "-c:a", "aac", "-b:a", "192k"]
            cmd_2 += self._p29_venc() + ["-pix_fmt", "yuv420p", str(out_3)]
            r_2 = subprocess.run(cmd_2, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            if r_2.returncode != 0:
                raise RuntimeError("ffmpeg splice failed: " + r_2.stderr.decode("utf-8", "replace")[-800:])
            try:
                mid_6.unlink()
            except OSError:
                pass
            log.info(f"Film viewer: spliced {len(segs_2)} piece(s) -> {out_3}")
            return out_3
            return
        segs = [dict(s) for s in segs]
        mid_dir = Path(dest_dir) / "pass1"
        mid_dir.mkdir(parents=True, exist_ok=True)
        mid = self._p13_bake(mid_dir)
        src = Path(self.video_path)
        out = Path(dest_dir) / f"{src.stem}-edited.mkv"
        W, H, FPS, _n = probe_video(mid)
        W, H = W // 2 * 2, H // 2 * 2
        mid_audio = _fa_audio_count(mid) > 0
        pieces = []
        for sg in segs:
            ins = sg.get("ins") or {"kind": "mid"}
            dur = max(0.05, sg["end"] - sg["start"])
            has_a = (mid_audio if ins["kind"] == "mid"
                     else (_fa_audio_count(ins["path"]) > 0 if ins["kind"] == "file" else False))
            pieces.append((sg, ins, dur, has_a))
        any_audio = any(p[3] for p in pieces)
        cmd = ["ffmpeg", "-y", "-v", "error"]
        parts, joined = [], ""
        for i, (sg, ins, dur, has_a) in enumerate(pieces):
            k = ins["kind"]
            if k == "mid":
                cmd += ["-ss", f"{sg['start']:.3f}", "-t", f"{dur:.3f}", "-i", str(mid)]
            elif k == "file":
                cmd += ["-ss", f"{sg['start']:.3f}", "-t", f"{dur:.3f}", "-i", ins["path"]]
            elif k == "still":
                cmd += ["-loop", "1", "-framerate", f"{FPS:.5f}", "-t", f"{dur:.3f}", "-i", ins["path"]]
            else:
                cmd += ["-f", "lavfi", "-t", f"{dur:.3f}", "-i", f"color=c={ins.get('color', 'black')}:s={W}x{H}:r={FPS:.5f}"]
            parts.append(
                f"[{i}:v:0]scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black,"
                f"setsar=1,fps={FPS:.5f},format=yuv420p,setpts=PTS-STARTPTS[v{i}]")
            if any_audio:
                if has_a:
                    parts.append(f"[{i}:a:0]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                                 f"apad=whole_dur={dur:.3f},atrim=0:{dur:.3f},asetpts=PTS-STARTPTS[a{i}]")
                else:
                    parts.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{dur:.3f},asetpts=PTS-STARTPTS[a{i}]")
                joined += f"[v{i}][a{i}]"
            else:
                joined += f"[v{i}]"
        parts.append(f"{joined}concat=n={len(pieces)}:v=1:a={1 if any_audio else 0}[vout]" + ("[aout]" if any_audio else ""))
        cmd += ["-filter_complex", ";".join(parts), "-map", "[vout]"]
        if any_audio:
            cmd += ["-map", "[aout]", "-c:a", "aac", "-b:a", "192k"]
        cmd += self._p29_venc() + ["-pix_fmt", "yuv420p", str(out)]
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if r.returncode != 0:
            raise RuntimeError("ffmpeg splice insert failed: " + r.stderr.decode("utf-8", "replace")[-800:])
        try:
            mid.unlink()
        except OSError:
            pass
        log.info(f"Film viewer: spliced {len(pieces)} piece(s) ({sum(1 for p in pieces if p[1]['kind'] != 'mid')} inserted) -> {out}")
        return out

    # ---- patch 32: take a region in one film window, insert it into another; splice bar styled like the normal scrub bar (_p32_*) ----

    def _p32_yank_2(self):
        """Take the region between the scrub handles (of the selected piece) so another film window can insert it."""
        self._p10_state()
        self._p11_ensure_region()
        reg = getattr(self, "_p11_reg", None)
        P = next((s for s in (self._p10_segs or []) if reg and s["id"] == reg["pid"]), None)
        if P is None or reg["b"] - reg["a"] < 0.05:
            notify("Nothing to take", "Select a piece and set the handles around the part you want", blocking=False)
            return
        spec = dict(P["ins"]) if P.get("ins") else {"kind": "file", "path": str(self.video_path)}
        clip = {"ins": spec, "start": float(reg["a"]), "end": float(reg["b"])}
        path = _p32_clip_file()
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(_p32_json.dumps(clip), encoding="utf-8")
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except OSError:
            log.exception("Film splice: could not store the taken region")
            notify("Could not take the region", "See the log", blocking=False)
            return
        notify("Region taken", _p32_clip_label(clip) + " - open another film window and use Insert > Insert the taken region", blocking=False)

    def _p32_clip_for_insert(self):
        clip = FilmApp._p32_clip_read()
        if clip is None:
            notify("Nothing taken", "In a film window, set the scrub handles and use Insert > Take the marked region first", blocking=False)
            return None, 0.0, 0.0
        return dict(clip["ins"]), float(clip["start"]), float(clip["end"])

    def _p31_menu(self, btn):
        m = tk.Menu(self.root, tearoff=0)
        for label, what in (("Film file…", "file"), ("Still image…", "still"), ("Black gap…", "black"),
                            ("White gap…", "white"), ("Copy of the marked region", "copy")):
            m.add_command(label=label, command=lambda w=what: self._p31_insert(w))
        m.add_separator()
        clip = FilmApp._p32_clip_read()
        m.add_command(label="Take the marked region (for another window)", command=self._p32_yank)
        m.add_command(label=("Insert the taken region: " + _p32_clip_label(clip)) if clip else "Insert the taken region (none taken yet)",
                      state=("normal" if clip else "disabled"), command=lambda: self._p31_insert("clip"))
        try:
            m.tk_popup(btn.winfo_rootx(), btn.winfo_rooty() + btn.winfo_height())
        finally:
            m.grab_release()

    def _p31_insert_cmd(self, what):
        self._p31_state_ok()
        if getattr(self, "_ed2_tool", None) != "trim":
            self._ed9_set_tool("trim")
        if self._p10_mode != "splice":
            self._p11_set_mode("splice")
        if what == "take":
            self._p32_yank()
        else:
            self._p31_insert(what)

    # ---- patch 33: film - main Play previews every edit the live player can't show; free middle-drag pan; windows stay on screen (_p33_*) ----

    def _p29_has_fx(self):
        """True when the main Play / Restart should use the rendered preview: edits the live player can't show
    (text / shapes / snips / drawing, crop, resize, effects, inserted splice pieces). Trim, splice and audio play live."""
        if getattr(self, "_ed2_active", False):
            return False
        try:
            if set(self._ed4_flags()) & {"overlays", "crop", "scale", "resize", "effects"}:
                return True
            return bool(self._p31_ins_pieces() and self._p10_active_segs())
        except Exception:
            log.exception("Film preview: could not decide whether the edits need a preview (non-fatal)")
            return False

    def _z_pan_drag(self, event):
        self.canvas.scan_dragto(event.x, event.y, gain=1)
        _p33_pan_clamp(self.canvas)

    def _p33_init(self, *args, **kwargs):
        try:
            if _fx.ensure_loaded():
                _p25_sync_audio_types()
        except Exception:
            log.exception("Film effects: plugin loading failed (non-fatal)")
        _p20_init(self, *args, **kwargs)
        try:
            from .leader import install_leader, typing_focus
            install_leader(self, self.root, "film")

            def on_space(event):
                from . import constants as _c
                if not getattr(_c, "FILM_SPACE_PLAYPAUSE", True):
                    return None
                if typing_focus(self.root):
                    return None
                try:
                    focus = self.root.focus_get()
                    if focus is not None and focus.winfo_class() in ("Button", "Checkbutton", "Radiobutton", "Scale", "Menubutton"):
                        return None   # those handle Space themselves
                except (KeyError, tk.TclError):
                    pass
                self._toggle_play()
                return "break"

            self.root.bind("<space>", on_space, add="+")
        except Exception:
            log.exception("Could not install the leader key / Space binding in the film window (non-fatal)")
        try:
            from .leader import typing_focus

            def on_escape(event):
                if getattr(self, "_p29_overlay", None) is None or typing_focus(self.root):
                    return None
                self._p29_close()
                return "break"

            def on_r(event):
                from . import constants as _c
                if not getattr(_c, "FILM_R_RESTART", True) or typing_focus(self.root):
                    return None
                try:
                    focus = self.root.focus_get()
                    if focus is not None and focus.winfo_class() in ("Scale", "Menubutton"):
                        return None
                except (KeyError, tk.TclError):
                    pass
                self._restart()
                return "break"

            self.root.bind("<Escape>", on_escape, add="+")
            self.root.bind("<r>", on_r, add="+")
        except Exception:
            log.exception("Could not install the R (restart) / Escape (close preview) keys in the film window (non-fatal)")
        try:
            self.canvas.configure(confine=False)
        except Exception:
            log.exception("Film viewer: could not enable free panning (non-fatal)")
        self._p33_install_guard()

    # ---- patch 37: film eraser shows the removal live (like the image version) instead of a white stipple trail (_p37_*) ----

    def _ed2_patch(self, el, scale):
        from PIL import Image
        key = (el["eid"], el.get("mode"), round(scale, 4), int(round(el["w"])), int(round(el["h"])))
        p_4 = self._ed2_cache.get(key)
        if p_4 is None:
            base = self._ed2_native_patch(el)
            if abs(scale - 1.0) < 1e-3:
                p_4 = base
            else:
                p_4 = base.resize((max(1, int(round(base.width * scale))), max(1, int(round(base.height * scale)))), Image.LANCZOS)
            if len(self._ed2_cache) > 300:
                self._ed2_cache.clear()
            self._ed2_cache[key] = p_4
        p = p_4
        er = getattr(self, "_p37_er", None)
        if not er or el.get("eid") not in er["eids"] or not er["pts"]:
            return p
        try:
            from PIL import Image, ImageDraw, ImageChops
            ox, oy = el["origin"]
            w, h = max(1.0, float(el["w"])), max(1.0, float(el["h"]))
            sx, sy = p.width / w, p.height / h
            rot = float(el.get("rotation", 0.0) or 0.0)
            pts = er["pts"]
            if abs(rot) > 0.01:
                # the patch is drawn upright and rotated afterwards, so bring the brush path into the upright frame
                cx, cy = ox + w / 2, oy + h / 2
                th = _p37_math.radians(rot)
                c, s = _p37_math.cos(th), _p37_math.sin(th)
                pts = [(cx + (x - cx) * c + (y - cy) * s, cy - (x - cx) * s + (y - cy) * c) for x, y in pts]
            sp = [((x - ox) * sx, (y - oy) * sy) for x, y in pts]
            width = max(1, int(round(er["width"] * (sx + sy) / 2)))
            r = width / 2.0
            mask = Image.new("L", p.size, 0)
            d = ImageDraw.Draw(mask)
            if len(sp) == 1:
                d.ellipse([sp[0][0] - r, sp[0][1] - r, sp[0][0] + r, sp[0][1] + r], fill=255)
            else:
                d.line(sp, fill=255, width=width, joint="curve")
                for qx, qy in (sp[0], sp[-1]):
                    d.ellipse([qx - r, qy - r, qx + r, qy + r], fill=255)
            a = p.getchannel("A")
            if ImageChops.multiply(a, mask).getbbox() is None:
                return p
            q = p.copy()
            q.putalpha(ImageChops.multiply(a, ImageChops.invert(mask)))
            return q
        except Exception:
            log.exception("Film eraser live preview failed for one element (non-fatal)")
            return p

    def _ed2_drag_move(self, event):
        r = self._p15_move(event)
        g = getattr(self, "_ed9_g", None)
        if g and g.get("kind") == "erase" and not getattr(self, "_ed8_split", None):
            try:
                self._ed2_clear_tmp()     # no stipple trail: the picture itself shows what is being erased
                er = getattr(self, "_p37_er", None)
                if er is None:
                    er = self._p37_er = {"eids": {e.get("eid") for e in self._ed9_cands("pen")}, "pts": [], "width": 1}
                er["pts"] = list(g["pts"])
                er["width"] = max(1, int(self._ed2_width))
                if getattr(self, "_p37_after", None) is None:
                    self._p37_after = self.canvas.after(15, lambda: self._p37_flush())
            except Exception:
                log.exception("Film eraser live preview failed (non-fatal)")
        return r

    def _ed2_release(self, event):
        had = getattr(self, "_p37_er", None) is not None
        self._p37_er = None
        a = getattr(self, "_p37_after", None)
        if a is not None:
            try:
                self.canvas.after_cancel(a)
            except Exception:
                pass
            self._p37_after = None
        r = self._p13_release(event)
        if had:
            try:
                self._ed2_redisplay()
            except Exception:
                pass
        return r

    # ---- patch 38: vim-style keys in Splice mode: y takes the marked region, p inserts the taken region, P inserts a copy (_p38_*) ----

    # ---- patch 39: splice vim keys - d (cut to the register), x (delete, register untouched), v (visual: mark a region with h/l, j/k) (_p39_*) ----

    def _p32_yank(self):
        r = self._p32_yank_2()
        if getattr(self, "_p39_vis", False):
            self._p39_set_vis(False)
        return r

    # ---- the same function under a second name ----
    _h_build_row = _g_build_row
    _p3_default_range = _p2_default_range
    _p3_unique_name = _p2_unique_name
    _p3_make = _p2_make
    _p11_seed = _p10_seed

    # ---- helpers that used to be module-level functions taking self ----

    def _ed3_remove(self, el):
        for i, e in enumerate(self.film_edits):
            if e is el:
                self.film_edits.pop(i)
                return

    def _p13_bake(self, dest_dir):
        from PIL import Image
        self._ed5_state()
        self._p13_state()
        src = Path(self.video_path)
        out = Path(dest_dir) / f"{src.stem}-edited.mkv"
        order = {l["id"]: i for i, l in enumerate(self.film_layers)}
        els = [e for e in sorted(self.film_edits, key=lambda e: order.get(e.get("layer_id"), 0)) if self._ed_find(e.get("layer_id")) is not None]
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(src)]
        chain = []
        last = "0:v"
        crop = self._ed5_crop
        if crop:
            pw = max(int(self.native_w), crop[0] + crop[2])
            ph = max(int(self.native_h), crop[1] + crop[3])
            if pw > int(self.native_w) or ph > int(self.native_h):
                chain.append(f"[0:v]pad={pw}:{ph}:0:0:color={self._p13_bg}[vpad]")
                last = "vpad"
        for n, el in enumerate(els):
            patch = self._ed2_native_patch(el)
            rot = el.get("rotation", 0.0)
            if abs(rot) > 0.01:
                patch = patch.rotate(-rot, expand=True, resample=Image.BICUBIC)
            cx = el["origin"][0] + el["w"] / 2
            cy = el["origin"][1] + el["h"] / 2
            x = int(round(cx - patch.width / 2))
            y = int(round(cy - patch.height / 2))
            png = Path(dest_dir) / f"ov{n}.png"
            patch.save(png)
            cmd += ["-i", str(png)]
            layer = self._ed_find(el["layer_id"])
            en = ""
            if layer.get("start") is not None or layer.get("end") is not None:
                s, e = self._ed_range(layer)
                en = f":enable='between(t,{s:.3f},{e:.3f})'"
            chain.append(f"[{last}][{n + 1}:v]overlay=x={x}:y={y}:format=auto{en}[v{n}]")
            last = f"v{n}"
        last = self._p25_video_chain(chain, last)   # _p25_fx: Film > Effects layers (source-time ranges)
        post = []
        if crop:
            cxx, cyy, cw, ch = crop
            post.append(f"crop={cw}:{ch}:{cxx}:{cyy}")
        sc = self._ed5_scale
        post.extend(self._p29_scale_post())
        if self._p13_out:
            post.append(f"scale={_p13_even(self._p13_out[0])}:{_p13_even(self._p13_out[1])}:flags=lanczos")
        elif sc == "orig":
            if crop:
                post.append(f"scale=trunc({int(self.native_w)}/2)*2:-2:flags=lanczos")
        elif sc != 1.0:
            post.append(f"scale=trunc(iw*{float(sc)}/2)*2:trunc(ih*{float(sc)}/2)*2:flags=lanczos")
        post.append("pad=ceil(iw/2)*2:ceil(ih/2)*2")
        tv = self._ed5_trim_vals()
        tparts = ""
        if tv:
            dur = self._ed_dur()
            tparts = f"start={tv[0]:.3f}"
            if tv[1] < dur - 0.05:
                tparts += f":end={tv[1]:.3f}"
            post.append(f"trim={tparts},setpts=PTS-STARTPTS")
        chain.append(f"[{last}]{','.join(post)}[vout]")
        audio_args = ["-an"]
        n_aud = _fa_audio_count(src)
        if n_aud > 0:
            aparts, acur = self._p2_audio_chain(n_aud, 0.0)
            if acur is not None:
                chain += aparts
                if tv:
                    chain.append(f"[{acur}]atrim={tparts},asetpts=PTS-STARTPTS[aout]")
                else:
                    chain.append(f"[{acur}]anull[aout]")
                audio_args = ["-map", "[aout]", "-c:a", "aac", "-b:a", "192k"]
        cmd += ["-filter_complex", ";".join(chain), "-map", "[vout]"] + audio_args + self._p29_venc() + [
            "-pix_fmt", "yuv420p", str(out)]
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if r.returncode != 0:
            raise RuntimeError("ffmpeg bake failed: " + r.stderr.decode("utf-8", "replace")[-800:])
        log.info(f"Film viewer: baked {len(els)} element(s), flags={self._ed4_flags()} -> {out}")
        return out

    def _p30_active(self):
        ov = getattr(self, "_p29_overlay", None)
        return ov if (ov is not None and ov.alive() and ov.path and ov.total) else None

    def _p32_clip_read(self=None):
        """The region last taken in ANY film window of this user (shared through a small file), or None."""
        try:
            clip = _p32_json.loads(_p32_clip_file().read_text(encoding="utf-8"))
            ins = clip["ins"]
            if ins.get("kind") in ("file", "still") and not os.path.exists(ins["path"]):
                return None
            float(clip["start"]), float(clip["end"])
            return clip
        except Exception:
            return None

    def _p33_install_guard(self):
        """A window that has to grow is shifted just enough to stay on its monitor (size-only geometry calls leave the position to the window manager)."""
        try:
            root = self.root
            if "geometry" in root.__dict__:
                return
            orig = root.geometry

            def guarded(newGeometry=None):
                if newGeometry is None:
                    return orig()
                try:
                    m = _p33_re.match(r"^(\d+)x(\d+)$", str(newGeometry))
                    if m and root.winfo_ismapped():
                        w, h = int(m.group(1)), int(m.group(2))
                        if w > root.winfo_width() or h > root.winfo_height():
                            from .capture import get_monitors
                            pm = _p33_re.match(r"^\d+x\d+\+(-?\d+)\+(-?\d+)$", orig())
                            if pm:
                                x, y = int(pm.group(1)), int(pm.group(2))
                                nx, ny = _p33_clamp(get_monitors(), x, y, w, h)
                                if (nx, ny) != (x, y):
                                    log.info(f"Film viewer: window grew to {w}x{h}; moved from +{x}+{y} to +{nx}+{ny} to stay on its monitor")
                                    newGeometry = f"{w}x{h}+{nx}+{ny}"
                except Exception:
                    log.exception("Film viewer: keep-on-screen check failed (non-fatal)")
                return orig(newGeometry)

            root.geometry = guarded
        except Exception:
            log.exception("Film viewer: could not install the keep-on-screen guard (non-fatal)")

    def _p37_flush(self):
        self._p37_after = None
        if getattr(self, "_p37_er", None) is None:
            return
        try:
            self._ed2_redisplay()
        except Exception:
            log.exception("Film eraser live preview redraw failed (non-fatal)")

    def _p39_vis_on(self):
        return bool(getattr(self, "_p39_vis", False)) and self._p10_splice_ui()

    def _p39_redraw(self):
        try:
            self._draw_scrub_playhead()
        except Exception:
            pass

    def _p39_sync_ui(self):
        # keep the "At" dropdown in the option row showing the same handle
        try:
            self._ed5_refresh(True)
        except Exception:
            pass

    def _p39_set_vis(self, on):
        self._p39_vis = bool(on)
        self._p39_pend = None
        self._p39_redraw()

    def _p39_piece(self):
        vis = self._p11_ensure_region()
        reg = getattr(self, "_p11_reg", None)
        if not vis or not reg:
            return None, None
        piece = next((v for v in vis if v["uid"] == reg["pid"]), None)
        return piece, reg

    def _p39_enter_visual(self):
        """Start a selection at the playhead: a short region you then widen with h/l, switching handle with j/k."""
        piece, reg = self._p39_piece()
        if piece is None:
            return
        t = self.frame_index / max(float(self.fps), 1.0)
        here = next((v for v in self._p11_vis() if not v.get("ins") and v["s"] - 0.02 <= t <= v["e"] + 0.02), None)
        if here is not None and here["uid"] != piece["uid"]:
            self._p11_select(here["uid"])
            piece, reg = self._p39_piece()
        if piece.get("ins") or not (piece["s"] - 0.02 <= t <= piece["e"] + 0.02):
            reg["a"], reg["b"] = piece["s"], min(piece["e"], piece["s"] + _P39_GAP)
        else:
            a = max(piece["s"], min(t, piece["e"] - _P39_GAP))
            reg["a"], reg["b"] = a, min(piece["e"], a + _P39_GAP)
        self._p31_state_ok()
        self._p31_at = "b"
        self._p39_sync_ui()
        self._p39_set_vis(True)

    def _p39_whole_piece(self):
        piece, reg = self._p39_piece()
        if piece is None:
            return
        reg["a"], reg["b"] = piece["s"], piece["e"]
        self._p39_redraw()

    def _p39_move_handle(self, direction, big):
        piece, reg = self._p39_piece()
        if piece is None:
            return
        step = float(_p39_const("FILM_SPLICE_BIG_STEP", 1.0) if big else _p39_const("FILM_SPLICE_STEP", 0.1))
        which = getattr(self, "_p31_at", "b")
        if which == "a":
            reg["a"] = max(piece["s"], min(reg["a"] + direction * step, reg["b"] - _P39_GAP))
        else:
            reg["b"] = min(piece["e"], max(reg["b"] + direction * step, reg["a"] + _P39_GAP))
        if not piece.get("ins"):
            try:
                self._p10_seek(reg[which])
            except Exception:
                pass
        self._p39_redraw()

    def _p39_toggle_handle(self):
        self._p31_state_ok()
        self._p31_at = "a" if self._p31_at == "b" else "b"
        self._p39_sync_ui()
        self._p39_redraw()

    def _p39_clip_data(self):
        piece, reg = self._p39_piece()
        if piece is None:
            return None
        P = next((s for s in (self._p10_segs or []) if s["id"] == reg["pid"]), None)
        if P is None or reg["b"] - reg["a"] < 0.05:
            notify("Nothing to take", "Select a piece and set the handles around the part you want", blocking=False)
            return None
        spec = dict(P["ins"]) if P.get("ins") else {"kind": "file", "path": str(self.video_path)}
        return {"ins": spec, "start": float(reg["a"]), "end": float(reg["b"])}

    def _p39_clip_store(self, clip):
        path = _p32_clip_file()
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(_p32_json.dumps(clip), encoding="utf-8")
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
            return True
        except OSError:
            log.exception("Film splice: could not store the cut region")
            notify("Could not cut the region", "See the log", blocking=False)
            return False

    def _p39_delete(self, to_register):
        """d (to_register) / x (void): remove the marked region; afterwards the handle sits where the region was, so p puts it back or elsewhere."""
        piece, reg = self._p39_piece()
        if piece is None:
            return
        clip = self._p39_clip_data() if to_register else None
        if to_register and clip is None:
            return
        P = next((s for s in self._p10_segs if s["id"] == reg["pid"]), None)
        had_right = bool(P) and (P["end"] - reg["b"] > 0.05)
        before = [dict(s) for s in self._p10_segs]
        self._p11_delete()
        if [dict(s) for s in self._p10_segs] == before:
            return      # nothing changed (e.g. it was the last piece)
        self._p31_state_ok()
        self._p31_at = "a" if had_right else "b"
        self._p39_set_vis(False)
        if to_register and self._p39_clip_store(clip):
            notify("Region cut", _p32_clip_label(clip) + " - p inserts it at the marked handle", blocking=False)
        else:
            notify("Region deleted", "The register was left alone" if not to_register else "", blocking=False)


# ---- folder setup hook for film windows (paths_setup) ----


# ---- film save/copy parity with the image windows ----
import time as _fs_time
import shutil as _fs_shutil
import threading as _fs_threading
from . import constants as _fs_consts
from . import paths_setup as _fs_paths


def _fs_stamp():
    return _fs_time.strftime("%Y%m%d-%H%M%S")


_fs_close_keep_reverted = True


# ---- film toolbar layout parity with the image window ----


# ---- film toolbar: dimension/scale row goes under the buttons, like the image window ----


# ---- film toolbar: dimension/scale readouts share the button row, right-aligned ----


# ---- film toolbar: readouts in a flush row directly under the buttons; taller checkboxes ----


# ---- film toolbar: readouts at the top-right of the button row, directly under Re-scale / Tabs ----


# ---- audio: separate tracks are mixed into one when a film leaves the temp file (Save / Sloppy Save / Save As / Copy) ----


def _fa_audio_count(path):
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, check=True,
        )
        return len([line for line in r.stdout.splitlines() if line.strip()])
    except Exception:
        log.exception(f"Film viewer: could not probe audio tracks of {path} (non-fatal)")
        return 0


def _fa_merge_for_export(src):
    """Returns (path_to_use, temp_dir_to_delete_or_None). One audio track or none: src unchanged."""
    src = Path(src)
    n = _fa_audio_count(src)
    if n <= 1:
        return src, None
    if src.parent.name.startswith("snippysnappy-record-"):
        workdir, cleanup = src.parent, None
    else:
        workdir = Path(tempfile.mkdtemp(prefix="snippysnappy-merge-"))
        cleanup = workdir
    out = workdir / f"{src.stem}-merged.mkv"
    graph = "".join(f"[0:a:{i}]" for i in range(n)) + f"amix=inputs={n}:duration=longest[a]"
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(src), "-filter_complex", graph,
           "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", str(out)]
    try:
        subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        log.info(f"Film viewer: merged {n} audio tracks into one -> {out}")
        return out, cleanup
    except Exception:
        log.exception("Film viewer: audio merge failed - exporting the file with its tracks unmerged")
        if cleanup is not None:
            shutil.rmtree(cleanup, ignore_errors=True)
        return src, None


# ---- film audio playback: speaker overlay, live volume/mute, ffmpeg -> numpy -> pacat ----
import fcntl as _fp_fcntl
from . import constants as _fp_consts


class _FilmAudio:
    def __init__(self, path, start_seconds, tracks, gain_fn):
        self._gain_fn = gain_fn
        self._stop = threading.Event()
        if tracks > 1:
            graph = "".join(f"[0:a:{i}]" for i in range(tracks)) + f"amix=inputs={tracks}:duration=longest[a]"
            maps = ["-filter_complex", graph, "-map", "[a]"]
        else:
            maps = ["-map", "0:a:0"]
        self._ff = subprocess.Popen(
            ["ffmpeg", "-v", "error", "-ss", f"{max(0.0, start_seconds):.3f}", "-i", str(path), *maps,
             "-vn", "-ac", "2", "-ar", "48000", "-f", "s16le", "-"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        try:
            self._pa = subprocess.Popen(
                ["pacat", "--playback", "--format=s16le", "--rate=48000", "--channels=2",
                 "--latency-msec=60", "--client-name=snippysnappy", "--stream-name=snippyfilm"],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception:
            self._ff.kill()
            raise
        try:
            _fp_fcntl.fcntl(self._pa.stdin.fileno(), 1031, 4096)   # F_SETPIPE_SZ: keep the pipe small so volume changes are heard quickly
        except Exception:
            pass
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            import numpy as np
        except Exception:
            np = None
            log.warning("Film viewer: numpy not available - volume slider will only mute/unmute")
        try:
            while not self._stop.is_set():
                data = self._ff.stdout.read(4096)
                if not data:
                    break
                g = self._gain_fn()
                if g <= 0.0:
                    data = bytes(len(data))
                elif g < 0.999 and np is not None:
                    if len(data) % 2:
                        data = data[:-1]
                    data = (np.frombuffer(data, dtype=np.int16) * g).astype(np.int16).tobytes()
                self._pa.stdin.write(data)
                self._pa.stdin.flush()
        except Exception:
            if not self._stop.is_set():
                log.exception("Film viewer: audio playback stopped unexpectedly")
        finally:
            try:
                self._pa.stdin.close()
            except Exception:
                pass
            try:
                self._pa.wait(timeout=10)
            except Exception:
                pass

    def stop(self):
        self._stop.set()
        for p in (self._ff, self._pa):
            try:
                p.kill()
            except Exception:
                pass
        for p in (self._ff, self._pa):
            try:
                p.wait(timeout=1)
            except Exception:
                pass


# ---- speaker overlay: coordinate-based hover check that can never stop rescheduling ----


# ---- film Tools row + Filmsnap ----
import time as _tl_time
import importlib as _tl_importlib
from . import paths_setup as _tl_paths


def _tl_stamp():
    return _tl_time.strftime("%Y%m%d-%H%M%S")


def _tl_toggle_tools(self):
    if self._tl_row is not None:
        self._tl_close_row()
        return
    if self.save_row.winfo_children():
        self._collapse_save_row()
    row = tk.Frame(self.toolbar_container, bg="#222222")
    row.pack(side="top", fill="x", after=self._tl_main_row)
    self._tl_row = row
    for text, cmd in (
        ("Extract Text", lambda: self._tl_todo("Extract Text")),
        ("Filmsnap", self._tl_filmsnap),
        ("Measure", lambda: self._tl_todo("Measure")),
        ("Magnify", lambda: self._tl_todo("Magnify")),
        ("Pin", lambda: self._tl_todo("Pin")),
    ):
        b = tk.Button(row, text=text, pady=BUTTON_PADY, command=cmd)
        b.pack(side="left", padx=2, pady=4)
        if text == "Filmsnap":
            self._tl_snap_btn = b
    if self._tl_tools_btn is not None:
        self._tl_tools_btn.config(relief="sunken")
    self._tl_fit()


def _tl_copy_image(path):
    for mod in ("clipboard", "selector_app"):
        try:
            fn = getattr(_tl_importlib.import_module("." + mod, __package__), "copy_image_to_clipboard", None)
            if fn is not None:
                fn(path)
                return True
        except Exception:
            continue
    log.error("Film viewer: could not find copy_image_to_clipboard")
    return False


# ---- film Tools: the first row itself becomes the tool picker (with Back), like the image window ----


def _t2_back(self):
    if self._tl_snap_mode:
        self._collapse_save_row()
    self._tl_snap_btn = None
    tf = getattr(self, "_tl_tools_frame", None)
    self._tl_tools_frame = None
    if tf is not None:
        tf.destroy()
    for w, info in getattr(self, "_tl_hidden", []):
        try:
            w.pack(**info)
        except tk.TclError:
            log.exception("Film viewer: could not restore a toolbar widget (non-fatal)")
    self._tl_hidden = []
    self._tl_fit()


# ---- Filmsnap Also copy + Extract Text (OCR) ----
import sys as _o_sys


def _o_find(name):
    pkg = __package__
    for mn, mod in list(_o_sys.modules.items()):
        if mn == pkg or mn.startswith(pkg + "."):
            fn = getattr(mod, name, None)
            if callable(fn):
                return fn
    log.error(f"Film viewer: could not find {name} in the loaded modules")
    return None


def _o_default_copy():
    return int(bool(getattr(_fs_consts, "DEFAULT_COPY_ON_SAVE", True)))


# ---- Filmsnap: own Also copy, no Also exit / Also save ----


# ---- Extract Text ----


# ---- tools row: Extract Text now live ----


# ---- film layout v3: Play/Restart first, tighter gaps, OCR top row + second row like the image window ----


# ---- OCR review layout fixes + Measure ----
import math as _m_math


# ---- Magnify ----


# ---- OCR row height lock, Magnify pin, Pin tool ----


def _k_fit(frame, prime=False):
    try:
        for c in frame.winfo_children():
            if isinstance(c, tk.Frame):
                c.pack_configure(pady=0)
        frame.pack_propagate(True)
        frame.update_idletasks()
        h = max(frame.winfo_reqheight(), getattr(frame, "_lock_h", 0))
        if prime:
            probe = tk.Frame(frame.master)
            for w in (tk.Button(probe, text="Sloppy Save", pady=BUTTON_PADY), tk.Checkbutton(probe, text="Also copy"), tk.Entry(probe, width=28)):
                w.pack(side="left")
            probe.update_idletasks()
            h = max(h, probe.winfo_reqheight())
            probe.destroy()
        frame._lock_h = h
        frame.config(height=h)
        frame.pack_propagate(False)
    except tk.TclError:
        pass


# ---- Magnify: pinned lens ----


# ---- Pin tool ----


# ---- OCR review button gaps, Pin row layout ----


def _q_walk(widget):
    seen_nav = False
    for c in widget.winfo_children():
        if isinstance(c, tk.Button) and c.winfo_manager() == "pack":
            if c.cget("text") in ("Back", "Cancel", "Close") and not seen_nav:
                seen_nav = True
                c.pack_configure(padx=(10, 2))
            else:
                c.pack_configure(padx=2)
        elif isinstance(c, tk.Frame):
            _q_walk(c)


# ---- video pin: pre-pin options + launcher ----
import json as _vp_json


# ---- film Pin row: pins list, probe gate ----
import sys as _vl_sys


def _vl_get(name):
    pkg = __package__
    co = _vl_sys.modules.get(pkg + ".coordinator")
    if co is not None and hasattr(co, name):
        return getattr(co, name)
    for mn, mod in list(_vl_sys.modules.items()):
        if (mn == pkg or mn.startswith(pkg + ".")) and hasattr(mod, name):
            return getattr(mod, name)
    log.error(f"Film viewer: could not find {name} for the pins list")
    return None


# ---- film window width guard ----
import re as _fw_re


# ---- film pins list: refresh on every roster update ----


# ---- Ctrl+scroll zoom and middle-drag pan ----


# ---- film Also checkboxes: default font, like the image toolbar ----


# ---- film Pins list: per-entry X ----


# ---- film edit model + orange range bar (_ed_*) ----


def _ed_fmt(t):
    t = max(0, int(t))
    return f"{t // 60:02d}:{t % 60:02d}"


# ---- film edit tools: Draw / Shape / Text overlays (_ed2_*) ----


# ---- canvas interaction ----


# ---- edit row ----


def _ed2_exit(self, cancel):
    if not self._ed2_active:
        return
    self._ed2_bind_canvas(False)
    self._ed2_clear_tmp()
    self._ed2_drag = None
    for w in self._ed2_rows:
        try:
            w.destroy()
        except tk.TclError:
            pass
    self._ed2_rows = []
    for w, info in self._ed2_hidden:
        try:
            w.pack(**info)
        except tk.TclError:
            pass
    self._ed2_hidden = []
    self._ed2_active = False
    if cancel and self._ed2_snap is not None:
        self.film_edits, self.film_layers, self._ed_counter, self.film_active_layer_id = (
            self._ed2_snap[0], self._ed2_snap[1], self._ed2_snap[2], self._ed2_snap[3])
        self._ed2_redo = []
    self._ed2_snap = None
    if self.side_panel_mode == "layers":
        self._refresh_side_panel()
    self._ed2_redisplay()
    self._draw_scrub_playhead()
    try:
        self._tl_fit()
    except Exception:
        log.exception("Film edit: _tl_fit failed (non-fatal)")
    log.info(f"Film viewer: Edit mode left ({'cancelled' if cancel else 'kept'}), {len(self.film_edits)} elements")


def _p20_prev_init(self, *args, **kwargs):
    self._cf_init(*args, **kwargs)
    try:
        for w in self._tl_main_row.winfo_children():
            if isinstance(w, tk.Button) and w.cget("text") == "Edit":
                w.config(command=self._ed2_enter)
    except Exception:
        log.exception("Film viewer: could not hook the Edit button (non-fatal)")


# ---- film edit: select / move / resize / rotate, undo stack (_ed3_*) ----
import math as _ed3_math

_ED3_AXES = {"n": (False, True), "s": (False, True), "e": (True, False), "w": (True, False),
             "nw": (True, True), "ne": (True, True), "sw": (True, True), "se": (True, True)}
_ED3_CORNERS = ("nw", "ne", "sw", "se")


# ---- film edit: bake edits into the exported film (_ed4_*) ----
import atexit as _ed4_atexit
import tempfile as _ed4_tempfile
import threading as _ed4_threading
import shutil as _ed4_shutil


# ---- film edit: trim / crop / output scale / audio edits, baked on export (_ed5_*) ----

_ED5_SCALES = [("100%", 1.0), ("75%", 0.75), ("50%", 0.5), ("25%", 0.25), ("150%", 1.5), ("200%", 2.0), ("Original width", "orig")]


# ---- trim uses the orange bar ----


# ---- crop tool ----


# ---- the third edit row and per-tool option rows ----


# ---- Cancel restores trim/crop/scale/audio too ----


# ---- preview playback respects the trim ----


# ---- re-encode flags and the bake ----


# ---- film edit: audio edits audible in preview (_ed6_*) ----


class _FilmAudioEd(_FilmAudio):
    def __init__(self, path, start_seconds, tracks, gain_fn, edits):
        self._gain_fn = gain_fn
        self._stop = threading.Event()
        parts, kept = [], []
        for i in range(tracks):
            a = edits[i] if i < len(edits) else {"mute": False, "vol": 1.0}
            if a.get("mute"):
                continue
            parts.append(f"[0:a:{i}]volume={float(a.get('vol', 1.0)):.3f}[a{i}]")
            kept.append(f"[a{i}]")
        if not kept:
            parts = ["[0:a:0]volume=0[a]"]
        elif len(kept) > 1:
            parts.append("".join(kept) + f"amix=inputs={len(kept)}:duration=longest[a]")
        else:
            parts.append(f"{kept[0]}anull[a]")
        maps = ["-filter_complex", ";".join(parts), "-map", "[a]"]
        self._ff = subprocess.Popen(
            ["ffmpeg", "-v", "error", "-ss", f"{max(0.0, start_seconds):.3f}", "-i", str(path), *maps,
             "-vn", "-ac", "2", "-ar", "48000", "-f", "s16le", "-"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        try:
            self._pa = subprocess.Popen(
                ["pacat", "--playback", "--format=s16le", "--rate=48000", "--channels=2",
                 "--latency-msec=60", "--client-name=snippysnappy", "--stream-name=snippyfilm"],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception:
            self._ff.kill()
            raise
        try:
            _fp_fcntl.fcntl(self._pa.stdin.fileno(), 1031, 4096)
        except Exception:
            pass
        threading.Thread(target=self._run, daemon=True).start()


# ---- film edit: native frame grabs (Filmsnap / Extract Text / Magnify) include overlays (_ed7_*) ----


# ---- film Layers panel parity, ghost, new-layer-on-edit, split/merge, restyled range bar (_ed8_*) ----


# ---- where new elements land: same rules as the image side ----


# ---- selecting a layer re-dims the preview ----


# ---- split ----


# ---- preview compose with ghosting ----


# ---- restyled range bar ----

def _ed8_handle(cv, x, mid, pressed, tags):
    x0, y0, x1, y1 = x - 4, mid - 9, x + 4, mid + 9
    face = "#b8b8b8" if pressed else "#d9d9d9"
    light, dark = ("#6e6e6e", "#ffffff") if pressed else ("#ffffff", "#6e6e6e")
    cv.create_rectangle(x0, y0, x1, y1, fill=face, outline="#2b2b2b", tags=tags)
    cv.create_line(x0 + 1, y1 - 1, x0 + 1, y0 + 1, x1 - 1, y0 + 1, fill=light, tags=tags)
    cv.create_line(x0 + 1, y1 - 1, x1 - 1, y1 - 1, x1 - 1, y0 + 1, fill=dark, tags=tags)
    cv.create_line(x, mid - 4, x, mid + 4, fill="#ff9900", width=2, tags=tags)


# ---- film edit UI rebuild: two-row toolbar, Mode dropdowns, crop handles, in-canvas Text (_ed9_*) ----


# ---- overlays ----


# ---- selecting / transforming ----


# ---- canvas handlers ----


# ---- in-canvas Text ----

def _ed9_marker_bbox(img):
    from PIL import ImageChops
    r, g, b, a = img.split()
    mr = r.point(lambda v: 255 if v == 254 else 0)
    mg = g.point(lambda v: 255 if v == 0 else 0)
    mb = b.point(lambda v: 255 if v == 1 else 0)
    ma = a.point(lambda v: 255 if v >= 128 else 0)
    return ImageChops.multiply(ImageChops.multiply(mr, mg), ImageChops.multiply(mb, ma)).getbbox()


# ---- tool / mode / option setters ----

def _ed9_set_tool(self, tool):
    self._ed9_state()
    self._ed9_text_confirm()
    self._ed9_clear_g()
    self._ed2_tool = tool
    self._ed3_sel = None
    self._ed2_build_rows()
    self._draw_scrub_playhead()
    self._ed2_redisplay()


def _ed9_set_mode(self, tool, mode):
    self._ed9_text_confirm()
    self._ed9_clear_g()
    if tool == "pen":
        self._ed9_pen_mode = mode
        if mode in ("pen", "highlight"):
            self._ed2_pen_kind = mode
    elif tool == "shape":
        self._ed9_shape_mode = mode
    elif tool == "text":
        self._ed9_text_mode = mode
    self._ed3_sel = None
    self._ed2_build_rows()
    self._ed2_redisplay()


# ---- option rows ----


# ---- patch 1: audio first, spacing, track labels, snip modes (_p1_*) ----


# ---- patch 2: audio layers (fade in / fade out / mute / volume boost), preview + bake (_p2_*) ----

_P2_TYPES = [("fade_in", "Fade in"), ("fade_out", "Fade out"), ("mute", "Mute"), ("boost", "Volume boost")]
_P2_NAMES = dict(_P2_TYPES)


class _P2AudioPlay(_FilmAudio):
    def __init__(self, path, start_seconds, parts, cur, gain_fn):
        self._gain_fn = gain_fn
        self._stop = threading.Event()
        graph = list(parts)
        graph.append(f"[{cur}]anull[a]" if cur else "[0:a:0]volume=0[a]")
        self._ff = subprocess.Popen(
            ["ffmpeg", "-v", "error", "-ss", f"{max(0.0, start_seconds):.3f}", "-i", str(path),
             "-filter_complex", ";".join(graph), "-map", "[a]",
             "-vn", "-ac", "2", "-ar", "48000", "-f", "s16le", "-"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        try:
            self._pa = subprocess.Popen(
                ["pacat", "--playback", "--format=s16le", "--rate=48000", "--channels=2",
                 "--latency-msec=60", "--client-name=snippysnappy", "--stream-name=snippyfilm"],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception:
            self._ff.kill()
            raise
        try:
            _fp_fcntl.fcntl(self._pa.stdin.fileno(), 1031, 4096)
        except Exception:
            pass
        threading.Thread(target=self._run, daemon=True).start()


# ---- patch 3: audio layers consolidated - one layer type with track checkboxes, volume, fade effect (_p3_*) ----

_P3_FX = [("none", "No fade"), ("fade_in", "Fade in"), ("fade_out", "Fade out")]


# ---- patch 4: audio layer picker with + button and None placeholder, Effect None (_p4_*) ----

_P4_FX = [("none", "None"), ("fade_in", "Fade in"), ("fade_out", "Fade out")]


# ---- patch 5: + button matches the layer selector, double-click cancels a dropdown (_p5_*) ----


# ---- patch 6: film Draw gets Spray Can and Eraser, Width/Spread split (_p6_*) ----

import random as _p6_random
import math as _p6_math


# ---- patch 7: snip-select-only handles, vim keys in film Text, Proportional by the readouts, snip cut option (_p7_*) ----

# ---- handles and grabbing only in Snip's Select mode (Snip Select is the one cross-tool select) ----


# ---- Proportional checkbox up by the dimension / scale readouts ----


# ---- vim keys in the film Text tool ----
_P7_REG = {"text": ""}
_P7_NAV = {"Left", "Right", "Up", "Down", "Home", "End", "Next", "Prior"}


def _p7_marker_mask(img):
    from PIL import ImageChops
    r, g, b, a = img.split()
    mr = r.point(lambda v: 255 if v == 254 else 0)
    mg = g.point(lambda v: 255 if v == 0 else 0)
    mb = b.point(lambda v: 255 if v == 1 else 0)
    ma = a.point(lambda v: 255 if v >= 128 else 0)
    return ImageChops.multiply(ImageChops.multiply(mr, mg), ImageChops.multiply(mb, ma))


# ---- film snip: copy (default) or cut-with-hole ----


# ---- patch 8: snip handles for snip objects in every Snip mode, all objects only in Snip Select; Proportional by the readouts (_p8_*) ----

# restore grabbing/handles in every Snip mode (patch 7 had removed them from Box/Ellipse/Free-form)


# ---- Proportional checkbox: same row as the dimension / scale readouts, just left of them ----


# ---- patch 9: Proportional checkbox directly under the dimension readout, right-aligned, original styling (_p9_*) ----


# ---- patch 10: Trim -> Splice (select / delete / reorder pieces of the runtime) + Proportional checkbox tweak (_p10_*) ----

# ---- Proportional checkbox: a little gap under the dimension readout, default (image-mode) font ----


# ---- splice model: ordered list of kept pieces, in SOURCE seconds, in OUTPUT order ----
# Edits (overlays, film layers, audio layers) stay anchored to the footage they were
# made on, so they travel with a piece when it is moved and vanish with it when deleted.

def _p10_seg_at(segs, t, eps=0.0):
    for i, sg in enumerate(segs):
        if sg["start"] - eps <= t < sg["end"]:
            return i
    return None


def _p10_next_start(segs, t):
    best = None
    for i, sg in enumerate(segs):
        if sg["start"] > t and (best is None or sg["start"] < segs[best]["start"]):
            best = i
    return best


def _p10_total(segs):
    return sum(sg["end"] - sg["start"] for sg in segs)


def _p10_trivial(segs, dur):
    return len(segs) == 1 and segs[0]["start"] <= 0.05 and segs[0]["end"] >= dur - 0.05


# undo / redo / cancel plumbing: "splice" is one more attribute the shared set-act code understands


# a spliced film ignores the plain trim (the splice list already says which parts are kept)


# ---- editing actions ----


# ---- option rows: Mode dropdown (Trim | Splice) + the splice controls and the output strip ----


_P10_COLORS = ("#3a5f8a", "#4a7a5a", "#7a5a8a", "#8a6a3a")


# ---- scrub bar: dim the removed parts, mark the cut lines, number the pieces ----


# ---- preview playback: skip removed parts and follow the new order ----


# ---- export: bake everything on the full timeline first, then cut the pieces out and join them in order ----


# ---- patch 11: Trim = handles + Reset only; Splice = handles select a region, Cut makes it a piece, drag pieces along the bar (_p11_*) ----

# a splice no longer replaces the trim: the pieces are clipped to the trim window, both apply together


# ---- drawing (splice view: pieces laid out in output order) ----


# ---- mouse: handles, piece select / drag ----
_p11_drag = None
_p11_drop = None


# ---- actions ----


def _p11_set_mode(self, v):
    self._p10_state()
    self._p10_mode = v
    self._p11_reg = None
    if v == "splice":
        self._p11_seed()
    self._ed5_refresh(True)


# ---- patch 13: film Resize matches the image Resize (Full Capture / Background, Scale, W x H, Color, corner handles) (_p13_*) ----


def _p13_even(v):
    return max(16, int(v) // 2 * 2)


# undo / redo / cancel: the explicit output size and the fill color are two more attributes


# ---- corner handles in the Resize tool ----


# select-mode crop handles must not push the canvas off the left / top edge of the frame


# ---- preview: show the fill color where the canvas grows past the video ----


# ---- export: pad first (so overlays survive in the new area), crop, then scale to the exact output size ----


# ---- patch 14: film Resize on a single compact row (selectors, small scale slider) ----


# ---- patch 15: film Resize row reordered (Region, Color, Scale, dimensions), no Proportional box, no rebuild jitter ----


# Resize has no use for the Proportional box (corner drags are free, like the image version).


# Changing Region / Color only changes the options row: rebuild just that row in place so the
# toolbar and window never shrink-then-grow (that was the jitter).


# ---- patch 16: film Resize previews the new size (dashed outline + stippled cut/extension) before Apply ----


def _p17_prev_clear(self):
    try:
        self.canvas.delete("p16prev")
    except tk.TclError:
        pass


def _p17_prev_draw(self, w, h):
    self._p16_clear()
    self._p13_state()
    w, h = _p13_even(w), _p13_even(h)
    c = self._ed5_crop
    x, y = (c[0], c[1]) if c else (0, 0)
    cw, ch = self._p13_canvas_dims()
    if self._p13_target == "capture":
        ow, oh = self._ed9_out_dims()
        nw, nh = cw * w / max(1, ow), ch * h / max(1, oh)
    else:
        nw, nh = w, h
    if abs(nw - cw) < 0.5 and abs(nh - ch) < 0.5:
        return
    s = self.display_scale or 1.0
    X0, Y0 = x * s, y * s
    CX1, CY1 = (x + cw) * s, (y + ch) * s
    NX1, NY1 = (x + nw) * s, (y + nh) * s
    tg = ("p16prev",)
    mk = self.canvas.create_rectangle
    if nw < cw:
        mk(NX1, Y0, CX1, CY1, fill="black", stipple="gray50", outline="", tags=tg)
    if nh < ch:
        mk(X0, NY1, min(NX1, CX1), CY1, fill="black", stipple="gray50", outline="", tags=tg)
    if nw > cw:
        mk(CX1, Y0, NX1, NY1, fill="#00b4ff", stipple="gray25", outline="", tags=tg)
    if nh > ch:
        mk(X0, CY1, min(NX1, CX1), NY1, fill="#00b4ff", stipple="gray25", outline="", tags=tg)
    mk(X0, Y0, NX1, NY1, outline="#ffaa00", width=2, dash=(5, 3), tags=tg)
    t = self.canvas.create_text(NX1 - 6, NY1 - 6, text=f"{w} x {h}", fill="#ffaa00", anchor="se",
                                font=("", 10, "bold"), tags=tg)
    bb = self.canvas.bbox(t)
    if bb:
        r = mk(bb[0] - 3, bb[1] - 2, bb[2] + 3, bb[3] + 2, fill="#111111", outline="#ffaa00", tags=tg)
        self.canvas.tag_lower(r, t)
    self.canvas.tag_raise("p16prev")


# ---- patch 17: film Resize preview follows zoom ----


# ---- patch 20: toolbar tooltips need to know this is a film window and which mode it is in ----


def _p20_init(self, *args, **kwargs):
    _p20_prev_init(self, *args, **kwargs)
    try:
        from .tooltips import film_mode
        self.root._tip_kind = "film"
        self.root._tip_mode = lambda: film_mode(self)
    except Exception:
        log.exception("Could not set up toolbar tooltips (non-fatal)")


# ---- patch 23: leader key, Space = play/pause, Effects placeholder (_p23_leader) ----


# ---- patch 25: film Effects mode (layers of video effects) + plugin effects for audio/video (_p25_fx) ----
from . import effects as _fx


def _p25_copy(layers):
    return [dict(l, params=dict(l.get("params") or {})) for l in layers]


# ---- the selected effect's range uses the orange handles on the scrub bar (same as audio layers) ----


# ---- editing actions ----


# ---- export: video effects go in after the text/shape overlays, before crop/scale/trim (source-time ranges) ----


# ---- plugin effects for the Audio tool: they join the layer's Effect menu (None / Fade in / Fade out / ...) ----


def _p25_audio_eff(fx):
    e = _fx.get(fx) if fx else None
    return e if (e is not None and e.kind == "audio") else None


def _p25_sync_audio_types():
    for e in _fx.effects("audio"):
        for lst in (_P3_FX, _P4_FX):
            if e.id not in dict(lst):
                lst.append((e.id, e.label))


# ---- patch 28: Play / Restart return to the front of the main row after Edit (_p28_exit_layout) ----


# ---- patch 29: Preview tool + effects preview (_p29_preview) ----
import shutil as _p29_shutil
import atexit as _p29_atexit


class _P29Overlay:
    """A canvas laid over the video canvas that plays a rendered preview file (the real edit bake, low resolution)."""

    def __init__(self, app):
        self.app = app
        self.cv = tk.Canvas(app.canvas.master, bg="black", highlightthickness=0)
        self.cv.place(in_=app.canvas, x=0, y=0, relwidth=1, relheight=1)
        tk.Misc.lift(self.cv)
        self.cv.bind("<Button-1>", self._click)
        self.path = None
        self.fps, self.total, self.tracks = 25.0, 0, 0
        self.pos = 0
        self.playing = False
        self.stream = None
        self.audio = None
        self.job = None
        self._photo = None
        self._dims = (0, 0)
        self._got = False
        self._t0 = 0.0
        self._i0 = 0
        self._spos = 0
        self.message("Rendering preview…")

    def alive(self):
        try:
            return bool(self.cv.winfo_exists())
        except tk.TclError:
            return False

    def message(self, text):
        try:
            self.cv.delete("msg")
            self.cv.create_text(max(10, self.cv.winfo_width() // 2), max(10, self.cv.winfo_height() // 2),
                                text=text, fill="white", font=("", 12), tags="msg")
        except tk.TclError:
            pass

    def load(self, path):
        w, h, fps, total = probe_video(path)
        self.path, self.fps, self.total = str(path), float(fps or 25.0), int(total or 0)
        self.tracks = int(_fa_audio_count(path) or 0)
        self.pos = 0
        self._vw, self._vh = int(w), int(h)
        self.cv.delete("msg")

    def _fit(self):
        cw, ch = max(2, self.cv.winfo_width()), max(2, self.cv.winfo_height())
        k = min(cw / max(1, self._vw), ch / max(1, self._vh))
        return max(2, int(self._vw * k) // 2 * 2), max(2, int(self._vh * k) // 2 * 2), cw, ch

    def _stop_streams(self):
        if self.job is not None:
            try:
                self.cv.after_cancel(self.job)
            except Exception:
                pass
            self.job = None
        s, self.stream = self.stream, None
        if s is not None:
            s.stop()
        a, self.audio = self.audio, None
        if a is not None:
            a.stop()

    def play(self, start=None):
        if not self.path or not self.alive():
            return
        self._stop_streams()
        if start is None:
            start = self.pos if self.pos < self.total - 1 else 0
        start = max(0, min(int(start), max(0, self.total - 1)))
        dw, dh, _cw, _ch = self._fit()
        self._dims = (dw, dh)
        self._i0, self._spos, self._got = start, start, False
        try:
            self.stream = _PlaybackStream(self.path, start / self.fps, dw, dh)
        except Exception:
            log.exception("Film preview: could not start playback")
            return
        if self.tracks:
            try:
                self.audio = _FilmAudio(self.path, start / self.fps, self.tracks, self.app._fp_gain)
            except Exception:
                log.exception("Film preview: could not start audio (non-fatal; is pacat installed?)")
                self.audio = None
        self.playing = True
        self.app._p29_sync_buttons()
        self._tick()

    def pause(self):
        self.playing = False
        self._stop_streams()
        self.app._p29_sync_buttons()

    def toggle(self):
        if self.playing:
            self.pause()
        else:
            self.play()

    def restart(self):
        self.play(0)

    def _tick(self):
        if not self.playing or self.stream is None or not self.alive():
            return
        st, latest = self.stream, None
        if not self._got:
            d = st.get()
            if d is not None:
                self._got, self._t0, self._spos, latest = True, time.monotonic(), self._i0, d
        else:
            target = self._i0 + int((time.monotonic() - self._t0) * self.fps)
            while self._spos < target:
                d = st.get()
                if d is None:
                    break
                self._spos += 1
                latest = d
        if latest is not None:
            self._draw(latest, self._spos)
        if st.finished() or self._spos >= self.total - 1:
            self.pos = max(0, self.total - 1)
            self.pause()
            return
        self.job = self.cv.after(max(1, int(500 / self.fps)), self._tick)

    def _draw(self, data, index):
        from PIL import Image, ImageTk
        dw, dh = self._dims
        self.pos = index
        try:
            self._photo = ImageTk.PhotoImage(Image.frombytes("RGB", (dw, dh), data))
            cw, ch = self.cv.winfo_width(), self.cv.winfo_height()
            self.cv.delete("frame")
            self.cv.create_image(cw // 2, ch // 2, image=self._photo, tags="frame")
        except tk.TclError:
            pass
        self.app._draw_scrub_playhead()

    def _click(self, event):
        self.toggle()

    def close(self):
        self._stop_streams()
        self.playing = False
        try:
            self.cv.destroy()
        except tk.TclError:
            pass

    # ---- class attributes that later patches added ----
    _seek_job = None

    # ---- patch 30: preview uses the normal scrub bar; "r" restarts (_p30_preview_scrub) ----

    def seek(self, frac):
        """Scrub target: stop, move the playhead now, show the exact frame shortly after the pointer settles."""
        self._stop_streams()
        self.playing = False
        self.app._p29_sync_buttons()
        self.pos = int(round(frac * max(0, self.total - 1)))
        self.app._draw_scrub_playhead()
        if self._seek_job is not None:
            try:
                self.cv.after_cancel(self._seek_job)
            except Exception:
                pass
        self._seek_job = self.cv.after(60, self._seek_show)

    def _seek_show(self):
        self._seek_job = None
        if not self.alive() or not self.path:
            return
        dw, dh, _cw, _ch = self._fit()
        try:
            r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{self.pos / self.fps:.4f}", "-i", self.path,
                                "-frames:v", "1", "-vf", f"scale={dw}:{dh}:flags=bilinear", "-pix_fmt", "rgb24",
                                "-f", "rawvideo", "-"],
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10)
        except Exception:
            log.exception("Film preview: could not decode the frame to show (non-fatal)")
            return
        if len(r.stdout) == dw * dh * 3:
            self._dims = (dw, dh)
            keep = self.pos
            self._draw(r.stdout, keep)


# ---- patch 30: preview uses the normal scrub bar; "r" restarts (_p30_preview_scrub) ----


# ---- patch 31: splice insertions - another film, a still image, a black/white gap, or a copy of a region (_p31_*) ----


def _p31_insert_cmd(self, what):
    """Menu / leader entry point: switches to Film > Splice first."""
    self._p31_state_ok()
    if getattr(self, "_ed2_tool", None) != "trim":
        self._ed9_set_tool("trim")
    if self._p10_mode != "splice":
        self._p11_set_mode("splice")
    self._p31_insert(what)


# flags: a splice with inserted pieces is a splice even when the source pieces alone would be trivial


# ---- export: bake the source pieces as before, then append / interleave the inserted ones ----


# ---- patch 32: take a region in one film window, insert it into another; splice bar styled like the normal scrub bar (_p32_*) ----
import json as _p32_json


def _p32_clip_file():
    # _p40: shared by every snippysnappy process of this user (the per-process temp folder is not)
    from .tempdir_setup import cache_dir
    return cache_dir() / f"snippysnappy-clip-{os.getuid()}.json"


def _p32_clip_label(clip):
    ins = clip["ins"]
    name = Path(ins["path"]).name if ins.get("path") else ("white gap" if ins.get("color") == "white" else "black gap")
    return f"{name} {_ed_fmt(clip['start'])}-{_ed_fmt(clip['end'])}"


# ---- splice view of the scrub bar: the normal thin track, colours and round playhead ----


# ---- patch 33: film - main Play previews every edit the live player can't show; free middle-drag pan; windows stay on screen (_p33_*) ----
import re as _p33_re


def _p33_clamp(monitors, x, y, w, h, edge_x=8, edge_y=64):
    """Top-left that keeps a w x h window inside the monitor it mostly sits on (frame / title bar slack included)."""
    if not monitors:
        return x, y
    cx, cy = x + w // 2, y + min(h, 80) // 2
    mon = next((m for m in monitors if m[1] <= cx < m[1] + m[3] and m[2] <= cy < m[2] + m[4]), None)
    if mon is None:
        mon = min(monitors, key=lambda m: abs(cx - (m[1] + m[3] / 2)) + abs(cy - (m[2] + m[4] / 2)))
    _n, mx, my, mw, mh = mon[:5]
    nx = max(mx, min(x, mx + mw - w - edge_x))
    ny = max(my, min(y, my + mh - h - edge_y))
    return nx, ny


def _p33_pan_clamp(canvas, margin=80):
    """With confine off the view can go past the picture; keep at least `margin` px of it in sight so it can't be lost."""
    try:
        sr = [float(v) for v in str(canvas.cget("scrollregion")).split()]
        if len(sr) != 4:
            return
        x0, y0, x1, y1 = sr
        cw, ch = canvas.winfo_width(), canvas.winfo_height()
        vx, vy = canvas.canvasx(0), canvas.canvasy(0)
        nx = max(x0 - (cw - margin), min(vx, x1 - margin))
        ny = max(y0 - (ch - margin), min(vy, y1 - margin))
        if abs(nx - vx) > 0.5 and x1 > x0:
            canvas.xview_moveto((nx - x0) / (x1 - x0))
        if abs(ny - vy) > 0.5 and y1 > y0:
            canvas.yview_moveto((ny - y0) / (y1 - y0))
    except tk.TclError:
        pass


# ---- patch 37: film eraser shows the removal live (like the image version) instead of a white stipple trail (_p37_*) ----
import math as _p37_math


# ---- patch 38: vim-style keys in Splice mode: y takes the marked region, p inserts the taken region, P inserts a copy (_p38_*) ----


# ---- patch 39: splice vim keys - d (cut to the register), x (delete, register untouched), v (visual: mark a region with h/l, j/k) (_p39_*) ----
_P39_GAP = 0.1


def _p39_const(name, default):
    from . import constants as _c
    return getattr(_c, name, default)


# ---- module-level names other modules import ----
_ed2_enter = FilmApp._noop_edit
_p17_draw = FilmApp._p16_draw
_p17_clear = FilmApp._p16_clear
_p29_play = FilmApp._p29_play
_p29_restart = FilmApp._p29_restart
_p33_pan_drag = FilmApp._z_pan_drag


# ---- patch 41: the _pN_ monkeypatch chains are folded into plain class methods ----
_p41_installed = True
