# snippysnappy v1.0.1
"""
Screen recording: the ffmpeg x11grab backend, a minimal standalone
region-picker for plain select mode (deliberately NOT reusing
SelectorApp - recording has no use for its document/edit/toolbar
model), the floating record/timer/Stop control, and toggle-stop via the
coordinator (see app.py's -r/--record handling for how "run -r again to
stop" is decided).
"""
import os
import signal
import shutil
import subprocess
import tempfile
import time
import uuid
import tkinter as tk
from pathlib import Path

from .logging_setup import log
from .capture import BACKEND, capture_full_screen, get_virtual_screen_bounds, get_monitors, WindowSelectCancelled
from .constants import FILM_REVIEW_MONITOR
from .notify_toast import notify
from .coordinator import (
    init_coordinator, _RecorderHandle, _register_recorder, _unregister_recorder,
    local_recorders, remote_recorders, request_remote_recorder_stop, shutdown_coordinator,
)


def _copy_file_uri_to_clipboard(path):
    """
    Puts a text/uri-list entry for this file onto the system clipboard -
    the same convention file managers use for "copy this file" (a
    reference to it, not its content), so pasting elsewhere (a file
    manager, a future snippysnappy film-paste handler) picks it up as a
    real file. Returns True/False for success, so the caller can give
    real feedback rather than claiming success unconditionally.
    """
    uri = path.resolve().as_uri()
    try:
        if BACKEND == "wayland":
            subprocess.run(["wl-copy", "--type", "text/uri-list"], input=uri.encode(), check=True)
        else:
            subprocess.run(["xclip", "-selection", "clipboard", "-t", "text/uri-list"], input=uri.encode(), check=True)
        return True
    except Exception:
        log.exception(f"Recorder: failed to copy file URI to clipboard for {path}")
        return False

SNIPPYFILM_DIR = Path.home() / "Videos" / "Snippyfilms"
RECORD_FPS = 30
RECORD_CRF = 18
RECORD_GOP = 15
RECORD_CODEC = "libx264"


def _even(n):
    return n if n % 2 == 0 else n - 1


def any_recording_active():
    """
    True if this process currently knows about a recorder anywhere - its
    own (always accurate) or a remote one (only as accurate as the last
    roster this process has received - see request_global_restart's own
    similar caveat for the same underlying mechanism). Used to make -r a
    toggle: app.py calls this right after briefly connecting to the
    coordinator, before deciding whether to start a new recording or
    stop an existing one.
    """
    return bool(local_recorders()) or bool(remote_recorders())


def stop_all_known_recordings():
    """Stops every recorder this process currently knows about - local ones directly, remote ones via one coordinator message each."""
    for handle in list(local_recorders()):
        handle._stop()
    for client_id, recorder_id in remote_recorders():
        request_remote_recorder_stop(client_id, recorder_id)


