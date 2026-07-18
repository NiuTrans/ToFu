"""lib/mcp/client/_vendor.py — vendored MCP servers: registry hot-reload,
source resolution, snapshot staleness detection, and connect pre-warm.

Internal MCP servers (hope-mcp, …) are private and not on PyPI, so a fresh
Tofu checkout has no way to obtain them. We ship the source in-repo and
pip-install it into Tofu's OWN interpreter on first connect. This module owns
the discovery/hot-reload/staleness pieces of that flow; the actual pip run +
async-job registry live in ``_install``.

Facade-routing: every access to a monkeypatchable name (state dicts,
``importlib`` / ``subprocess`` modules, ``_repo_root`` / ``_resolve_launcher``
/ ``_try_autoinstall_launcher`` / ``prewarm_vendored_launcher`` /
``_run_vendor_script``) goes through ``_pkg()`` so a test's
``monkeypatch.setattr(lib.mcp.client, name, ...)`` is honoured.
"""

from __future__ import annotations

import os

from lib.log import get_logger
from lib.mcp.client._state import _pkg

logger = get_logger(__name__)


def _ensure_writable_caches(env: dict[str, str]) -> None:
    """Redirect launcher caches AND data dirs to a project-local dir when
    ``$HOME`` is read-only.

    On locked-down / shared deployments the home dir (and hence
    ``$HOME/.cache`` and ``$HOME/.local/share``) is often not writable —
    sometimes not even traversable (no execute bit) — for the running user.
    ``uv`` / ``uvx`` then fail in two successive places, exiting before the
    MCP handshake so every stdio server reports "Connection closed":

      1. cache:  "Failed to initialize cache at ``~/.cache/uv``: Permission
         denied" — fixed by ``UV_CACHE_DIR`` / ``XDG_CACHE_HOME``.
      2. data:   "failed to read directory ``~/.local/share/uv/python``:
         Permission denied" — fixed by ``XDG_DATA_HOME`` and the explicit
         ``UV_*_DIR`` vars below (redirecting the managed-Python + tool dirs).

    Every cache/data-controlling env var is pointed (only when the caller
    has not set it already) at a writable directory under the repo's
    ``data/`` tree, which is guaranteed writable by the rest of Tofu. Because
    that tree usually lives on a different filesystem than ``$HOME``,
    ``UV_LINK_MODE=copy`` is also set so uv does not abort/warn when it cannot
    hardlink across filesystems. No-op when the chosen directory cannot be
    created.
    """
    cache_root = os.environ.get('TOFU_MCP_CACHE_DIR') or os.path.join(
        os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..')),
        'data', 'mcp-cache',
    )
    share_root = os.path.join(cache_root, 'share')
    try:
        os.makedirs(cache_root, exist_ok=True)
        os.makedirs(share_root, exist_ok=True)
    except OSError as e:
        logger.warning('[MCP] cannot create launcher cache dir %s: %s', cache_root, e)
        return

    # uv / uvx, npm / npx, generic XDG, and pip caches + data dirs.
    defaults = {
        # ── caches ──
        'UV_CACHE_DIR': os.path.join(cache_root, 'uv'),
        'XDG_CACHE_HOME': cache_root,
        'npm_config_cache': os.path.join(cache_root, 'npm'),
        'PIP_CACHE_DIR': os.path.join(cache_root, 'pip'),
        # ── data dirs (managed Python, installed tools) ──
        'XDG_DATA_HOME': share_root,
        'UV_PYTHON_INSTALL_DIR': os.path.join(share_root, 'uv', 'python'),
        'UV_TOOL_DIR': os.path.join(share_root, 'uv', 'tools'),
        'UV_TOOL_BIN_DIR': os.path.join(cache_root, 'bin'),
        # ── cross-filesystem safety ──
        # data/ is typically a different mount than $HOME, so hardlinking the
        # cache into the venv fails; copy mode avoids the error/warning.
        'UV_LINK_MODE': 'copy',
    }
    for key, path in defaults.items():
        env.setdefault(key, path)


# ── Hot-reload of the vendored registry ──────────────────

def _vendored_path() -> str:
    return getattr(_pkg()._vendored_mod, '__file__', '') or ''


