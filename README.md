# snippysnappy

Screenshot capture, annotation, OCR and screen recording for Linux desktops. It's written in Python/Tk and depends on very little: Python, Pillow and standard system tools.

- **Capture:** select a region (overlay across all monitors), a window, or the monitor under the mouse. You can also add a delay, open an existing image, or start from a blank canvas.
- **Edit:** pen, highlighter, spray, shapes, text (with vim keys), snip/move, crop and resize, layers, undo/redo.
- **OCR:** "Extract Text" runs tesseract and copies the result to the clipboard.
- **Pins:** keep a capture floating on screen.
- **Recording:** a region, window or monitor, with optional per-source audio tracks, and a film editor with trim, splice, drawing, Filmsnap, OCR and export.
- **X11 and Wayland:**
  - X11: Pillow/XCB, with maim as fallback.
  - wlroots compositors (Sway, Hyprland, river, Wayfire, labwc): grim, slurp and wf-recorder.
  - GNOME/KDE: the desktop portal, plus PipeWire for recording.

## Install

```sh
curl -fsSLO https://raw.githubusercontent.com/toillover1337/snippysnappy/main/install.sh
chmod +x install.sh
./install.sh
```

Run it as your normal user, not with sudo. The installer:

1. checks for dependencies and lists anything missing. It asks before installing them with your package manager (emerge, apt, dnf, pacman, zypper, guix or nix); this is the only step that uses `sudo`. On Gentoo it also writes the needed USE flags to `/etc/portage/package.use/snippysnappy`.
2. downloads the latest tagged release, checks it, and installs it to `~/.local/bin/snippysnappy.d/`. It also adds the wrapper `~/.local/bin/snippysnappy`, a menu entry and an icon.
3. on XFCE, offers to set up the keyboard shortcuts below.

### Nix and Guix

The installer uses `nix profile install` (or `nix-env`) and `guix install`. These install into your user profile, so no sudo is needed. It installs Python as one environment that includes Tk and Pillow, and the `snippysnappy` wrapper runs that specific Python. On NixOS and Guix System it also prints a snippet for adding the dependencies to your system or home configuration. If you'd rather manage them that way, run it with `--no-deps --python /path/to/python3`.

GNOME/KDE recording on Nix needs GStreamer's plugin path set up, which is easiest done in your NixOS configuration.

## Update, roll back, remove

```sh
~/.local/bin/snippysnappy.d/install.sh --check       # installed vs latest (exit code 10 = update available)
~/.local/bin/snippysnappy.d/install.sh --update      # update to the latest release
~/.local/bin/snippysnappy.d/install.sh --version v1.0.0   # install a specific release
~/.local/bin/snippysnappy.d/install.sh --rollback    # go back to the previous version
~/.local/bin/snippysnappy.d/install.sh --uninstall
```

Each update keeps the previous version in `~/.local/bin/snippysnappy.d/backups/` (the newest 3 are kept). Your settings in `~/.config/snippysnappy/config.py` are never touched by updates. Other options: `--yes`, `--no-deps`, `--deps-only`, `--session x11|wayland|both`, `--shortcuts`, `--python PATH`, `--dry-run`, `--local DIR`.

## Usage

```
snippysnappy                 select a region (Print)
snippysnappy --window        a window (Ctrl+Print)
snippysnappy --fullscreen    the monitor under the mouse (Super+Print)
snippysnappy --delay [N]     select, then capture after N seconds (Shift+Print)
snippysnappy --record        start/stop a recording (Alt+Print; add Ctrl=-w, Super=-f, Shift=-d)
snippysnappy --open [FILE]   open an image (Super+Ctrl+Print)
snippysnappy --nil           blank canvas (Super+Ctrl+Shift+Print)
snippysnappy --version
```

`snippysnappy --help` lists everything. To change a setting, edit `~/.config/snippysnappy/config.py`; it is created with every option commented out the first time you run the program.

## Dependencies

| | |
|---|---|
| required | Python 3.9+ with Tk, Pillow |
| recommended | ffmpeg (with libx264 and PulseAudio input) for recording and the film viewer, tesseract for OCR, notify-send, pactl/pacat for audio |
| X11 | xclip, xdotool, xrandr, xprop, maim |
| Wayland (wlroots) | grim, slurp, wl-clipboard, wlr-randr, wf-recorder (plus XWayland, which Tk needs) |
| Wayland (GNOME/KDE) | xdg-desktop-portal with your desktop's backend, wl-clipboard, GStreamer with the PipeWire plugin (for recording) |

### Wayland notes

- On tiling compositors, snippysnappy windows float automatically (Sway/Hyprland). Turn this off with `WAYLAND_FLOAT_WINDOWS = False`.
- On Sway, the Pins list's "View" button can only bring a window forward if you set `focus_on_window_activation focus`.
- On GNOME/KDE, the desktop asks for permission the first time you take a screenshot or start a recording.

## License

GPL-3.0, see `LICENSE`.