def pick_region_interactive_x11():
    """
    A minimal standalone drag-to-select rectangle picker for `-r` in
    plain select mode. Shows the current screen as a backdrop (so the
    user can see what they're selecting), drag a rectangle, release to
    confirm, Escape to cancel. Returns (x, y, w, h) in screen
    coordinates, or raises WindowSelectCancelled. Runs its own Tk root/
    mainloop, entirely separate from anything else in the app - this is
    called before the main capture flow's own root exists, the same way
    args.window's select_window_id() already runs before that root is
    ever deiconified.
    """
    from PIL import Image, ImageTk

    monitors = get_monitors()
    vx, vy, vw, vh = get_virtual_screen_bounds(monitors)
    fd, tmp_name = tempfile.mkstemp(suffix=".png", prefix="snippysnappy-record-select-")
    os.close(fd)
    tmp_path = Path(tmp_name)
    capture_full_screen(tmp_path)

    result = {"rect": None, "cancelled": False}
    root = tk.Tk(className="snippysnappy-record-select")
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.geometry(f"{vw}x{vh}+{vx}+{vy}")
    img = Image.open(tmp_path)
    photo = ImageTk.PhotoImage(img)
    canvas = tk.Canvas(root, width=vw, height=vh, highlightthickness=0, cursor="crosshair")
    canvas.pack(fill="both", expand=True)
    canvas.create_image(0, 0, anchor="nw", image=photo)

    state = {}

    def on_press(event):
        state["x0"], state["y0"] = event.x, event.y
        state["rect_id"] = canvas.create_rectangle(event.x, event.y, event.x, event.y, outline="#ff4444", width=2)

    def on_drag(event):
        if "rect_id" in state:
            canvas.coords(state["rect_id"], state["x0"], state["y0"], event.x, event.y)

    def on_release(event):
        if "x0" not in state:
            return
        x1, y1 = min(state["x0"], event.x), min(state["y0"], event.y)
        x2, y2 = max(state["x0"], event.x), max(state["y0"], event.y)
        if x2 - x1 < 10 or y2 - y1 < 10:
            return
        result["rect"] = (vx + x1, vy + y1, x2 - x1, y2 - y1)
        root.destroy()

    def on_escape(event):
        result["cancelled"] = True
        root.destroy()

    canvas.bind("<ButtonPress-1>", on_press)
    canvas.bind("<B1-Motion>", on_drag)
    canvas.bind("<ButtonRelease-1>", on_release)
    root.bind("<Escape>", on_escape)
    root.update_idletasks()
    root.focus_force()
    try:
        root.grab_set_global()
    except tk.TclError:
        log.warning("Recorder: grab_set_global() failed (window not yet viewable?) - Escape may be unreliable here")
    root.mainloop()
    try:
        tmp_path.unlink(missing_ok=True)
    except Exception:
        pass
    if result["cancelled"] or result["rect"] is None:
        raise WindowSelectCancelled()
    return result["rect"]


def _x11_display():
    """The X display to grab (from $DISPLAY, e.g. ":0" or ":1"); x11grab wants host:display.screen. _p40_installed"""
    d = os.environ.get("DISPLAY") or ":0"
    host, _, rest = d.rpartition(":")
    if "." not in rest:
        rest += ".0"
    return f"{host}:{rest}"


class Recorder:
    def __init__(self, x, y, w, h, output_path):
        self.output_path = output_path
        w = max(2, _even(w))
        h = max(2, _even(h))
        audio_inputs = []
        try:
            from .audio_setup import resolve_inputs
            audio_inputs = resolve_inputs(_AUDIO_SPEC)
        except Exception:
            log.exception("Recorder: could not resolve audio inputs - recording video only")
        self.vproc = None
        self.proc, self.vproc = _rec_start(x, y, w, h, output_path, audio_inputs)
        # patch 44: no blocking sleep - audio_fallback_poll() (driven by a Tk timer) watches the
        # first 0.7 s and restarts video-only if ffmpeg dies on the audio inputs.
        self._geom = (x, y, w, h)
        self._audio_inputs = audio_inputs
        self._audio_deadline = time.monotonic() + 0.7 if audio_inputs else 0.0
        log.info(f"Recorder: ffmpeg started (pid={self.proc.pid}) capturing {w}x{h}+{x}+{y} with {len(audio_inputs)} audio track(s) -> {output_path}")

    def audio_fallback_poll(self):
        """True while the start-up audio check is still running (call again shortly)."""
        if not self._audio_deadline:
            return False
        if self.proc.poll() is not None:
            self._audio_deadline = 0.0
            log.warning(f"Recorder: ffmpeg exited right away with audio inputs {self._audio_inputs} - falling back to video only")
            x, y, w, h = self._geom
            _rec_kill(self.vproc)
            self.proc, self.vproc = _rec_start(x, y, w, h, self.output_path, [])
            log.info(f"Recorder: ffmpeg restarted video-only (pid={self.proc.pid})")
            return False
        if time.monotonic() >= self._audio_deadline:
            self._audio_deadline = 0.0
            return False
        return True

    def stop(self):
        if self.vproc is not None and self.vproc.poll() is None:
            # patch 45 (Wayland): stop wf-recorder first so it finishes its stream; ffmpeg then
            # sees end-of-input (-shortest ends the audio tracks with it).
            try:
                self.vproc.send_signal(signal.SIGINT)
                self.vproc.wait(timeout=5)
            except Exception:
                _rec_kill(self.vproc)
            _close_screencast(self.vproc)
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
        if self.vproc is not None:
            Path(self.output_path).with_suffix(".video.fifo").unlink(missing_ok=True)
        if self.proc.poll() is not None:
            log.info(f"Recorder: stopped, output at {self.output_path}")
            return
        try:
            self.proc.send_signal(signal.SIGINT)
        except Exception:
            log.exception("Recorder: could not send SIGINT to ffmpeg - terminating instead")
            self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            log.warning("Recorder: ffmpeg did not exit after graceful stop - killing")
            self.proc.kill()
        log.info(f"Recorder: stopped, output at {self.output_path}")

    def is_running(self):
        if self.vproc is not None and self.vproc.poll() is not None and self.proc.poll() is None:
            # patch 45: wf-recorder died (e.g. compositor without wlr-screencopy) - ffmpeg would
            # otherwise wait on the FIFO forever
            log.warning(f"Recorder: the Wayland video source exited (rc={self.vproc.returncode}) - see the "
                        f"*.log next to {self.output_path}")
            _rec_kill(self.proc)
        return self.proc.poll() is None