def _reload_vendored_if_changed() -> None:
    """Re-import ``vendored.py`` if its mtime advanced; merge new rows live.

    Cheap: one ``os.path.getmtime`` stat per call, and the actual reload only
    runs when the file genuinely changed. Failures (file gone, syntax error
    mid-edit) are swallowed — we keep the last-good registry rather than break
    every connect. Never DELETES rows (a half-saved edit shouldn't drop a
    server mid-flight); it only adds/updates.
    """
    pkg = _pkg()
    path = _vendored_path()
    if not path:
        return
    try:
        mtime = os.path.getmtime(path)
    except OSError as e:
        logger.debug('[MCP] vendored.py stat failed (%s) — keeping last-good', e)
        return
    if mtime <= pkg._vendored_mtime:
        return
    with pkg._vendored_reload_lock:
        # Re-check under lock — another thread may have just reloaded.
        try:
            mtime = os.path.getmtime(path)
        except OSError as e:
            logger.debug('[MCP] vendored.py re-stat under lock failed (%s)', e)
            return
        if mtime <= pkg._vendored_mtime:
            return
        try:
            pkg.importlib.reload(pkg._vendored_mod)
            fresh = dict(getattr(pkg._vendored_mod, 'VENDORED_LAUNCHERS', {}) or {})
        except Exception as e:
            # Mid-edit syntax error or transient read failure — keep last-good
            # registry and retry on the next mtime bump.
            logger.warning('[MCP] vendored.py reload failed (keeping last-good '
                           'registry): %s', e)
            # Advance the baseline so we don't hammer reload on every call while
            # the file is briefly broken; the next real save bumps mtime again.
            pkg._vendored_mtime = mtime
            return
        added = [k for k in fresh if k not in pkg._VENDORED_LAUNCHERS]
        # Merge in place (preserve dict identity for test monkeypatches).
        pkg._VENDORED_LAUNCHERS.update(fresh)
        pkg._vendored_mtime = mtime
        if added:
            logger.info('[MCP] vendored registry hot-reloaded: +%d new server(s) %s',
                        len(added), ', '.join(sorted(added)))


# ── Vendored-snapshot staleness detection (+ opt-in auto-vendor) ──
#
# Mirror scripts/vendor_mcp.sh's EXCLUDES so the comparison matches exactly
# what a real vendor would copy — otherwise __pycache__/egg-info would make a
# freshly-vendored snapshot look "stale" forever.
_VENDOR_EXCLUDE_DIRS = frozenset({
    '.git', '__pycache__', '.pytest_cache', '.ruff_cache', 'build', 'dist',
    '.tofu', '.chatui', '.venv', 'venv',
})


def _vendor_excluded_dir(name: str) -> bool:
    return name in _VENDOR_EXCLUDE_DIRS or name.endswith('.egg-info')


def _vendor_excluded_file(name: str) -> bool:
    return name.endswith('.pyc') or name.endswith('.egg-link')


def _vendor_tree_signature(base: str) -> dict[str, tuple[int, int]]:
    """Map relpath → (size, int(mtime)) for vendor-relevant files under ``base``.

    Honours the same excludes as vendor_mcp.sh. ``rsync -a`` preserves size +
    mtime, so a faithful vendored snapshot has an identical signature; any
    difference (added/removed/edited file) means drift.
    """
    sig: dict[str, tuple[int, int]] = {}
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if not _vendor_excluded_dir(d)]
        for fn in filenames:
            if _vendor_excluded_file(fn):
                continue
            full = os.path.join(dirpath, fn)
            try:
                st = os.stat(full)
            except OSError as e:
                logger.debug('[MCP] stat(%s) failed during vendor sig: %s', full, e)
                continue
            sig[os.path.relpath(full, base)] = (st.st_size, int(st.st_mtime))
    return sig


def _snapshot_stale_reason(sibling: str, snapshot: str) -> str:
    """Return a concise drift description, or '' when the snapshot is fresh."""
    if not os.path.isdir(snapshot):
        return 'snapshot missing'
    sib = _vendor_tree_signature(sibling)
    snap = _vendor_tree_signature(snapshot)
    if sib == snap:
        return ''
    missing = sorted(set(sib) - set(snap))      # in sibling, absent from snapshot
    extra = sorted(set(snap) - set(sib))        # in snapshot, deleted from sibling
    changed = sorted(r for r in (set(sib) & set(snap)) if sib[r] != snap[r])
    parts = []
    if missing:
        parts.append(f'{len(missing)} new/missing (e.g. {", ".join(missing[:3])})')
    if changed:
        parts.append(f'{len(changed)} changed (e.g. {", ".join(changed[:3])})')
    if extra:
        parts.append(f'{len(extra)} stale-extra (e.g. {", ".join(extra[:3])})')
    return '; '.join(parts) or 'content differs'


