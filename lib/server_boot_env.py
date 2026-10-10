"""Executable-environment preparation (extracted from server.py).

Owns the pre-import environment contract for the real server executable:
BLAS/OpenMP native-pool bounding, conda/uv re-exec into the marked
environment, child-process native search paths, and the canonical ``.env``
load. Everything here runs before routes, NumPy, pools, or storage are
imported; keep it dependency-free (stdlib + runtime_guards/tofu_dotenv
only) and free of application imports. The server.py wrapper supplies the
executable-only guard; this imported module never infers the caller from
its own __name__. Manager handoff and early resource configuration live in
bootstrap_pkg.server_entry.

This module must NOT import ``server`` (server.py re-exports these names so
``server.<name>`` and existing tests keep working byte-identically).
"""

from __future__ import annotations

import json
import os
import sys

_PROJ_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))


def _install_numeric_thread_defaults() -> int:
    """Bound implicit BLAS/OpenMP pools before NumPy or ML imports.

    High-core personal hosts otherwise make OpenBLAS eagerly retain one native
    worker per visible CPU (64 in the measured deployment) even while Tofu is
    idle.  Tofu already owns request, DB, agent, and tool executors; an
    additional host-sized pool per numeric runtime causes oversubscription and
    needless thread stacks under memory pressure. ``TOFU_NUMERIC_THREADS`` is
    the process-wide ceiling; a smaller library-specific value remains valid,
    while a larger inherited host setting is clamped before imports.
    """
    from runtime_guards import deployment_resource_default
    default = deployment_resource_default(
        'TOFU_NUMERIC_THREADS', os.environ)
    raw = os.environ.get('TOFU_NUMERIC_THREADS', str(default))
    try:
        workers = int(raw or default)
    except (TypeError, ValueError):
        workers = default
    workers = max(1, min(32, workers))
    for name in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS',
                 'MKL_NUM_THREADS', 'NUMEXPR_NUM_THREADS'):
        try:
            library_workers = int(os.environ.get(name, '') or workers)
        except (TypeError, ValueError, OverflowError):
            library_workers = workers
        os.environ[name] = str(max(1, min(workers, library_workers)))
    return workers


def _tofu_export_env_native_paths(env_prefix, backend):
    """Put the env's lib/ + bin/ on the search paths for CHILD processes.

    The headless-Chromium half (LD_LIBRARY_PATH + fontconfig) is delegated to
    chromium_env.ensure_chromium_env() — the single source of truth shared with
    bootstrap.py, tests/conftest.py and lib/motion_video. It resolves from
    sys.prefix, so it works even with no .tofu_env.json marker (a fresh clone,
    an exported bundle, or Docker), which is precisely the case the four
    hand-copied versions used to miss. See chromium_env.py's docstring.

    What stays here is the marker-specific part: PATH and the CONDA_* shims.
    """
    # Chromium's GUI libs + fonts. Passing env_prefix only ADDS a candidate;
    # resolution still works when the marker is absent or wrong.
    try:
        from chromium_env import ensure_chromium_env
        ensure_chromium_env(env_prefix=env_prefix)
    except Exception as _e:
        sys.stderr.write(f'[server.py] chromium env setup skipped: {_e}\n')

    if not env_prefix or not os.path.isdir(env_prefix):
        return
    env_bin = os.path.join(env_prefix, 'bin')
    if os.path.isdir(env_bin):
        _cur = os.environ.get('PATH', '')
        if env_bin not in _cur.split(os.pathsep):
            os.environ['PATH'] = (env_bin + os.pathsep + _cur) if _cur else env_bin
    # Only masquerade as a conda env when we ARE one. A uv venv
    # (backend='uv') is not conda; setting CONDA_PREFIX would make
    # bootstrap.py's _running_in_conda_env() misfire and route its pip
    # fallback down the conda-forge branch.
    if backend != 'uv':
        os.environ.setdefault('CONDA_PREFIX', env_prefix)


def _tofu_maybe_reexec_into_env():
    """Re-exec into Tofu's conda env if not already there."""
    marker = os.path.join(_PROJ_DIR, '.tofu_env.json')
    if not os.path.isfile(marker):
        # No marker (fresh clone, exported bundle, Docker, pip layout) — there
        # is nothing to re-exec into, but Chromium still needs its GUI libs and
        # fonts on the search paths. chromium_env resolves those from
        # sys.prefix, so this path is no longer a dead browser.
        _tofu_export_env_native_paths('', '')
        return
    try:
        with open(marker, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
    except (OSError, ValueError) as _e:
        # Unreadable/corrupt marker: nothing to re-exec into. Visible like the
        # chromium branch above; this module stays stdlib-only (no lib.log).
        sys.stderr.write(f'[server.py] env marker unreadable, skipping re-exec: {_e}\n')
        return
    target_py = cfg.get('python') or ''
    env_prefix = cfg.get('env_prefix') or ''
    backend = cfg.get('backend') or ''
    if not target_py or not os.access(target_py, os.X_OK):
        return
    # Are we ALREADY running inside the target env? Prefer a prefix check over a
    # bare interpreter-path comparison: a uv venv's bin/python is a symlink to a
    # base CPython, so realpath(target_py) can equal realpath(sys.executable)
    # even though we are NOT running with the venv's site-packages active —
    # comparing sys.prefix to env_prefix catches that. Fall back to the
    # interpreter-path compare when env_prefix is absent.
    already_in_env = False
    if env_prefix:
        try:
            already_in_env = (os.path.realpath(sys.prefix) == os.path.realpath(env_prefix))
        except OSError:
            already_in_env = (sys.prefix == env_prefix)
    else:
        try:
            already_in_env = os.path.realpath(target_py) == os.path.realpath(sys.executable)
        except OSError:
            already_in_env = (target_py == sys.executable)
    # Export the env's native-library search path BEFORE the already_in_env
    # early return. Chromium is a CHILD process and resolves libatk /
    # libatk-bridge / libnss out of $env_prefix/lib, which is not on the
    # default linker path. `python server.py` launched directly with the env
    # interpreter (the documented way, and how the supervisor runs it) takes
    # that early return, so while this lived inside the re-exec branch the
    # variables were only ever set on the path that re-execs — a directly
    # launched server left them unset and every Playwright launch died with
    # "libatk-1.0.so.0: cannot open shared object file".
    _tofu_export_env_native_paths(env_prefix, backend)
    if already_in_env:
        return
    if os.environ.get('_TOFU_ENV_REEXEC') == '1':
        return
    os.environ['_TOFU_ENV_REEXEC'] = '1'
    try:
        os.execv(target_py, [target_py, *sys.argv])
    except OSError:
        os.environ.pop('_TOFU_ENV_REEXEC', None)


def _load_dotenv():
    from tofu_dotenv import load_dotenv_file
    load_dotenv_file(os.path.join(_PROJ_DIR, '.env'))
