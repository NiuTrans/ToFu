"""Dependency install machinery: conda/pip/requirements.txt + error heuristics.

STDLIB-ONLY CONTRACT — see bootstrap_pkg.env_reexec.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
import sys

from . import runtime
from .env_reexec import BASE_DIR
from .process import run_command

PIP_TIMEOUT = 300            # per-package install timeout
# Packages that should never be auto-installed (security / system-level)
_INSTALL_BLOCKLIST = frozenset({
    'python', 'python3', 'gcc', 'g++', 'make', 'cmake', 'apt', 'yum',
    'brew', 'sudo', 'pip', 'setuptools', 'wheel',
})
# Map requirements.txt line → conda-forge package spec. Used when we detect
# we're running inside a conda env (install.sh created one).
# conda-forge builds link against an older sysroot glibc (2.17) so they work
# on CentOS-7-class hosts where pip's manylinux wheels crash with
# "GLIBC_2.25 not found" (classic lxml failure mode).
_CONDA_PYTHON_DEPS = [
    # ── Boot-critical: the server cannot start without these ──
    # quart + hypercorn are the ASGI stack (server.py).
    'quart>=0.20',
    'hypercorn>=0.17',
    # orjson is REQUIRED (not optional) — the SSE state snapshot in
    # routes/chat.py depends on it to avoid the event-loop stall (see
    # requirements.txt). Storage schema compilation no longer depends on an
    # ORM/compiler package, so the repair set stays limited to live imports.
    'orjson>=3.9',
    'psycopg>=3.2',
    'psycopg-pool>=3.2',
    'requests>=2.31',
    'psutil>=5.9',
    # Must match requirements.txt exactly: this list is the repair path that
    # force-reinstalls boot-critical imports, so a lower floor here would
    # silently downgrade a healthy env below what the app requires.
    'trafilatura>=2.3.1',
    'playwright>=1.40',
    'pillow>=10.0',
    'python-pptx>=0.6.21',
    'fonttools>=4.40',
    'brotli>=1.1',
    'lxml>=5.3',
    'lxml_html_clean>=0.4',
    # Bounded on BOTH sides — see requirements.txt. Tofu's client speaks the
    # v2 API (>=2,<3); vendored servers pin their own mcp in their own
    # isolated envs. This list feeds the PRE-BOOT installer, so a spec that
    # disagrees with requirements.txt here builds a different client before
    # the app has even started.
    'mcp>=2,<3',
]
# Boot-critical packages the conda-forge repair path MUST cover — asserted by
# tests/bootstrap/test_bootstrap_conda_deps_coverage.py so this list can never again
# silently drift below what server boot + the chat hot-path require. Names are
# the bare (version-stripped, lower-cased) package names.
_CRITICAL_BOOT_PACKAGES = ('quart', 'hypercorn', 'orjson')
def _running_in_conda_env() -> bool:
    """True when the current Python is running inside a conda env."""
    return bool(sys.prefix and os.path.isdir(os.path.join(sys.prefix, 'conda-meta')))
def _find_conda_exe() -> str | None:
    """Locate the conda executable reachable from the current env."""
    import shutil as _sh
    # Prefer the one that created the current env
    for env_name in ('CONDA_EXE', 'MAMBA_EXE'):
        val = os.environ.get(env_name)
        if val and os.path.isfile(val):
            return val
    # PATH lookup
    for name in ('conda', 'mamba'):
        path = _sh.which(name)
        if path:
            return path
    # Locate the manager from the verified interpreter, not inherited activation.
    prefix = sys.prefix if _running_in_conda_env() else ''
    if prefix:
        # e.g. ~/miniforge3/envs/tofu → ~/miniforge3/bin/conda
        for up in (prefix, os.path.dirname(os.path.dirname(prefix))):
            for rel in ('bin/conda', 'Scripts/conda.exe'):
                cand = os.path.join(up, rel)
                if os.path.isfile(cand):
                    return cand
    return None
def _try_conda_install_deps() -> bool:
    """Install dependencies via `conda install -c conda-forge` into the
    current verified interpreter prefix without modifying shared base settings.

    This is the preferred repair path when we're inside a conda env —
    conda-forge avoids the glibc-mismatch issue that breaks pip's
    manylinux lxml wheel on older hosts (CentOS 7 / glibc 2.17).

    Returns True on success.
    """
    if not _running_in_conda_env():
        runtime._bus.emit('log', 'Current interpreter is not a verified conda environment')
        return False
    conda = _find_conda_exe()
    if not conda:
        runtime._bus.emit('log', 'conda not found — falling back to pip path')
        return False

    env_prefix = os.path.realpath(sys.prefix)
    env_name = os.path.basename(env_prefix)

    runtime._bus.emit('phase', json.dumps({
        'id': 'conda-deps',
        'label': f'🐍 Detected conda env ({env_name}) — installing deps from conda-forge',
        'status': 'active',
        'detail': 'conda-forge wheels link against older glibc (CentOS 7-compatible). '
                  'Pip manylinux wheels of lxml often require GLIBC_2.25+.',
    }))

    # Never purge pip packages or update/configure the shared conda base.
    target = ['-p', env_prefix]

    # --force-reinstall handles the case where pip previously dropped an
    # incompatible manylinux wheel over conda's files. Without it, conda's
    # cached metadata says the package is satisfied and it no-ops.
    cmd = [conda, 'install', *target, '-c', 'conda-forge',
           '--override-channels', '-y',
           '--force-reinstall', *_CONDA_PYTHON_DEPS]
    runtime._bus.emit('log', f'$ {" ".join(cmd)}')

    try:
        rc, output = run_command(
            cmd, cwd=BASE_DIR, timeout=600,
            emit=lambda line: runtime._bus.emit('pip_output', line))
    except Exception as e:
        runtime._bus.emit('log', f'Failed to run conda: {e}')
        runtime._bus.emit('phase', json.dumps({
            'id': 'conda-deps',
            'label': '🐍 conda failed to start',
            'status': 'error',
        }))
        return False


    if rc == 0:
        runtime._bus.emit('log', '✅ conda install succeeded')
        runtime._bus.emit('phase', json.dumps({
            'id': 'conda-deps',
            'label': '🐍 Dependencies installed from conda-forge',
            'status': 'done',
        }))
        return True

    runtime._bus.emit('log', f'❌ conda install failed (exit code {rc})')
    runtime._bus.emit('phase', json.dumps({
        'id': 'conda-deps',
        'label': '🐍 conda install failed',
        'status': 'error',
        'detail': f'Exit code {rc}. Falling back to pip…',
    }))
    return False


def _verify_javascript_runtime() -> bool:
    from .runtime_checks import ensure_javascript_runtime
    healthy, detail = ensure_javascript_runtime(
        root=Path(BASE_DIR), emit=lambda line: runtime._bus.emit('pip_output', line))
    runtime._bus.emit('log', detail)
    runtime._bus.emit('phase', json.dumps({
        'id': 'javascript-runtime', 'label': 'JavaScript runtime verification',
        'status': 'done' if healthy else 'error', 'detail': detail,
    }))
    return healthy


def _try_requirements_txt() -> bool:
    """Try to install all packages from requirements.txt.

    This is the fast path: if a requirements.txt exists, we can install
    everything from it without needing the LLM at all.  This is critical
    for freshly-exported projects where the LLM API keys haven't been
    configured yet.

    If the current Python is inside a conda env, we use conda-forge
    instead of pip — this avoids the glibc-mismatch trap where pip's
    manylinux wheels (esp. lxml) require a newer glibc than the host.

    Returns True if dependencies were successfully installed.
    """
    req_path = os.path.join(BASE_DIR, 'requirements.txt')
    if not os.path.isfile(req_path):
        return False

    # Prefer conda when we're inside a conda env — see _try_conda_install_deps
    # docstring for the glibc rationale.
    if _running_in_conda_env() and _try_conda_install_deps():
        return _verify_javascript_runtime()

    runtime._bus.emit('phase', json.dumps({
        'id': 'reqtxt',
        'label': '📋 Found requirements.txt — installing all dependencies…',
        'status': 'active',
    }))
    runtime._bus.emit('log', f'Found {req_path}')

    cmd = [sys.executable, '-m', 'pip', 'install', '--no-input', '-r', req_path]
    runtime._bus.emit('log', f'$ {" ".join(cmd)}')

    try:
        rc, output = run_command(
            cmd, cwd=BASE_DIR, timeout=PIP_TIMEOUT,
            emit=lambda line: runtime._bus.emit('pip_output', line))
    except Exception as e:
        runtime._bus.emit('log', f'Failed to run pip: {e}')
        runtime._bus.emit('phase', json.dumps({
            'id': 'reqtxt',
            'label': '📋 requirements.txt — pip failed to start',
            'status': 'error',
        }))
        return False


    if rc == 0:
        if not _verify_javascript_runtime():
            return False
        runtime._bus.emit('log', '✅ pip install -r requirements.txt succeeded')
        runtime._bus.emit('phase', json.dumps({
            'id': 'reqtxt',
            'label': '📋 requirements.txt — all dependencies installed',
            'status': 'done',
        }))
        return True
    else:
        runtime._bus.emit('log', f'❌ pip install -r requirements.txt failed (exit code {rc})')
        runtime._bus.emit('phase', json.dumps({
            'id': 'reqtxt',
            'label': '📋 requirements.txt — pip install failed',
            'status': 'error',
            'detail': f'Exit code {rc}. Some packages may need system-level deps.',
        }))
        return False
# A deliberately conservative, stdlib-only subset of PEP 508: no references,
# markers, options or arbitrary-version equality. Bounds keep regex work small.
_DIST_NAME = r'[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?'
_VERSION = r'v?(?:[0-9]+!)?[0-9]+(?:\.[0-9]+)*(?:(?:a|b|rc)[0-9]+)?(?:\.post[0-9]+)?(?:\.dev[0-9]+)?(?:\+[a-zA-Z0-9]+(?:[._-][a-zA-Z0-9]+)*)?'
_SPECIFIER = rf'(?:(?:==|!=)(?:{_VERSION}|[0-9]+(?:\.[0-9]+)*\.\*)|(?:<=|>=|<|>){_VERSION}|~=[0-9]+\.[0-9]+(?:\.[0-9]+)*)'
_PACKAGE_SPEC = re.compile(
    rf'(?P<name>{_DIST_NAME})(?:\[{_DIST_NAME}(?:,{_DIST_NAME})*\])?'
    rf'(?:{_SPECIFIER}(?:,{_SPECIFIER})*)?', re.ASCII)


def _safe_package_name(package: object) -> str | None:
    if not isinstance(package, str) or not 1 <= len(package) <= 256:
        return None
    match = _PACKAGE_SPEC.fullmatch(package)
    return re.sub(r'[-_.]+', '-', match['name']).lower() if match else None


def _allowed_repair_packages() -> set[str]:
    """Repository declarations plus the existing fixed mypyc repair mapping."""
    allowed = {name for _, name in _MYPYC_PACKAGE_PATTERNS}
    try:
        with open(os.path.join(BASE_DIR, 'requirements.txt'), encoding='utf-8') as source:
            # Bootstrap must stay bounded even with an unexpectedly large file.
            text = source.read(256 * 1024 + 1)
        if len(text) > 256 * 1024:
            return allowed
        for line in text.splitlines():
            name = _safe_package_name(line.split('#', 1)[0].strip())
            if name:
                allowed.add(name)
    except (OSError, UnicodeError):
        pass
    return allowed


def _pip_install(packages: list[str]) -> tuple[bool, str]:
    """Run pip install for the given packages, emitting SSE progress.

    Legacy ``conda:`` suggestions are rejected. The repair loop may install
    Python packages but never a database server or another system service.

    Returns (success: bool, output: str).
    """
    # Validate the whole suggestion before logging or spawning anything.
    if not isinstance(packages, list) or not 1 <= len(packages) <= 32:
        return False, 'Expected between 1 and 32 Python package suggestions.'
    allowed = _allowed_repair_packages()
    for package in packages:
        name = _safe_package_name(package)
        if not name or name in _INSTALL_BLOCKLIST or name not in allowed:
            return False, 'Rejected unsafe or undeclared Python package suggestion.'
    safe_pkgs = packages

    cmd = [sys.executable, '-m', 'pip', 'install', '--no-input'] + safe_pkgs
    runtime._bus.emit('log', f'$ {" ".join(cmd)}')

    try:
        rc, output = run_command(
            cmd, cwd=BASE_DIR, timeout=PIP_TIMEOUT,
            emit=lambda line: runtime._bus.emit('pip_output', line))
    except Exception as e:
        msg = f'Failed to run pip: {e}'
        runtime._bus.emit('log', msg)
        return False, msg

    full_output = output

    if rc == 0:
        runtime._bus.emit('log', '✅ pip install succeeded')
        return True, full_output
    else:
        runtime._bus.emit('log', f'❌ pip install failed (exit code {rc})')
        return False, full_output
def _is_import_or_package_error(stderr_text: str) -> bool:
    """Heuristic: does the traceback look like a missing-package error?"""
    indicators = [
        'ModuleNotFoundError',
        'ImportError',
        'No module named',
        'cannot import name',
        'pkg_resources.DistributionNotFound',
        'ModuleNotFoundError',
        'No matching distribution found',
    ]
    return any(ind in stderr_text for ind in indicators)
def _is_mypyc_error(stderr_text: str) -> bool:
    """Heuristic: does the error look like a broken mypyc compiled extension?

    Packages like charset-normalizer, black, and mypy ship mypyc-compiled
    .so/.pyd files.  When a user's Python version or platform doesn't match
    the compiled extension, the import fails with:
        No module named '<hash>__mypyc'
    or:
        partially initialized module '...' has no attribute 'md__mypyc'

    Fix: ``pip install --force-reinstall <package>`` to get a wheel that
    matches the current Python.
    """
    return bool(re.search(r"No module named '[0-9a-f]+__mypyc'", stderr_text)
                or '__mypyc' in stderr_text)
# Known packages that ship mypyc-compiled extensions.
# Keys are regex patterns matched against the stderr traceback to identify
# which pip package is broken.  Patterns use word boundaries to avoid
# false positives (e.g. '__mypyc' should NOT match the 'mypyc' entry).
_MYPYC_PACKAGE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'charset_normalizer'),          'charset-normalizer'),
    (re.compile(r'\bblack\b'),                   'black'),
    (re.compile(r'\bmypy[^c]|\bmypy$'),          'mypy'),
]
def _detect_mypyc_broken_packages(stderr_text: str) -> list[str]:
    """Detect which pip packages have broken mypyc extensions from the traceback.

    Returns a list of pip package names to force-reinstall.
    """
    packages = set()
    # Look for known package names in the traceback context
    for pattern, pip_name in _MYPYC_PACKAGE_PATTERNS:
        if pattern.search(stderr_text):
            packages.add(pip_name)
    # Fallback: if we see __mypyc but can't identify the package,
    # force-reinstall charset-normalizer (by far the most common culprit)
    if not packages and '__mypyc' in stderr_text:
        packages.add('charset-normalizer')
    return sorted(packages)
def _try_fix_mypyc(stderr_text: str) -> bool:
    """Try to fix broken mypyc compiled extensions by force-reinstalling.

    Returns True if packages were reinstalled (caller should retry server).
    """
    packages = _detect_mypyc_broken_packages(stderr_text)
    if not packages:
        return False

    pkg_str = ', '.join(packages)
    runtime._bus.emit('phase', json.dumps({
        'id': 'mypyc-fix',
        'label': f'🔧 Fixing broken mypyc extensions: {pkg_str}',
        'status': 'active',
        'detail': 'These packages have compiled C extensions that don\'t match '
                  'your Python version. Force-reinstalling to get correct wheels…',
    }))
    runtime._bus.emit('log', f'Detected broken mypyc extensions in: {pkg_str}')
    runtime._bus.emit('log', 'Running pip install --force-reinstall to fix…')

    cmd = [sys.executable, '-m', 'pip', 'install', '--force-reinstall',
           '--no-input'] + packages
    runtime._bus.emit('log', f'$ {" ".join(cmd)}')

    try:
        rc, output = run_command(
            cmd, cwd=BASE_DIR, timeout=PIP_TIMEOUT,
            emit=lambda line: runtime._bus.emit('pip_output', line))
    except Exception as e:
        runtime._bus.emit('log', f'Failed to run pip: {e}')
        runtime._bus.emit('phase', json.dumps({
            'id': 'mypyc-fix',
            'label': '🔧 mypyc fix — pip failed to start',
            'status': 'error',
        }))
        return False


    if rc == 0:
        runtime._bus.emit('log', f'✅ Force-reinstalled: {pkg_str}')
        runtime._bus.emit('phase', json.dumps({
            'id': 'mypyc-fix',
            'label': f'🔧 Fixed mypyc extensions: {pkg_str}',
            'status': 'done',
        }))
        return True
    else:
        runtime._bus.emit('log', f'❌ pip install --force-reinstall failed (exit code {rc})')
        runtime._bus.emit('phase', json.dumps({
            'id': 'mypyc-fix',
            'label': '🔧 mypyc fix failed',
            'status': 'error',
            'detail': f'Exit code {rc}. Try manually: '
                      f'pip install --force-reinstall {pkg_str}',
        }))
        return False
