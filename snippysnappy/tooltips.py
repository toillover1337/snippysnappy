# snippysnappy v1.0.1
"""
Hover tips for toolbar buttons (and a few checkboxes). Looked up by the button's own label text:
    (kind, mode, label)  ->  (kind, label)  ->  label
kind is "image" or "film" (set on the window's root); mode is where in the UI you are
("main", "edit", "tools", "resize", "ocr", "measure", "magnify", "pin" for image; "main", "edit",
"tools" for film). Buttons in dialogs and popups have no kind and get no tip from here.

Change a tip in your config instead of editing this file (it is overwritten by updates):
    TOOLTIP_OVERRIDES = {"Copy": "my own text", ("film", "Copy"): "film-only text"}
"""

TIPS = {
    # ---- main toolbar ----
    "Edit": "Open the editing tools",
    "Tools": "Open the Tools menu (Extract Text, Measure, Magnify, Pin)",
    "Save@main": "Show the save options",
    ("image", "Save@row"): "Save to your Snippysnaps folder (asks for a name)",
    ("film", "Save@row"): "Save to your Snippyfilms folder",
    ("image", "Sloppy Save"): "Quick-save to the temporary Sloppysnaps folder (cleaned up automatically)",
    ("film", "Sloppy Save"): "Quick-save to the temporary Sloppyfilms folder (cleaned up automatically)",
    "Sloppy Save": "Quick-save to the temporary Sloppy folder (cleaned up automatically)",
    "Save As…": "Save with a file type, folder and name you choose",
    "Confirm": "Confirm the name and save",
    ("image", "Copy"): "Copy the image to the clipboard",
    ("film", "Copy"): "Copy the film file to the clipboard",
    ("image", "main", "Redo"): "Discard this capture and take a new one",
    ("image", "main", "Cancel"): "Discard this capture (asks first)",
    ("film", "main", "Cancel"): "Discard this recording and delete the file (asks first)",
    "Also save": "Also save a file when you press Copy",
    "Also copy": "Also copy to the clipboard when you save",
    "Also exit": "Close the window after Copy / Save",
    "Restart All": "Restart every open snippysnappy window (asks first)",
    "Re-scale": "Re-fit the zoom to the window",
    "Restart": "Pause and jump back to the first frame",
    "Play": "Play the film",
    "Pause": "Pause the film",
    "Mute": "Mute playback (does not change the saved film)",
    "Loop": "Start again when the film ends",
    "Autoplay": "Start playing automatically",

    # ---- edit mode ----
    ("image", "edit", "Undo"): "Undo the last edit",
    ("image", "edit", "Redo"): "Redo the last undone edit",
    ("film", "edit", "Undo"): "Undo the last edit",
    ("film", "edit", "Redo"): "Redo the last undone edit",
    ("image", "edit", "Done"): "Keep the edits and leave the editor",
    ("film", "edit", "Done"): "Keep the edits (applied when you Save or Copy) and leave the editor",
    ("image", "edit", "Cancel"): "Throw away everything done since you entered the editor",
    ("film", "edit", "Cancel"): "Throw away everything done since you entered the editor",
    ("image", "Snip"): "Lift a region into a movable, resizable, rotatable object",
    ("film", "Snip"): "Copy a region of the frame into a movable, resizable, rotatable object",
    ("image", "Draw"): "Pen, spray can, highlighter and eraser",
    ("film", "Draw"): "Pen and highlighter",
    "Text": "Add and edit text boxes",
    "Shape": "Draw lines, boxes and ellipses",
    "Resize": "Resize the picture or its canvas",
    "Preview": "Play your current edits (including effects) as a low-resolution render; nothing is changed",
    "Insert ▾": "Insert a film, a still image, a black/white gap or a copy of the marked region at the chosen handle",
    "Audio": "Edit the audio: mute, boost, fade",
    "Film": "Trim, splice or add effects (fade, checkerboard, plugins) to the film; effects show up after export",
    "Trim": "Trim the start and end of the film, or splice it",
    "Select": "Select, move, resize and rotate objects",
    "Back": "Go back to the previous toolbar",
    "Undo": "Undo the last change",
    "Redo": "Redo the last undone change",

    # ---- tools ----
    "Extract Text": "OCR: extract text from the image",
    "Measure": "Measure distances with boxes and lines",
    "Magnify": "Show a magnifier lens over the picture",
    "Pin": "Pin a region of the screen on your desktop",
    "Pins (0)": "Show or hide the pins opened from this window",
    ("image", "measure", "Clear"): "Remove all measurement boxes",
    ("image", "pin", "Clear"): "Close all pins opened from this window",
    "Clear": "Clear everything this tool has drawn",
    "Run OCR": "Extract text from the painted region",
    "Extract": "Extract text from the painted region",
    "Copy Text": "Copy the extracted text to the clipboard",
    "Save Text": "Save the extracted text",
    "Proportional": "Keep the aspect ratio while resizing",

    # ---- resize / trim / misc ----
    "Apply Scale": "Resize by the percentage on the slider",
    "Apply Dimensions": "Resize to the width and height typed in W and H",
    "Reset": "Reset this tool's changes",
    "Set start here": "Start the film at the current playback position",
    "Set end here": "End the film at the current playback position",
    "Cut": "Cut the film at the current handles into separate pieces",
    "Delete": "Delete the selected item",
    "Delete layer": "Delete the selected layer",
    "Clear crop": "Remove the crop from the selected object",
    "Add text": "Place this text",
    "None": "No color",
    "+ New Layer": "Add a new layer",
    "+ Blank": "Open a blank canvas in a new tab",
    "+ Open File…": "Open a file in a new tab",
}


def _lookup(table, key):
    return table.get(key) if isinstance(table, dict) else None


def _has_sibling_button(widget, labels):
    try:
        for child in widget.master.winfo_children():
            try:
                if child is not widget and child.cget("text") in labels:
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def toolbar_tip(widget):
    """Tip text for this widget or None. Never raises."""
    try:
        top = widget.winfo_toplevel()
        kind = getattr(top, "_tip_kind", None)
        if kind is None:
            return None
        label = widget.cget("text")
        if not label:
            return None
        if label == "Save":
            label = "Save@main" if _has_sibling_button(widget, ("Edit", "Tools")) else "Save@row"
        mode_fn = getattr(top, "_tip_mode", None)
        mode = mode_fn() if callable(mode_fn) else None
        keys = [(kind, mode, label), (kind, label), label]
        try:
            from . import constants
            custom = getattr(constants, "TOOLTIP_OVERRIDES", None)
        except Exception:
            custom = None
        for table in (custom, TIPS):
            for key in keys:
                tip = _lookup(table, key)
                if tip:
                    return tip
            # "Save@main" can also be overridden as plain "Save"
            if table is custom and label.startswith("Save@"):
                tip = _lookup(table, "Save")
                if tip:
                    return tip
    except Exception:
        return None
    return None


def image_mode(app):
    if getattr(app, "resize_mode_active", False):
        return "resize"
    if getattr(app, "ocr_paint_mode_active", False):
        return "ocr"
    if getattr(app, "measure_mode_active", False):
        return "measure"
    if getattr(app, "magnify_mode_active", False):
        return "magnify"
    if getattr(app, "pin_mode_active", False):
        return "pin"
    if getattr(app, "edit_mode_active", False):
        return "edit"
    if getattr(app, "tools_row_expanded", False):
        return "tools"
    return "main"


def film_mode(app):
    if getattr(app, "_ed2_active", False):
        return "edit"
    if getattr(app, "_tl_tools_frame", None) is not None:
        return "tools"
    return "main"
