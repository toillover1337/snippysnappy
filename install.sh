#!/usr/bin/env bash
# snippysnappy installer / updater v1.0.0
#
# Installs snippysnappy from GitHub (a tagged release, the latest by default), keeps it
# updated, rolls back, or uninstalls. Run it as YOUR user - it only uses sudo for the
# system package step (dependencies), and asks before doing that.
#
#   ./install.sh                  install or update to the latest release (interactive)
#   ./install.sh --update         update the program files only (deps only if something is missing)
#   ./install.sh --check          show installed vs latest version (exit 10 = update available)
#   ./install.sh --version v1.2.0 install a specific tag (also: --ref main for the dev branch)
#   ./install.sh --local DIR      install from a local checkout instead of downloading
#   ./install.sh --rollback       restore the previous version from backups/
#   ./install.sh --uninstall      remove snippysnappy (asks about config, logs, backups)
#   options: --yes (assume yes)  --no-deps  --deps-only  --session x11|wayland|both
#            --shortcuts (XFCE keyboard shortcuts)  --dry-run (show what would happen)
#
# Layout (unchanged from earlier versions):
#   ~/.local/bin/snippysnappy                 wrapper on PATH
#   ~/.local/bin/snippysnappy.d/snippysnappy.py, snippysnappy/, VERSION, install.sh
#   ~/.local/bin/snippysnappy.d/backups/      previous versions (newest 3 kept)
#   ~/.config/snippysnappy/config.py          your settings - never touched by updates
#
# Override the source with SNIPPYSNAPPY_REPO=<git url> (used for testing).

set -uo pipefail

INSTALLER_VERSION="1.0.0"
REPO_URL="${SNIPPYSNAPPY_REPO:-https://github.com/toillover1337/snippysnappy}"
INSTALL_DIR="${SNIPPYSNAPPY_DIR:-$HOME/.local/bin/snippysnappy.d}"
WRAPPER="$HOME/.local/bin/snippysnappy"
DESKTOP_FILE="${XDG_DATA_HOME:-$HOME/.local/share}/applications/snippysnappy.desktop"
ICON_FILE="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/256x256/apps/snippysnappy.png"
BACKUP_DIR="$INSTALL_DIR/backups"
KEEP_BACKUPS=3
PROGRAM_FILES=(snippysnappy snippysnappy.py snippysnappy-icon.png VERSION install.sh)

ACTION="install"; REF=""; LOCAL_SRC=""; ASSUME_YES=0; DEPS=1; DEPS_ONLY=0; SESSION=""; SHORTCUTS=0; DRY=0
STEPS=()

# ---------------------------------------------------------------- output
c_b=$'\033[1;34m'; c_y=$'\033[1;33m'; c_r=$'\033[1;31m'; c_g=$'\033[1;32m'; c_0=$'\033[0m'
[[ -t 1 ]] || { c_b=""; c_y=""; c_r=""; c_g=""; c_0=""; }
LOG_FILE=""
_logf() { [[ -n "$LOG_FILE" ]] && printf '%s [%s] installer: %s\n' "$(date '+%H:%M:%S')" "$1" "$2" >>"$LOG_FILE" 2>/dev/null; return 0; }
log()  { printf '%s==>%s %s\n' "$c_b" "$c_0" "$1"; _logf INFO "$1"; }
ok()   { printf '%s ok%s %s\n' "$c_g" "$c_0" "$1"; STEPS+=("OK: $1"); _logf INFO "$1"; }
warn() { printf '%sWARNING:%s %s\n' "$c_y" "$c_0" "$1" >&2; STEPS+=("WARN: $1"); _logf WARNING "$1"; }
die()  { printf '%sERROR:%s %s\n' "$c_r" "$c_0" "$1" >&2; _logf ERROR "$1"; summary; exit 1; }
summary() {
    ((${#STEPS[@]})) || return 0
    echo; log "Summary:"; local s; for s in "${STEPS[@]}"; do echo "    $s"; done
}
ask() {   # ask "question" [default y|n]
    local q="$1" def="${2:-y}" a
    ((ASSUME_YES)) && { [[ "$def" == y ]]; return; }
    [[ -t 0 ]] || { [[ "$def" == y ]]; return; }
    if [[ "$def" == y ]]; then read -r -p "$q [Y/n] " a; [[ -z "$a" || "$a" =~ ^[Yy] ]]
    else read -r -p "$q [y/N] " a; [[ "$a" =~ ^[Yy] ]]; fi
}
run() {   # run a command, or just print it with --dry-run
    if ((DRY)); then printf '    (dry-run) %s\n' "$*"; return 0; fi
    "$@"
}

usage() { sed -n '2,27p' "$0" | sed 's/^# \{0,1\}//'; exit 0; }

# ---------------------------------------------------------------- args
while (($#)); do
    case "$1" in
        --update) ACTION="update" ;;
        --check) ACTION="check" ;;
        --rollback) ACTION="rollback" ;;
        --uninstall) ACTION="uninstall" ;;
        --version) REF="${2:?--version needs a tag, e.g. v1.0.0}"; shift; [[ "$REF" == v* ]] || REF="v$REF" ;;
        --ref) REF="${2:?--ref needs a branch or tag}"; shift ;;
        --local) LOCAL_SRC="${2:?--local needs a directory}"; shift ;;
        --yes|-y) ASSUME_YES=1 ;;
        --no-deps) DEPS=0 ;;
        --deps-only) DEPS_ONLY=1 ;;
        --session) SESSION="${2:?--session needs x11, wayland or both}"; shift ;;
        --shortcuts) SHORTCUTS=1 ;;
        --dry-run) DRY=1 ;;
        -h|--help) usage ;;
        --installer-version) echo "$INSTALLER_VERSION"; exit 0 ;;
        *) echo "Unknown option: $1 (see --help)" >&2; exit 2 ;;
    esac
    shift
