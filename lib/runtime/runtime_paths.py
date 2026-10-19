r"""lib/runtime/runtime_paths.py — Single source of truth for the writable data/logs roots.

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

from runtime_path_policy import get_runtime_base, resolve_runtime_base
from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['data_root', 'logs_root', 'uploads_root', 'project_sessions_root',
           'is_frozen']

# The repository / bundle root (dir that CONTAINS lib/, static/, server.py).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def is_frozen() -> bool:
    """True when running inside a PyInstaller (or similar) frozen bundle."""
    return bool(getattr(sys, 'frozen', False))


def _resolve_base() -> str:
    """Explicit recomputation for diagnostics/tests; runtime uses one snapshot."""
    return resolve_runtime_base(repository_root=_REPO_ROOT)


_BASE = get_runtime_base()


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


def uploads_root() -> str:
    """Absolute path to the writable ``uploads/`` directory (created on demand).

    User-uploaded / generated assets (chat images, generated PNGs+SVGs, paper
    PDFs + figure manifests, translated PPTX) are mutable USER STATE — the same
    class as ``data/`` — and are referenced from the DB by ``/api/images/…`` /
    ``/api/paper/…`` URLs. They MUST co-locate with the resolved base so a
    relocated install (``$TOFU_DATA_DIR`` / ``TOFU_DATA_LAYOUT=xdg`` / a frozen
    build) keeps images next to the DB that points at them. Historically each
    consumer recomputed ``<repo>/uploads`` from its own ``__file__``, which
    split the assets away from the DB the moment the base moved off the code
    tree. In the default (in-tree) layout ``_BASE == _REPO_ROOT`` so this is
    byte-identical to the old path.
    """
    path = os.path.join(_BASE, 'uploads')
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as e:
        logger.warning('Could not create uploads root %s: %s', path, e)
    return path


def project_sessions_root() -> str:
    """Absolute path to the project-session store (created on demand).

    Holds per-project undo/redo history — ``<session_id>/modifications.json``,
    which carries the PRE-image of every file the assistant edited (personal
    conversation content). This is mutable USER STATE: the in-tree location is
    registered as ``lib/.project_sessions/`` in
    :data:`lib.runtime.runtime_layout.INSTALL_STATE` (so the update skip-list / export
    excludes / ``.gitignore`` all recognise it from ONE source), and the
    relocated location falls under that registry's ``data/`` entry. Historically
    written INTO the code tree at ``<repo>/lib/.project_sessions`` — so a frozen
    / read-only / relocated install would try to write under a read-only
    ``lib/``.

    Layout policy, chosen to keep EXISTING in-tree installs byte-identical (a
    populated ``lib/.project_sessions`` must keep resolving to the SAME dir so
    no undo history is orphaned):

      * In-tree base (``_BASE == _REPO_ROOT``) → the legacy
        ``<repo>/lib/.project_sessions`` (unchanged; zero migration).
      * Relocated base (``$TOFU_DATA_DIR`` / ``TOFU_DATA_LAYOUT=xdg`` / frozen)
        → ``<base>/data/project_sessions``, co-located with the DB rather than
        the code tree. The ``.``-prefix is dropped off the tree since it no
        longer needs to hide inside a source checkout.
    """
    if os.path.abspath(_BASE) == os.path.abspath(_REPO_ROOT):
        path = os.path.join(_REPO_ROOT, 'lib', '.project_sessions')
    else:
        path = os.path.join(_BASE, 'data', 'project_sessions')
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as e:
        logger.warning('Could not create project-sessions root %s: %s', path, e)
    return path
