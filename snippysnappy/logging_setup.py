# snippysnappy v1.0.3
"""Logging setup and subprocess-run helpers (logged wrappers around run()/Popen())."""
import os
import logging
import logging.handlers
import re
import subprocess
import sys
import time
from datetime import datetime


_LOG_DATE_SEPARATOR_RE = re.compile(r"^===== (\d{4}-\d{2}-\d{2}) =====$", re.MULTILINE)


class _DateSeparatorRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """
    RotatingFileHandler that keeps a "===== YYYY-MM-DD =====" separator line
    marking each day's first log entry - log lines themselves stay time-only
    (HH:MM:SS) per LOG format, so without this, which day a given line
    belongs to is otherwise only guessable from file mtimes. A separator is
    written whenever the file's last one isn't today's date, including when
    there isn't one at all yet (brand-new file, or right after a rotation
    hands us a fresh empty one).
    """
    def _write_separator_if_needed(self):
        today = datetime.now().strftime("%Y-%m-%d")
        last_date = None
        try:
            if os.path.exists(self.baseFilename):
                # patch 43: only the tail - the last separator is all we need
                with open(self.baseFilename, "rb") as f:
                    f.seek(0, os.SEEK_END)
                    size = f.tell()
                    f.seek(max(0, size - 262144))
                    tail = f.read().decode("utf-8", errors="replace")
                matches = _LOG_DATE_SEPARATOR_RE.findall(tail)
                if not matches and size > 262144:
                    with open(self.baseFilename, "r", encoding="utf-8", errors="replace") as f:
                        matches = _LOG_DATE_SEPARATOR_RE.findall(f.read())
                if matches:
                    last_date = matches[-1]
        except Exception:
            pass
        if last_date != today:
            try:
                with open(self.baseFilename, "a", encoding="utf-8") as f:
                    f.write(f"===== {today} =====\n")
            except Exception:
                pass

    def doRollover(self):
        super().doRollover()
        self._write_separator_if_needed()

    def emit(self, record):
        # patch 43: several snippysnappy processes share this file; when another one rotated it,
        # reopen so this process writes to the current log instead of the rotated-out copy.
        try:
            if self.stream is not None:
                st_disk = os.stat(self.baseFilename)
                st_open = os.fstat(self.stream.fileno())
                if (st_disk.st_ino, st_disk.st_dev) != (st_open.st_ino, st_open.st_dev):
                    self.stream.close()
                    self.stream = self._open()
        except FileNotFoundError:
            try:
                self.stream.close()
            except Exception:
                pass
            self.stream = self._open()
        except Exception:
            pass
        super().emit(record)


def setup_logging():
    logger = logging.getLogger("snippysnappy")
    if logger.handlers:
        return logger
    logger.setLevel(logging.WARNING)   # bootstrap level; set_log_level() applies the configured LOG_LEVEL
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    logger.addHandler(console)

    # File logging is attached later by attach_file_logging(), once the user's config has been read
    # (so LOG_PATH etc. can be overridden). Until then records are buffered here.
    boot = logging.handlers.MemoryHandler(capacity=1000, flushLevel=logging.CRITICAL + 1)
    logger.addHandler(boot)
    return logger


log = setup_logging()


def attach_file_logging(log_path, max_bytes, backups):  # _p19_attach_file_logging
    """Called once from constants.py after the user config is applied. log_path None/"" = no file logging.
    Records logged before this point (buffered) are replayed into the file."""
    logger = logging.getLogger("snippysnappy")
    boot = next((h for h in logger.handlers if isinstance(h, logging.handlers.MemoryHandler)), None)
    fh = None
    if log_path:
        try:
            path = os.path.expanduser(str(log_path))
            fh = _DateSeparatorRotatingFileHandler(
                path, mode="a", maxBytes=int(max_bytes), backupCount=int(backups),
            )
            fh._write_separator_if_needed()
            fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
            logger.addHandler(fh)
        except (OSError, ValueError, TypeError) as e:
            fh = None
            logger.warning(f"Could not open log file {log_path!r} ({e}); continuing with console logging only.")
    if boot is not None:
        if fh is not None:
            boot.setTarget(fh)
            boot.flush()
        logger.removeHandler(boot)
        boot.close()
    if fh is not None:
        logger.debug(f"File logging active at {log_path} (rotating at {max_bytes} bytes, {backups} backups)")
    else:
        logger.debug("File logging disabled")


def set_log_level(level):  # _p18_log_level
    """Accepts "DEBUG"/"INFO"/"WARNING"/"ERROR"/"CRITICAL" (any case) or a logging int. Invalid -> WARNING."""
    valid = {"DEBUG": logging.DEBUG, "INFO": logging.INFO, "WARNING": logging.WARNING,
             "WARN": logging.WARNING, "ERROR": logging.ERROR, "CRITICAL": logging.CRITICAL}
    if isinstance(level, str):
        num = valid.get(level.strip().upper())
    else:
        num = level if level in valid.values() else None
    if num is None:
        log.setLevel(logging.WARNING)
        log.warning(f"Invalid LOG_LEVEL {level!r} in config - using WARNING (valid: DEBUG, INFO, WARNING, ERROR)")
        return
    log.setLevel(num)


def run_logged(cmd, **kwargs):
    """
    subprocess.run wrapper that logs the command and its outcome.
    """
    log.debug(f"Running: {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, **kwargs)
        log.debug(f"Finished: {cmd[0]} (rc={result.returncode})")
        return result
    except subprocess.CalledProcessError as e:
        log.error(f"{cmd[0]} failed (rc={e.returncode}): {getattr(e, 'stderr', '')}")
        raise
    except subprocess.TimeoutExpired:
        log.warning(f"{cmd[0]} did not answer within {kwargs.get('timeout')}s - gave up")   # _p40
        raise
    except Exception:
        log.exception(f"Unexpected error running {cmd[0]}")
        raise


def popen_logged(cmd, **kwargs):
    """
    subprocess.Popen wrapper that logs the command being launched (fire-and-forget).
    """
    log.debug(f"Launching (detached): {' '.join(cmd)}")
    try:
        return subprocess.Popen(cmd, **kwargs)
    except Exception:
        log.exception(f"Unexpected error launching {cmd[0]}")
        raise


