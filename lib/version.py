"""Tofu version — reads from the VERSION file at project root.

Usage:
    from lib.version import __version__
    # → '0.5.0'
"""

from pathlib import Path

_VERSION_FILE = Path(__file__).resolve().parent.parent / 'VERSION'

try:
    __version__ = _VERSION_FILE.read_text(encoding='utf-8').strip()
except Exception:
    __version__ = '0.0.0-dev'