def run_recording_session(x, y, w, h, monitor_bounds=None):
    """
    This process's own dedicated Tk mainloop: starts the ffmpeg capture,
    shows a small floating always-on-top timer+Stop control, and joins
    the coordinator purely so ANOTHER snippysnappy invocation's -r can
    find and stop THIS recording (see stop_all_known_recordings) - the
    toggle decision itself lives in app.py.
    """
    import tempfile as _tempfile
    output_path = Path(_tempfile.mkdtemp(prefix="snippysnappy-record-")) / f"snippyfilm-{time.strftime('%Y%m%d-%H%M%S')}.mkv"

    root = tk.Tk(className="snippysnappy-record")
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.configure(bg="#111111")

    init_coordinator(root)
    recorder_id = str(uuid.uuid4())
    try:
        recorder = Recorder(x, y, w, h, output_path)
    except Exception as e:
        # patch 46: e.g. the portal's share-screen dialog was cancelled, or no stream came back
        cancelled = type(e).__name__ == "PortalCancelled"
        log.log(20 if cancelled else 40, f"Recorder: could not start ({e})", exc_info=not cancelled)
        notify("Recording cancelled" if cancelled else f"Recording failed to start: {e}", blocking=False)
        try:
            shutdown_coordinator()
        except Exception:
            pass
        root.destroy()
        return
    start_time = time.monotonic()

    def _audio_watch():
        if recorder.audio_fallback_poll():
            root.after(50, _audio_watch)

    root.after(50, _audio_watch)
    stop_state = {"intentional": False}

    def _open_film():
        """
        Hides the floating control and opens the review window as a Toplevel
        of THIS root, so this process's single mainloop and coordinator
        connection keep running (that is what lets the film window appear in
        other windows' Tabs panels). The window's own close path destroys
        this root, which ends the process.
        """
        film_bounds = monitor_bounds
        try:
            cx = root.winfo_rootx() + root.winfo_width() // 2
            cy = root.winfo_rooty() + root.winfo_height() // 2
            for _name, mx, my, mw, mh in get_monitors():
                if mx <= cx < mx + mw and my <= cy < my + mh:
                    film_bounds = (mx, my, mw, mh)
                    break
            log.info(f"Recorder: control window at ({cx},{cy}) - opening the film window on monitor bounds {film_bounds}")
        except Exception:
            log.exception("Recorder: could not work out the control window's monitor - using the capture's monitor (non-fatal)")
        if FILM_REVIEW_MONITOR in ("capture", "other") and monitor_bounds is not None:
            try:
                capture_b = tuple(monitor_bounds)
                if FILM_REVIEW_MONITOR == "capture":
                    film_bounds = capture_b
                elif tuple(film_bounds) == capture_b:
                    others = [(mx, my, mw, mh) for _n, mx, my, mw, mh in get_monitors() if (mx, my, mw, mh) != capture_b]
                    if others:
                        film_bounds = others[0]
                log.info(f"Recorder: FILM_REVIEW_MONITOR={FILM_REVIEW_MONITOR!r} - film window bounds {film_bounds}")
            except Exception:
                log.exception("Recorder: could not apply FILM_REVIEW_MONITOR (non-fatal)")
        root.withdraw()
        if not output_path.exists():
            log.warning(f"Recorder: no output file at {output_path} - nothing to review")
            root.destroy()
            return
        try:
            from .film_viewer import FilmApp
            top = tk.Toplevel(root)
            top.withdraw()
            FilmApp(top, output_path, film_bounds, on_closed=root.destroy)
            top.deiconify()
            top.lift()
            top.focus_force()
        except Exception:
            log.exception("Recorder: could not open the film window")
            root.destroy()

    def do_stop(event=None):
        if not recorder.is_running():
            return
        # Set BEFORE recorder.stop() - _tick's poll can fire asynchronously
        # (it's a root.after() callback) the instant ffmpeg actually exits,
        # which can race ahead of the rest of this function. The flag must
        stop_state["intentional"] = True
        recorder.stop()
        try:
            _unregister_recorder(handle)
        except Exception:
            log.exception("Recorder: failed to unregister from the coordinator (non-fatal)")
        notify("Recording stopped", blocking=False)
        _open_film()

    handle = _RecorderHandle(recorder_id, do_stop)
    try:
        _register_recorder(handle)
    except Exception:
        log.exception("Recorder: failed to register with the coordinator (non-fatal - a second -r elsewhere won't be able to stop this one)")

    frame = tk.Frame(root, bg="#111111", highlightbackground="#ff4444", highlightthickness=1)
    frame.pack(fill="both", expand=True, padx=1, pady=1)
    tk.Label(frame, text="\u25cf", fg="#ff4444", bg="#111111", font=("", 11)).pack(side="left", padx=(8, 2), pady=6)
    timer_label = tk.Label(frame, text="00:00", fg="white", bg="#111111", font=("", 11, "bold"))
    timer_label.pack(side="left", padx=(0, 8))
    tk.Button(frame, text="Stop", command=do_stop).pack(side="left", padx=(0, 8), pady=4)

    def _tick():
        if stop_state["intentional"]:
            return
        recorder.audio_fallback_poll()   # never mistake the audio fallback restart for an unexpected end
        if not recorder.is_running():
            log.warning("Recorder: ffmpeg process ended unexpectedly - closing the control window")
            try:
                _unregister_recorder(handle)
            except Exception:
                pass
            _open_film()
            return
        elapsed = int(time.monotonic() - start_time)
        timer_label.configure(text=f"{elapsed // 60:02d}:{elapsed % 60:02d}")
        root.after(500, _tick)

    root.update_idletasks()
    w_req, h_req = root.winfo_reqwidth(), root.winfo_reqheight()
    screen_w = root.winfo_screenwidth()
    root.geometry(f"{w_req}x{h_req}+{screen_w - w_req - 20}+20")

    root.protocol("WM_DELETE_WINDOW", do_stop)
    root.after(500, _tick)
    log.info(f"Recorder: control window shown, recording to {output_path}")
    root.mainloop()


