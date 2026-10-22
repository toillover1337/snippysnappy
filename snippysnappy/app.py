# snippysnappy v1.0.0
"""Entry point: CLI argument parsing and the top-level dispatch (select/fullscreen/window/open/nil/pin-view modes)."""
import argparse
import os
import pickle
import shutil
import sys
import tempfile
import tkinter as tk
from pathlib import Path

from .constants import *
from .logging_setup import log
from .capture import (
    BACKEND, get_monitors, monitor_at_point, pick_fallback_monitor, get_mouse_position,
    get_virtual_screen_bounds, capture_full_screen, capture_monitor, capture_window,
    select_window_id, get_window_geometry, wait_capture_delay,
    pick_region_interactive_wayland, pick_output_interactive_wayland, WindowSelectCancelled,
)
from .notify_toast import notify, ensure_dbus_env
from .fuzzy_open import parse_nil_args, make_nil_placeholder, get_default_open_dir, resolve_open_path, is_image_file
from .prompt_windows import OpenPromptApp, GuiPromptApp, DelayPromptApp
from .coordinator import init_coordinator, shutdown_coordinator, set_restart_handler, _LOCAL_WINDOWS
from .selector_app import SelectorApp
from .pin_viewer import run_pin_viewer

def _restart_handler():
    """
    Registered via set_restart_handler - called whenever the whole snippysnappy fleet is told to restart on whatever code is
    currently installed. Snapshots every window this PROCESS owns
    (there can be more than one, from pop-out) into a one-shot pickle
    file, then re-execs with --restore-session. Hands the fresh process
    its previous tabs back instead of a blank slate. Runs inside a Tk
    callback, so os.execv here replaces the whole process immediately.

    Each window's temp files (its capture, any resize/snip temp images)
    live under system temp (mkstemp) or the ORIGINAL capture_tempdir -
    both survive execv untouched.
    """
    try:
        from .capture import wait_written
        wait_written()          # patch 43: every capture PNG on disk before the paths are handed on
        snapshots = []
        for app in list(_LOCAL_WINDOWS):
            app._ensure_documents_initialized()
            app._exit_all_modes_safely()
            app.documents[app.current_document_index] = app._capture_document_state()
            snapshots.append({
                "documents": app.documents,
                "current_document_index": app.current_document_index,
                "side_panel_mode": app.side_panel_mode,
                "bounds": app.current_monitor_bounds,
            })
        fd, session_path = tempfile.mkstemp(suffix=".pkl", prefix="snippysnappy-session-")
        os.close(fd)
        with open(session_path, "wb") as f:
            pickle.dump(snapshots, f)
        total_tabs = sum(len(sn["documents"]) for sn in snapshots)
        log.info(f"Restart: serialized {len(snapshots)} window(s)/{total_tabs} tab(s) -> {session_path}")

        # Strip any stale --restore-session PATH (and its value) already
        # present in our OWN argv.
        cleaned = []
        skip_next = False
        for a in sys.argv[1:]:
            if skip_next:
                skip_next = False
                continue
            if a == "--restore-session":
                skip_next = True
                continue
            cleaned.append(a)
        new_argv = [sys.argv[0]] + cleaned + ["--restore-session", session_path]
        os.execv(sys.executable, [sys.executable] + new_argv)
    except Exception:
        log.exception("Restart: failed to serialize this process's tabs - restarting anyway, but this window's tabs will be lost this time")
        os.execv(sys.executable, [sys.executable] + sys.argv)


