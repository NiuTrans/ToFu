"""lib/mcp/client/_install.py — launcher resolution + async install-job registry.

Resolves bare launcher names to absolute paths next to the running
interpreter and exposes the non-blocking install-job registry the install
route polls. Vendored internal servers are NOT installed into Tofu's
interpreter anymore — they launch isolated via
``lib.mcp.client._vendor.vendored_launch_argv`` (``uv run --with-editable``);
this module only keeps the generic resolver, the job registry, and the
user-facing "how to install this launcher" hints.

Facade-routing: install-job state (``_install_last_error`` / ``_install_jobs``),
the ``subprocess`` module, and the cross-referenced functions
(``_resolve_launcher`` / ``_find_vendored_source`` /
``prewarm_vendored_launcher``) are all resolved through ``_pkg()`` so tests
can monkeypatch them on ``lib.mcp.client``.
"""

from __future__ import annotations

import os
import sys
import threading
import time

from lib.log import get_logger
from lib.mcp.client._state import _pkg
from lib.mcp.client._vendor import _ensure_writable_caches

logger = get_logger(__name__)


# ── Launcher install hints ───────────────────────────────

_LAUNCHER_HINTS = {
    'uvx': (
        'Install uv (provides uvx): '
        '`curl -LsSf https://astral.sh/uv/install.sh | sh` '
        '(or `pip install uv`). After install, restart Tofu so the new PATH is picked up.'
    ),
    'npx': (
        'Install Node.js (provides npx): '
        'https://nodejs.org/ (LTS recommended). After install, restart Tofu.'
    ),
    'pipx': (
        'Install pipx: `python3 -m pip install --user pipx && pipx ensurepath`. '
        'After install, restart Tofu.'
    ),
    'node': (
        'Install Node.js: https://nodejs.org/ (LTS recommended).'
    ),
    'python3': (
        'Python 3 is missing from PATH — very unusual. Check your shell PATH.'
    ),
}


# ── Async install jobs (route returns immediately; UI polls) ──────────
#
# Why: a cold dependency resolution of a vendored server's isolated env can
# take minutes. Even though our bundled Hypercorn server has no per-request
# kill timer, the app explicitly supports running behind reverse proxies
# (cloud-IDE / notebook; see server.py `_detect_reverse_proxy`) whose own
# response timeouts WOULD cut a multi-minute synchronous POST — re-introducing
# the exact "install times out" symptom one layer down. So the install route
# kicks the uv warm off into a background thread and returns
# ``{status:'installing'}`` at once; the front end polls
# ``/catalog/install/status`` until it flips to ``ready`` / ``error``.


def get_install_job(command: str) -> dict | None:
    """Return a snapshot of the install job for ``command`` (or None)."""
    pkg = _pkg()
    with pkg._install_jobs_lock:
        job = pkg._install_jobs.get(command)
        return dict(job) if job is not None else None


def start_install_job(command: str) -> dict:
    """Start (or re-attach to) a background uv warm for ``command``.

    Idempotent: if a job is already ``installing`` it is returned as-is
    (re-clicking Install never spawns a second warm). For NON-vendored
    commands that already resolve we return ``ready`` immediately. For
    VENDORED commands an on-PATH console script is NOT readiness — it may be
    a stale coupled copy from the pip era, and the bridge launches the
    isolated copy regardless — so the uv warm always runs.

    Returns the job snapshot dict.
    """
    import shutil as _shutil
    pkg = _pkg()
    warmable = bool(command) and os.sep not in command and \
        pkg.vendored_launch_argv(command) is not None
    if not command or os.sep in command or (
            not warmable
            and (_shutil.which(command) or pkg._resolve_launcher(command))):
        snap = {'state': 'ready', 'detail': '', 'started': time.time(), 'ended': time.time()}
        with pkg._install_jobs_lock:
            pkg._install_jobs[command] = snap
        return dict(snap)

    with pkg._install_jobs_lock:
        existing = pkg._install_jobs.get(command)
        if existing is not None and existing.get('state') == 'installing':
            return dict(existing)
        job = {'state': 'installing', 'detail': '', 'started': time.time(), 'ended': 0.0}
        pkg._install_jobs[command] = job

    def _worker():
        try:
            ready, detail = pkg.prewarm_vendored_launcher(command)
        except Exception as e:  # defensive — never let the thread die silently
            ready, detail = False, f'install crashed: {e}'
            logger.error('[MCP] install job for %r crashed: %s', command, e, exc_info=True)
        with pkg._install_jobs_lock:
            pkg._install_jobs[command] = {
                'state': 'ready' if ready else 'error',
                'detail': detail,
                'started': job['started'],
                'ended': time.time(),
            }
        logger.info('[MCP] install job for %r finished: %s',
                    command, 'ready' if ready else f'error ({detail})')

    threading.Thread(target=_worker, name=f'mcp-install:{command}', daemon=True).start()
    with pkg._install_jobs_lock:
        return dict(pkg._install_jobs[command])


