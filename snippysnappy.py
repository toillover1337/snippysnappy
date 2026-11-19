#!/usr/bin/env python3
# snippysnappy v1.0.5
"""
Thin launcher - NOT part of the snippysnappy package itself. This is what
you install at ~/.local/bin/snippysnappy.d/snippysnappy.py (same filename
and same location the old single-file version used), so your existing
`~/.local/bin/snippysnappy` wrapper script (`exec python3
"$HOME/.local/bin/snippysnappy.d/snippysnappy.py" "$@"`) keeps working
completely unchanged - it just now dispatches into the snippysnappy/
package folder that sits right next to this file, instead of being the
whole program itself.

Install layout (matches the old layout, with one new folder):
    ~/.local/bin/snippysnappy.d/
        snippysnappy.py       <- this file
        snippysnappy/         <- the package (constants.py, selector_app.py, etc.)
        snippysnappy.log      <- unchanged location
        sniptext/              <- unchanged location
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))



def _tray_cli(argv):
    """snippysnappy --tray-json | --tray-status | --tray-menu [CMD] | --tray-do ID - talk to the running instance's control
    socket (see snippysnappy/tray.py). Handled here, before the package is imported, so waybar polling stays cheap."""
    import json
    import shutil
    import socket
    import subprocess
    cmd = argv[0]
    path = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/snippysnappy-run-{os.getuid()}", "snippysnappy", "tray.sock")

    def ask(req):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(3)
        try:
            s.connect(path)
            s.sendall((json.dumps(req) + "\n").encode())
            data = b""
            while not data.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
            return json.loads(data.decode())
        except (OSError, ValueError):
            return None
        finally:
            s.close()

    def leaves(nodes, prefix=""):
        out = []
        for n in nodes:
            if n.get("separator"):
                continue
            label = prefix + n.get("label", "")
            if n.get("children"):
                out += leaves(n["children"], label + " > ")
            elif n.get("id"):
                out.append((label, n["id"]))
        return out

    if cmd == "--tray-status":
        r = ask({"cmd": "status"})
        print(json.dumps({k: v for k, v in r.items() if k != "ok"} if r else
                         {"text": "", "tooltip": "snippysnappy is not running", "class": "inactive", "alt": "inactive"}))
        return 0
    if cmd == "--tray-json":
        r = ask({"cmd": "tree"})
        if not r:
            print("snippysnappy: not running (no control socket at %s)" % path, file=sys.stderr)
            return 1
        print(json.dumps(r["tree"]))
        return 0
    if cmd in ("--tray-do", "--tray-menu"):
        if cmd == "--tray-do":
            if len(argv) < 2:
                print("snippysnappy: --tray-do needs an id (see --tray-json)", file=sys.stderr)
                return 2
            choice = argv[1]
        else:
            r = ask({"cmd": "tree"})
            if r:
                items = leaves(r["tree"])
            else:
                sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
                from snippysnappy.tray import static_tree
                items = leaves(static_tree())
            launcher = " ".join(argv[1:])
            if not launcher:
                try:
                    from snippysnappy.constants import TRAY_LAUNCHER as launcher
                except Exception:
                    launcher = ""
                try:
                    cfg = {}
                    exec(open(os.path.expanduser("~/.config/snippysnappy/config.py")).read(), cfg)
                    launcher = cfg.get("TRAY_LAUNCHER", launcher)
                except Exception:
                    pass
            if not launcher:
                for cand in ("fuzzel --dmenu", "wofi --dmenu", "rofi -dmenu", "bemenu", "tofi", "dmenu"):
                    if shutil.which(cand.split()[0]):
                        launcher = cand
                        break
            if not launcher:
                print("snippysnappy: no menu program found - install fuzzel/wofi/rofi/bemenu/dmenu or set TRAY_LAUNCHER", file=sys.stderr)
                return 1
            import shlex
            p = subprocess.run(shlex.split(launcher), input="\n".join(l for l, _ in items), text=True, stdout=subprocess.PIPE)
            picked = p.stdout.strip()
            choice = dict(items).get(picked)
            if not choice:
                return 0
        r = ask({"cmd": "do", "id": choice})
        if r is None and (choice.startswith("new:") or choice in ("edit-open", "edit-video", "edit-blank")):
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from snippysnappy.tray import launch      # no running instance: just start it
            launch(choice)
            return 0
        return 0 if r and r.get("ok") else 1
    print(f"snippysnappy: unknown option {cmd}", file=sys.stderr)
    return 2


def _update_cli(extra):
    """snippysnappy -u / --update [installer options]: run the installer's update. Uses the install.sh that sits next to this
    file; if it was deleted, downloads the installer from GitHub first (it then fetches the latest tagged release itself).
    Handled before the package is imported, so it also repairs an install whose package files are broken."""
    import shutil
    import subprocess
    import tempfile
    here = os.path.dirname(os.path.abspath(__file__))
    if os.path.isdir(os.path.join(here, ".git")):
        print("snippysnappy: this is a git checkout, not an installed copy - update it with 'git pull' "
              "(then ./install.sh --local . to install it).", file=sys.stderr)
        return 1
    script = os.path.join(here, "install.sh")
    tmp = None
    if not os.path.isfile(script):
        url = "https://raw.githubusercontent.com/toillover1337/snippysnappy/main/install.sh"
        print(f"snippysnappy: install.sh not found next to the program - downloading it from {url}")
        try:
            import urllib.request
            tmp = tempfile.mkdtemp(prefix="snippysnappy-update-")
            script = os.path.join(tmp, "install.sh")
            with urllib.request.urlopen(url, timeout=30) as r, open(script, "wb") as f:
                f.write(r.read())
            os.chmod(script, 0o700)
        except Exception as e:
            print(f"snippysnappy: could not download the installer ({e}). Get it from "
                  "https://github.com/toillover1337/snippysnappy and run ./install.sh --update", file=sys.stderr)
            return 1
    if not shutil.which("bash"):
        print("snippysnappy: bash is needed to run the installer", file=sys.stderr)
        return 1
    env = dict(os.environ, SNIPPYSNAPPY_DIR=here)
    try:
        return subprocess.call(["bash", script, "--update"] + list(extra), env=env)
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


if len(sys.argv) > 1 and sys.argv[1] in ("-u", "--update"):
    sys.exit(_update_cli(sys.argv[2:]))

if len(sys.argv) > 1 and sys.argv[1] in ("--tray-json", "--tray-status", "--tray-menu", "--tray-do"):
    sys.exit(_tray_cli(sys.argv[1:]))

from snippysnappy.app import main

if __name__ == "__main__":
    try:
        main()
    except Exception:
        from snippysnappy.logging_setup import log
        log.exception("Unhandled exception, snippysnappy is exiting")
