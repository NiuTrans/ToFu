"""Canonical data/log base selection without application or storage imports.

Entry points: resolve_runtime_base for explicit diagnostic environments;
get_runtime_base for the one process startup decision shared by data and logs.
Only frozen writable startup probes create an exclusive temporary file. Source
and read-only resolution never create directories. Dependencies: stdlib only.
"""
from __future__ import annotations

from collections.abc import Mapping
from threading import Lock
import logging
import os
import sys
import tempfile

_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
_logger = logging.getLogger(__name__)


def _per_user_base(environment: Mapping[str, str]) -> str:
    if sys.platform.startswith('win'):
        base = environment.get('LOCALAPPDATA') or os.path.expanduser('~')
        return os.path.join(base, 'Tofu')
    if sys.platform == 'darwin':
        return os.path.join(os.path.expanduser('~'), 'Library', 'Application Support', 'Tofu')
    base = environment.get('XDG_DATA_HOME') or os.path.join(os.path.expanduser('~'), '.local', 'share')
    return os.path.join(base, 'Tofu')


def _populated(path: str) -> bool:
    try:
        with os.scandir(path) as entries:
            return next(entries, None) is not None
    except OSError:
        return False


def _probe_writable(path: str) -> bool:
    try:
        # The executable directory already exists. Never truncate a fixed
        # filename or follow a pre-existing probe symlink into user content.
        with tempfile.TemporaryFile(prefix='.tofu-write-probe-', dir=path):
            pass
        return True
    except OSError:
        return False


def resolve_runtime_base(
    environment: Mapping[str, str] | None = None,
    *,
    repository_root: str | os.PathLike[str] = _REPO_ROOT,
    allow_probe: bool = True,
) -> str:
    environment = os.environ if environment is None else environment
    explicit = environment.get('TOFU_DATA_DIR', '')
    if explicit:
        explicit = os.path.abspath(explicit)
        base = os.path.dirname(explicit) if os.path.basename(explicit) == 'data' else explicit
        _logger.info('Data layout: explicit TOFU_DATA_DIR override → %s', base)
        return base
    if getattr(sys, 'frozen', False):
        if not allow_probe:
            raise ValueError('read-only frozen diagnostics require an explicit TOFU_DATA_DIR')
        sibling = os.path.dirname(sys.executable)
        return sibling if _probe_writable(sibling) else os.path.abspath(_per_user_base(environment))
    repository_root = os.path.abspath(repository_root)
    layout = (environment.get('TOFU_DATA_LAYOUT') or 'auto').strip().lower()
    if layout == 'intree':
        _logger.info('Data layout: in-tree repo root (TOFU_DATA_LAYOUT=intree) → %s', repository_root)
        return repository_root
    if layout not in ('auto', 'xdg'):
        _logger.warning('Unknown TOFU_DATA_LAYOUT=%r; treating as "auto"', layout)
    if layout != 'xdg' and _populated(os.path.join(repository_root, 'data')):
        _logger.info('Data layout: existing in-tree data/ found → %s', repository_root)
        return repository_root
    base = os.path.abspath(_per_user_base(environment))
    _logger.info('Data layout: %s → %s',
                 'per-user XDG (TOFU_DATA_LAYOUT=xdg)' if layout == 'xdg'
                 else 'fresh source checkout (no populated in-tree data/)', base)
    return base


_startup_base: str | None = None
_startup_lock = Lock()


def get_runtime_base() -> str:
    """One startup choice; data creation cannot change the later log decision."""
    global _startup_base
    with _startup_lock:
        if _startup_base is None:
            _startup_base = resolve_runtime_base()
        return _startup_base
