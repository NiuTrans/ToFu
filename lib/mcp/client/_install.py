"""lib/mcp/client/_install.py — launcher resolution + first-connect auto-install.

Resolves bare launcher names to absolute paths next to the running
interpreter, pip-installs vendored servers into Tofu's own env on first
connect, and exposes a non-blocking async install-job registry the install
route polls. Also owns the user-facing "how to install this launcher" hints.

Facade-routing: install-guard state (``_install_attempted`` /
``_install_last_error`` / ``_install_cmd_locks`` / ``_install_jobs``), the
``subprocess`` module, and the cross-referenced functions
(``_resolve_launcher`` / ``_run_pip_install`` / ``_find_vendored_source`` /
``_try_autoinstall_launcher`` / ``prewarm_vendored_launcher``) are all
resolved through ``_pkg()`` so tests can monkeypatch them on
``lib.mcp.client``.
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


def _try_autoinstall_launcher(command: str) -> str | None:
    """pip-install a vendored MCP server into Tofu's interpreter, then resolve.

    Returns the resolved absolute launcher path on success, else ``None``.
    Installs into ``sys.executable`` (the very env Tofu runs in) so the new
    console script lands next to the interpreter where ``_resolve_launcher``
    finds it — no PATH/venv changes required.

    ``PIP_REQUIRE_VIRTUALENV`` is explicitly neutralised for the child: many
    internal conda setups enable it globally, which makes a bare ``pip
    install`` abort with "Could not find an activated virtualenv" even though
    installing into the active conda env is exactly what we want.
    """
    pkg = _pkg()
    found = pkg._find_vendored_source(command)
    if not found:
        # Unregistered or no source on disk — nothing we can pip-install. This
        # is NOT a sticky failure: we never touch _install_attempted, so a
        # later call (e.g. after the source appears) can still try.
        return None
    src, editable = found

    # Grab (or create) the per-command lock, then hold it across the whole
    # check-then-pip section so a concurrent caller for the SAME command waits
    # here instead of launching a second simultaneous pip on the same source.
    with pkg._install_lock:
        cmd_lock = pkg._install_cmd_locks.setdefault(command, threading.Lock())

    with cmd_lock:
        with pkg._install_lock:
            if command in pkg._install_attempted:
                # pip already RAN for this command this process (possibly by
                # the caller we just waited behind). Re-resolve in case it
                # succeeded, but never pip again. (The guard is only set AFTER
                # a real pip run below, so a transient pre-pip error never
                # wedges the command permanently.)
                return pkg._resolve_launcher(command)
        return pkg._run_pip_install(command, src, editable)


def _run_pip_install(command: str, src: str, editable: bool) -> str | None:
    """Actually shell out to pip for ``command``; caller holds its cmd lock."""
    pkg = _pkg()
    pip_args = [sys.executable, '-m', 'pip', 'install', '--no-input']
    if editable:
        pip_args.append('-e')
    pip_args.append(src)

    child_env = dict(os.environ)
    child_env['PIP_REQUIRE_VIRTUALENV'] = 'false'
    _ensure_writable_caches(child_env)

    logger.info('[MCP] auto-installing vendored launcher %r from %s (editable=%s)',
                command, src, editable)
    try:
        proc = pkg.subprocess.run(
            pip_args, env=child_env, capture_output=True, text=True,
            timeout=300,
        )
    except (pkg.subprocess.TimeoutExpired, OSError) as e:
        # pip never produced a result. Do NOT set the one-shot guard — this is
        # often transient (timeout, transient OS error), so a later retry (or
        # the "Reinstall" button) should be allowed to try again.
        msg = f'pip did not complete: {e}'
        logger.error('[MCP] auto-install of %r failed to run pip: %s', command, e)
        with pkg._install_lock:
            pkg._install_last_error[command] = msg
        return None

    # pip actually ran (rc known) → this counts as the one allowed attempt.
    with pkg._install_lock:
        pkg._install_attempted.add(command)

    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or '').strip().splitlines()[-5:]
        joined = ' | '.join(tail)
        logger.error('[MCP] auto-install of %r failed (rc=%d): %s',
                     command, proc.returncode, joined)
        with pkg._install_lock:
            pkg._install_last_error[command] = (
                f'pip install {src} exited {proc.returncode}: {joined}')
        return None

    resolved = pkg._resolve_launcher(command)
    if resolved:
        logger.info('[MCP] auto-install of %r OK → %s', command, resolved)
        with pkg._install_lock:
            pkg._install_last_error.pop(command, None)
    else:
        logger.error('[MCP] auto-install of %r reported success but launcher '
                     'still not resolvable', command)
        with pkg._install_lock:
            pkg._install_last_error[command] = (
                f'pip install {src} succeeded but the {command!r} console '
                'script is still not next to the interpreter — check the '
                "package's [project.scripts] entry.")
    return resolved


# ── Async install jobs (route returns immediately; UI polls) ──────────
#
# Why: a cold ``pip install`` of a vendored server can take minutes. Even
# though our bundled Hypercorn server has no per-request kill timer, the app
# explicitly supports running behind reverse proxies (cloud-IDE / notebook;
# see server.py `_detect_reverse_proxy`) whose own response timeouts WOULD
# cut a multi-minute synchronous POST — re-introducing the exact "install
# times out" symptom one layer down, and leaving a half-installed package.
# So the install route kicks the pip pre-warm off into a background thread
# and returns ``{status:'installing'}`` at once; the front end polls
# ``/catalog/install/status`` until it flips to ``ready`` / ``error``.


def get_install_job(command: str) -> dict | None:
    """Return a snapshot of the install job for ``command`` (or None)."""
    pkg = _pkg()
    with pkg._install_jobs_lock:
        job = pkg._install_jobs.get(command)
        return dict(job) if job is not None else None


def start_install_job(command: str) -> dict:
    """Start (or re-attach to) a background pip pre-warm for ``command``.

    Idempotent: if a job is already ``installing`` it is returned as-is
    (re-clicking Install never spawns a second pip — the per-command lock in
    ``_try_autoinstall_launcher`` would serialize them anyway, but we avoid
    even queuing a redundant thread). If the launcher already resolves we
    return ``ready`` immediately without touching pip.

    Returns the job snapshot dict.
    """
    import shutil as _shutil
    pkg = _pkg()
    # Fast path: already importable → no job needed.
    if not command or os.sep in command or _shutil.which(command) or pkg._resolve_launcher(command):
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

    # Vendored internal MCP servers (registered in lib/mcp/vendored.py) are
    # auto-installed on first connect. If we're showing a hint for one, the
    # zero-touch install must have FAILED — surface the captured reason and
    # the exact manual command pointing at the real in-repo source, instead
    # of the useless generic "package manager" line.
    src_info = pkg._find_vendored_source(base)
    if src_info is not None:
        src, editable = src_info
        with pkg._install_lock:
            why = pkg._install_last_error.get(base, '')
        pip_cmd = f'{sys.executable} -m pip install {"-e " if editable else ""}{src}'
        msg = (
            f'{base!r} is a bundled MCP server that Tofu tries to auto-install '
            f'on first connect, but that did not succeed. '
        )
        if why:
            msg += f'Reason: {why}. '
        msg += (
            f'Fix it manually with:\n    {pip_cmd}\n'
            f'(run in the same env that started Tofu), then click Reinstall. '
            'If pip succeeded but the launcher is still missing, restart Tofu.'
        )
        return msg

    # Project-bundled MCP servers shipped as a source tree under
    # ``vendor/<base>/`` (internal exports) but NOT in the vendored registry.
    try:
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
        vendor_path = os.path.join(repo_root, 'vendor', base)
        if os.path.isfile(os.path.join(vendor_path, 'pyproject.toml')):
            return (
                f'This project bundles {base!r} under {vendor_path!r}, but the '
                f'launcher script is not on PATH for the running interpreter. '
                f'Install it with: `pip install {vendor_path}` (run inside the '
                f'same conda env that started the server), then restart Tofu.'
            )
    except Exception as e:
        logger.debug('[MCP] vendor/ probe for %s failed: %s', base, e)

    return _LAUNCHER_HINTS.get(base,
        f'Install {command!r} via your package manager, or make sure it is on PATH.'
    )