done

if [[ $EUID -eq 0 ]]; then
    die "Run this as your normal user, not root - it installs into your home folder and asks for sudo only for system packages."
fi
[[ -n "$HOME" && -d "$HOME" ]] || die "\$HOME is not set to a directory."
[[ -d "$INSTALL_DIR" ]] && LOG_FILE="$INSTALL_DIR/snippysnappy.log"

# ---------------------------------------------------------------- versions
installed_version() {
    if [[ -f "$INSTALL_DIR/VERSION" ]]; then tr -d ' \n' <"$INSTALL_DIR/VERSION"; return; fi
    if [[ -f "$INSTALL_DIR/snippysnappy/constants.py" ]]; then
        sed -n 's/^SNIPPYSNAPPY_VERSION = "\([^"]*\)".*/\1/p' "$INSTALL_DIR/snippysnappy/constants.py" | head -1; return
    fi
    if [[ -f "$INSTALL_DIR/snippysnappy.py" ]]; then   # old single-file program
        local v; v=$(sed -n 's/^SNIPPYSNAPPY_VERSION = "\([^"]*\)".*/\1/p' "$INSTALL_DIR/snippysnappy.py" | head -1)
        echo "${v:-legacy}"; return
    fi
    echo ""
}

_github_api() {   # owner/repo from REPO_URL, for the no-git fallback
    local p="${REPO_URL#https://github.com/}"; p="${p%.git}"; p="${p%/}"
    [[ "$p" != "$REPO_URL" ]] && echo "$p"
}

fetch_url() {   # fetch_url URL -> stdout
    if command -v curl >/dev/null; then curl -fsSL --proto '=https' --tlsv1.2 "$1"
    elif command -v wget >/dev/null; then wget -qO- --https-only "$1"
    else return 1; fi
}

latest_tag() {
    local tags=""
    if command -v git >/dev/null; then
        tags=$(git ls-remote --tags --refs "$REPO_URL" 2>/dev/null | awk '{print $2}' | sed 's#^refs/tags/##')
    fi
    if [[ -z "$tags" ]]; then
        local p; p=$(_github_api) || true
        [[ -n "$p" ]] && tags=$(fetch_url "https://api.github.com/repos/$p/tags?per_page=100" 2>/dev/null \
            | sed -n 's/.*"name": *"\([^"]*\)".*/\1/p')
    fi
    printf '%s\n' "$tags" | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | tail -1
}

ver_gt() { [[ "$1" != "$2" && "$(printf '%s\n%s\n' "$1" "$2" | sort -V | tail -1)" == "$1" ]]; }

# ---------------------------------------------------------------- fetch + verify
fetch_release() {   # fetch_release REF DEST
    local ref="$1" dest="$2"
    if command -v git >/dev/null; then
        if git -c advice.detachedHead=false clone -q --depth 1 --branch "$ref" "$REPO_URL" "$dest/src" 2>"$dest/git.err"; then
            rm -rf "$dest/src/.git"; return 0
        fi
        warn "git clone failed ($(tail -1 "$dest/git.err")) - trying the release tarball"
    fi
    local kind="tags"; [[ "$ref" == v[0-9]* ]] || kind="heads"
    mkdir -p "$dest/src"
    fetch_url "$REPO_URL/archive/refs/$kind/$ref.tar.gz" | tar -xz --strip-components=1 -C "$dest/src" \
        || return 1
}