def _open_recording_main(path_str):
    import sys as _sys
    from pathlib import Path as _Path
    from . import constants as _c
    from . import fuzzy_open as _fo
    exts = tuple(getattr(_c, "VIDEO_FILE_EXTENSIONS", (".mkv", ".mp4", ".webm", ".mov", ".avi")))
    vdir = getattr(_c, "VIDEO_FUZZ_DIR", None)
    if vdir:
        base = _Path(vdir).expanduser()
    else:
        vids = _Path.home() / "Videos"
        base = vids if vids.is_dir() else _fo.get_default_open_dir()
    if path_str.strip().lower() == "prompt":
        saved = {}
        for name, mod in list(_sys.modules.items()):
            if name.split(".")[0] == "snippysnappy" and hasattr(mod, "IMAGE_FILE_EXTENSIONS"):
                saved[name] = mod.IMAGE_FILE_EXTENSIONS
                mod.IMAGE_FILE_EXTENSIONS = exts
        root = tk.Tk(className="snippysnappy-open-recording")
        root.withdraw()
        chosen = None
        try:
            mode = DEFAULT_OPEN_FILE_PICKER
            while True:
                if mode == "fuzzy":
                    prompt_app = OpenPromptApp(root, base)
                    root.deiconify()
                    root.mainloop()
                    switched = prompt_app.switch_to_gui_requested
                    chosen = prompt_app.result_path
                else:
                    prompt_app = GuiPromptApp(root, base)
                    root.deiconify()
                    root.mainloop()
                    switched = prompt_app.switch_to_cli_requested
                    chosen = prompt_app.result_path
                for child in root.winfo_children():
                    child.destroy()
                root.withdraw()
                if switched:
                    mode = "gui" if mode == "fuzzy" else "fuzzy"
                    continue
                break
        finally:
            for name, val in saved.items():
                _sys.modules[name].IMAGE_FILE_EXTENSIONS = val
            try:
                root.destroy()
            except Exception:
                pass
        if chosen is None:
            log.info("--open-recording prompt: cancelled, nothing chosen")
            return
        open_path = _Path(chosen)
    else:
        open_path = _fo.resolve_open_path(path_str, base)
    try:
        open_path = open_path.resolve(strict=True)
    except OSError:
        log.error(f"--open-recording: file not found: {open_path}")
        print(f"snippysnappy: file not found: {open_path}", file=_sys.stderr)
        _sys.exit(1)
    if open_path.suffix.lower() not in exts:
        log.error(f"--open-recording: not a recognised video file: {open_path}")
        print(f"snippysnappy: not a recognised video file (got: {open_path.suffix or '(no extension)'})", file=_sys.stderr)
        _sys.exit(1)
    log.info(f"Opening existing recording: {open_path}")
    from .film_viewer import show_film_viewer
    show_film_viewer(str(open_path))


