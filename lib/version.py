"""Tofu version from a checkout's VERSION or installed distribution metadata.

Usage:
    from lib.version import __version__
    # → '0.5.0'
"""

from pathlib import Path

_VERSION_FILE = Path(__file__).resolve().parent.parent / 'VERSION'

try:
    __version__ = _VERSION_FILE.read_text(encoding='utf-8').strip()
except Exception as checkout_error:
    try:
        from importlib.metadata import version as _distribution_version
        __version__ = _distribution_version('tofu-agent')
    except Exception as metadata_error:
        import logging as _logging
        _logging.getLogger(__name__).debug(
            'VERSION and tofu-agent metadata unavailable, using fallback: '
            'checkout=%s metadata=%s', checkout_error, metadata_error)
        __version__ = '0.0.0-dev'