def _auto_vendor_enabled() -> bool:
    return os.environ.get('TOFU_MCP_AUTO_VENDOR', '').strip().lower() in {
        '1', 'true', 'yes', 'on',
    }


def _run_vendor_script(command: str, root: str) -> bool:
    """Shell out to scripts/vendor_mcp.sh for ONE server. Returns success."""
    import sys
    pkg = _pkg()
    script = os.path.join(root, 'scripts', 'vendor_mcp.sh')
    if not os.path.isfile(script):
        logger.warning('[MCP] auto-vendor requested but %s not found', script)
        return False
    child_env = dict(os.environ)
    child_env['TOFU_PYTHON'] = sys.executable
    _ensure_writable_caches(child_env)
    try:
        proc = pkg.subprocess.run(
            ['bash', script, command], cwd=root, env=child_env,
            capture_output=True, text=True, timeout=120,
        )
    except (pkg.subprocess.TimeoutExpired, OSError) as e:
        logger.error('[MCP] auto-vendor of %r failed to run: %s', command, e)
        return False
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or '').strip().splitlines()[-5:]
        logger.error('[MCP] auto-vendor of %r failed (rc=%d): %s',
                     command, proc.returncode, ' | '.join(tail))
        return False
    return True


def _check_snapshot_staleness(command: str) -> None:
    """Detect (always) and optionally rebuild (opt-in) a stale ``tools/`` snapshot.

    Safe by design: never writes anything unless ``TOFU_MCP_AUTO_VENDOR`` is
    truthy. No-op for non-vendored commands and on deploys with no sibling.
    """
    pkg = _pkg()
    spec = pkg._VENDORED_LAUNCHERS.get(command)
    if not spec:
        return
    sources = spec.get('sources', [])
    sibling_rel = next((s for s in sources if not s.startswith('tools/')), None)
    snap_rel = next((s for s in sources if s.startswith('tools/')), None)
    if not sibling_rel or not snap_rel:
        return  # need BOTH a sibling source and a snapshot dest to compare
    root = pkg._repo_root()
    sibling = os.path.abspath(os.path.join(root, sibling_rel))
    snapshot = os.path.abspath(os.path.join(root, snap_rel))
    # Only meaningful where the sibling dev checkout exists — that's the only
    # thing we could vendor FROM. On a deploy (snapshot-only) stay silent.
    if not os.path.isfile(os.path.join(sibling, 'pyproject.toml')):
        return

    try:
        reason = pkg._snapshot_stale_reason(sibling, snapshot)
    except OSError as e:
        logger.debug('[MCP] snapshot staleness check for %r failed: %s', command, e)
        return

    with pkg._snapshot_lock:
        already = pkg._snapshot_reported.get(command)
        if reason == already:
            return  # same state as last time we acted — stay quiet
        pkg._snapshot_reported[command] = reason

    if not reason:
        return  # fresh (possibly just re-vendored) — nothing to say

    logger.warning(
        '[MCP] vendored snapshot tools/%s is STALE vs sibling %s: %s. '
        'Run `make vendor-mcp %s` before committing/deploying.',
        command, sibling, reason, command)

    if not pkg._auto_vendor_enabled():
        return

    logger.info('[MCP] TOFU_MCP_AUTO_VENDOR set — rebuilding snapshot tools/%s', command)
    if pkg._run_vendor_script(command, root):
        # Recompute so we report the fresh state (and don't re-warn next time).
        try:
            new_reason = pkg._snapshot_stale_reason(sibling, snapshot)
        except OSError as e:
            logger.debug('[MCP] post-vendor staleness recheck failed: %s', e)
            new_reason = ''
        with pkg._snapshot_lock:
            pkg._snapshot_reported[command] = new_reason
        if new_reason:
            logger.warning('[MCP] auto-vendor of %r ran but snapshot still '
                           'differs: %s', command, new_reason)
        else:
            logger.info('[MCP] auto-vendor of %r OK — snapshot tools/%s now fresh',
                        command, command)


