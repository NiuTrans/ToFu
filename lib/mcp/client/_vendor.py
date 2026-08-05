"""lib/mcp/client/_vendor.py — vendored MCP servers: registry hot-reload,
source resolution, snapshot staleness detection, and connect pre-warm.

Internal MCP servers (hope-mcp, …) are private and not on PyPI, so a fresh
Tofu checkout has no way to obtain them. We ship the source next to the repo
(sibling dev checkout, export ``vendor/`` bundle, or ``tools/`` snapshot) and
launch each server ISOLATED via ``uv run --no-project --with-editable
<source>`` — the server's dependency tree (its own ``mcp`` included) never
touches Tofu's interpreter. This module owns the discovery / hot-reload /
staleness / launch-resolution pieces of that flow; the async-job registry
lives in ``_install``.

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

#: Default supply-chain cutoff for MCP launcher dependency resolution.
#:
#: Everything an MCP server needs was on the index by this date, so the floor
#: costs nothing today while making every cold resolve reproducible. RAISE IT
#: DELIBERATELY (a dated, reviewed bump) to adopt newer server releases —
#: that is the whole point: upgrades become an explicit edit with a diff,
#: instead of something that happens to whoever resolves next.
_SUPPLY_CUTOFF_DEFAULT = '2026-07-27T00:00:00Z'

#: Marker file written into each npm ``_npx/<hash>/`` slot recording the
#: cutoff its ``package-lock.json`` was resolved under.
_NPX_CUTOFF_MARKER = '.tofu-supply-cutoff'


def _reconcile_npx_cache(npm_cache: str, cutoff: str) -> int:
    """Evict ``_npx`` slots whose lock was resolved under a DIFFERENT cutoff.

    WHY THIS IS REQUIRED, NOT DEFENSIVE (measured 2026-07-31)
    ---------------------------------------------------------
    npm caches each ``npx -y <pkg>`` invocation as a slot under
    ``$npm_config_cache/_npx/<hash>/`` holding a ``package.json`` +
    ``package-lock.json``. When a later run supplies ``--before`` (our
    cutoff), npm reconciles the request against that EXISTING lock; because
    the lock was resolved with no cutoff it can name versions published after
    it, npm judges the lock untrustworthy and aborts:

        npm error code ECOMPROMISED
        npm error Lock compromised

    Measured on the same cache dir: cutoff ON -> ECOMPROMISED on 3/3 runs;
    cutoff OFF -> the server starts. Deleting the slot and retrying WITH the
    cutoff also starts cleanly, which isolates the stale lock as the cause.
    ``npm cache verify`` does NOT repair it (it garbage-collected 86 corrupt
    content entries and the failure persisted) because the slot is not
    content-addressed cache -- it is a materialised install tree.

    The blast radius is the worst possible shape: a machine that has NEVER run
    Tofu is fine (no slots), while every EXISTING deployment breaks on every
    npx-launched server the moment the cutoff ships. So the cutoff is only
    correct if it also owns the migration of trees resolved under the old
    rules -- otherwise "reproducible" is a property of empty disks only.

    npm does not record ``before`` in the lock, so staleness cannot be read
    back out of npm's own metadata. We therefore stamp our own marker beside
    the lock and treat a missing/differing marker as stale. That makes the
    check EXACT rather than heuristic: no version parsing, no date comparison
    against publish times, and slots already reconciled are left untouched, so
    this is a one-time cost per slot rather than a wipe on every connect.

    Deleting a slot is safe: it is a cache npm rebuilds on demand (measured --
    the wiped 12306 slot rebuilt and the server started).

    Returns the number of slots evicted.
    """
    npx_root = os.path.join(npm_cache, '_npx')
    if not os.path.isdir(npx_root):
        return 0
    import shutil as _shutil

    evicted = 0
    try:
        slots = os.listdir(npx_root)
    except OSError as e:
        logger.debug('[MCP] cannot list npx cache %s: %s', npx_root, e)
        return 0

    for slot in slots:
        slot_dir = os.path.join(npx_root, slot)
        lock = os.path.join(slot_dir, 'package-lock.json')
        # No lock => nothing for npm to reconcile against => cannot trigger
        # ECOMPROMISED. Leave it alone.
        if not os.path.isfile(lock):
            continue
        marker = os.path.join(slot_dir, _NPX_CUTOFF_MARKER)
        try:
            with open(marker, encoding='utf-8') as f:
                stamped = f.read().strip()
        except OSError as e:
            logger.debug('[MCP] npx cutoff marker unreadable (%s) — treating slot as stale', e)
            stamped = ''
        if stamped == cutoff:
            continue  # already reconciled under the active cutoff
        try:
            _shutil.rmtree(slot_dir)
            os.makedirs(slot_dir, exist_ok=True)
            with open(marker, 'w', encoding='utf-8') as f:
                f.write(cutoff)
            evicted += 1
        except OSError as e:
            # Never fail a connect over cache hygiene -- npm would merely
            # re-raise ECOMPROMISED, which is no worse than before.
            logger.warning('[MCP] could not evict stale npx slot %s: %s', slot_dir, e)

    if evicted:
        logger.info('[MCP] evicted %d npx cache slot(s) resolved under a '
                    'different supply cutoff (now %s) -- npm would otherwise '
                    'abort with ECOMPROMISED', evicted, cutoff)
    return evicted


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

    # ── Supply-chain cutoff: make a FLOATING launcher spec reproducible ──
    #
    # Measured (2026-07-31): the frozen spec
    # ``uvx --from 'overleaf-mcp-plus[compile]>=0.1.3'`` produced FIVE
    # different ``mcp`` versions across the 30 envs in data/mcp-cache —
    # 1.27.2 / 1.28.0 / 1.28.1 / 1.29.0 and, three times, the breaking
    # **2.0.0** that crashes the server at import. The server's own version
    # never moved (0.2.1 every time): 100% of the drift was TRANSITIVE. So
    # pinning launcher specs cannot fix this, and neither can isolating the
    # environments — each cold resolve is an independent lottery against
    # whatever is on the index at that instant.
    #
    # A date cutoff fixes the whole tree at once, without touching the 50
    # floating specs in the catalog. Measured: two cold resolves with the same
    # cutoff produced byte-identical dependency trees; ``--before`` on npm
    # likewise pinned the transitive tree (zod 3.24.1 vs an unconstrained
    # 3.25.76 + 4.4.3). Both knobs are read from the environment, so this one
    # seam — which every launcher subprocess already passes through — covers
    # uv/uvx and npm/npx alike.
    #
    # ``setdefault`` semantics apply as everywhere else here: an operator who
    # exports their own value keeps it, and TOFU_MCP_SUPPLY_CUTOFF='' disables
    # the floor entirely (opting back in to floating resolution).
    cutoff = os.environ.get('TOFU_MCP_SUPPLY_CUTOFF', _SUPPLY_CUTOFF_DEFAULT).strip()

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

    if cutoff:
        # uv wants an RFC3339 instant; npm's --before takes a plain date.
        env.setdefault('UV_EXCLUDE_NEWER', cutoff)
        env.setdefault('npm_config_before', cutoff.split('T', 1)[0])
        # NOTE: npx-cache reconciliation deliberately does NOT happen here.
        # This function runs INSIDE the readiness timer (the owner task builds
        # its env just before spawning), so an eviction here forces the cold
        # rebuild to compete with the handshake for the same 65s budget --
        # measured 58.6s / 65.0s / 63.8s across three trials, i.e. a coin flip.
        # Reconciliation is a cache-MIGRATION concern, not a per-spawn one, so
        # it belongs in connect_server's pre-flight; see reconcile_for_connect.


def _npx_rebuild_pending(npm_cache: str, cutoff: str) -> int:
    """Count slots we evicted that npm has NOT rebuilt yet.

    WHY THIS EXISTS (measured 2026-07-31 — a real flaw in the first fix)
    -------------------------------------------------------------------
    Eviction is GLOBAL: one pass clears every stale slot in the cache. But the
    wide budget was handed only to the connect that happened to trigger that
    pass. With two stale slots, ``github`` connected first, evicted BOTH, took
    the 300s budget for itself and finished in 43.1s — then ``12306-train``
    connected with the ORDINARY budget against a tree that was still empty and
    failed at **31.4s**, i.e. on the narrow 30s handshake timer. 6 OK / 1 FAIL.

    So "did I just evict something" is the wrong question; the right one is
    "is a rebuild still outstanding for anyone". An evicted slot is left as a
    directory containing ONLY the marker (``_reconcile_npx_cache`` removes the
    tree, recreates the dir, writes the marker), and npm repopulates
    ``package.json``/``package-lock.json``/``node_modules`` when it next runs
    that package. That makes "marker present, lock absent" an exact, durable,
    self-clearing signal — it survives process restarts and needs no in-memory
    bookkeeping, which matters because a fleet connect sweep and a later manual
    reconnect are different call stacks.
    """
    npx_root = os.path.join(npm_cache, '_npx')
    if not os.path.isdir(npx_root):
        return 0
    pending = 0
    try:
        slots = os.listdir(npx_root)
    except OSError as e:
        logger.debug('[MCP] cannot list npx cache %s: %s', npx_root, e)
        return 0
    for slot in slots:
        slot_dir = os.path.join(npx_root, slot)
        marker = os.path.join(slot_dir, _NPX_CUTOFF_MARKER)
        if not os.path.isfile(marker):
            continue
        # A slot we emptied has the marker but no lock yet. Once npm rebuilds
        # it the lock reappears and this stops counting.
        if not os.path.isfile(os.path.join(slot_dir, 'package-lock.json')):
            pending += 1
    return pending


def reconcile_for_connect() -> int:
    """Pre-flight cache migration. Returns how many npx rebuilds are OUTSTANDING.

    Called from ``connect_server`` BEFORE the readiness timer starts. A non-zero
    return means a cold dependency download is unavoidable for this connect, so
    the caller widens the readiness budget.

    The value counts OUTSTANDING rebuilds, not just evictions performed by THIS
    call, because eviction is global while the budget is per-connect — see
    ``_npx_rebuild_pending`` for the measured failure that distinction fixes
    (a 31.4s failure on the narrow timer for the second server in a sweep).

    WHY THE BUDGET IS REPORTED RATHER THAN HIDDEN (measured 2026-07-31)
    -------------------------------------------------------------------
    Evicting a slot is correct -- npm would otherwise abort with ECOMPROMISED
    against the pre-cutoff lock -- but the rebuild it forces is not free. The
    FIRST connect after an eviction was measured at 58.6s / 65.0s / 63.8s
    against a readiness ceiling of ``MCP_CONNECT_TIMEOUT * 2 + 5`` = 65s. That
    is a coin flip, and the losing side surfaces as ``BrokenResourceError`` --
    indistinguishable from a server that genuinely crashed. Trading a
    deterministic failure for a nondeterministic one is a bad trade even though
    the average improves.

    Reporting it keeps the ordinary ceiling intact (a server that never comes up
    is still a fast, honest failure -- the distinction lib/mcp/types.py:18-25
    insists on) while giving the one state we can positively identify -- "a
    dependency download is pending because we deleted the tree" -- the time it
    actually needs.
    """
    env: dict[str, str] = {}
    _ensure_writable_caches(env)
    cutoff = env.get('UV_EXCLUDE_NEWER', '')
    cache = env.get('npm_config_cache', '')
    if not cutoff or not cache:
        return 0  # cutoff disabled, or cache dir unusable -- nothing to migrate
    try:
        _reconcile_npx_cache(cache, cutoff)
        return _npx_rebuild_pending(cache, cutoff)
    except Exception as e:  # cache hygiene must never break a connect
        logger.warning('[MCP] npx cache reconcile failed: %s', e)
        return 0


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
    # The DEV sibling is the only thing we could ever vendor FROM. ``vendor/``
    # rows are export bundles, not dev checkouts — picking one here would make
    # every export deployment warn 'snapshot missing' spuriously.
    sibling_rel = next((s for s in sources if s.startswith('..')), None)
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


def vendored_launch_argv(command: str) -> list[str] | None:
    """Resolve a vendored bare command to its ISOLATED launch argv.

    Returns ``['uv', 'run', '--no-project', '--with-editable', <src>, command]``
    when ``command`` names a registered vendored server whose source exists,
    else ``None``. This is THE decoupling mechanism: the spawned process gets
    its own resolved environment, so the server's ``mcp`` and Tofu's ``mcp``
    are independent resolutions (a shared interpreter is the only thing that
    ever coupled them).

    ``--with-editable`` is load-bearing, not a preference. Measured
    2026-07-31: ``uvx --from <dir>`` serves a cached wheel build, and neither
    ``--refresh`` nor ``--reinstall`` picked up a NEW file added to the
    source — the installed package simply lacked it. Editable links the source
    tree instead, so sibling dev edits and freshly re-vendored snapshots are
    live on the next connect.

    ``--no-project`` keeps uv from resolving the enclosing chatui project.
    """
    if not command or os.sep in command or (os.altsep and os.altsep in command):
        return None
    found = _pkg()._find_vendored_source(command)
    if not found:
        return None
    src, _editable = found
    return ['uv', 'run', '--no-project', '--with-editable', src, command]


def prewarm_vendored_launcher(command: str) -> tuple[bool, str]:
    """Resolve a vendored server's isolated env BEFORE connect.

    Intended to run in a normal worker thread (the install route's job worker
    does exactly this), NOT on the MCP event loop — the subprocess here can
    take a cold dependency-resolution hit and must never freeze live MCP
    traffic.

    Being on PATH is deliberately NOT a fast path anymore: the bridge launches
    the isolated copy regardless of what console scripts exist in the shared
    env, so the only warm that matters is the uv one.

    Returns ``(ready, detail)`` — ``(True, launch argv)`` on success,
    ``(False, reason)`` on failure (also stored in ``_install_last_error``).
    Non-vendored commands return ``(True, '')`` so callers just proceed.
    """
    pkg = _pkg()
    if not command or os.sep in command:
        return True, ''
    argv = pkg.vendored_launch_argv(command)
    if argv is None:
        # Not something we can warm — let connect surface the hint.
        return True, ''

    env = dict(os.environ)
    _ensure_writable_caches(env)
    import_name = command.replace('-', '_')
    warm = argv[:-1] + ['python', '-c', f'import {import_name}']
    logger.info('[MCP] pre-warming vendored launcher %r: %s', command, ' '.join(warm))
    try:
        proc = pkg.subprocess.run(
            warm, env=env, capture_output=True, text=True, timeout=300)
    except (pkg.subprocess.TimeoutExpired, OSError) as e:
        msg = f'uv warm did not complete: {e}'
        logger.error('[MCP] pre-warm of %r failed: %s', command, e)
        with pkg._install_lock:
            pkg._install_last_error[command] = msg
        return False, msg
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or '').strip().splitlines()[-5:]
        msg = f'uv warm exited {proc.returncode}: ' + ' | '.join(tail)
        logger.error('[MCP] pre-warm of %r failed: %s', command, msg)
        with pkg._install_lock:
            pkg._install_last_error[command] = msg
        return False, msg
    with pkg._install_lock:
        pkg._install_last_error.pop(command, None)
    logger.info('[MCP] pre-warm of %r OK', command)
    return True, ' '.join(argv)


def prewarm_all_vendored() -> dict[str, str]:
    """Warm the isolated env of every registered vendored launcher.

    Meant to run ONCE in a background thread at startup so the App-Store
    "Install" click (and boot auto-connect) is normally just the sub-second
    MCP handshake instead of a cold dependency resolution. Blocking — never
    call from the event loop.

    Returns ``{command: 'ok' | reason}`` for the launchers that had a source
    to warm.
    """
    pkg = _pkg()
    pkg._reload_vendored_if_changed()
    out: dict[str, str] = {}
    for command in list(pkg._VENDORED_LAUNCHERS.keys()):
        try:
            if pkg._find_vendored_source(command) is None:
                continue
            ready, detail = pkg.prewarm_vendored_launcher(command)
            out[command] = 'ok' if ready else (detail or 'warm failed')
        except Exception as e:  # never let pre-warm crash boot
            logger.warning('[MCP] pre-warm of %r failed: %s', command, e)
            out[command] = f'error: {e}'
    return out
