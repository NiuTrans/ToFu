r"""lib/runtime_paths.py — Single source of truth for the writable data/logs roots.

Historically every module computed its data/logs location as
``dirname(dirname(__file__))/data`` — i.e. the *repository* root. That is
correct for a source checkout, but WRONG for a frozen desktop build:

  * PyInstaller ``--onedir`` lays the app out as
    ``C:\Program Files\Tofu\Tofu.exe`` + ``C:\Program Files\Tofu\_internal\``,
    with ``lib/`` living under ``_internal/``. So ``dirname(dirname(__file__))``
    resolves *inside* ``_internal`` — a directory under ``Program Files`` that a
    standard (non-admin) user CANNOT write to. Every attempt to create
    ``data/pgdata``, ``data/config``, ``data/tofu.db`` or ``logs/`` then fails
    with ``PermissionError`` and the app crashes on first launch.

The fix: resolve the *writable* data/logs roots ONCE, here, honouring (in order):

  1. ``$TOFU_DATA_DIR`` — an explicit override. The desktop launcher sets this
     to a writable ``data/`` directory next to the executable
     (``desktop/launcher.py``). Also useful for source runs that want a
     relocated data dir.
  2. Frozen build with no override → a per-user, always-writable location:
     ``%LOCALAPPDATA%\Tofu`` on Windows, ``~/.local/share/Tofu`` elsewhere.
     (The exe-sibling ``data/`` is preferred only when it is actually
     writable — a portable/unzipped build — otherwise we fall back to the
     per-user dir so a Program Files install still works.)
  3. Source checkout → the repository root (unchanged legacy behaviour).

``data_root()`` and ``logs_root()`` return absolute paths and guarantee the
directory exists. Modules that used to write ``os.path.join(BASE_DIR, 'data')``
should call ``data_root()`` instead so a single policy governs every artifact.
"""

import os
import sys

from lib.env_compat import getenv_compat
from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['data_root', 'logs_root', 'is_frozen']

# The repository / bundle root (dir that CONTAINS lib/, static/, server.py).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def is_frozen() -> bool:
    """True when running inside a PyInstaller (or similar) frozen bundle."""
    return bool(getattr(sys, 'frozen', False))


def _per_user_root() -> str:
    """A per-user, guaranteed-writable base dir for a frozen install."""
    if sys.platform.startswith('win'):
        base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
        return os.path.join(base, 'Tofu')
    if sys.platform == 'darwin':
        return os.path.join(os.path.expanduser('~'), 'Library',
                            'Application Support', 'Tofu')
    base = os.environ.get('XDG_DATA_HOME') or os.path.join(
        os.path.expanduser('~'), '.local', 'share')
    return os.path.join(base, 'Tofu')


def _dir_is_writable(path: str) -> bool:
    """True if *path* exists (or can be created) and we can write into it."""
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, '.tofu_write_probe')
        with open(probe, 'w') as fh:
            fh.write('')
        os.remove(probe)
        return True
    except OSError:
        return False


def _resolve_base() -> str:
    """Resolve the writable base directory that holds data/ and logs/."""
    explicit = getenv_compat('TOFU_DATA_DIR', default='')
    if explicit:
        # The launcher passes a full path to the DATA directory itself; accept
        # both "…/data" (use its parent as the base) and a base dir.
        explicit = os.path.abspath(explicit)
        base = (os.path.dirname(explicit)
                if os.path.basename(explicit) == 'data' else explicit)
        return base

    if is_frozen():
        # Prefer the exe-sibling location (portable build); fall back to a
        # per-user dir when that sits under a read-only install root.
        #
        # Probe the BASE dir itself (<exe_dir>), NOT a subdir like <exe>/data.
        # lib/log.py's inline twin makes the SAME frozen-fallback decision and
        # must reach the SAME verdict, or data/ and logs/ could split to
        # different roots on a partially-writable install. One probe of the
        # shared base = one verdict for both data/ and logs/.
        exe_sibling = os.path.dirname(sys.executable)
        if _dir_is_writable(exe_sibling):
            return exe_sibling
        per_user = _per_user_root()
        logger.info('Frozen build: exe dir not writable, using per-user root %s',
                    per_user)
        return per_user

    return _REPO_ROOT


_BASE = _resolve_base()


def data_root() -> str:
    """Absolute path to the writable ``data/`` directory (created on demand)."""
    path = os.path.join(_BASE, 'data')
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as e:
        logger.warning('Could not create data root %s: %s', path, e)
    return path


def logs_root() -> str:
    """Absolute path to the writable ``logs/`` directory (created on demand)."""
    path = os.path.join(_BASE, 'logs')
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as e:
        logger.warning('Could not create logs root %s: %s', path, e)
    return path