verify_tree() {   # verify_tree DIR EXPECTED_VERSION(or empty)
    local d="$1" want="$2" f
    for f in snippysnappy/__init__.py snippysnappy/app.py snippysnappy.py VERSION; do
        [[ -f "$d/$f" ]] || { warn "release is missing $f"; return 1; }
    done
    if [[ -n "$want" ]]; then
        local have; have=$(tr -d ' \n' <"$d/VERSION")
        [[ "$have" == "$want" ]] || { warn "release VERSION file says $have, expected $want"; return 1; }
    fi
    # refuse symlinks / anything escaping the tree
    if find "$d" -type l | grep -q .; then warn "release contains symlinks - refusing"; return 1; fi
    python3 - "$d" <<'PY' || { warn "release does not byte-compile with $(python3 --version 2>&1)"; return 1; }
import sys, pathlib
base = pathlib.Path(sys.argv[1])
bad = 0
for f in [base / "snippysnappy.py", *sorted((base / "snippysnappy").glob("*.py"))]:
    try:
        compile(f.read_bytes(), str(f), "exec")
    except SyntaxError as e:
        print(f"  {f.name}: {e}", file=sys.stderr); bad += 1
sys.exit(1 if bad else 0)
PY
}

# ---------------------------------------------------------------- dependencies
detect_pkg_manager() {
    local m; for m in emerge apt-get dnf pacman zypper; do command -v "$m" >/dev/null && { echo "$m"; return; }; done
    echo unknown
}

detect_session() {
    local t="${XDG_SESSION_TYPE:-}"
    if [[ "$t" == wayland || -n "${WAYLAND_DISPLAY:-}" ]]; then echo wayland
    elif [[ "$t" == x11 || -n "${DISPLAY:-}" ]]; then echo x11
    else echo unknown; fi
}

is_wlroots() { [[ -n "${SWAYSOCK:-}${HYPRLAND_INSTANCE_SIGNATURE:-}${WAYFIRE_SOCKET:-}" ]] \
    || [[ "${XDG_CURRENT_DESKTOP:-}" =~ (sway|Hyprland|river|Wayfire|labwc|niri) ]]; }

# requirement keys -> "check"; a check is either py:<python statement> or a command name
declare -A CHECK=(
    [tk]="py:import tkinter"           [pillow]="py:import PIL"          [pillow-tk]="py:from PIL import ImageTk"
    [ffmpeg]=ffmpeg [tesseract]=tesseract [notify]=notify-send [pulse]=pactl
    [xclip]=xclip [xdotool]=xdotool [xrandr]=xrandr [xprop]=xprop [maim]=maim
    [grim]=grim [slurp]=slurp [wl-clipboard]=wl-copy [wlr-randr]=wlr-randr [wf-recorder]=wf-recorder
    [gst]=gst-launch-1.0
)
declare -A WHAT=(
    [tk]="Python Tk (required)" [pillow]="Pillow (required)" [pillow-tk]="Pillow Tk support"
    [ffmpeg]="ffmpeg (recording, film viewer)" [tesseract]="tesseract (OCR)" [notify]="notify-send (notifications)"
    [pulse]="PulseAudio/PipeWire-pulse tools (audio recording)"
    [xclip]="xclip (clipboard)" [xdotool]="xdotool (window pick, mouse position)" [xrandr]="xrandr (monitors)"
    [xprop]="xprop" [maim]="maim (capture fallback, window capture)"
    [grim]="grim (Wayland capture)" [slurp]="slurp (Wayland region/window pick)" [wl-clipboard]="wl-clipboard"
    [wlr-randr]="wlr-randr (Wayland monitors)" [wf-recorder]="wf-recorder (Wayland recording)"
    [gst]="GStreamer + PipeWire plugin (GNOME/KDE recording)"
)

have_req() {
    local c="${CHECK[$1]}"
    if [[ "$c" == py:* ]]; then python3 -c "${c#py:}" >/dev/null 2>&1
    elif [[ "$1" == gst ]]; then command -v gst-launch-1.0 >/dev/null && gst-inspect-1.0 pipewiresrc >/dev/null 2>&1
    else command -v "$c" >/dev/null; fi
}

