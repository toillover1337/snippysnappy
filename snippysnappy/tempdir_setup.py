# snippysnappy v1.0.1
"""Per-process private temp folders under a user-only cache directory, with cleanup of dead processes' leftovers."""
import atexit
import os
import shutil
import tempfile
import time
from pathlib import Path

from .logging_setup import log


def _owner_alive(pid):
    try:
        return b"snippysnappy" in Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False


def _newest_mtime(p):
    try:
        newest = p.lstat().st_mtime
    except OSError:
        return time.time()
    if p.is_dir() and not p.is_symlink():
        for root, dirs, files in os.walk(p):
            for name in dirs + files:
                try:
                    newest = max(newest, os.lstat(os.path.join(root, name)).st_mtime)
                except OSError:
                    pass
    return newest


def cache_dir():
    """~/.cache/snippysnappy (or $XDG_CACHE_HOME/snippysnappy), created mode 0700. Shared by every process of this user."""
    d = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")) / "snippysnappy"
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d


def _remove(p):
    if p.is_dir() and not p.is_symlink():
        shutil.rmtree(p, ignore_errors=True)
    else:
        p.unlink(missing_ok=True)


def install_temp_dir(configured=None, keep_hours=24):
    if configured:
        base = Path(str(configured)).expanduser()
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")) / "snippysnappy" / "tmp"
    try:
        base.mkdir(parents=True, exist_ok=True)
        os.chmod(base, 0o700)
        mine = base / f"p{os.getpid()}"
        mine.mkdir(exist_ok=True)
        os.chmod(mine, 0o700)
    except OSError:
        log.exception(f"Temp dir: could not set up {base} - leaving the system temp folder in use")
        return None
    tempfile.tempdir = str(mine)
    os.environ["TMPDIR"] = str(mine)

    def _rmdir_if_empty():
        try:
            mine.rmdir()
        except OSError:
            pass

    atexit.register(_rmdir_if_empty)

    now = time.time()
    swept = 0
    try:
        keep = float(keep_hours or 0) * 3600
        for d in base.iterdir():
            name = d.name
            if name.startswith("pinclips-"):
                # video-pin staging folders: a running pin moves its folder into its own p<pid> folder,
                # so one still sitting here belongs to a pin that never started or crashed early.
                if now - _newest_mtime(d) > max(keep, 3600):
                    _remove(d)
                    swept += 1
                continue
            if not (name.startswith("p") and name[1:].isdigit()):
                continue
            pid = int(name[1:])
            if pid == os.getpid() or _owner_alive(pid):
                continue
            if now - _newest_mtime(d) > keep:
                _remove(d)
                swept += 1
    except OSError:
        log.exception("Temp dir: sweep of dead processes' folders failed (non-fatal)")
    legacy = 0
    try:
        uid = os.getuid()
        keep_names = {f"snippysnappy-{uid}", f"snippysnappy-run-{uid}"}   # coordinator socket folder (no XDG_RUNTIME_DIR)
        for e in Path("/tmp").glob("snippysnappy-*"):
            try:
                if e.name in keep_names:
                    continue
                if e.lstat().st_uid != uid:
                    continue
                if now - _newest_mtime(e) > 3600:
                    _remove(e)
                    legacy += 1
            except OSError:
                pass
    except OSError:
        pass
    log.info(f"Temp dir: using {mine} (swept {swept} old folder(s) here, {legacy} old item(s) in /tmp)")
    return mine
