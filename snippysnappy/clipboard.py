# snippysnappy v1.0.1
"""Clipboard read/write for images and text (X11 and Wayland)."""
import subprocess

from .logging_setup import log, run_logged, popen_logged
from .capture import BACKEND

def copy_image_to_clipboard_x11(path):
    popen_logged(
        ["xclip", "-selection", "clipboard", "-t", "image/png", "-i", str(path)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def copy_image_to_clipboard_wayland(path):
    with open(path, "rb") as f:
        data = f.read()
    p = popen_logged(
        ["wl-copy", "-t", "image/png"],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    p.stdin.write(data)
    p.stdin.close()


def copy_image_to_clipboard(path):
    if BACKEND == "wayland":
        copy_image_to_clipboard_wayland(path)
    else:
        copy_image_to_clipboard_x11(path)


def copy_text_to_clipboard_x11(text):
    p = popen_logged(
        ["xclip", "-selection", "clipboard"],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    p.stdin.write(text.encode())
    p.stdin.close()


def copy_text_to_clipboard_wayland(text):
    p = popen_logged(
        ["wl-copy"],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    p.stdin.write(text.encode())
    p.stdin.close()


def copy_text_to_clipboard(text):
    if BACKEND == "wayland":
        copy_text_to_clipboard_wayland(text)
    else:
        copy_text_to_clipboard_x11(text)


def get_system_clipboard_image():
    """
    Returns a PIL Image if the system clipboard currently holds image data
    (from any application, not just this one), else None. Used by the
    system-register paste shortcuts (Ctrl+Shift+V / Super+p).
    """
    from PIL import Image
    import io
    cmd = ["wl-paste", "-t", "image/png"] if BACKEND == "wayland" else ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"]
    try:
        result = run_logged(cmd, capture_output=True, check=True, timeout=3)   # _p40: a hung clipboard owner must not freeze the window
        if not result.stdout:
            return None
        return Image.open(io.BytesIO(result.stdout)).convert("RGBA")
    except Exception:
        log.debug("get_system_clipboard_image: no image data on the clipboard (or the read failed)")
        return None


def get_system_clipboard_text():
    """
    Returns the system clipboard's plain-text content (from any
    application), or None if it holds no text. Used by the system-register
    paste shortcuts when the target is a text box rather than the canvas.
    """
    cmd = ["wl-paste", "-n"] if BACKEND == "wayland" else ["xclip", "-selection", "clipboard", "-o"]
    try:
        result = run_logged(cmd, capture_output=True, check=True, text=True, timeout=3)
        return result.stdout if result.stdout else None
    except Exception:
        log.debug("get_system_clipboard_text: no text data on the clipboard (or the read failed)")
        return None