pkgs_for() {   # pkgs_for MANAGER KEY -> package names (space separated)
    local m="$1" k="$2"
    case "$m:$k" in
        emerge:tk) echo "dev-lang/python" ;;            emerge:pillow|emerge:pillow-tk) echo "dev-python/pillow" ;;
        emerge:ffmpeg) echo "media-video/ffmpeg" ;;      emerge:tesseract) echo "app-text/tesseract" ;;
        emerge:notify) echo "x11-libs/libnotify" ;;      emerge:pulse) echo "media-libs/libpulse" ;;
        emerge:xclip) echo "x11-misc/xclip" ;;           emerge:xdotool) echo "x11-misc/xdotool" ;;
        emerge:xrandr) echo "x11-apps/xrandr" ;;         emerge:xprop) echo "x11-apps/xprop" ;;
        emerge:maim) echo "media-gfx/maim" ;;            emerge:grim) echo "gui-apps/grim" ;;
        emerge:slurp) echo "gui-apps/slurp" ;;           emerge:wl-clipboard) echo "gui-apps/wl-clipboard" ;;
        emerge:wlr-randr) echo "gui-apps/wlr-randr" ;;   emerge:wf-recorder) echo "gui-apps/wf-recorder" ;;
        emerge:gst) echo "media-libs/gstreamer media-libs/gst-plugins-good media-libs/gst-plugins-bad media-plugins/gst-plugins-x264" ;;

        apt-get:tk) echo "python3-tk" ;;                 apt-get:pillow) echo "python3-pil" ;;
        apt-get:pillow-tk) echo "python3-pil.imagetk" ;; apt-get:ffmpeg) echo "ffmpeg" ;;
        apt-get:tesseract) echo "tesseract-ocr" ;;       apt-get:notify) echo "libnotify-bin" ;;
        apt-get:pulse) echo "pulseaudio-utils" ;;        apt-get:xrandr) echo "x11-xserver-utils" ;;
        apt-get:xprop) echo "x11-utils" ;;
        apt-get:gst) echo "gstreamer1.0-tools gstreamer1.0-pipewire gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly" ;;

        dnf:tk) echo "python3-tkinter" ;;                dnf:pillow) echo "python3-pillow" ;;
        dnf:pillow-tk) echo "python3-pillow-tk" ;;       dnf:notify) echo "libnotify" ;;
        dnf:pulse) echo "pulseaudio-utils" ;;
        dnf:gst) echo "gstreamer1 pipewire-gstreamer gstreamer1-plugins-good gstreamer1-plugins-bad-free gstreamer1-plugins-ugly-free" ;;

        pacman:tk) echo "tk" ;;                          pacman:pillow|pacman:pillow-tk) echo "python-pillow" ;;
        pacman:tesseract) echo "tesseract tesseract-data-eng" ;;
        pacman:notify) echo "libnotify" ;;               pacman:pulse) echo "libpulse" ;;
        pacman:xrandr) echo "xorg-xrandr" ;;             pacman:xprop) echo "xorg-xprop" ;;
        pacman:gst) echo "gstreamer gst-plugin-pipewire gst-plugins-good gst-plugins-bad gst-plugins-ugly" ;;

        zypper:tk) echo "python3-tk" ;;                  zypper:pillow) echo "python3-Pillow" ;;
        zypper:pillow-tk) echo "python3-Pillow-tk" ;;    zypper:tesseract) echo "tesseract-ocr" ;;
        zypper:notify) echo "libnotify-tools" ;;         zypper:pulse) echo "pulseaudio-utils" ;;
        zypper:gst) echo "gstreamer-utils gstreamer-plugin-pipewire gstreamer-plugins-good gstreamer-plugins-bad gstreamer-plugins-ugly" ;;

        *:ffmpeg) echo "ffmpeg" ;; *:xclip) echo "xclip" ;; *:xdotool) echo "xdotool" ;; *:xrandr) echo "xrandr" ;;
        *:xprop) echo "xprop" ;; *:maim) echo "maim" ;; *:grim) echo "grim" ;; *:slurp) echo "slurp" ;;
        *:wl-clipboard) echo "wl-clipboard" ;; *:wlr-randr) echo "wlr-randr" ;; *:wf-recorder) echo "wf-recorder" ;;
        *) echo "" ;;
    esac
}

