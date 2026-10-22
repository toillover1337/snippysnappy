# snippysnappy v1.0.0
"""Video controls for a pin made from a film: cropped playback, scrub bar, loop, restart, volume."""
import json
import os
import queue
import shutil
import subprocess
import threading
import time
import tkinter as tk
from pathlib import Path

from PIL import Image

from .logging_setup import log

PAUSE_ICON = "\u25ae\u25ae"


class _Stream:
    def __init__(self, clip, start, box):
        x, y, w, h = box
        self.w, self.h = w, h
        self.q = queue.Queue(maxsize=8)
        self.stop_ev = threading.Event()
        self.done = threading.Event()
        self.proc = subprocess.Popen(
            ["ffmpeg", "-v", "error", "-ss", f"{max(0.0, start):.4f}", "-i", str(clip), "-an",
             "-vf", f"crop={w}:{h}:{x}:{y}", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        size = self.w * self.h * 3
        try:
            while not self.stop_ev.is_set():
                data = self.proc.stdout.read(size)
                if len(data) < size:
                    break
                while not self.stop_ev.is_set():
                    try:
                        self.q.put(data, timeout=0.1)
                        break
                    except queue.Full:
                        continue
        except Exception:
            log.exception("Video pin: frame reader failed")
        finally:
            self.done.set()

    def get(self):
        try:
            return self.q.get_nowait()
        except queue.Empty:
            return None

    def finished(self):
        return self.done.is_set() and self.q.empty()

    def stop(self):
        self.stop_ev.set()
        try:
            self.proc.kill()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=1)
        except Exception:
            pass


def _fmt(seconds):
    s = max(0, int(seconds))
    return f"{s // 60:02d}:{s % 60:02d}"


def install(root, original, render, make_btn, state, close_fn, bar_h):
    from .film_viewer import _FilmAudio

    spec = json.loads(os.environ["SNIPPYSNAPPY_PIN_VIDEO"])
    clip = Path(spec["clip"])
    stage = Path(spec["stage"])
    # _p40: only ever delete a folder this feature created; adopt it into this process's own temp
    # folder so a killed pin's copy is swept like any other leftover.
    if not (stage.name.startswith("pinclips-") and clip.parent == stage and stage.is_dir() and not stage.is_symlink()):
        raise RuntimeError(f"video pin: unexpected stage folder {stage!r}")
    try:
        import tempfile
        mine = Path(tempfile.gettempdir())
        if stage.parent != mine:
            moved = mine / stage.name
            os.rename(stage, moved)
            stage, clip = moved, moved / clip.name
            spec.update(stage=str(stage), clip=str(clip))
            os.environ["SNIPPYSNAPPY_PIN_VIDEO"] = json.dumps(spec)   # a Restart All re-exec finds the moved folder
    except OSError:
        log.warning(f"Video pin: could not move {stage} into this process's temp folder (non-fatal)")
    box = [int(v) for v in spec["box"]]
    fps = float(spec["fps"]) or 30.0
    total = max(1, int(spec["frames"]))
    tracks = int(spec["tracks"])
    st = {
        "playing": False, "idx": 0, "i0": 0, "t0": 0.0, "pos": 0, "got": False, "stream": None, "audio": None,
        "loop": bool(spec.get("loop")), "vol": max(0.0, min(1.0, float(spec.get("volume", 100)) / 100.0)),
        "muted": False, "job": None, "wanted": None, "busy": False,
    }
    bg = "#111111"
    bar = tk.Frame(root, bg=bg)

    def show(data):
        original.paste(Image.frombytes("RGB", (box[2], box[3]), data))
        render()

    def draw_time():
        time_lbl.configure(text=f"{_fmt(st['idx'] / fps)} / {_fmt(total / fps)}")

    def draw_scrub(event=None):
        draw_time()
        c = scrub
        c.delete("s")
        w = c.winfo_width()
        if w <= 1:
            return
        pad = 6
        span = max(1, w - 2 * pad)
        px = pad + int(st["idx"] / max(1, total - 1) * span)
        mid = max(8, c.winfo_height() // 2)
        c.create_rectangle(pad, mid - 2, w - pad, mid + 2, fill="#444444", outline="", tags=("s",))
        c.create_rectangle(pad, mid - 2, px, mid + 2, fill="#00b4ff", outline="", tags=("s",))
        c.create_oval(px - 5, mid - 5, px + 5, mid + 5, fill="white", outline="#888888", tags=("s",))

    def gain():
        return 0.0 if st["muted"] else st["vol"]

    def stop_media():
        if st["job"] is not None:
            try:
                root.after_cancel(st["job"])
            except Exception:
                pass
            st["job"] = None
        if st["stream"] is not None:
            st["stream"].stop()
            st["stream"] = None
        if st["audio"] is not None:
            st["audio"].stop()
            st["audio"] = None

    def pause():
        st["playing"] = False
        stop_media()
        play_btn.configure(text="\u25b6")

    def play():
        if st["idx"] >= total - 1:
            st["idx"] = 0
        stop_media()
        st["i0"] = st["pos"] = st["idx"]
        st["got"] = False
        try:
            st["stream"] = _Stream(clip, st["idx"] / fps, box)
        except Exception:
            log.exception("Video pin: could not start playback")
            return
        if tracks > 0:
            try:
                st["audio"] = _FilmAudio(clip, st["idx"] / fps, tracks, gain)
            except Exception:
                log.exception("Video pin: could not start audio (non-fatal)")
        st["playing"] = True
        play_btn.configure(text=PAUSE_ICON)
        tick()

    def toggle(event=None):
        if st["playing"]:
            pause()
        else:
            play()

    def tick():
        if not st["playing"] or st["stream"] is None:
            return
        s = st["stream"]
        latest = None
        if not st["got"]:
            d = s.get()
            if d is not None:
                st["got"] = True
                st["t0"] = time.monotonic()
                st["pos"] = st["i0"]
                latest = d
        else:
            target = st["i0"] + int((time.monotonic() - st["t0"]) * fps)
            while st["pos"] < target:
                d = s.get()
                if d is None:
                    break
                st["pos"] += 1
                latest = d
        if latest is not None:
            show(latest)
            st["idx"] = st["pos"]
            draw_scrub()
        if s.finished() or st["pos"] >= total - 1:
            if st["loop"]:
                st["idx"] = 0
                play()
            else:
                pause()
            return
        st["job"] = root.after(max(1, int(500 / fps)), tick)

    def restart(event=None):
        st["idx"] = 0
        draw_scrub()
        play()

    def toggle_loop(event=None):
        st["loop"] = not st["loop"]
        loop_btn.configure(fg="#00e0a0" if st["loop"] else "white")

    def decode_still(idx):
        st["wanted"] = idx
        if st["busy"]:
            return
        st["busy"] = True

        def work():
            while True:
                i = st["wanted"]
                data = None
                try:
                    out = subprocess.run(
                        ["ffmpeg", "-v", "error", "-ss", f"{i / fps:.4f}", "-i", str(clip), "-frames:v", "1",
                         "-vf", f"crop={box[2]}:{box[3]}:{box[0]}:{box[1]}", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"],
                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30,
                    ).stdout
                    if len(out) == box[2] * box[3] * 3:
                        data = out
                except Exception:
                    log.exception("Video pin: could not decode a frame")
                try:
                    root.after(0, lambda d=data, ix=i: on_still(d, ix))
                except Exception:
                    return
                if st["wanted"] == i:
                    st["busy"] = False
                    break

        threading.Thread(target=work, daemon=True).start()

    def on_still(data, idx):
        if data is not None and idx == st["idx"] and not st["playing"]:
            show(data)

    def scrub_to(event):
        if st["playing"]:
            pause()
        w = scrub.winfo_width()
        span = max(1, w - 12)
        frac = max(0.0, min(1.0, (event.x - 6) / span))
        st["idx"] = int(round(frac * (total - 1)))
        draw_scrub()
        decode_still(st["idx"])

    def draw_speaker():
        c = speaker
        c.delete("all")
        c.create_polygon(4, 8, 9, 8, 15, 3, 15, 19, 9, 14, 4, 14, fill="white", outline="white")
        if st["muted"] or st["vol"] <= 0:
            c.create_line(18, 7, 25, 15, fill="#ff5555", width=2)
            c.create_line(25, 7, 18, 15, fill="#ff5555", width=2)
        else:
            c.create_arc(11, 6, 23, 16, start=-50, extent=100, style="arc", outline="white", width=2)

    def toggle_mute(event=None):
        st["muted"] = not st["muted"]
        draw_speaker()

    def set_volume(val):
        st["vol"] = max(0.0, min(1.0, float(val) / 100.0))
        if st["vol"] > 0:
            st["muted"] = False
        draw_speaker()

    restart_btn = make_btn(bar, "|\u25c0\u25c0")
    play_btn = make_btn(bar, "\u25b6")
    loop_btn = make_btn(bar, "\u21bb")
    for b, cmd in ((play_btn, toggle), (restart_btn, restart), (loop_btn, toggle_loop)):
        b.bind("<Button-1>", lambda e, c=cmd: c())
        b.pack(side="left")
    for _b in (restart_btn, play_btn, loop_btn):
        _b.configure(font=("", 9, "bold"), width=3, padx=2, pady=0, anchor="center")
    time_lbl = tk.Label(bar, text="", fg="white", bg=bg, font=("", 9))
    if tracks > 0:
        vol = tk.Canvas(root, width=26, height=64, bg=bg, highlightthickness=1, highlightbackground="#333333")
        tip = tk.Label(root, text="", fg="white", bg="#111111", font=("", 10, "bold"),
                       highlightthickness=1, highlightbackground="#00b4ff", padx=4, pady=1)

        def draw_vol():
            vol.delete("v")
            py = 56 - int(st["vol"] * 48)
            vol.create_rectangle(11, 8, 15, 56, fill="#444444", outline="", tags=("v",))
            vol.create_rectangle(11, py, 15, 56, fill="#00b4ff", outline="", tags=("v",))
            vol.create_oval(8, py - 5, 18, py + 5, fill="white", outline="#888888", tags=("v",))

        def vol_drag(e):
            st["voldrag"] = True
            st["voldrag_t"] = time.monotonic()
            set_volume(max(0.0, min(1.0, (56 - e.y) / 48.0)) * 100)
            draw_vol()
            tip.config(text=f"{int(round(st['vol'] * 100))}%")
            tip.place(x=e.x_root - root.winfo_rootx() + 18, y=e.y_root - root.winfo_rooty(), anchor="w")
            tip.lift()

        def vol_release(e):
            st["voldrag"] = False
            tip.place_forget()

        vol.bind("<ButtonPress-1>", vol_drag)
        vol.bind("<B1-Motion>", vol_drag)
        vol.bind("<ButtonRelease-1>", vol_release)
        draw_vol()
        speaker = tk.Canvas(bar, width=28, height=22, bg=bg, highlightthickness=0)
        speaker.bind("<Button-1>", toggle_mute)
        speaker.pack(side="right")
        draw_speaker()
    time_lbl.pack(side="right", padx=(4, 4))
    scrub = tk.Canvas(bar, height=18, width=30, bg=bg, highlightthickness=0)
    scrub.pack(side="left", fill="x", expand=True, padx=(6, 0))
    scrub.bind("<ButtonPress-1>", scrub_to)
    scrub.bind("<B1-Motion>", scrub_to)
    scrub.bind("<Configure>", draw_scrub)
    loop_btn.configure(fg="#00e0a0" if st["loop"] else "white")

    root.update_idletasks()
    bar_h[0] = bar.winfo_reqheight()

    def inside():
        try:
            px, py = root.winfo_pointerxy()
        except Exception:
            return False
        return (root.winfo_rootx() <= px < root.winfo_rootx() + root.winfo_width()
                and root.winfo_rooty() <= py < root.winfo_rooty() + root.winfo_height())

    shown = {"v": False}

    def poll():
        try:
            ppx, ppy = root.winfo_pointerxy()

            def over(w):
                try:
                    x0, y0 = w.winfo_rootx(), w.winfo_rooty()
                    return x0 <= ppx < x0 + w.winfo_width() and y0 <= ppy < y0 + w.winfo_height()
                except tk.TclError:
                    return False

            want = inside() and not state.get("resize_mode")
            if want != shown["v"]:
                shown["v"] = want
                if want:
                    bar.place(x=0, rely=1.0, relwidth=1.0, anchor="sw")
                    bar.lift()
                else:
                    bar.place_forget()
            if tracks > 0:
                dragging = bool(st.get("voldrag")) and (time.monotonic() - st.get("voldrag_t", 0.0)) < 2.0
                if st.get("voldrag") and not dragging:
                    st["voldrag"] = False
                    tip.place_forget()
                show_vol = want and shown["v"] and (over(speaker) or (vol.winfo_ismapped() and over(vol)) or dragging)
                if show_vol:
                    if not vol.winfo_ismapped():
                        sx = speaker.winfo_rootx() - root.winfo_rootx() + (speaker.winfo_width() - 26) // 2
                        sy = speaker.winfo_rooty() - root.winfo_rooty()
                        vol.place(x=sx, y=sy, anchor="sw")
                    tk.Misc.tkraise(vol)
                elif vol.winfo_ismapped():
                    vol.place_forget()
        except Exception:
            if not st.get("poll_err"):
                st["poll_err"] = True
                log.exception("Video pin: control poll error (logged once; polling continues)")
        finally:
            if not st.get("dead"):
                try:
                    root.after(60, poll)
                except tk.TclError:
                    pass


    def on_destroy(event):
        if event.widget is not root:
            return
        st["playing"] = False
        st["dead"] = True
        stop_media()
        shutil.rmtree(stage, ignore_errors=True)

    state["hide_bars"] = True
    root.bind("<Destroy>", on_destroy, add="+")
    root.bind("<space>", toggle, add="+")
    poll()
    draw_scrub()
    log.info(f"Video pin: {box[2]}x{box[3]} region, {total} frames @ {fps:.2f}fps, {tracks} audio track(s), clip {clip}")
    if spec.get("autoplay", True):
        root.after(300, play)
