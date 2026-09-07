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

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['data_root', 'logs_root', 'uploads_root', 'project_sessions_root',
           'is_frozen']

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
    except OSError as e:
        logger.debug('[runtime_paths] _dir_is_writable(%r) failed: %s', path, e)
        return False


def _dir_is_populated(path: str) -> bool:
    """True if ``path`` exists and contains at least one entry.

    Used to detect an EXISTING in-tree ``data/`` install: a fresh ``git clone``
    ships NO ``data/`` at all (the whole dir is gitignored — ``git ls-files
    data/`` is empty), so a populated in-tree ``data/`` unambiguously means
    "this user already runs from the code tree" → keep them there (zero
    migration). This reads the filesystem, not a marker, so it can't lie.
    """
    try:
        with os.scandir(path) as it:
            return any(True for _ in it)
    except OSError as e:
        logger.debug('[runtime_paths] _dir_is_populated(%r) failed: %s', path, e)
        return False


def _source_checkout_base() -> str:
    """Resolve the base dir for a plain (non-frozen) source checkout.

    Policy — keep USER STATE out of the code tree by DEFAULT so ``git pull`` /
    a tarball overlay can never race an in-tree open SQLite WAL or an in-tree
    DB. Honours ``$TOFU_DATA_LAYOUT``:

      * ``intree``            — force the legacy repo-root layout.
      * ``xdg``               — force the per-user XDG dir, unconditionally.
      * ``auto`` (default)    — in-tree ONLY when ``<repo>/data`` already
                                exists and is populated (an existing install —
                                keep it working, zero migration); otherwise a
                                fresh clone → the per-user XDG dir, so the code
                                tree stays pure source.

    ``$TOFU_DATA_DIR`` (handled by the caller) still overrides everything.
    """
    layout = (os.environ.get('TOFU_DATA_LAYOUT', 'auto') or 'auto').strip().lower()
    intree = _REPO_ROOT
    if layout == 'intree':
        logger.info('Data layout: in-tree repo root (TOFU_DATA_LAYOUT=intree) → %s', intree)
        return intree
    if layout == 'xdg':
        per_user = _per_user_root()
        logger.info('Data layout: per-user XDG (TOFU_DATA_LAYOUT=xdg) → %s', per_user)
        return per_user
    if layout != 'auto':
        logger.warning('Unknown TOFU_DATA_LAYOUT=%r; treating as "auto"', layout)
    # auto — existing populated in-tree install wins (back-compat, no migration).
    if _dir_is_populated(os.path.join(intree, 'data')):
        logger.info('Data layout: existing in-tree data/ found → %s (set '
                    'TOFU_DATA_LAYOUT=xdg to relocate)', intree)
        return intree
    per_user = _per_user_root()
    logger.info('Data layout: fresh source checkout (no populated in-tree data/) '
                '→ per-user root %s (keeps user state out of the code tree; set '
                'TOFU_DATA_LAYOUT=intree to override)', per_user)
    return per_user


def _resolve_base() -> str:
    """Resolve the writable base directory that holds data/ and logs/."""
    explicit = os.environ.get('TOFU_DATA_DIR', '')
    if explicit:
        # The launcher passes a full path to the DATA directory itself; accept
        # both "…/data" (use its parent as the base) and a base dir.
        explicit = os.path.abspath(explicit)
        base = (os.path.dirname(explicit)
                if os.path.basename(explicit) == 'data' else explicit)
        logger.info('Data layout: explicit TOFU_DATA_DIR override → %s', base)
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

    return _source_checkout_base()


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
    :data:`lib.runtime_layout.INSTALL_STATE` (so the update skip-list / export
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