# ---- audio capture: one flac track per source, kept separate until export ----
_AUDIO_SPEC = None   # set by app.py for ONE recording (--audio); None = config only


def _wayland_record_tool():
    """patch 46: 'wf-recorder' | 'portal' | None (honours WAYLAND_RECORD_TOOL)."""
    from . import constants as _c
    pref = str(getattr(_c, "WAYLAND_RECORD_TOOL", "auto") or "auto").lower()
    have_wf = bool(shutil.which("wf-recorder"))
    have_portal = bool(shutil.which("gst-launch-1.0")) and _gst_has("pipewiresrc")
    if pref == "wf-recorder":
        return "wf-recorder" if have_wf else None
    if pref == "portal":
        return "portal" if have_portal else None
    return "wf-recorder" if have_wf else ("portal" if have_portal else None)


def _gst_has(element):
    try:
        return subprocess.run(["gst-inspect-1.0", element], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=10).returncode == 0
    except Exception:
        return False


def recording_unavailable_reason():
    """patch 45/46: None when screen recording can work in this session, else a message for the user."""
    from .capture import BACKEND
    if not shutil.which("ffmpeg"):
        return "Screen recording needs ffmpeg."
    if BACKEND == "wayland" and _wayland_record_tool() is None:
        return ("Screen recording on Wayland needs wf-recorder (Sway, Hyprland, river, Wayfire, labwc) or, on "
                "GNOME/KDE, gst-launch-1.0 with the PipeWire GStreamer plugin (pipewiresrc) and xdg-desktop-portal.")
    return None