def _resolve_launcher(command: str) -> str | None:
    """Best-effort resolve a bare launcher name to an absolute path.

    Why this exists: the #1 cause of "launcher X is not on PATH" reports is
    a Python console script (e.g. ``hope-mcp``, ``overleaf-mcp``) that WAS
    pip-installed — but into the same interpreter that runs Tofu, whose
    ``bin/`` directory isn't on the *spawned subprocess's* PATH (conda envs
    activated via a wrapper, systemd units, IDE-launched servers, …). The
    script sits right next to ``sys.executable`` yet ``shutil.which`` (which
    only searches ``$PATH``) misses it, so the user is told to "install"
    something that is in fact already installed.

    We look, in order, for an executable named ``command`` in:
      1. the directory of the running interpreter (``sys.executable``) —
         this is where ``pip install`` drops console scripts for the very
         env Tofu runs in;
      2. ``<base_prefix>/bin`` (covers venvs whose scripts were installed
         against the base interpreter);
      3. a ``Scripts`` sibling (Windows layout), for completeness.

    Returns an absolute path if found, else ``None``. Only meaningful for a
    BARE command (no path separator) — anything with a slash is taken
    as-is by the caller.
    """
    if not command or os.sep in command or (os.altsep and os.altsep in command):
        return None

    candidates: list[str] = []
    exe_dir = os.path.dirname(os.path.abspath(sys.executable)) if sys.executable else ''
    for base in (exe_dir, getattr(sys, 'base_prefix', '') or ''):
        if not base:
            continue
        # base_prefix is a prefix, not a bin dir — append bin/ when needed.
        bin_dirs = [base] if os.path.basename(base) in ('bin', 'Scripts') \
            else [os.path.join(base, 'bin'), os.path.join(base, 'Scripts')]
        for bd in bin_dirs:
            candidates.append(os.path.join(bd, command))

    seen: set[str] = set()
    for cand in candidates:
        if cand in seen:
            continue
        seen.add(cand)
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            logger.info('[MCP] resolved launcher %r → %s (not on PATH, found '
                        'next to interpreter)', command, cand)
            return cand
    return None


def _prepend_interpreter_bin_to_path(env: dict[str, str]) -> None:
    """Prepend the running interpreter's ``bin/`` dir to ``env['PATH']``.

    Ensures stdio MCP child processes inherit the same environment Tofu
    runs in. Without this, a server installed into Tofu's conda env can
    launch (we resolve its absolute path) but then fails when it shells
    out to a sibling tool from the same env (e.g. ``hope-mcp`` → ``hope``)
    because that tool isn't on the inherited PATH. Idempotent: skips if the
    dir is already the first PATH entry.
    """
    exe_dir = os.path.dirname(os.path.abspath(sys.executable)) if sys.executable else ''
    if not exe_dir or not os.path.isdir(exe_dir):
        return
    current = env.get('PATH', '')
    parts = current.split(os.pathsep) if current else []
    if parts and parts[0] == exe_dir:
        return
    env['PATH'] = os.pathsep.join([exe_dir, *[p for p in parts if p != exe_dir]])


def _launcher_install_hint(command: str) -> str:
    """Return an actionable install hint for a missing launcher binary.

    If the project ships a sibling source tree under ``vendor/<base>/``
    (e.g. internal exports bundle ``vendor/xuecheng-mcp/``), prefer a
    ``pip install`` hint that points straight at it instead of the
    generic "install via your package manager" fallback. The bundled
    repos are usually private and not publishable on PyPI, so users have
    no other source.
    """
    pkg = _pkg()
    base = command.rsplit('/', 1)[-1]

    # Vendored internal MCP servers (registered in lib/mcp/vendored.py) launch
    # isolated via `uv run --with-editable`. If we're showing a hint for one,
    # the source resolution or the uv warm failed — surface the captured
    # reason and the exact manual warm command.
    src_info = pkg._find_vendored_source(base)
    if src_info is not None:
        src, _editable = src_info
        with pkg._install_lock:
            why = pkg._install_last_error.get(base, '')
        warm_cmd = (
            f'uv run --no-project --with-editable {src} '
            f"python -c 'import {base.replace('-', '_')}'"
        )
        msg = (
            f'{base!r} is a bundled MCP server launched isolated from {src!r} '
            f'via `uv run --with-editable`, but the env could not be resolved. '
        )
        if why:
            msg += f'Reason: {why}. '
        msg += (
            f'Verify the source is intact and the warm succeeds manually:\n'
            f'    {warm_cmd}\n'
            'Also check that `uv` itself is on PATH, then click Reinstall.'
        )
        return msg

    # Project-bundled MCP servers shipped as a source tree under
    # ``vendor/<base>/`` (internal exports) but NOT in the vendored registry.
    try:
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
        vendor_path = os.path.join(repo_root, 'vendor', base)
        if os.path.isfile(os.path.join(vendor_path, 'pyproject.toml')):
            return (
                f'This project bundles {base!r} under {vendor_path!r}. It '
                f'launches isolated via `uv run --no-project --with-editable '
                f'{vendor_path} {base}`; check that `uv` is on PATH and that '
                f'the source tree is intact, then restart Tofu.'
            )
    except Exception as e:
        logger.debug('[MCP] vendor/ probe for %s failed: %s', base, e)

    return _LAUNCHER_HINTS.get(base,
        f'Install {command!r} via your package manager, or make sure it is on PATH.'
    )
