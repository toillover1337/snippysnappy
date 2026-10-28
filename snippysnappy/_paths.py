# snippysnappy v1.0.3
"""
Shared filesystem locations, derived from where this PACKAGE lives on disk.
"""
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
INSTALL_DIR = PACKAGE_DIR.parent
