#!/usr/bin/env python3
# snippysnappy v1.0.1
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

from snippysnappy.app import main

if __name__ == "__main__":
    try:
        main()
    except Exception:
        from snippysnappy.logging_setup import log
        log.exception("Unhandled exception, snippysnappy is exiting")
