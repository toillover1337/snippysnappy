# snippysnappy v1.0.0
"""
Lets this package run as `python3 -m snippysnappy [args...]`, and is also
what the "launch a pin viewer" subprocess call in selector_app.py
re-invokes.
"""
from .app import main

if __name__ == "__main__":
    try:
        main()
    except Exception:
        from .logging_setup import log
        log.exception("Unhandled exception, snippysnappy is exiting")
