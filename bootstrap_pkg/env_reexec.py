"""Tofu env detection + re-exec (extracted from bootstrap.py).

STDLIB-ONLY CONTRACT: this package is imported by bootstrap.py precisely
when third-party packages may all be missing. Never add a non-stdlib import.
"""
from __future__ import annotations

import json
import os
import sys

# Repo root = parent of this package dir (facade bootstrap.py lives there).
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# ══════════════════════════════════════════════════════════
#  Auto-activate Tofu's conda env via .tofu_env.json marker
# ══════════════════════════════════════════════════════════
# Mirror of the guard at the top of server.py — runs BEFORE we touch any
# pip / conda / subprocess logic so the rest of bootstrap.py operates
# inside the right interpreter (so subprocess [sys.executable, 'server.py']
# correctly inherits the env's python).
def _tofu_export_env_native_paths(env_prefix, backend, env_name=None):
    """Put the env's lib/ + bin/ on the search paths for CHILD processes.

    The headless-Chromium half (LD_LIBRARY_PATH + fontconfig) is delegated to
    chromium_env.ensure_chromium_env() — the single source of truth shared with
    server.py, tests/conftest.py and lib/motion_video. It resolves from
    sys.prefix rather than from this marker, so it also works on a fresh clone /
    exported bundle where no .tofu_env.json exists. chromium_env is stdlib-only
    BY CONTRACT precisely so bootstrap.py — whose job is to run when deps are
    still missing — can import it safely.

    Must run even when we are ALREADY in the env, since that path spawns
    Chromium too. Idempotent.
    """
    try:
        from chromium_env import ensure_chromium_env
        ensure_chromium_env(env_prefix=env_prefix)
    except Exception as e:
        sys.stderr.write(f'[bootstrap.py] chromium env setup skipped: {e}\n')

    if not env_prefix or not os.path.isdir(env_prefix):
        return
    env_bin = os.path.join(env_prefix, 'bin')
    if os.path.isdir(env_bin):
        _cur = os.environ.get('PATH', '')
        if env_bin not in _cur.split(os.pathsep):
            os.environ['PATH'] = (env_bin + os.pathsep + _cur) if _cur else env_bin
    # A uv venv (backend='uv') is not a conda env — don't set CONDA_PREFIX,
    # or _running_in_conda_env() below misfires and routes the pip fallback
    # down the conda-forge branch.
    if backend != 'uv':
        os.environ.setdefault('CONDA_PREFIX', env_prefix)
        if env_name:
            os.environ.setdefault('CONDA_DEFAULT_ENV', env_name)
def _tofu_maybe_reexec_into_env():
    marker = os.path.join(BASE_DIR, '.tofu_env.json')
    if not os.path.isfile(marker):
        # No marker — nothing to re-exec into, but Chromium still needs its GUI
        # libs + fonts, which chromium_env resolves from sys.prefix.
        _tofu_export_env_native_paths('', '')
        return
    try:
        with open(marker, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
    except Exception as e:
        sys.stderr.write(
            f'[bootstrap.py] Could not read .tofu_env.json ({e}) — '
            f'continuing with current python.\n')
        return
    target_py = cfg.get('python') or ''
    env_prefix = cfg.get('env_prefix') or ''
    backend = cfg.get('backend') or ''
    if not target_py or not os.access(target_py, os.X_OK):
        return
    # Prefer a prefix check over a bare interpreter-path compare: a uv venv's
    # bin/python symlinks to a base CPython, so realpath(target_py) can equal
    # realpath(sys.executable) while the venv's site-packages are NOT active.
    # Comparing sys.prefix to env_prefix catches that (falls back to the
    # interpreter-path compare when env_prefix is absent, e.g. conda markers).
    same = False
    if env_prefix:
        try:
            same = (os.path.realpath(sys.prefix) == os.path.realpath(env_prefix))
        except OSError:
            same = (sys.prefix == env_prefix)
    else:
        try:
            same = os.path.realpath(target_py) == os.path.realpath(sys.executable)
        except OSError:
            same = (target_py == sys.executable)
    # Export BEFORE the early return — see server.py's twin helper. A direct
    # `python bootstrap.py` with the env interpreter takes this return, and
    # every Chromium it later spawns would otherwise miss $env_prefix/lib.
    _tofu_export_env_native_paths(env_prefix, backend, cfg.get('env_name'))
    if same:
        return
    if os.environ.get('_TOFU_ENV_REEXEC') == '1':
        sys.stderr.write(
            '\033[33m[bootstrap.py] WARNING: _TOFU_ENV_REEXEC=1 was inherited '
            'from your shell, but the current python\n'
            f'  ({sys.executable})\n'
            '  is NOT the env python recorded in .tofu_env.json\n'
            f'  ({target_py}).\n'
            '  Run:  unset _TOFU_ENV_REEXEC _TOFU_VIA_BOOTSTRAP\n'
            '  Overriding the leaked guard and re-execing into the env python now.\033[0m\n')
        sys.stderr.flush()
    os.environ['_TOFU_ENV_REEXEC'] = '1'
    sys.stderr.write(f'[bootstrap.py] Re-exec into Tofu env python: {target_py}\n')
    sys.stderr.flush()
    try:
        os.execv(target_py, [target_py, *sys.argv])
    except OSError as e:
        sys.stderr.write(f'[bootstrap.py] os.execv failed: {e}\n')
        os.environ.pop('_TOFU_ENV_REEXEC', None)
