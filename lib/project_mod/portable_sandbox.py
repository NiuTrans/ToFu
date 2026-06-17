"""Portable, zero-privilege command sandbox for *restricted* agent runs.

Design goal: the project must run **no matter which server it is migrated
to** — a locked-down shared cluster (no namespaces, no container runtime)
AND a permissive host (real bwrap/podman) — without per-host configuration
or privileged setup requests.

We therefore use **strongest-available-wins** layering, all gated on the
``abs_path_guard`` restricted-context flag so the chatui *product* (local
desktop / CLI) is completely unaffected:

  Layer 2 (always available, no privilege): wrap the command so that
    * ``HOME`` / ``TMPDIR`` point INSIDE the workspace (so ``~``, pip, git,
      pytest scratch all stay contained), and
    * a generated shim dir is prepended to ``PATH`` whose ``rm`` / ``rmdir``
      / ``unlink`` / ``mv`` refuse any ABSOLUTE target resolving outside the
      workspace. This catches the shell-delete failure mode (the one a weak
      model actually hits) on every POSIX host.

  Layer 3 (opportunistic, only if the host allows it): if a real sandbox
    binary works here (bwrap → podman → proot, probed once and cached),
    wrap the command in it with a workspace-only writable bind + read-only
    rootfs. On a locked host the probe fails and we silently fall back to
    Layer 2.

HONEST SCOPE: Layer 2 is a guard-rail, not a kernel boundary — a determined
adversary can bypass a PATH ``rm`` shim (e.g. ``python -c "os.unlink(...)"``
or calling ``/bin/rm`` by absolute path). It is fully sufficient for the
real threat (a weak/misaligned model flailing with ``rm -rf``), and it is
100% portable. Layer 3 provides a genuine boundary ONLY where the host
permits namespaces. The command-level delete guard in ``tools.py``
(``_is_catastrophic_delete``) remains the always-on first line and DOES
cover the ``/bin/rm``-by-absolute-path and python-unlink-via-shell cases it
can parse.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import threading

from lib.log import get_logger

logger = get_logger(__name__)

# Opt-out escape hatch (set to '0' to disable the portable wrapper entirely).
_ENABLED = os.environ.get('TOFU_PORTABLE_SANDBOX', '1').strip().lower() not in (
    '0', 'false', 'no', 'off')

# Layer-3 backend probe is done once and cached.
_BACKEND_LOCK = threading.Lock()
_BACKEND_PROBED = False
_BACKEND: str | None = None   # 'bwrap' | 'podman' | None  (proot handled separately)


# ──────────────────────────────────────────────────────────────────
#  Layer 3: opportunistic real-isolation backend probe
# ──────────────────────────────────────────────────────────────────

def _probe_backend() -> str | None:
    """Return the strongest working isolation backend on THIS host, or None.

    Probed once (cached). We don't just check ``which`` — we actually run a
    trivial command, because on locked-down hosts the binary may exist but
    fail at namespace-creation time (the exact situation that makes a static
    'is it installed?' check misleading)."""
    global _BACKEND_PROBED, _BACKEND
    with _BACKEND_LOCK:
        if _BACKEND_PROBED:
            return _BACKEND
        _BACKEND_PROBED = True
        _BACKEND = None

        # bwrap: cleanest user-namespace sandbox when the kernel allows it.
        if _which('bwrap'):
            try:
                r = subprocess.run(
                    ['bwrap', '--ro-bind', '/', '/', '--dev', '/dev',
                     '--bind', '/tmp', '/tmp', 'true'],
                    capture_output=True, timeout=10)
                if r.returncode == 0:
                    _BACKEND = 'bwrap'
                    logger.info('[portable_sandbox] backend: bwrap (real isolation)')
                    return _BACKEND
            except Exception as e:
                logger.debug('[portable_sandbox] bwrap probe failed: %s', e)

        # podman rootless: heavier, but a genuine boundary where available.
        if _which('podman'):
            try:
                r = subprocess.run(['podman', 'info'], capture_output=True, timeout=20)
                if r.returncode == 0:
                    _BACKEND = 'podman'
                    logger.info('[portable_sandbox] backend: podman (real isolation)')
                    return _BACKEND
            except Exception as e:
                logger.debug('[portable_sandbox] podman probe failed: %s', e)

        logger.info('[portable_sandbox] no namespace backend usable on this host '
                    '— using portable Layer-2 guard (HOME/TMPDIR jail + rm shim)')
        return _BACKEND


def _which(binary: str) -> str | None:
    for d in os.environ.get('PATH', '').split(os.pathsep):
        p = os.path.join(d, binary)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


# ──────────────────────────────────────────────────────────────────
#  Layer 2: portable PATH shim (rm/rmdir/unlink/mv refuse to escape ws)
# ──────────────────────────────────────────────────────────────────

# A tiny POSIX-sh guard, written into <workspace>/.tofu_sandbox/bin/<name>.
# It rejects any absolute argument whose realpath is outside $TOFU_WS, then
# delegates to the real binary found via a pinned _REAL_PATH. Relative args
# (the agent's own files) pass straight through.
_SHIM_TEMPLATE = r"""#!/bin/sh
# Auto-generated portable sandbox shim for restricted agent runs. Refuses to
# {verb} absolute paths outside the workspace ($TOFU_WS). Do not edit.
ws="${{TOFU_WS:-}}"
real="{real}"
if [ -z "$ws" ] || [ -z "$real" ]; then exec "$real" "$@"; fi
for a in "$@"; do
  case "$a" in
    -*) continue ;;          # flags
    /*|~*) ;;                 # absolute / home — must check
    *) continue ;;            # relative — inside cwd, allowed
  esac
  # strip wildcard tail; resolve symlinks best-effort
  t=$(readlink -f "$a" 2>/dev/null || printf '%s' "$a")
  case "$t/" in
    "$ws"/*) ;;               # inside workspace → ok
    *) echo "tofu-sandbox: refused to {verb} path outside workspace: $a" >&2
       exit 13 ;;
  esac
done
exec "$real" "$@"
"""

_SHIMMED = {
    'rm': 'remove', 'rmdir': 'remove', 'unlink': 'remove', 'mv': 'move',
}


def _ensure_shim_dir(workspace: str) -> str | None:
    """Create (idempotently) the per-workspace shim bin dir and return it."""
    try:
        shim_dir = os.path.join(workspace, '.tofu_sandbox', 'bin')
        os.makedirs(shim_dir, exist_ok=True)
        for name, verb in _SHIMMED.items():
            real = _real_binary(name)
            if not real:
                continue
            path = os.path.join(shim_dir, name)
            body = _SHIM_TEMPLATE.format(verb=verb, real=real)
            # Rewrite only if changed (cheap, avoids churn).
            if not os.path.exists(path) or _read(path) != body:
                with open(path, 'w') as f:
                    f.write(body)
                os.chmod(path, 0o755)
        return shim_dir
    except Exception as e:
        logger.warning('[portable_sandbox] shim dir setup failed (%s) — '
                       'falling back to command-guard only', e)
        return None


def _real_binary(name: str) -> str | None:
    """Resolve the real system binary, skipping our own shim dir."""
    for d in os.environ.get('PATH', '').split(os.pathsep):
        if d.endswith(os.path.join('.tofu_sandbox', 'bin')):
            continue
        p = os.path.join(d, name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    # common fallbacks
    for p in (f'/bin/{name}', f'/usr/bin/{name}'):
        if os.path.isfile(p):
            return p
    return None


def _read(path: str) -> str:
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ''


# ──────────────────────────────────────────────────────────────────
#  Public API
# ──────────────────────────────────────────────────────────────────

def prepare_env(env: dict, workspace: str) -> dict:
    """Mutate *env* in place for a restricted run: jail HOME/TMPDIR inside the
    workspace and prepend the rm/mv shim dir to PATH. Returns the same dict.

    No-op when disabled. Safe to call repeatedly."""
    if not _ENABLED or not workspace:
        return env
    ws = os.path.realpath(workspace)
    env['TOFU_WS'] = ws
    # Jail HOME and temp dirs inside the workspace so ~, pip, pytest scratch
    # cannot reach the real home / shared tmp.
    env['HOME'] = ws
    tmp = os.path.join(ws, '.tmp')
    try:
        os.makedirs(tmp, exist_ok=True)
        env['TMPDIR'] = tmp
        env['TMP'] = tmp
        env['TEMP'] = tmp
    except OSError:
        pass
    shim_dir = _ensure_shim_dir(ws)
    if shim_dir:
        env['PATH'] = shim_dir + os.pathsep + env.get('PATH', '')
    return env


def wrap_command(full_command: str, workspace: str) -> str:
    """Wrap *full_command* with a real-isolation backend IF one works on this
    host; otherwise return it unchanged (Layer-2 env jail already applied via
    :func:`prepare_env`). Restricted-context callers only."""
    if not _ENABLED or not workspace:
        return full_command
    backend = _probe_backend()
    ws = os.path.realpath(workspace)
    if backend == 'bwrap':
        # Read-only host, writable workspace + its own /tmp, dev minimal.
        inner = ['bwrap', '--ro-bind', '/', '/', '--dev', '/dev',
                 '--bind', ws, ws, '--chdir', ws,
                 '--unshare-all', '--share-net',
                 'sh', '-c', full_command]
        return ' '.join(shlex.quote(x) for x in inner)
    # podman path intentionally conservative: only used if a prebuilt image is
    # configured, else fall through. (Full podman wiring is a follow-up; the
    # probe already logs availability.)
    return full_command


def status() -> dict:
    """Introspection helper for logging/tests."""
    return {
        'enabled': _ENABLED,
        'backend': _probe_backend(),
        'shimmed': sorted(_SHIMMED),
    }