_TOKEN_FILE = Path.home() / ".cache" / "snippysnappy" / "screencast-restore-token"


def _portal_video_proc(x, y, w, h, fifo, errlog):
    """
    patch 46: GNOME/KDE (and any portal) recording. The portal shows its own "share screen"
    dialog the first time (the choice is remembered via a restore token); the chosen monitor's
    PipeWire stream is scaled to logical size, cropped to the region, encoded like the X11 path
    and written as matroska into the FIFO ffmpeg reads.
    """
    from .dbus_mini import ScreenCast
    from .capture import get_monitors, monitor_at_point, _include_cursor
    token = None
    try:
        token = _TOKEN_FILE.read_text().strip() or None
    except OSError:
        pass
    sc = ScreenCast(restore_token=token, cursor=_include_cursor())
    if sc.restore_token:
        try:
            _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
            _TOKEN_FILE.write_text(sc.restore_token)
            os.chmod(_TOKEN_FILE, 0o600)
        except OSError:
            pass
    if sc.position and sc.size:
        mx, my = sc.position
        mw, mh = sc.size
    else:
        _n, mx, my, mw, mh = monitor_at_point(get_monitors(), x + w / 2, y + h / 2)
    left, top = max(0, x - mx), max(0, y - my)
    right, bottom = max(0, (mx + mw) - (x + w)), max(0, (my + mh) - (y + h))
    pipe = ["gst-launch-1.0", "-e", "pipewiresrc", f"fd={sc.fd}", f"path={sc.node}", "do-timestamp=true",
            "keepalive-time=1000", "always-copy=true", "!", "videoconvert", "!", "videoscale", "!",
            f"video/x-raw,width={mw},height={mh}", "!",
            "videocrop", f"left={left}", f"top={top}", f"right={right}", f"bottom={bottom}", "!",
            "videorate", "!", f"video/x-raw,framerate={RECORD_FPS}/1", "!", "videoconvert", "!",
            "video/x-raw,format=I420", "!",
            "x264enc", "speed-preset=ultrafast", "pass=qual", f"quantizer={RECORD_CRF}", f"key-int-max={RECORD_GOP}", "!",
            "h264parse", "!", "matroskamux", "!", "filesink", f"location={fifo}"]
    log.info(f"Recorder: portal stream node {sc.node} monitor {mw}x{mh}+{mx}+{my}, crop l{left} t{top} r{right} b{bottom}")
    vproc = subprocess.Popen(pipe, stdin=subprocess.DEVNULL, stdout=errlog, stderr=errlog, pass_fds=(sc.fd,))
    vproc._screencast = sc   # keep the portal session (D-Bus connection) alive with the process
    return vproc


def _rec_kill(proc):
    if proc is not None and getattr(proc, "_screencast", None) is not None and proc.poll() is not None:
        _close_screencast(proc)
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=2)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    _close_screencast(proc)


def _close_screencast(proc):
    sc = getattr(proc, "_screencast", None)
    if sc is not None:
        proc._screencast = None
        sc.close()


