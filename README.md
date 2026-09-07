snippysnappy - lightweight screenshot capture with adjustable selection,free-hand edit (pen/highlighter), and OCR text extraction.

Invocations:

    snippysnappy               
    
    Select mode: overlay on the monitor under your cursor, drag to select a region.
    
    snippysnappy --fullscreen  
    
    Whole monitor under your cursor is captured and pre-selected immediately (no drag needed) in a normal decorated window; drag the corner handles if you want to crop it further.
    
    snippysnappy --window      
    
    Click a window to capture just its content, in a normal decorated window.

Main toolbar:

    - Copy: straight to clipboard, no file written, closes immediately.
    
    - Save: "Normal Save" (prompts for a filename, default snipsnap-<timestamp>, into ~/Pictures/Screenshots) or "Slop Save" (no prompt, fixed name slopsnap-<timestamp>.png, into a separate folder for quick/incidental shots). A checkbox (on by default) also copies to clipboard on save.
    
    - Edit: color palette, Pen (opaque) or Highlighter (translucent), Done bakes it in.
    
    - Extract Text: optionally paint over just the text you want OCR'd, then Run OCR; result is copied to clipboard.
    

Dependencies:
    media-gfx/maim x11-misc/xclip app-text/tesseract dev-python/pillow x11-misc/xdotool x11-apps/xrandr dev-lang/python
    
# tk use flag needed on python for tkinter support

Make sure to create a snippysnappy.d folder in ~/.local/bin; script and logging goes here by default. For better calling, use this wrapper script written to PATH:
    

    cat > ~/.local/bin/snippysnappy << 'EOF'
    #!/bin/bash
    exec python3 "$HOME/.local/bin/snippysnappy/snippysnappy.py" "$@"
    EOF
    chmod +x ~/.local/bin/snippysnappy

Usage:
    Write this file to ~/.local/bin/snippysnappy.d/snippysnappy.py; write wrapper as above.
    chmod +x ~/.local/bin/snippysnappy.d/snippysnappy.py
    snippysnappy                  # bind to Print
    snippysnappy --fullscreen     # bind to Shift+Print
    snippysnappy --window         # bind to Ctrl+Shift+Print

Logging:
  Logs to console always, and to LOG_PATH below unless that line is commented
  out. Uses a size-capped ROTATING log (appends across runs, only rotates to
  a fresh file once it exceeds MAX_LOG_BYTES, keeping LOG_BACKUP_COUNT old
  copies) rather than wiping on every single invocation - so a crash from a
  few runs ago is still readable, not destroyed the instant you press the
  hotkey again. If the file can't be opened for any reason, we log a warning
  and continue with console-only logging rather than crashing.

For more information, see commenting in the snippysnappy.py file.