class _HelpFormatter(argparse.RawDescriptionHelpFormatter):  # _p19_help_formatter
    """Wider, terminal-scaled option column; epilog wrapped to the terminal width (table rows get a
    hanging indent); on Python 3.14+ colors every single-dash option like a short one (-ro, -ra)."""

    def __init__(self, prog, indent_increment=2, max_help_position=None, width=None):
        if width is None:
            width = shutil.get_terminal_size().columns - 2
        if max_help_position is None:
            max_help_position = min(34, max(20, width // 3))
        super().__init__(prog, indent_increment, max_help_position, width)

    def _is_long_option(self, string):
        return string.startswith("--")

    def _fill_text(self, text, width, indent):
        import textwrap
        out, para = [], []

        def flush():
            if para:
                out.append(textwrap.fill(
                    " ".join(para), width, initial_indent=indent, subsequent_indent=indent,
                    break_long_words=False, break_on_hyphens=False,
                ))
                para.clear()

        for line in text.splitlines():
            if not line.strip():
                flush()
                out.append("")
                continue
            lead = len(line) - len(line.lstrip())
            if not lead:
                para.append(line.strip())   # hard-wrapped source lines of one paragraph join up
                continue
            flush()
            sub = " " * lead
            if " = " in line:
                sub += " " * (line.index(" = ") + 3 - lead)
            out.append(textwrap.fill(
                line.strip(), width, initial_indent=indent + " " * lead, subsequent_indent=indent + sub,
                break_long_words=False, break_on_hyphens=False,
            ))
        flush()
        return "\n".join(out)


def main():
    if not any(a in ("-h", "--help", "--version", "-V") for a in sys.argv[1:]):
        from .capture import check_x_display, install_wayland_window_rules   # patch 45/46
        check_x_display()
        install_wayland_window_rules()
    try:
        from . import constants as _tc
        from .tempdir_setup import install_temp_dir
        install_temp_dir(getattr(_tc, "TEMP_DIR", None), getattr(_tc, "TEMP_KEEP_HOURS", 24))
    except Exception:
        log.exception("Temp dir setup failed (non-fatal) - the system temp folder stays in use")
    parser = argparse.ArgumentParser(
        formatter_class=_HelpFormatter,
        epilog=(
            "Recommended WM-level keybinding scheme (NOT read or enforced by this script itself):\n"
            "\n"
            "a base key picks the CAPTURE MODE, modifiers stack on top\n"
            "to add --window/--fullscreen/--delay/--record, per this table:\n"
            "\n"
            "    Print                  = normal/select-mode capture (all combos below)\n"
            "    Alt+Print              = --record (stacks: +Ctrl=-w, +Super=-f, +Shift=-d)\n"
            "    Control+Print          = --window\n"
            "    Super+Print            = --fullscreen\n"
            "    Shift+Print            = --delay\n"
            "    Super+Ctrl+Print       = --open\n"
            "    Super+Ctrl+Shift+Print = --nil\n"
            "\n"
            "--record (-r) is a TOGGLE - running it again while a recording is already active stops the recording.\n"
        ),
    )
    from .constants import SNIPPYSNAPPY_VERSION
    parser.add_argument("-V", "--version", action="version", version=f"snippysnappy {SNIPPYSNAPPY_VERSION}")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "-w", "--window", action="store_true",
        help="Capture a single window (click to select, Esc to cancel); capture adjustable",
    )
    group.add_argument(
        "-f", "--fullscreen", action="store_true",
        help="Capture the whole monitor; still adjustable via handles",
    )
    group.add_argument(
        "-o", "--open", metavar="PATH", default=None,
        help=(
            "Open an image file for editing/annotation, instead of taking a new "
            "screenshot. PATH may be absolute, or relative. Pass 'prompt' instead of a path to open a "
            "small fuzzy-finder picker window."
        ),
    )
    group.add_argument(
        "-ro", "--open-recording", metavar="PATH", default=None,
        help=(
            "Open an existing video file in its own Snippyfilm window. "
            "Same rules apply: PATH may be absolute or relative. "
            "Pass 'prompt' to open the picker filtered to video files."
        ),
    )
    group.add_argument(
        "--pin-view", metavar="PATH", default=None,
        help=argparse.SUPPRESS,   # internal only - launched by the main app's Pin tool as its own detached process; see enter_pin_mode/run_pin_viewer
    )
    parser.add_argument(
        "--open-film", metavar="PATH", default=None,
        help=argparse.SUPPRESS,   # internal only - opens an existing film in its own Snippyfilm window (used by the film window's "+ Open File...")
    )
    parser.add_argument(
        "--pin-id", metavar="UUID", default=None,
        help=argparse.SUPPRESS,   # internal only - see --pin-view/_launch_pinned_region: lets the LAUNCHING window recognize this pin as its own in the Pins list
    )
    parser.add_argument(
        "--pin-proportional", metavar="0|1", default="1",
        help=argparse.SUPPRESS,   # internal only - see --pin-view/_launch_pinned_region: this pin's own corner-handle resize behavior
    )
    group.add_argument(
        "-n", "--nil", nargs="*", metavar="ARG", default=None,
        help=(
            "Start with a blank canvas instead of taking a screenshot (no capture at all). "
            "Optionally followed by a WxH dimension (e.g. 1200x1000) and/or a color code "
            "(w/white, b/black, t/transparent), in either order, to override the configuration "
            "for this run only. Example: -n 1200x1000 b"
        ),
    )
    group.add_argument(
        "--restore-session", metavar="PATH", default=None,
        help=argparse.SUPPRESS,   # internal only - written by the Restart All feature (see coordinator.request_global_restart / this module's _restart_handler)
    )
    parser.add_argument(
        "-r", "--record", action="store_true",
        help=(
            "Record video instead of a still image - combines with plain select mode, "
            "-w, or -f (not with -o, -n, or --restore-session), and with -d for a delayed "
            "start. Running -r again while a recording is already active (this process or "
            "another) stops that recording instead of starting a new one."
        ),
    )
    parser.add_argument(
        "-a", "--audio", action="store_true",
        help="Only valid together with -r: opens a small prompt to choose what audio/devices to record for this one recording. Defaults come from RECORD_AUDIO / AUDIO_DESKTOP_DEVICE / AUDIO_MIC_DEVICE in the config.",
    )
    parser.add_argument(
        "-ra", "--record-audio", dest="record_audio_combo", action="store_true",
        help="Shorthand for -r -a.",
    )
    parser.add_argument(
        "--demo", action="store_true",
        help=(
            "Only valid together with -r: start this recording even if another recording is already "
            "active, instead of stopping it. Plain -r then stops only the most recently started "
            "recording, and the one before it on the next press (each recording's own Stop button "
            "still stops just that one). Lets snippysnappy record itself being used."
        ),
    )
    parser.add_argument(
        "-d", "--delay", nargs="?", const="__default__", default=None, metavar="SECONDS|prompt",
        help=(
            "Waits before the actual pixels are captured - combinable with plain select mode, "
            "--window, or --fullscreen (not with --open or --nil, which have nothing to "
            "'capture' after a delay). For select mode and --window, the interactive pick/"
            "selection step happens FIRST at normal speed, same as without --delay; only once "
            "that's done does the delay start counting down, with the screen showing normally "
            "(not frozen) so you can arrange whatever should appear in the shot. The actual "
            "capture is taken fresh at the end of the delay, reflecting whatever's on screen "
            "then - not whatever was on screen at selection time. Escape cancels the whole "
            "capture during the countdown (X11 only - see _X11EscapePoller). A bare -d waits "
            f"DEFAULT_CAPTURE_DELAY_SECONDS ({DEFAULT_CAPTURE_DELAY_SECONDS:g}s); -d 5 waits 5 "
            "seconds instead; -d prompt opens a small popup to type the delay in on the fly - "
            "handy for a single keybind you want to reuse with a different delay each time, "
            "rather than needing several keybinds for different fixed lengths."
        ),
    )
    args = parser.parse_args()
    if args.pin_view is not None:
        # Entirely separate code path: no capture, no coordinator, no
        # document model - just a minimal floating image viewer
        run_pin_viewer(args.pin_view, forced_pin_id=args.pin_id, proportional=(args.pin_proportional != "0"))
        return

    if args.open_film is not None:
        # Same idea as --pin-view
        from .film_viewer import show_film_viewer
        show_film_viewer(args.open_film)
        return

    if getattr(args, "record_audio_combo", False):
        args.record = True
        args.audio = True
    if args.audio and not args.record:
        print("snippysnappy: --audio only applies to recordings - use it with -r (or use -ra)", file=sys.stderr)
        sys.exit(1)
    if args.demo and not args.record:
        print("snippysnappy: --demo only applies to recordings - use it with -r", file=sys.stderr)
        sys.exit(1)
    if args.open_recording is not None:
        _open_recording_main(args.open_recording)
        return

    if args.record:
        # Entirely separate code path from the normal image-capture flow
        if args.open is not None or args.nil is not None or args.restore_session is not None:
            print("snippysnappy: --record cannot be combined with --open, --nil, or --restore-session", file=sys.stderr)
            sys.exit(1)
        from .recorder import (
            run_recording_session, pick_region_interactive_x11,
            any_recording_active, stop_latest_known_recording,
        )
        from .recorder import recording_unavailable_reason   # patch 45

        if args.demo:
            log.info("--record --demo: starting a new recording and leaving any recording already active alone")
        else:
            toggle_root = tk.Tk(className="snippysnappy-record-toggle")
            toggle_root.withdraw()
            init_coordinator(toggle_root)
            toggle_root.after(300, toggle_root.quit)
            toggle_root.mainloop()
            if any_recording_active():
                log.info("--record: a recording is already active - stopping the most recent one instead of starting a new one")
                stop_latest_known_recording()
                shutdown_coordinator()
                toggle_root.destroy()
                notify("Recording stopped", blocking=False)
                return
            shutdown_coordinator()
            toggle_root.destroy()

        _why = recording_unavailable_reason()   # checked after the stop-toggle so -r can still stop one
        if _why:
            log.error(f"--record: {_why}")
            print(f"snippysnappy: {_why}", file=sys.stderr)
            notify(_why, blocking=False)
            sys.exit(1)

        if args.audio:
            from .audio_setup import prompt_audio_choice
            audio_choice = prompt_audio_choice()
            if audio_choice is None:
                log.info("--record --audio: prompt cancelled - exiting without recording")
                return
            from . import recorder as _recorder_mod
            _recorder_mod._AUDIO_SPEC = audio_choice

        delay_value = None
        if args.delay == "__default__":
            delay_value = DEFAULT_CAPTURE_DELAY_SECONDS
        elif args.delay is not None:
            try:
                delay_value = float(args.delay)
                if delay_value < 0:
                    raise ValueError
            except ValueError:
                print(f"snippysnappy: invalid --delay value: {args.delay!r}", file=sys.stderr)
                sys.exit(1)

        record_monitor_bounds = None
        try:
            if args.window:
                if BACKEND == "wayland":
                    x, y, w, h = pick_region_interactive_wayland()
                else:
                    win_id = select_window_id()
                    x, y, w, h = get_window_geometry(win_id)
                # -w has no monitor lookup of its own
                # derive it the same way --open/--nil already do, from
                # the WINDOW's own position
                monitors = get_monitors()
                try:
                    _name, mx, my, mw, mh = monitor_at_point(monitors, x + w / 2, y + h / 2)
                    record_monitor_bounds = (mx, my, mw, mh)
                except Exception:
                    log.warning("--record -w: could not determine monitor bounds for the recorded window (non-fatal - FilmApp will fall back to the full virtual screen)")
            elif args.fullscreen:
                if BACKEND == "wayland":
                    _name, x, y, w, h = pick_output_interactive_wayland()
                else:
                    mouse_x, mouse_y = get_mouse_position()
                    monitors = get_monitors()
                    _name, x, y, w, h = monitor_at_point(monitors, mouse_x, mouse_y)
                record_monitor_bounds = (x, y, w, h)   # -f's own region IS the whole monitor already
            else:
                x, y, w, h = pick_region_interactive_x11()
                monitors = get_monitors()
                try:
                    _name, mx, my, mw, mh = monitor_at_point(monitors, x + w / 2, y + h / 2)
                    record_monitor_bounds = (mx, my, mw, mh)
                except Exception:
                    log.warning("--record: could not determine monitor bounds for the selected region (non-fatal - FilmApp will fall back to the full virtual screen)")
        except WindowSelectCancelled:
            log.info("--record: selection cancelled - exiting without recording")
            return

        if delay_value:
            wait_capture_delay(delay_value)

        run_recording_session(x, y, w, h, monitor_bounds=record_monitor_bounds)
        return
    log.info(f"snippysnappy starting, args: window={args.window} fullscreen={args.fullscreen} open={args.open!r} nil={args.nil!r} delay={args.delay!r} restore_session={args.restore_session!r}")
    if args.delay is not None and (args.open is not None or args.nil is not None or args.restore_session is not None):
        log.warning("--delay has no effect with --open, --nil, or --restore-session (nothing to wait to 'capture') - ignoring it")
        args.delay = None
    log.info(f"Backend: {BACKEND}")
    log.debug(
        "Environment: DISPLAY=%r DBUS_SESSION_BUS_ADDRESS=%r XDG_RUNTIME_DIR=%r XDG_CURRENT_DESKTOP=%r"
        % (
            os.environ.get("DISPLAY"),
            os.environ.get("DBUS_SESSION_BUS_ADDRESS"),
            os.environ.get("XDG_RUNTIME_DIR"),
            os.environ.get("XDG_CURRENT_DESKTOP"),
        )
    )
    ensure_dbus_env()

    with tempfile.TemporaryDirectory() as tmp:
        full_path = Path(tmp) / "full.png"
        root = tk.Tk(className="snippysnappy")
        root.withdraw()

        def log_callback_exception(exc, val, tb):
            log.error("Unhandled exception inside a Tkinter callback (button/binding):", exc_info=(exc, val, tb))
        root.report_callback_exception = log_callback_exception
        init_coordinator(root)
        set_restart_handler(_restart_handler)

        delay_value = None
        if args.delay == "__default__":
            delay_value = DEFAULT_CAPTURE_DELAY_SECONDS
        elif args.delay is not None and args.delay.strip().lower() == "prompt":
            prompt_app = DelayPromptApp(root, DEFAULT_CAPTURE_DELAY_SECONDS)
            root.deiconify()
            root.mainloop()
            for child in root.winfo_children():
                child.destroy()
            root.withdraw()
            if prompt_app.result_seconds is None:
                log.info("--delay prompt: cancelled, exiting without capturing")
                sys.exit(0)
            delay_value = prompt_app.result_seconds
            log.info(f"--delay prompt: using {delay_value:g}s")
        elif args.delay is not None:
            try:
                delay_value = float(args.delay)
                if delay_value < 0:
                    raise ValueError
            except ValueError:
                print(f"snippysnappy: invalid --delay value: {args.delay!r} (expected a number of seconds, or 'prompt')", file=sys.stderr)
                sys.exit(1)

        if args.window:
            try:
                if BACKEND == "wayland":
                    x, y, w, h = pick_region_interactive_wayland()
                    monitors = get_monitors()
                    mon = monitor_at_point(monitors, x, y)
                    if delay_value:
                        wait_capture_delay(delay_value)
                    capture_monitor(x, y, w, h, full_path)
                else:
                    win_id = select_window_id()
                    x, y, w, h = get_window_geometry(win_id)
                    monitors = get_monitors()
                    mon = monitor_at_point(monitors, x, y)
                    if delay_value:
                        wait_capture_delay(delay_value)
                    capture_window(win_id, full_path)
            except WindowSelectCancelled:
                log.info("--window: selection cancelled with Escape - exiting without capturing")
                sys.exit(0)
            _, mx, my, mw, mh = mon
            try:
                from .monitor_pick import pick_review_bounds
                mx, my, mw, mh = pick_review_bounds(monitors, (mx, my, mw, mh))
            except Exception:
                log.exception("--window: could not pick a non-active monitor (non-fatal) - using the active one")
            root.deiconify()
            SelectorApp(
                root, full_path, mode="window", bounds=(mx, my, mw, mh), auto_select_full=True,
                monitors=monitors, capture_tempdir=tmp,
            )
            notify("Capture taken", blocking=False)
        elif args.fullscreen:
            if BACKEND == "wayland":
                try:
                    name, x, y, w, h = pick_output_interactive_wayland()
                except WindowSelectCancelled:
                    log.info("--fullscreen: monitor pick cancelled with Escape - exiting without capturing")
                    sys.exit(0)
                monitors = get_monitors()
                if delay_value:
                    wait_capture_delay(delay_value)
                capture_monitor(x, y, w, h, full_path)
            else:
                mouse_x, mouse_y = get_mouse_position()
                monitors = get_monitors()
                name, x, y, w, h = monitor_at_point(monitors, mouse_x, mouse_y)
                if delay_value:
                    wait_capture_delay(delay_value)
                capture_monitor(x, y, w, h, full_path)
            _fs_bounds = (x, y, w, h)
            try:
                from .monitor_pick import pick_review_bounds
                _fs_bounds = pick_review_bounds(monitors, (x, y, w, h))
            except Exception:
                log.exception("--fullscreen: could not pick a non-active monitor (non-fatal) - using the active one")
            root.deiconify()
            SelectorApp(
                root, full_path, mode="fullscreen", bounds=_fs_bounds, auto_select_full=True,
                monitors=monitors, capture_tempdir=tmp, current_monitor_name=name,
            )
            notify("Capture taken", blocking=False)
        elif args.open:
            if args.open.strip().lower() == "prompt":
                mode = DEFAULT_OPEN_FILE_PICKER
                open_path = None
                while True:
                    if mode == "fuzzy":
                        prompt_app = OpenPromptApp(root, get_default_open_dir())
                        root.deiconify()
                        root.mainloop()
                        switched = prompt_app.switch_to_gui_requested
                        chosen = prompt_app.result_path
                    else:
                        prompt_app = GuiPromptApp(root, get_default_open_dir())
                        root.deiconify()
                        root.mainloop()
                        switched = prompt_app.switch_to_cli_requested
                        chosen = prompt_app.result_path
                    for child in root.winfo_children():
                        child.destroy()
                    root.withdraw()
                    if switched:
                        mode = "gui" if mode == "fuzzy" else "fuzzy"
                        continue
                    open_path = chosen
                    break
                if open_path is None:
                    log.info("--open prompt: cancelled, nothing chosen")
                    sys.exit(0)
            else:
                open_path = resolve_open_path(args.open)
            try:
                open_path = open_path.resolve(strict=True)
            except OSError:
                log.error(f"--open: file not found: {open_path}")
                print(f"snippysnappy: file not found: {open_path}", file=sys.stderr)
                sys.exit(1)
            if not is_image_file(open_path):
                log.error(f"--open: not an image file: {open_path}")
                print(f"snippysnappy: please select an image file (got: {open_path.suffix or '(no extension)'})", file=sys.stderr)
                sys.exit(1)
            try:
                from PIL import Image as _Image
                with _Image.open(open_path) as _img:
                    _img.verify()
            except Exception:
                log.exception(f"--open: not a readable image file: {open_path}")
                print(f"snippysnappy: not a readable image file: {open_path}", file=sys.stderr)
                sys.exit(1)

            monitors = get_monitors()
            mon = None
            if BACKEND == "x11":
                try:
                    mouse_x, mouse_y = get_mouse_position()
                    mon = monitor_at_point(monitors, mouse_x, mouse_y)
                except Exception:
                    log.warning("Could not determine mouse position for --open; using fallback monitor selection")
            if mon is None and BACKEND == "wayland":
                from .capture import wayland_active_monitor   # patch 45
                mon = wayland_active_monitor(monitors)
            if mon is None:
                mon = pick_fallback_monitor(monitors)
            _, mx, my, mw, mh = mon
            try:
                from .monitor_pick import pick_review_bounds
                mx, my, mw, mh = pick_review_bounds(monitors, (mx, my, mw, mh))
            except Exception:
                log.exception("--open: could not pick a non-active monitor (non-fatal) - using the active one")

            work_path = Path(tmp) / f"opened{open_path.suffix or '.png'}"
            shutil.copy(open_path, work_path)
            log.info(f"Opening existing image: {open_path}")
            root.deiconify()
            SelectorApp(
                root, work_path, mode="window", bounds=(mx, my, mw, mh), auto_select_full=True,
                monitors=monitors, capture_tempdir=tmp,
            )
        elif args.nil is not None:
            nil_w, nil_h, nil_color = parse_nil_args(args.nil)
            log.info(f"--nil: starting with a blank {nil_w}x{nil_h} canvas ({nil_color}), no screenshot taken")

            monitors = get_monitors()
            mon = None
            if BACKEND == "x11":
                try:
                    mouse_x, mouse_y = get_mouse_position()
                    mon = monitor_at_point(monitors, mouse_x, mouse_y)
                except Exception:
                    log.warning("Could not determine mouse position for --nil; using fallback monitor selection")
            if mon is None and BACKEND == "wayland":
                from .capture import wayland_active_monitor   # patch 45
                mon = wayland_active_monitor(monitors)
            if mon is None:
                mon = pick_fallback_monitor(monitors)
            _, mx, my, mw, mh = mon
            try:
                from .monitor_pick import pick_review_bounds
                mx, my, mw, mh = pick_review_bounds(monitors, (mx, my, mw, mh))
            except Exception:
                log.exception("--nil: could not pick a non-active monitor (non-fatal) - using the active one")

            placeholder_path = Path(tmp) / "nil.png"
            make_nil_placeholder(placeholder_path, nil_color)
            root.deiconify()
            app = SelectorApp(
                root, placeholder_path, mode="window", bounds=(mx, my, mw, mh), auto_select_full=True,
                monitors=monitors, capture_tempdir=tmp,
            )
            # Activate the canvas layer immediately (rather than waiting for
            # the usual first Edit/OCR/Magnify/Resize touch) so Save/Copy
            # produce the requested size/color - including real transparency
            # - even if the user never enters Edit at all. _no_real_capture
            # skips ever pasting the 1x1 placeholder in (there's nothing
            # real to show), and _force_show_canvas_composite makes the
            # main toolbar display the blank canvas itself from the start,
            # rather than the (otherwise invisible) 1x1 placeholder file.
            app._activate_canvas_if_needed()
            app.canvas_x1, app.canvas_y1 = 0, 0
            app.canvas_x2, app.canvas_y2 = nil_w, nil_h
            app.canvas_bg_color = nil_color
            app._no_real_capture = True
            # sel_coords was set by auto_select_full to match the 1x1
            # placeholder file (0,0,1,1) - left as-is, the main toolbar's
            # crop-selection handles would cluster into a tiny box in the
            # corner instead of spanning the actual blank canvas.
            app.sel_coords = (0, 0, nil_w, nil_h)
            app._force_show_canvas_composite = True
            if app._sync_base_dims_for_mode():
                app._recalculate_display()
            # SelectorApp.__init__'s own auto_select_full handling already
            # called draw_handles() once, using the WRONG 1x1 placeholder
            # size (before any of the overrides above ran) - nothing since
            # then re-drew them, so the stale tiny handles just sat there.
            # update_dimension_display() has the exact same problem - it
            # was ALSO called once during __init__ with the wrong 1x1
            # sel_coords, so it kept reading "1 x 1" even once the handles
            # themselves were fixed.
            app.draw_handles()
            app.update_dimension_display()
            notify("Blank canvas ready", f"{nil_w}x{nil_h}, {nil_color}", blocking=False)
        elif args.restore_session:
            window_snapshots = None
            try:
                from .coordinator import safe_loads   # patch 42: plain data + images only
                rp = Path(args.restore_session)
                st = rp.lstat()
                if not rp.is_file() or rp.is_symlink() or st.st_uid != os.getuid():
                    raise PermissionError(f"{rp} is not a regular file owned by this user")
                window_snapshots = safe_loads(rp.read_bytes())
            except Exception:
                log.exception(f"--restore-session: could not load {args.restore_session!r} - starting a normal fresh select-mode capture instead")
            finally:
                try:
                    Path(args.restore_session).unlink(missing_ok=True)
                except Exception:
                    log.exception(f"--restore-session: could not remove the (one-shot) session file {args.restore_session!r} (non-fatal)")

            if not window_snapshots:
                monitors = get_monitors()
                x, y, w, h = get_virtual_screen_bounds(monitors)
                log.info(f"Select mode (restore fallback): spanning full virtual screen {w}x{h}+{x}+{y} across {len(monitors)} monitor(s)")
                capture_full_screen(full_path)
                root.deiconify()
                SelectorApp(root, full_path, mode="monitor", bounds=(x, y, w, h), capture_tempdir=tmp, capture_delay=None)
                notify("Session restore failed - starting fresh", blocking=False)
            else:
                monitors = get_monitors()
                log.info(f"--restore-session: reconstructing {len(window_snapshots)} window(s) from {args.restore_session}")
                for i, snap in enumerate(window_snapshots):
                    docs = snap["documents"]
                    cur_idx = min(snap["current_document_index"], len(docs) - 1)
                    bounds = snap.get("bounds")
                    if bounds is None:
                        _, bx, by, bw, bh = pick_fallback_monitor(monitors)
                        bounds = (bx, by, bw, bh)
                    # Only the FIRST window in this process gets the real
                    # root and the real capture_tempdir - a process only
                    # ever has one "with tempfile.TemporaryDirectory()"
                    # block, and Redo Capture (the only thing that reads
                    # capture_tempdir) only makes sense for one of them.
                    if i == 0:
                        target_root = root
                        root.deiconify()
                    else:
                        target_root = tk.Toplevel(root)
                        target_root.withdraw()
                    new_app = SelectorApp(
                        target_root, docs[cur_idx]["image_path"], mode="window", bounds=bounds,
                        auto_select_full=True, monitors=monitors, capture_tempdir=(tmp if i == 0 else None),
                    )
                    new_app.documents = docs
                    new_app.current_document_index = cur_idx
                    new_app._restore_document_state(docs[cur_idx])
                    new_app._finish_document_switch(resize_window=True)
                    new_app.side_panel_mode = snap.get("side_panel_mode")
                    if new_app.side_panel_mode is not None:
                        new_app._refresh_side_panel()
                    if i > 0:
                        target_root.deiconify()
                        target_root.lift()
                notify(f"Restored {len(window_snapshots)} window(s) after restart", blocking=False)
        else:
            monitors = get_monitors()
            x, y, w, h = get_virtual_screen_bounds(monitors)
            log.info(f"Select mode: spanning full virtual screen {w}x{h}+{x}+{y} across {len(monitors)} monitor(s)")
            capture_full_screen(full_path)
            try:
                from .capture import cached_open as _diag_open
                _dimg = _diag_open(full_path)
                if True:
                    log.info(f"Actual captured image size: {_dimg.size[0]}x{_dimg.size[1]} (compare against the virtual screen size logged above - a mismatch here means the capture tool itself, not this script, isn't spanning both monitors)")
            except Exception:
                log.exception("Could not read back captured image size for diagnostics (non-fatal)")
            root.deiconify()
            SelectorApp(root, full_path, mode="monitor", bounds=(x, y, w, h), capture_tempdir=tmp, capture_delay=delay_value)
            notify("Ready for selection", blocking=False)

        log.debug("Entering Tk mainloop")
        root.mainloop()
        shutdown_coordinator()
        log.info("Tk mainloop exited, script ending")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("Unhandled exception, snippysnappy is exiting")