def _rec_start(x, y, w, h, output_path, audio_inputs):
    """
    Returns (ffmpeg_proc, video_source_proc_or_None). X11: one ffmpeg (x11grab). patch 45 -
    Wayland: wf-recorder (patch 45) or portal+PipeWire via gst-launch-1.0 (patch 46) encodes the
    region (same codec settings) into a FIFO and ffmpeg muxes it with the per-source flac audio
    tracks exactly as on X11 (video copied, not re-encoded).
    """
    from .capture import BACKEND
    quiet = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if BACKEND != "wayland":
        return subprocess.Popen(_rec_build_cmd(x, y, w, h, output_path, audio_inputs), **quiet), None
    out = Path(output_path)
    fifo = out.with_suffix(".video.fifo")   # wf-recorder prints status on stdout, so no plain pipe
    try:
        fifo.unlink(missing_ok=True)
        os.mkfifo(fifo, 0o600)
    except OSError:
        log.exception("Recorder: could not create the wf-recorder FIFO")
        raise
    proc = subprocess.Popen(_rec_build_cmd(x, y, w, h, output_path, audio_inputs, video_from_pipe=str(fifo)), **quiet)
    tool = _wayland_record_tool()
    with open(out.with_suffix(f".{tool}.log"), "wb") as errlog:
        if tool == "portal":
            try:
                vproc = _portal_video_proc(x, y, w, h, fifo, errlog)
            except Exception:
                _rec_kill(proc)
                raise
            return proc, vproc
        vcmd = ["wf-recorder", "-D", "-g", f"{x},{y} {w}x{h}", "-c", RECORD_CODEC, "-r", str(RECORD_FPS),
                "-x", "yuv420p", "-p", "preset=ultrafast", "-p", f"crf={RECORD_CRF}", "-p", f"g={RECORD_GOP}",
                "-m", "matroska", "-f", str(fifo)]
        log.info(f"Recorder: wf-recorder: {' '.join(vcmd)}")
        vproc = subprocess.Popen(vcmd, stdin=subprocess.PIPE, stdout=errlog, stderr=errlog)
    try:
        vproc.stdin.write(b"Y\n")   # 0.4.x asks "Output file exists. Overwrite?" for the FIFO
        vproc.stdin.close()
    except OSError:
        pass
    return proc, vproc
    vcmd = ["wf-recorder", "-D", "-g", f"{x},{y} {w}x{h}", "-c", RECORD_CODEC, "-r", str(RECORD_FPS),
            "-x", "yuv420p", "-p", "preset=ultrafast", "-p", f"crf={RECORD_CRF}", "-p", f"g={RECORD_GOP}",
            "-m", "matroska", "-f", str(fifo)]
    log.info(f"Recorder: wf-recorder: {' '.join(vcmd)}")
    vproc = subprocess.Popen(vcmd, stdin=subprocess.PIPE, stdout=errlog, stderr=errlog)
    try:
        vproc.stdin.write(b"Y\n")   # 0.4.x asks "Output file exists. Overwrite?" for the FIFO
        vproc.stdin.close()
    except OSError:
        pass
    finally:
        errlog.close()
    return proc, vproc


def _rec_build_cmd(x, y, w, h, output_path, audio_inputs, video_from_pipe=False):
    if video_from_pipe:
        cmd = ["ffmpeg", "-y", "-thread_queue_size", "1024", "-f", "matroska", "-i", video_from_pipe]
    else:
        cmd = ["ffmpeg", "-y", "-thread_queue_size", "1024", "-f", "x11grab", "-framerate", str(RECORD_FPS),
               "-video_size", f"{w}x{h}", "-i", f"{_x11_display()}+{x},{y}"]
    for _label, dev in audio_inputs:
        cmd += ["-thread_queue_size", "1024", "-f", "pulse", "-i", dev]
    if video_from_pipe:
        cmd += ["-c:v", "copy"] + (["-shortest"] if audio_inputs else [])
    else:
        cmd += [
            "-c:v", RECORD_CODEC, "-preset", "ultrafast", "-crf", str(RECORD_CRF),
            "-g", str(RECORD_GOP), "-pix_fmt", "yuv420p",
        ]
    if audio_inputs:
        cmd += ["-map", "0:v"]
        for i, (label, _dev) in enumerate(audio_inputs):
            cmd += ["-map", f"{i + 1}:a", f"-metadata:s:a:{i}", f"title={label}"]
        cmd += ["-c:a", "flac"]
    cmd.append(str(output_path))
    return cmd


def stop_latest_known_recording():
    from . import coordinator as _co
    found = []
    for handle in local_recorders():
        found.append((float(getattr(handle, "started", 0.0) or 0.0), handle, None, None))
    for cid, windows in _co._coord.get("roster_cache", {}).items():
        if cid == _co._coord["client_id"]:
            continue
        for w in windows:
            if w.get("kind") == "recorder":
                found.append((float(w.get("started", 0.0) or 0.0), None, cid, w["window_id"]))
    if not found:
        return
    found.sort(key=lambda t: t[0])
    _started, handle, cid, rid = found[-1]
    if handle is not None:
        handle._stop()
    else:
        request_remote_recorder_stop(cid, rid)


# ---- patch 41: the _pN_ monkeypatch chains are folded into plain class methods ----
_p41_installed = True