gentoo_use() {   # USE flags the program relies on
    local f="/etc/portage/package.use/snippysnappy" want
    want=$'dev-lang/python tk\ndev-python/pillow tk\nmedia-video/ffmpeg x264 pulseaudio'
    if [[ -f "$f" && "$(cat "$f")" == "$want" ]]; then return 0; fi
    log "USE flags needed (python tk, pillow tk, ffmpeg x264 + pulseaudio) -> $f"
    if [[ -f "$f" ]] && ! ask "$f exists with different content. Replace it?" n; then
        warn "left $f unchanged - check the USE flags above yourself"; return 0
    fi
    printf '%s\n' "$want" | run sudo tee "$f" >/dev/null && ok "wrote $f"
}

install_deps() {
    local mgr session keys=() missing=() k pk pkgs=()
    mgr=$(detect_pkg_manager)
    session="${SESSION:-}"
    if [[ -z "$session" ]]; then
        local det; det=$(detect_session)
        if ((ASSUME_YES)) || [[ ! -t 0 ]]; then session="$det"; [[ "$session" == unknown ]] && session=x11
        else
            echo "Which display server(s) should dependencies cover? (detected: $det)"
            select s in x11 wayland both; do [[ -n "$s" ]] && { session="$s"; break; }; done
        fi
    fi
    log "Dependencies for: $session (package manager: $mgr)"
    keys=(tk pillow pillow-tk ffmpeg tesseract notify pulse)
    [[ "$session" == x11 || "$session" == both ]] && keys+=(xclip xdotool xrandr xprop maim)
    if [[ "$session" == wayland || "$session" == both ]]; then
        keys+=(wl-clipboard slurp)
        if is_wlroots || [[ "$session" == both ]]; then keys+=(grim wlr-randr wf-recorder)
        else keys+=(gst); log "GNOME/KDE-style Wayland: screenshots use the desktop portal, recording uses GStreamer + PipeWire"; fi
        [[ "$session" == wayland ]] && keys+=(xdotool xrandr)   # XWayland helpers
    fi
    for k in "${keys[@]}"; do
        if have_req "$k"; then printf '    have  %s\n' "${WHAT[$k]}"
        else printf '    MISS  %s\n' "${WHAT[$k]}"; missing+=("$k"); fi
    done
    if [[ "$mgr" == emerge ]] && ! ((DRY)); then gentoo_use; fi
    ((${#missing[@]})) || { ok "all dependencies present"; return 0; }
    if [[ "$mgr" == unknown ]]; then
        warn "no supported package manager found - install the MISS items above yourself"; return 0
    fi
    for k in "${missing[@]}"; do
        for pk in $(pkgs_for "$mgr" "$k"); do
            if [[ "$mgr" == emerge ]] && command -v portageq >/dev/null \
                && [[ -z "$(portageq best_visible / "$pk" 2>/dev/null)" ]]; then
                warn "$pk is not available in your configured repositories (maybe GURU) - skipped"; continue
            fi
            [[ " ${pkgs[*]} " == *" $pk "* ]] || pkgs+=("$pk")
        done
    done
    ((${#pkgs[@]})) || return 0
    case "$mgr" in
        emerge)  local cmd=(sudo emerge --ask --noreplace --newuse "${pkgs[@]}") ;;
        apt-get) local cmd=(sudo apt-get install "${pkgs[@]}") ;;
        dnf)     local cmd=(sudo dnf install "${pkgs[@]}") ;;
        pacman)  local cmd=(sudo pacman -S --needed "${pkgs[@]}") ;;
        zypper)  local cmd=(sudo zypper install "${pkgs[@]}") ;;
    esac
    [[ "$mgr" == dnf ]] && [[ " ${missing[*]} " == *" ffmpeg "* ]] && \
        warn "Fedora: recording needs ffmpeg with libx264 (RPM Fusion 'ffmpeg', not 'ffmpeg-free')"
    log "About to run: ${cmd[*]}"
    if ask "Install these packages now?" y; then
        if run "${cmd[@]}"; then ((DRY)) && log "(dry-run: nothing installed)" || ok "dependencies installed"; else warn "package install failed or was cancelled - continuing"; fi
    else
        warn "skipped package install - missing: ${missing[*]}"
    fi
}

# ---------------------------------------------------------------- install pieces
backup_current() {
    local cur f any=0 dest
    cur=$(installed_version); [[ -n "$cur" ]] || return 0
    for f in "${PROGRAM_FILES[@]}"; do [[ -e "$INSTALL_DIR/$f" ]] && any=1; done
    ((any)) || return 0
    dest="$BACKUP_DIR/${cur}-$(date +%Y%m%d-%H%M%S)"
    run mkdir -p "$dest" || return 1
    for f in "${PROGRAM_FILES[@]}"; do
        [[ -e "$INSTALL_DIR/$f" ]] && { run mv "$INSTALL_DIR/$f" "$dest/" || return 1; }
    done
    ok "backed up $cur to ${dest/#$HOME/~}"
    # prune: keep the newest KEEP_BACKUPS
    local old; old=$(ls -1dt "$BACKUP_DIR"/*/ 2>/dev/null | tail -n +$((KEEP_BACKUPS + 1)))
    [[ -n "$old" ]] && while IFS= read -r f; do run rm -rf -- "$f"; done <<<"$old"
    return 0
}

place_files() {   # place_files SRC
    local src="$1" f
    for f in "${PROGRAM_FILES[@]}"; do
        [[ -e "$src/$f" ]] || continue
        run cp -a "$src/$f" "$INSTALL_DIR/$f" || return 1
    done
    run find "$INSTALL_DIR/snippysnappy" -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null
    run chmod 755 "$INSTALL_DIR/snippysnappy.py"
    [[ -f "$INSTALL_DIR/install.sh" ]] && run chmod 755 "$INSTALL_DIR/install.sh"
    run chmod -R go-w "$INSTALL_DIR/snippysnappy"
    return 0
}

write_if_changed() {   # write_if_changed PATH CONTENT MODE
    local path="$1" content="$2" mode="$3"
    if [[ -f "$path" && "$(cat "$path")" == "$content" ]]; then return 0; fi
    if [[ -f "$path" ]] && ! ask "$path differs from what this version installs. Replace it?" y; then
        warn "kept your $path"; return 0
    fi
    ((DRY)) && { echo "    (dry-run) write $path"; return 0; }
    mkdir -p "$(dirname "$path")" && printf '%s\n' "$content" >"$path" && chmod "$mode" "$path" && ok "wrote ${path/#$HOME/~}"
}

write_launchers() {
    write_if_changed "$WRAPPER" '#!/bin/bash
exec python3 "$HOME/.local/bin/snippysnappy.d/snippysnappy.py" "$@"' 755
    if [[ -f "$INSTALL_DIR/snippysnappy-icon.png" ]] && ! ((DRY)); then
        mkdir -p "$(dirname "$ICON_FILE")" && cp -f "$INSTALL_DIR/snippysnappy-icon.png" "$ICON_FILE"
        command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -t "${ICON_FILE%/256x256/*}" 2>/dev/null
    fi
    write_if_changed "$DESKTOP_FILE" "[Desktop Entry]
Type=Application
Name=snippysnappy
Comment=Screenshot capture, annotation, OCR and screen recording
Exec=$WRAPPER
Icon=snippysnappy
Terminal=false
Categories=Utility;Graphics;
StartupWMClass=Snippysnappy" 644
    case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) warn "~/.local/bin is not on your PATH - add it in your shell profile to run 'snippysnappy' by name" ;; esac
}

xfce_shortcuts() {
    command -v xfconf-query >/dev/null || { warn "xfconf-query not found - keyboard shortcuts not set (not XFCE?)"; return 0; }
    local -a binds=(
        "Print|snippysnappy"                        "<Primary>Print|snippysnappy --window"
        "<Super>Print|snippysnappy --fullscreen"    "<Shift>Print|snippysnappy --delay"
        "<Alt>Print|snippysnappy --record"          "<Primary><Alt>Print|snippysnappy --record --window"
        "<Super><Alt>Print|snippysnappy --record --fullscreen"
        "<Shift><Alt>Print|snippysnappy --record --delay"
        "<Super><Primary>Print|snippysnappy --open" "<Super><Primary><Shift>Print|snippysnappy --nil"
    )
    log "XFCE keyboard shortcuts (the scheme from 'snippysnappy --help'):"
    local b key cmd cur
    for b in "${binds[@]}"; do
        key="${b%%|*}"; cmd="$WRAPPER${b#*|snippysnappy}"
        cur=$(xfconf-query -c xfce4-keyboard-shortcuts -p "/commands/custom/$key" 2>/dev/null || true)
        if [[ "$cur" == "$cmd" ]]; then printf '    already  %-30s %s\n' "$key" "$cmd"; continue; fi
        if [[ -n "$cur" ]] && ! ask "  $key is bound to '$cur'. Rebind to '$cmd'?" n; then continue; fi
        [[ -z "$cur" ]] && ! ask "  Bind $key -> $cmd ?" y && continue
        run xfconf-query -c xfce4-keyboard-shortcuts -p "/commands/custom/$key" -n -t string -s "$cmd" \
            && ok "shortcut $key -> ${b#*|}"
    done
}

verify_install() {
    local want="$1" got
    ((DRY)) && return 0
    got=$(python3 "$INSTALL_DIR/snippysnappy.py" --version 2>/dev/null | awk '{print $2}')
    [[ "$got" == "$want" ]]
}

do_rollback() {
    local last; last=$(ls -1dt "$BACKUP_DIR"/*/ 2>/dev/null | head -1)
    [[ -n "$last" ]] || die "no backups in ${BACKUP_DIR/#$HOME/~}"
    last="${last%/}"
    log "Rolling back to ${last##*/} (current: $(installed_version))"
    ask "Continue?" y || exit 0
    local hold; hold=$(mktemp -d "$INSTALL_DIR/.rollback.XXXXXX") || die "mktemp failed"
    local f
    for f in "${PROGRAM_FILES[@]}"; do [[ -e "$INSTALL_DIR/$f" ]] && run mv "$INSTALL_DIR/$f" "$hold/"; done
    for f in "${PROGRAM_FILES[@]}"; do [[ -e "$last/$f" ]] && run mv "$last/$f" "$INSTALL_DIR/"; done
    run rmdir "$last" 2>/dev/null
    # what we just replaced becomes the newest backup, so a second --rollback undoes this one
    local v; v=$(cd "$hold" && { [[ -f VERSION ]] && tr -d ' \n' <VERSION || echo unknown; })
    run mv "$hold" "$BACKUP_DIR/${v}-rolled-back-$(date +%Y%m%d-%H%M%S)"
    ok "now at $(installed_version)"
    summary
}

do_uninstall() {
    log "This removes the program files, the wrapper, the menu entry and the icon."
    ((ASSUME_YES)) || ask "Uninstall snippysnappy?" n || exit 0
    local f
    for f in "${PROGRAM_FILES[@]}"; do [[ -e "$INSTALL_DIR/$f" ]] && run rm -rf -- "$INSTALL_DIR/$f"; done
    for f in "$WRAPPER" "$DESKTOP_FILE" "$ICON_FILE"; do [[ -e "$f" ]] && run rm -f -- "$f"; done
    ok "program removed"
    [[ -d "$BACKUP_DIR" ]] && ask "Also delete backups of older versions?" n && run rm -rf -- "$BACKUP_DIR" && ok "backups removed"
    [[ -d "$HOME/.config/snippysnappy" ]] && ask "Also delete your settings (~/.config/snippysnappy)?" n \
        && run rm -rf -- "$HOME/.config/snippysnappy" && ok "settings removed"
    if [[ -d "$INSTALL_DIR" ]] && ask "Also delete the log and sniptext folder in ${INSTALL_DIR/#$HOME/~}?" n; then
        run rm -rf -- "$INSTALL_DIR" && ok "${INSTALL_DIR/#$HOME/~} removed"
    fi
    if command -v xfconf-query >/dev/null; then
        local props; props=$(xfconf-query -c xfce4-keyboard-shortcuts -l -v 2>/dev/null | awk -v w="$WRAPPER" '$2 == w {print $1}')
        if [[ -n "$props" ]] && ask "Remove XFCE shortcuts that run snippysnappy?" y; then
            xfconf-query -c xfce4-keyboard-shortcuts -l -v 2>/dev/null | grep -F "$WRAPPER" | awk '{print $1}' \
                | while read -r p; do run xfconf-query -c xfce4-keyboard-shortcuts -p "$p" -r; done
            ok "shortcuts removed"
        fi
    fi
    echo "Screenshots and recordings in ~/Pictures and ~/Videos were left alone."
    summary
}

# ---------------------------------------------------------------- main
CUR=$(installed_version)

case "$ACTION" in
    check)
        LATEST=$(latest_tag)
        echo "installed: ${CUR:-none}"
        echo "latest:    ${LATEST:-unknown (no tags reachable)}"
        if [[ -n "$LATEST" && -n "$CUR" ]] && ver_gt "${LATEST#v}" "$CUR"; then echo "update available"; exit 10; fi
        exit 0 ;;
    rollback) do_rollback; exit 0 ;;
    uninstall) do_uninstall; exit 0 ;;