def _find_vendored_source(command: str) -> tuple[str, bool] | None:
    """Resolve the install source for ``command``.

    Returns ``(src_dir, editable)`` for the first candidate dir that contains
    a ``pyproject.toml``, or ``None`` if the command is unregistered / no
    source exists. ``editable`` is True only when the resolved dir is OUTSIDE
    the repo's ``tools/`` tree (i.e. a sibling dev checkout) — the vendored
    snapshot under ``tools/`` is always installed non-editable.

    Hot-reloads ``vendored.py`` first when its mtime advanced, so a server row
    added to the running process's source is picked up WITHOUT a restart.
    """
    pkg = _pkg()
    pkg._reload_vendored_if_changed()
    spec = pkg._VENDORED_LAUNCHERS.get(command)
    if not spec:
        return None
    root = pkg._repo_root()
    tools_dir = os.path.join(root, 'tools') + os.sep
    for rel in spec['sources']:
        cand = rel if os.path.isabs(rel) else os.path.abspath(os.path.join(root, rel))
        if os.path.isfile(os.path.join(cand, 'pyproject.toml')):
            is_vendored = (cand + os.sep).startswith(tools_dir)
            return cand, (not is_vendored)
    return None


def is_vendored_launcher(command: str) -> bool:
    """True when ``command`` is a bare, registered vendored MCP launcher.

    Used by the install route to decide whether a slow pip pre-warm is even
    possible before attempting the (fast) connect. No-op for npx/uvx commands
    and anything with a path separator.
    """
    if not command or os.sep in command or (os.altsep and os.altsep in command):
        return False
    return _pkg()._find_vendored_source(command) is not None


def prewarm_vendored_launcher(command: str) -> tuple[bool, str]:
    """Ensure a vendored launcher is importable BEFORE connect.

    Intended to run in a normal worker thread (e.g. the Flask request handler
    for ``/catalog/install``), NOT on the MCP event loop — so the blocking
    ``pip install`` here is fine and never freezes live MCP traffic.

    Returns ``(ready, detail)``:
      * ``(True, path)``  — launcher already resolvable, or pip install succeeded.
      * ``(True, '')``    — already on PATH (nothing to do).
      * ``(False, why)``  — install attempted and failed; ``why`` is the
        captured pip stderr / reason (also stored in ``_install_last_error``).

    Safe to call even for non-vendored commands: returns ``(True, '')`` so the
    caller just proceeds to connect.
    """
    import shutil as _shutil
    pkg = _pkg()
    if not command or os.sep in command:
        return True, ''
    if _shutil.which(command):
        return True, ''
    resolved = pkg._resolve_launcher(command)
    if resolved:
        return True, resolved
    if pkg._find_vendored_source(command) is None:
        # Not something we can pip-install — let connect surface the hint.
        return True, ''
    resolved = pkg._try_autoinstall_launcher(command)
    if resolved:
        return True, resolved
    with pkg._install_lock:
        why = pkg._install_last_error.get(command, '')
    return False, why


def prewarm_all_vendored() -> dict[str, str]:
    """Pip-install every registered vendored launcher that isn't resolvable yet.

    Meant to run ONCE in a background thread at startup so the App-Store
    "Install" click is normally just the sub-second MCP handshake instead of
    a cold pip install. Blocking (pip) — never call from the event loop.

    Returns ``{command: 'ok' | reason}`` for the launchers that needed work
    (already-resolvable ones are skipped silently).
    """
    pkg = _pkg()
    pkg._reload_vendored_if_changed()
    out: dict[str, str] = {}
    for command in list(pkg._VENDORED_LAUNCHERS.keys()):
        try:
            import shutil as _shutil
            if _shutil.which(command) or pkg._resolve_launcher(command):
                continue
            if pkg._find_vendored_source(command) is None:
                continue
            ready, detail = pkg.prewarm_vendored_launcher(command)
            out[command] = 'ok' if ready else (detail or 'install failed')
        except Exception as e:  # never let pre-warm crash boot
            logger.warning('[MCP] pre-warm of %r failed: %s', command, e)
            out[command] = f'error: {e}'
    return out