esac

if ((DEPS_ONLY)); then install_deps; summary; exit 0; fi

# which version?
SRC_DIR=""; WANT=""; STAGE=""
cleanup() { [[ -n "$STAGE" && -d "$STAGE" ]] && rm -rf -- "$STAGE"; }
trap cleanup EXIT

if [[ -n "$LOCAL_SRC" ]]; then
    SRC_DIR=$(cd "$LOCAL_SRC" && pwd) || die "no such directory: $LOCAL_SRC"
    WANT=$(tr -d ' \n' <"$SRC_DIR/VERSION" 2>/dev/null)
    log "Installing from local folder $SRC_DIR (version ${WANT:-?})"
else
    EXPLICIT_REF="$REF"
    if [[ -z "$REF" ]]; then
        REF=$(latest_tag)
        if [[ -z "$REF" ]]; then
            warn "no release tags found at $REPO_URL - using the main branch"
            REF="main"
        fi
    fi
    [[ "$REF" == v[0-9]* ]] && WANT="${REF#v}"
    if [[ "$ACTION" == update && -n "$WANT" && "$CUR" == "$WANT" ]]; then
        log "snippysnappy $CUR is already the latest release."; exit 0
    fi
    log "Installed: ${CUR:-none}   ->   installing: $REF"
    if [[ -n "$CUR" && -n "$WANT" ]] && ver_gt "$CUR" "$WANT"; then
        ask "That is OLDER than what you have ($CUR). Downgrade?" "$([[ -n "$EXPLICIT_REF" ]] && echo y || echo n)" \
            || { log "Not downgrading."; exit 0; }
    fi
fi

mkdir -p "$INSTALL_DIR" "$INSTALL_DIR/sniptext" || die "cannot create $INSTALL_DIR"
chmod 700 "$INSTALL_DIR" 2>/dev/null
LOG_FILE="$INSTALL_DIR/snippysnappy.log"
_logf INFO "installer $INSTALLER_VERSION: $ACTION ${REF:-local} (installed: ${CUR:-none})"

if ((DEPS)); then
    if [[ "$ACTION" == update ]]; then
        miss=0; for k in tk pillow; do have_req "$k" || miss=1; done
        ((miss)) && install_deps
    else
        install_deps
    fi
fi
python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null || die "Python 3.9 or newer is required ($(python3 --version 2>&1))."
have_req tk || die "Python's tkinter is not available - install it (Gentoo: dev-lang/python with USE=tk) and re-run."
have_req pillow || die "Pillow is not available for $(command -v python3) - install it and re-run."

if [[ -z "$SRC_DIR" ]]; then
    STAGE=$(mktemp -d "$INSTALL_DIR/.staging.XXXXXX") || die "mktemp failed"
    log "Downloading $REF from $REPO_URL"
    fetch_release "$REF" "$STAGE" || die "download failed (no git, curl or wget? network?)"
    SRC_DIR="$STAGE/src"
    [[ -z "$WANT" ]] && WANT=$(tr -d ' \n' <"$SRC_DIR/VERSION" 2>/dev/null)
fi
verify_tree "$SRC_DIR" "$WANT" || die "release check failed - nothing was changed"
ok "release $WANT verified"

backup_current || die "backup failed - nothing was replaced"
if ! place_files "$SRC_DIR"; then
    warn "copying files failed - restoring the previous version"
    ASSUME_YES=1 do_rollback; exit 1
fi
ok "installed program files ($WANT)"
write_launchers

if ((SHORTCUTS)) || { [[ -z "$CUR" ]] && [[ "${XDG_CURRENT_DESKTOP:-}" == *XFCE* ]] && ask "Set up XFCE keyboard shortcuts (Print and friends)?" y; }; then
    xfce_shortcuts
fi

if verify_install "$WANT"; then
    ok "snippysnappy --version reports $WANT"
else
    warn "the new version did not start cleanly ('python3 $INSTALL_DIR/snippysnappy.py --version')"
    if [[ -n "$CUR" ]] && ask "Roll back to $CUR?" y; then ASSUME_YES=1 do_rollback; exit 1; fi
fi
summary
echo
log "Done: snippysnappy $WANT. Update later with: ${INSTALL_DIR/#$HOME/~}/install.sh --update"
