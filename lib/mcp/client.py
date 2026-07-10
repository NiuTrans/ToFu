"""lib/mcp/client.py — MCP Bridge: lifecycle, discovery, dispatch.

Manages MCP server subprocesses, discovers their tools, translates to OpenAI
function-calling format, and dispatches tool calls back to the correct server.

Design notes:
  - Each MCP server runs as a child process (stdio transport) or remote
    endpoint (SSE transport).
  - Sessions are long-lived and reused across task rounds.
  - A background asyncio event loop runs in a dedicated daemon thread so
    the sync Flask/tool-handler code can call ``bridge.call_tool()`` without
    blocking the main event loop.
  - Thread-safe: all public methods are safe to call from any thread.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any

from lib.log import audit_log, get_logger, log_context
from lib.mcp.config import load_mcp_config
from lib.mcp.types import (
    MCP_BREAKER_BASE_BACKOFF,
    MCP_BREAKER_MAX_BACKOFF,
    MCP_CRED_PROBE_INTERVAL,
    MCP_CALL_TIMEOUT,
    MCP_CONNECT_TIMEOUT,
    MCP_DEGRADED_TIMEOUT_STREAK,
    MCP_KEEPALIVE_INTERVAL,
    MCP_MAX_RESULT_CHARS,
    MCP_PING_TIMEOUT,
    MCPToolInfo,
    make_namespaced_name,
    parse_namespaced_name,
)

logger = get_logger(__name__)


# ── Connect-failure diagnostics ──────────────────────────

# How many bytes of child-process stderr to keep for failure diagnosis.
# Just enough to surface a Python traceback or a "module not found" line
# without flooding the UI / log file.
_MCP_STDERR_TAIL_BYTES = 8192


def _unwrap_exception_group(exc: BaseException) -> BaseException:
    """Return the deepest non-group leaf exception in a (possibly nested)
    ``BaseExceptionGroup`` chain.

    anyio / mcp wrap the real failure in 2-3 levels of
    ``ExceptionGroup: unhandled errors in a TaskGroup (1 sub-exception)``,
    which is useless to show users. We walk ``.exceptions`` to find the
    first concrete leaf and return it. If no leaf is found (degenerate
    case), the original exception is returned unchanged.
    """
    try:
        Group = BaseExceptionGroup  # type: ignore[name-defined]
    except NameError:  # pragma: no cover — Python < 3.11
        return exc

    seen: set[int] = set()
    cur: BaseException | None = exc
    while isinstance(cur, Group):
        if id(cur) in seen:
            break
        seen.add(id(cur))
        subs = list(getattr(cur, 'exceptions', ()) or ())
        if not subs:
            break
        # Prefer the first non-group sub; otherwise descend into the first group.
        leaf = next((s for s in subs if not isinstance(s, Group)), None)
        cur = leaf if leaf is not None else subs[0]
    return cur if cur is not None else exc


def _is_transport_dead_error(exc: BaseException) -> bool:
    """Heuristically decide whether ``exc`` means the MCP transport is gone.

    Used by the reactive reconnect path: a tool call that fails because the
    underlying stdio pipe / SSE stream died (subprocess crashed, peer closed,
    broken pipe) is retryable after a fresh reconnect, whereas a genuine
    tool-level error (bad args, server-side exception) is not — reconnecting
    would just loop. We match on the concrete leaf exception type/text.

    Conservative by design: when unsure we return False, so we never mask a
    real tool error behind an endless reconnect cycle.
    """
    leaf = _unwrap_exception_group(exc)
    # anyio stream-lifecycle errors are the canonical "pipe is gone" signals.
    name = type(leaf).__name__
    if name in (
        'ClosedResourceError', 'BrokenResourceError', 'EndOfStream',
        'BrokenPipeError', 'ConnectionResetError', 'ConnectionError',
    ):
        return True
    text = (str(leaf) or '').lower()
    needles = (
        'connection closed', 'broken pipe', 'closed resource',
        'connection reset', 'end of stream', 'transport closed',
        'session is closed', 'peer closed',
    )
    return any(n in text for n in needles)


def _is_call_timeout_error(exc: BaseException) -> bool:
    """True when ``exc`` is a tool-call timeout (transport read-timeout or the
    outer thread-future timeout), as opposed to a tool-level / transport-dead
    error. Used to drive the call-level degraded-health gate.

    Matches:
      - ``concurrent.futures.TimeoutError`` / ``asyncio.TimeoutError`` /
        builtin ``TimeoutError`` raised by the outer ``future.result(...)``;
      - mcp's ``McpError`` whose text reports "Timed out while waiting for
        response" (the read_timeout_seconds path).
    """
    leaf = _unwrap_exception_group(exc)
    if isinstance(leaf, (TimeoutError, asyncio.TimeoutError)):
        return True
    if type(leaf).__name__ in ('TimeoutError', 'McpError'):
        text = (str(leaf) or '').lower()
        if 'timed out while waiting' in text or 'timeout' in text:
            return True
    return False


def _read_stderr_tail(f, max_bytes: int = _MCP_STDERR_TAIL_BYTES) -> str:
    """Read the last ``max_bytes`` bytes of a binary tempfile, decoded as UTF-8.

    Returns '' on any failure — diagnostics must never raise.
    """
    if f is None:
        return ''
    try:
        f.flush()
    except OSError as e:
        logger.debug('[MCP] stderr tempfile flush failed: %s', e)
    try:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - max_bytes))
        data = f.read()
    except OSError as e:
        logger.debug('[MCP] Could not read stderr tail: %s', e)
        return ''
    if not data:
        return ''
    # `errors='replace'` makes decode infallible for utf-8, but keep a
    # narrow guard for the rare LookupError if stdlib codec is missing.
    try:
        text = data.decode('utf-8', errors='replace')
    except (UnicodeDecodeError, LookupError) as e:
        logger.debug('[MCP] stderr tail decode failed: %s', e)
        return ''
    # Trim partial leading line if we sliced mid-line.
    if size > max_bytes and '\n' in text:
        text = text.split('\n', 1)[1]
    return text.strip()


class MCPConnectError(RuntimeError):
    """Connection failure for a single MCP server with a user-facing message
    that includes the actionable root cause and the child process's stderr
    tail (when stdio transport).

    ``str(e)`` yields the formatted user-facing message; ``.cause`` holds
    the original (unwrapped) leaf exception, ``.stderr_tail`` holds the
    captured stderr (may be empty).
    """

    def __init__(self, server_name: str, cause: BaseException, stderr_tail: str = ''):
        self.server_name = server_name
        self.cause = cause
        self.stderr_tail = stderr_tail or ''
        super().__init__(self._format())

    def _format(self) -> str:
        cause_msg = (str(self.cause) or type(self.cause).__name__).strip()
        # MCP's "Connection closed" is too terse — clarify it.
        if cause_msg == 'Connection closed':
            cause_msg = (
                'Connection closed by server during initialize '
                '(the launcher started but exited before completing the MCP handshake)'
            )
        msg = f'MCP server {self.server_name!r}: {cause_msg}'
        if self.stderr_tail:
            tail = self.stderr_tail
            # Cap at 1500 chars when surfaced to the UI — full tail is in logs.
            if len(tail) > 1500:
                tail = '…' + tail[-1500:]
            msg += f'\n\nServer stderr (tail):\n{tail}'
        return msg


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
        os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')),
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


def _coerce_one(value: Any, schema: dict[str, Any]) -> Any:
    """Best-effort coerce ``value`` to match ``schema``'s declared type.

    Handles the most common LLM-shaped mistakes: strings-instead-of-ints,
    strings-instead-of-bools, and single-value-instead-of-array. Unknown
    / unparseable values are returned unchanged so downstream jsonschema
    validation still surfaces a clear error for genuine type mismatches.

    Supports JSON Schema ``type`` as either a single string or a list
    (e.g. ``["integer","null"]``) — the first non-null entry is used.
    """
    if not isinstance(schema, dict):
        return value
    t = schema.get('type')
    # resolve `type: ["integer", "null"]` → "integer"
    if isinstance(t, list):
        t = next((x for x in t if x != 'null'), None)

    # anyOf / oneOf: try each branch, return the first that produces a
    # value whose Python type matches the branch. Keeps the behavior
    # conservative — if none match, fall through.
    for key in ('anyOf', 'oneOf'):
        branches = schema.get(key)
        if isinstance(branches, list) and branches:
            for branch in branches:
                coerced = _coerce_one(value, branch)
                if coerced is not value:
                    return coerced
            return value

    if t == 'integer' and isinstance(value, str):
        s = value.strip()
        if s and (s.lstrip('-').isdigit()):
            try:
                return int(s)
            except ValueError as _e_audit:
                logger.debug('[client] _coerce_one caught %s: %s', type(_e_audit).__name__, _e_audit)
                return value
    elif t == 'number' and isinstance(value, str):
        s = value.strip()
        try:
            return float(s)
        except ValueError as _e_audit:
            logger.debug('[client] _coerce_one caught %s: %s', type(_e_audit).__name__, _e_audit)
            return value
    elif t == 'boolean' and isinstance(value, str):
        s = value.strip().lower()
        if s in ('true', '1', 'yes', 'y'):
            return True
        if s in ('false', '0', 'no', 'n'):
            return False
    elif t == 'array':
        items_schema = schema.get('items') or {}
        # Wrap scalar-instead-of-array.
        if not isinstance(value, list):
            value = [value]
        if isinstance(items_schema, dict):
            return [_coerce_one(v, items_schema) for v in value]
        return value
    elif t == 'object' and isinstance(value, dict):
        props = schema.get('properties') or {}
        if isinstance(props, dict):
            return {
                k: (_coerce_one(v, props[k]) if k in props else v)
                for k, v in value.items()
            }
    return value


def _coerce_args_to_schema(
    arguments: dict[str, Any], schema: dict[str, Any],
) -> dict[str, Any]:
    """Walk a tool-call arg dict and coerce each entry per the tool's input schema."""
    if not isinstance(arguments, dict) or not isinstance(schema, dict):
        return arguments
    props = schema.get('properties')
    if not isinstance(props, dict):
        return arguments
    out: dict[str, Any] = {}
    for k, v in arguments.items():
        sub = props.get(k)
        if isinstance(sub, dict):
            out[k] = _coerce_one(v, sub)
        else:
            out[k] = v
    return out


def _extract_read_only_hint(tool: Any) -> bool:
    """Return the MCP ``annotations.readOnlyHint`` for *tool* (default False).

    The MCP spec puts behavioural hints on ``Tool.annotations`` (a
    ``ToolAnnotations`` object with optional ``readOnlyHint`` / ``destructiveHint``
    / … fields). Older servers omit it entirely. We treat a tool as read-only
    ONLY when the hint is explicitly True — anything else (missing, False,
    unparsable) is conservatively treated as a write tool by the caller.
    """
    annotations = getattr(tool, 'annotations', None)
    if annotations is None:
        return False
    try:
        hint = getattr(annotations, 'readOnlyHint', None)
        if hint is None and isinstance(annotations, dict):
            hint = annotations.get('readOnlyHint')
        return hint is True
    except Exception as e:
        logger.debug('[MCP] readOnlyHint extraction failed for %s: %s',
                     getattr(tool, 'name', '?'), e)
        return False


# ── Vendored MCP servers: first-connect auto-install ─────
#
# Internal MCP servers (hope-mcp, …) are private and not on PyPI, so a fresh
# Tofu checkout has no way to obtain them — the user just sees "launcher X is
# not on PATH". To make onboarding zero-touch we ship the source in-repo and
# pip-install it into Tofu's OWN interpreter on first connect.
#
# The registry + repo_root live in the tiny, dependency-free
# ``lib/mcp/vendored`` module so the release-time vendoring script can read
# the same source of truth WITHOUT importing this heavy module. The
# underscore aliases below preserve the historical internal names used
# throughout this file (and monkeypatched by tests).
from lib.mcp import vendored as _vendored_mod
from lib.mcp.vendored import VENDORED_LAUNCHERS as _VENDORED_LAUNCHERS
from lib.mcp.vendored import repo_root as _repo_root

# Guard so a failing install is attempted at most once per process per
# command — otherwise every reconnect sweep would re-run pip. Protected by
# ``_install_lock``.
_install_attempted: set[str] = set()
_install_lock = threading.Lock()
# Per-command serialization so two concurrent callers (e.g. the startup
# pre-warm racing a user "Install" click) never run ``pip install`` of the
# SAME source at once — the second waits, then sees ``_install_attempted`` and
# just re-resolves. The registry dict itself is guarded by ``_install_lock``.
_install_cmd_locks: dict[str, threading.Lock] = {}
# Last auto-install failure reason per command, surfaced in the connect error
# so the user sees WHY zero-touch install failed (pip stderr / no source /
# timeout) instead of a generic "not on PATH" dead-end. Set under _install_lock.
_install_last_error: dict[str, str] = {}

# ── Hot-reload of the vendored registry ──────────────────
#
# ``vendored.py`` is imported once at startup, so a NEW row added to
# ``VENDORED_LAUNCHERS`` while Tofu is already running is invisible until a
# restart — the #1 papercut when onboarding a freshly-added internal MCP.
# To make that truly zero-touch we re-read the file when its mtime changes
# and MERGE any new/changed rows into the LIVE ``_VENDORED_LAUNCHERS`` dict.
#
# Why merge-in-place instead of rebinding: the live dict's *identity* is part
# of the contract — tests monkeypatch entries via ``setitem`` on it, and the
# module-level alias is shared. We mutate the existing object so every holder
# (including a test's patched view) sees the update without a rebind.
_vendored_mtime: float = 0.0
_vendored_reload_lock = threading.Lock()


def _vendored_path() -> str:
    return getattr(_vendored_mod, '__file__', '') or ''


def _reload_vendored_if_changed() -> None:
    """Re-import ``vendored.py`` if its mtime advanced; merge new rows live.

    Cheap: one ``os.path.getmtime`` stat per call, and the actual reload only
    runs when the file genuinely changed. Failures (file gone, syntax error
    mid-edit) are swallowed — we keep the last-good registry rather than break
    every connect. Never DELETES rows (a half-saved edit shouldn't drop a
    server mid-flight); it only adds/updates.
    """
    path = _vendored_path()
    if not path:
        return
    global _vendored_mtime
    try:
        mtime = os.path.getmtime(path)
    except OSError as e:
        logger.debug('[MCP] vendored.py stat failed (%s) — keeping last-good', e)
        return
    if mtime <= _vendored_mtime:
        return
    with _vendored_reload_lock:
        # Re-check under lock — another thread may have just reloaded.
        try:
            mtime = os.path.getmtime(path)
        except OSError as e:
            logger.debug('[MCP] vendored.py re-stat under lock failed (%s)', e)
            return
        if mtime <= _vendored_mtime:
            return
        try:
            importlib.reload(_vendored_mod)
            fresh = dict(getattr(_vendored_mod, 'VENDORED_LAUNCHERS', {}) or {})
        except Exception as e:
            # Mid-edit syntax error or transient read failure — keep last-good
            # registry and retry on the next mtime bump.
            logger.warning('[MCP] vendored.py reload failed (keeping last-good '
                           'registry): %s', e)
            # Advance the baseline so we don't hammer reload on every call while
            # the file is briefly broken; the next real save bumps mtime again.
            _vendored_mtime = mtime
            return
        added = [k for k in fresh if k not in _VENDORED_LAUNCHERS]
        # Merge in place (preserve dict identity for test monkeypatches).
        _VENDORED_LAUNCHERS.update(fresh)
        _vendored_mtime = mtime
        if added:
            logger.info('[MCP] vendored registry hot-reloaded: +%d new server(s) %s',
                        len(added), ', '.join(sorted(added)))


# Baseline the mtime at import time so an edit made BETWEEN process start and
# the first connect is still detected (mtime then strictly advances).
try:
    _vendored_mtime = os.path.getmtime(_vendored_path()) if _vendored_path() else 0.0
except OSError as e:
    logger.debug('[MCP] vendored.py baseline stat failed (%s) — defaulting mtime=0', e)
    _vendored_mtime = 0.0


# ── Vendored-snapshot staleness detection (+ opt-in auto-vendor) ──
#
# The ``tools/<name>`` snapshot is what a DEPLOY (no sibling checkout) installs
# from; it is committed to git and refreshed at release time via
# ``make vendor-mcp`` (scripts/vendor_mcp.sh rsyncs sibling → snapshot). The
# papercut: it's easy to edit the sibling and forget to re-vendor, so a STALE
# snapshot gets committed/shipped.
#
# On a dev box the running process installs from the SIBLING (editable), never
# the snapshot — so we never silently rewrite git-tracked files during a
# request. Instead:
#   A) ALWAYS-ON detection: at connect, compare sibling vs snapshot and LOG a
#      loud warning when they drift (zero writes).
#   B) OPT-IN rebuild: only when TOFU_MCP_AUTO_VENDOR is truthy do we shell out
#      to vendor_mcp.sh to refresh the snapshot — explicit, never the default.
#
# Both are no-ops on a deploy (no sibling on disk = nothing to compare/rebuild)
# and for non-vendored servers (npx/uvx).

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


# Last drift state we LOGGED per command ('' = fresh), so we warn once per
# distinct drift instead of on every reconnect. Protected by _snapshot_lock.
_snapshot_reported: dict[str, str] = {}
_snapshot_lock = threading.Lock()


def _run_vendor_script(command: str, root: str) -> bool:
    """Shell out to scripts/vendor_mcp.sh for ONE server. Returns success."""
    script = os.path.join(root, 'scripts', 'vendor_mcp.sh')
    if not os.path.isfile(script):
        logger.warning('[MCP] auto-vendor requested but %s not found', script)
        return False
    child_env = dict(os.environ)
    child_env['TOFU_PYTHON'] = sys.executable
    _ensure_writable_caches(child_env)
    try:
        proc = subprocess.run(
            ['bash', script, command], cwd=root, env=child_env,
            capture_output=True, text=True, timeout=120,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
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
    spec = _VENDORED_LAUNCHERS.get(command)
    if not spec:
        return
    sources = spec.get('sources', [])
    sibling_rel = next((s for s in sources if not s.startswith('tools/')), None)
    snap_rel = next((s for s in sources if s.startswith('tools/')), None)
    if not sibling_rel or not snap_rel:
        return  # need BOTH a sibling source and a snapshot dest to compare
    root = _repo_root()
    sibling = os.path.abspath(os.path.join(root, sibling_rel))
    snapshot = os.path.abspath(os.path.join(root, snap_rel))
    # Only meaningful where the sibling dev checkout exists — that's the only
    # thing we could vendor FROM. On a deploy (snapshot-only) stay silent.
    if not os.path.isfile(os.path.join(sibling, 'pyproject.toml')):
        return

    try:
        reason = _snapshot_stale_reason(sibling, snapshot)
    except OSError as e:
        logger.debug('[MCP] snapshot staleness check for %r failed: %s', command, e)
        return

    with _snapshot_lock:
        already = _snapshot_reported.get(command)
        if reason == already:
            return  # same state as last time we acted — stay quiet
        _snapshot_reported[command] = reason

    if not reason:
        return  # fresh (possibly just re-vendored) — nothing to say

    logger.warning(
        '[MCP] vendored snapshot tools/%s is STALE vs sibling %s: %s. '
        'Run `make vendor-mcp %s` before committing/deploying.',
        command, sibling, reason, command)

    if not _auto_vendor_enabled():
        return

    logger.info('[MCP] TOFU_MCP_AUTO_VENDOR set — rebuilding snapshot tools/%s', command)
    if _run_vendor_script(command, root):
        # Recompute so we report the fresh state (and don't re-warn next time).
        try:
            new_reason = _snapshot_stale_reason(sibling, snapshot)
        except OSError as e:
            logger.debug('[MCP] post-vendor staleness recheck failed: %s', e)
            new_reason = ''
        with _snapshot_lock:
            _snapshot_reported[command] = new_reason
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
    _reload_vendored_if_changed()
    spec = _VENDORED_LAUNCHERS.get(command)
    if not spec:
        return None
    root = _repo_root()
    tools_dir = os.path.join(root, 'tools') + os.sep
    for rel in spec['sources']:
        cand = rel if os.path.isabs(rel) else os.path.abspath(os.path.join(root, rel))
        if os.path.isfile(os.path.join(cand, 'pyproject.toml')):
            is_vendored = (cand + os.sep).startswith(tools_dir)
            return cand, (not is_vendored)
    return None


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
    found = _find_vendored_source(command)
    if not found:
        # Unregistered or no source on disk — nothing we can pip-install. This
        # is NOT a sticky failure: we never touch _install_attempted, so a
        # later call (e.g. after the source appears) can still try.
        return None
    src, editable = found

    # Grab (or create) the per-command lock, then hold it across the whole
    # check-then-pip section so a concurrent caller for the SAME command waits
    # here instead of launching a second simultaneous pip on the same source.
    with _install_lock:
        cmd_lock = _install_cmd_locks.setdefault(command, threading.Lock())

    with cmd_lock:
        with _install_lock:
            if command in _install_attempted:
                # pip already RAN for this command this process (possibly by
                # the caller we just waited behind). Re-resolve in case it
                # succeeded, but never pip again. (The guard is only set AFTER
                # a real pip run below, so a transient pre-pip error never
                # wedges the command permanently.)
                return _resolve_launcher(command)
        return _run_pip_install(command, src, editable)


def _run_pip_install(command: str, src: str, editable: bool) -> str | None:
    """Actually shell out to pip for ``command``; caller holds its cmd lock."""
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
        proc = subprocess.run(
            pip_args, env=child_env, capture_output=True, text=True,
            timeout=300,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        # pip never produced a result. Do NOT set the one-shot guard — this is
        # often transient (timeout, transient OS error), so a later retry (or
        # the "Reinstall" button) should be allowed to try again.
        msg = f'pip did not complete: {e}'
        logger.error('[MCP] auto-install of %r failed to run pip: %s', command, e)
        with _install_lock:
            _install_last_error[command] = msg
        return None

    # pip actually ran (rc known) → this counts as the one allowed attempt.
    with _install_lock:
        _install_attempted.add(command)

    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or '').strip().splitlines()[-5:]
        joined = ' | '.join(tail)
        logger.error('[MCP] auto-install of %r failed (rc=%d): %s',
                     command, proc.returncode, joined)
        with _install_lock:
            _install_last_error[command] = (
                f'pip install {src} exited {proc.returncode}: {joined}')
        return None

    resolved = _resolve_launcher(command)
    if resolved:
        logger.info('[MCP] auto-install of %r OK → %s', command, resolved)
        with _install_lock:
            _install_last_error.pop(command, None)
    else:
        logger.error('[MCP] auto-install of %r reported success but launcher '
                     'still not resolvable', command)
        with _install_lock:
            _install_last_error[command] = (
                f'pip install {src} succeeded but the {command!r} console '
                'script is still not next to the interpreter — check the '
                "package's [project.scripts] entry.")
    return resolved



def is_vendored_launcher(command: str) -> bool:
    """True when ``command`` is a bare, registered vendored MCP launcher.

    Used by the install route to decide whether a slow pip pre-warm is even
    possible before attempting the (fast) connect. No-op for npx/uvx commands
    and anything with a path separator.
    """
    if not command or os.sep in command or (os.altsep and os.altsep in command):
        return False
    return _find_vendored_source(command) is not None


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
    if not command or os.sep in command:
        return True, ''
    if _shutil.which(command):
        return True, ''
    resolved = _resolve_launcher(command)
    if resolved:
        return True, resolved
    if _find_vendored_source(command) is None:
        # Not something we can pip-install — let connect surface the hint.
        return True, ''
    resolved = _try_autoinstall_launcher(command)
    if resolved:
        return True, resolved
    with _install_lock:
        why = _install_last_error.get(command, '')
    return False, why



def prewarm_all_vendored() -> dict[str, str]:
    """Pip-install every registered vendored launcher that isn't resolvable yet.

    Meant to run ONCE in a background thread at startup so the App-Store
    "Install" click is normally just the sub-second MCP handshake instead of
    a cold pip install. Blocking (pip) — never call from the event loop.

    Returns ``{command: 'ok' | reason}`` for the launchers that needed work
    (already-resolvable ones are skipped silently).
    """
    _reload_vendored_if_changed()
    out: dict[str, str] = {}
    for command in list(_VENDORED_LAUNCHERS.keys()):
        try:
            import shutil as _shutil
            if _shutil.which(command) or _resolve_launcher(command):
                continue
            if _find_vendored_source(command) is None:
                continue
            ready, detail = prewarm_vendored_launcher(command)
            out[command] = 'ok' if ready else (detail or 'install failed')
        except Exception as e:  # never let pre-warm crash boot
            logger.warning('[MCP] pre-warm of %r failed: %s', command, e)
            out[command] = f'error: {e}'
    return out



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
#
# State is a tiny in-process dict (single-process server). Each entry::
#   {state: 'installing'|'ready'|'error', detail: str, started: float, ended: float}
_install_jobs: dict[str, dict] = {}
_install_jobs_lock = threading.Lock()


def get_install_job(command: str) -> dict | None:
    """Return a snapshot of the install job for ``command`` (or None)."""
    with _install_jobs_lock:
        job = _install_jobs.get(command)
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
    # Fast path: already importable → no job needed.
    if not command or os.sep in command or _shutil.which(command) or _resolve_launcher(command):
        snap = {'state': 'ready', 'detail': '', 'started': time.time(), 'ended': time.time()}
        with _install_jobs_lock:
            _install_jobs[command] = snap
        return dict(snap)

    with _install_jobs_lock:
        existing = _install_jobs.get(command)
        if existing is not None and existing.get('state') == 'installing':
            return dict(existing)
        job = {'state': 'installing', 'detail': '', 'started': time.time(), 'ended': 0.0}
        _install_jobs[command] = job

    def _worker():
        try:
            ready, detail = prewarm_vendored_launcher(command)
        except Exception as e:  # defensive — never let the thread die silently
            ready, detail = False, f'install crashed: {e}'
            logger.error('[MCP] install job for %r crashed: %s', command, e, exc_info=True)
        with _install_jobs_lock:
            _install_jobs[command] = {
                'state': 'ready' if ready else 'error',
                'detail': detail,
                'started': job['started'],
                'ended': time.time(),
            }
        logger.info('[MCP] install job for %r finished: %s',
                    command, 'ready' if ready else f'error ({detail})')

    threading.Thread(target=_worker, name=f'mcp-install:{command}', daemon=True).start()
    with _install_jobs_lock:
        return dict(_install_jobs[command])


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
    base = command.rsplit('/', 1)[-1]

    # Vendored internal MCP servers (registered in lib/mcp/vendored.py) are
    # auto-installed on first connect. If we're showing a hint for one, the
    # zero-touch install must have FAILED — surface the captured reason and
    # the exact manual command pointing at the real in-repo source, instead
    # of the useless generic "package manager" line.
    src_info = _find_vendored_source(base)
    if src_info is not None:
        src, editable = src_info
        with _install_lock:
            why = _install_last_error.get(base, '')
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
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
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



# ══════════════════════════════════════════════════════════
#  Async core — runs on a dedicated event loop thread
# ══════════════════════════════════════════════════════════

class _MCPServerHandle:
    """Internal handle for a connected MCP server.

    Lifecycle is driven by a dedicated "owner" coroutine (see
    ``MCPBridge._server_owner``).  That coroutine opens the
    ``AsyncExitStack`` holding the stdio/SSE transport + ``ClientSession``
    context managers, signals readiness via ``_ready_future``, then blocks
    on ``_shutdown_event`` until shutdown is requested.  This guarantees
    the context stack is always closed **from the same task that opened
    it**, avoiding the anyio cancel-scope mismatch that would otherwise
    make ``aclose()`` hang for ~130s.
    """

    __slots__ = (
        'name', 'config', 'session', 'tools',
        'server_name', 'server_version',  # from InitializeResult.serverInfo
        '_shutdown_event',   # asyncio.Event — set() to request shutdown
        '_ready_future',     # asyncio.Future[list[Tool]] — resolved when init+list_tools done
        '_closed_future',    # asyncio.Future[None] — resolved when owner task exits
        '_owner_task',       # asyncio.Task — the owner coroutine handle
        '_stderr_file',      # tempfile.SpooledTemporaryFile capturing child stderr (stdio transport only)
    )

    def __init__(self, name: str, config: dict):
        self.name = name
        self.config = config
        self.session = None       # mcp.ClientSession (set after connect)
        self.tools: list = []     # list of mcp.types.Tool
        self.server_name = ''     # reported by server via InitializeResult.serverInfo.name
        self.server_version = ''  # reported by server via InitializeResult.serverInfo.version
        self._shutdown_event = None
        self._ready_future = None
        self._closed_future = None
        self._owner_task = None
        self._stderr_file = None


class MCPBridge:
    """Bridge between MCP servers and Tofu's tool system.

    Lifecycle:
        1. ``connect_all()`` — reads config, launches servers, discovers tools.
        2. ``get_openai_tool_defs()`` — returns translated tool definitions.
        3. ``call_tool(namespaced_name, args)`` — dispatches to correct server.
        4. ``disconnect_all()`` — gracefully shuts down all servers.

    Thread safety:
        All public methods are thread-safe.  Async operations run on an
        internal event loop managed by a daemon thread.
    """

    def __init__(self) -> None:
        self._servers: dict[str, _MCPServerHandle] = {}
        self._tool_index: dict[str, MCPToolInfo] = {}  # namespaced_name → info
        self._lock = threading.Lock()

        # Per-server reconnect serialization: prevents the reactive
        # (call_tool) and proactive (keepalive) recovery paths from
        # reconnecting the same server concurrently.
        self._reconnect_locks: dict[str, threading.Lock] = {}

        # Dedicated asyncio event loop for MCP sessions
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._started = False

        # Background keepalive (proactive health-check + reconnect) loop.
        self._keepalive_task: asyncio.Task | None = None
        self._keepalive_stop: asyncio.Event | None = None

        # Per-server circuit breaker: name → (consecutive_failures, next_retry_ts).
        # Set after a reconnect attempt FAILS; gates the keepalive loop so a
        # permanently-broken server isn't respawned every sweep. Protected by
        # ``self._lock``. Cleared on any successful (re)connect.
        self._breaker: dict[str, tuple[int, float]] = {}

        # Per-server CONSECUTIVE call-timeout streak: name → count. Distinct
        # from the reconnect breaker above — this tracks tool calls that time
        # out on a transport that is otherwise alive. At
        # ``MCP_DEGRADED_TIMEOUT_STREAK`` the server is 'degraded' and the next
        # call fast-fails instead of blocking for the full timeout again. Any
        # successful call resets it to 0. Protected by ``self._lock``.
        self._timeout_streak: dict[str, int] = {}

        # Last-known good config per server. Retained so the keepalive loop
        # can keep retrying a server whose live handle was torn down by a
        # FAILED reconnect (connect_server pops the old handle before it
        # re-registers, so a crash-on-start server would otherwise vanish
        # from ``self._servers`` and never be retried). Protected by ``self._lock``.
        self._configs: dict[str, dict] = {}

        # Per-server CREDENTIAL health: name → {status, checked_at, detail}.
        # A SECOND health axis distinct from transport health — a live
        # subprocess with a valid protocol ping can still hold an EXPIRED
        # session cookie/token, so every real tool call fails. Populated by
        # ``_run_cred_probe`` (once on connect + periodically in keepalive) for
        # servers whose catalog entry declares a ``health_probe``. Surfaced via
        # ``get_cred_health`` → the settings panel. Protected by ``self._lock``.
        #   status ∈ {'ok', 'expired', 'unknown'}
        self._cred_health: dict[str, dict] = {}
        # Monotonic timestamp of the last credential probe per server, so the
        # keepalive sweep re-probes at most once per MCP_CRED_PROBE_INTERVAL.
        self._cred_probe_ts: dict[str, float] = {}

    # ── Event loop management ─────────────────────────────

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        """Start the background event loop thread if not running."""
        if self._loop is not None and self._loop.is_running():
            return self._loop
        loop = asyncio.new_event_loop()
        self._loop = loop

        def _run():
            asyncio.set_event_loop(loop)
            loop.run_forever()

        t = threading.Thread(target=_run, name='mcp-event-loop', daemon=True)
        t.start()
        self._loop_thread = t
        # Wait for loop to be running
        for _ in range(50):
            if loop.is_running():
                break
            time.sleep(0.05)
        return loop

    def _run_async(self, coro, timeout: float | None = None) -> Any:
        """Run an async coroutine on the MCP event loop, blocking until done.

        Args:
            coro: The coroutine to drive to completion.
            timeout: Outer (thread-side) wall-clock budget in seconds. The
                inner ``_async_call_tool`` already bounds the request with the
                resolved per-server ``read_timeout_seconds``; this outer cap
                must therefore EXCEED that value (we add 10s of headroom),
                otherwise a server with a longer per-call timeout than the
                global default would be killed here before its own deadline.
                Defaults to ``MCP_CALL_TIMEOUT + 10`` for non-call paths
                (connect / disconnect) that don't carry a per-server budget.
        """
        if timeout is None:
            timeout = MCP_CALL_TIMEOUT + 10
        loop = self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result(timeout=timeout)

    # ── Connection management ─────────────────────────────

    def connect_all(self) -> dict[str, list[str]]:
        """Connect to all enabled MCP servers defined in config.

        Returns:
            Dict mapping server_name → list of tool names discovered.
        """
        config = load_mcp_config()
        if not config:
            logger.info('[MCP] No MCP servers configured')
            return {}

        result = {}
        for name, srv_cfg in config.items():
            if not srv_cfg.get('enabled', True):
                logger.info('[MCP] Skipping disabled server: %s', name)
                continue
            try:
                tools = self.connect_server(name, srv_cfg)
                result[name] = [t.name for t in tools]
            except MCPConnectError as e:
                # Already-formatted root cause + stderr tail. Log the
                # one-line summary at error level; the full chained
                # traceback only at debug.
                logger.error('[MCP] Failed to connect server %s: %s', name, e)
                logger.debug('[MCP] Full traceback for %s:', name,
                             exc_info=(type(e.cause), e.cause, e.cause.__traceback__))
            except Exception as e:
                logger.error('[MCP] Failed to connect server %s: %s', name, e, exc_info=True)
        return result

    # Shutdown budget for a single server owner task (seconds). The owner
    # coroutine should close its context stack near-instantly once signaled
    # (same task that opened it → no cancel-scope mismatch), so this is
    # really a defense-in-depth cap for pathological cases (e.g. subprocess
    # stuck on a syscall). Intentionally modest to keep the UI responsive.
    _DISCONNECT_TIMEOUT = 5.0

    def connect_server(self, name: str, srv_cfg: dict) -> list:
        """Connect to a single MCP server and discover its tools.

        Args:
            name: Unique server identifier (used as namespace).
            srv_cfg: Server configuration dict.

        Returns:
            List of ``mcp.types.Tool`` objects discovered.
        """
        # Detect (and, only if TOFU_MCP_AUTO_VENDOR is set, rebuild) a stale
        # tools/<name> snapshot vs its sibling dev checkout. Always-on path is
        # log-only — never writes git-tracked files by default. No-op for
        # non-vendored commands and on deploys without a sibling.
        try:
            cmd = srv_cfg.get('command', '') if srv_cfg.get('transport', 'stdio') != 'sse' else ''
            if cmd and os.sep not in cmd:
                _check_snapshot_staleness(cmd)
        except Exception as e:
            logger.debug('[MCP] snapshot staleness check skipped: %s', e)

        # Tear down any existing server with the same name BEFORE taking
        # the lock for the new registration. The disconnect itself hits
        # the async loop; holding self._lock across it would freeze every
        # concurrent GET /api/v1/mcp/catalog for the duration.
        had_old = False
        with self._lock:
            had_old = name in self._servers
        if had_old:
            logger.info('[MCP] Reconnecting server %s (was already connected)', name)
            try:
                self._disconnect_one(name)
            except Exception as e:
                # Non-fatal: worst case the OS reaps the stale subprocess
                # when the event loop shuts down. Always log with context.
                logger.warning('[MCP] Error disconnecting old %s: %s', name, e)

        with log_context(f'mcp_connect:{name}', logger=logger):
            handle, tools = self._run_async(self._async_start_owner(name, srv_cfg))

        with self._lock:
            self._servers[name] = handle
            # Build tool index
            for tool in tools:
                ns_name = make_namespaced_name(name, tool.name)
                self._tool_index[ns_name] = MCPToolInfo(
                    server_name=name,
                    tool_name=tool.name,
                    namespaced_name=ns_name,
                    description=tool.description or '',
                    input_schema=tool.inputSchema or {'type': 'object', 'properties': {}},
                    openai_def=self._tool_to_openai(name, tool),
                    read_only_hint=_extract_read_only_hint(tool),
                )
            self._started = True

        logger.info('[MCP] Server %s connected — %d tools discovered: %s',
                    name, len(tools),
                    ', '.join(t.name for t in tools))
        # A successful (re)connect clears any circuit-breaker backoff and
        # records the working config for future retries.
        with self._lock:
            self._configs[name] = dict(srv_cfg)
            if self._breaker.pop(name, None) is not None:
                logger.info('[MCP] Circuit breaker reset for %s (reconnected)', name)
        self._start_keepalive()
        # Verify stored CREDENTIALS in the background — a live subprocess does
        # not imply a valid session cookie/token. Non-blocking; no-op unless
        # the server's catalog entry declares a health_probe.
        self._probe_cred_health_async(name)
        return tools

    # ── Circuit breaker ──────────────────────────────────

    def _breaker_blocks(self, name: str) -> bool:
        """True if ``name`` is in backoff and its next-retry time hasn't arrived.

        Read-only check used by the keepalive sweep to skip servers that are
        currently being backed off after repeated reconnect failures.
        """
        with self._lock:
            entry = self._breaker.get(name)
        if entry is None:
            return False
        _, next_retry_ts = entry
        return time.time() < next_retry_ts

    def get_breaker_state(self, name: str) -> dict[str, Any] | None:
        """Return the circuit-breaker status for ``name`` for UI surfacing.

        Returns ``None`` when the server has no recorded reconnect failures
        (healthy / never failed). Otherwise a dict::

            {
              'failures': int,        # consecutive failed reconnects
              'retry_in': float,      # seconds until next retry (>=0)
              'next_retry_ts': float, # absolute epoch seconds
            }

        ``retry_in`` is clamped to 0 when the backoff window has already
        elapsed (a retry is due on the next keepalive sweep).
        """
        with self._lock:
            entry = self._breaker.get(name)
        if entry is None:
            return None
        failures, next_retry_ts = entry
        return {
            'failures': failures,
            'retry_in': max(0.0, next_retry_ts - time.time()),
            'next_retry_ts': next_retry_ts,
        }

    # ── Credential health (transport-alive but stored secret expired) ──

    def get_cred_health(self, name: str) -> dict[str, Any] | None:
        """Return the last credential-probe result for ``name`` for UI surfacing.

        Returns ``None`` when the server has never been probed (no
        ``health_probe`` declared, or not yet run). Otherwise a dict::

            {
              'status': 'ok' | 'expired' | 'unknown',
              'checked_at': float,   # epoch seconds of the last probe
              'detail': str,         # short reason (empty for 'ok')
            }

        Only ``status == 'expired'`` is actionable — the UI shows a
        "credentials expired" badge. ``'unknown'`` (probe raised / could not
        classify) is deliberately NOT surfaced as a failure so a transient
        network blip never cries wolf about a still-valid cookie.
        """
        with self._lock:
            entry = self._cred_health.get(name)
        return dict(entry) if entry is not None else None

    def _cred_probe_spec(self, name: str) -> dict | None:
        """Resolve + validate the ``health_probe`` spec for ``name``.

        The standard credential-health contract (see lib/mcp/health_probe.py)
        can be declared TWO ways, checked in priority order:

          1. The server's LIVE config (``handle.config`` / ``self._configs``) —
             so a user's custom ``mcp_servers.json`` entry can opt in with a
             ``"health_probe": {...}`` key, no code change needed.
          2. The curated catalog entry (late import to avoid a registry↔client
             cycle) — how in-tree servers like Overleaf ship a default probe.

        Returns a NORMALIZED spec (``{tool, args, fail_patterns}`` with the
        default auth patterns merged in) or ``None`` when no valid probe is
        declared. A malformed probe logs a warning via ``validate_health_probe``
        and resolves to ``None`` (never crashes the sweep).
        """
        from lib.mcp.health_probe import validate_health_probe

        # 1. Live config (custom servers + any config override).
        with self._lock:
            handle = self._servers.get(name)
            cfg = dict(handle.config) if handle is not None else self._configs.get(name)
        raw = cfg.get('health_probe') if isinstance(cfg, dict) else None

        # 2. Catalog default.
        if raw is None:
            try:
                from lib.mcp.registry import get_catalog_entry
                entry = get_catalog_entry(name)
                raw = entry.get('health_probe') if entry else None
            except Exception as e:
                logger.debug('[MCP] cred-probe catalog lookup for %s failed: %s',
                             name, e)
                raw = None

        return validate_health_probe(raw, server=name)

    def _run_cred_probe(self, name: str) -> dict | None:
        """Run the credential health probe for ``name`` and record the result.

        SYNC — safe to call from a worker thread (it drives ``call_tool``,
        which owns its own event-loop indirection). Best-effort: any failure
        classifies as ``'unknown'`` and never raises into the caller (the
        keepalive sweep must not die on a probe error).

        Returns the recorded health dict, or None when there is no probe to run
        (no spec / server not connected).
        """
        spec = self._cred_probe_spec(name)
        if spec is None:
            return None
        with self._lock:
            connected = name in self._servers
        if not connected:
            return None

        from lib.mcp.health_probe import classify_probe_result

        tool = spec['tool']
        args = spec.get('args', {}) or {}
        ns_name = make_namespaced_name(name, tool)

        status = 'unknown'
        detail = ''
        try:
            result = self.call_tool(ns_name, dict(args))
            # PURE classifier owns the ok/expired verdict; a RAISED call (below)
            # is the only path to 'unknown' — a transport blip must never be
            # mislabelled as an expired credential.
            status, detail = classify_probe_result(result, spec)
            if status == 'expired':
                logger.warning('[MCP] Credential probe for %s → EXPIRED (%s)',
                               name, detail)
            else:
                logger.debug('[MCP] Credential probe for %s → ok', name)
        except Exception as e:
            status = 'unknown'
            detail = str(e)[:200]
            logger.debug('[MCP] Credential probe for %s raised (%s) — '
                         'reported as unknown', name, _unwrap_exception_group(e))

        now = time.time()
        record = {'status': status, 'checked_at': now, 'detail': detail}
        with self._lock:
            self._cred_health[name] = record
            self._cred_probe_ts[name] = now
        if status == 'expired':
            audit_log('mcp_credential_expired', server=name, tool=tool)
        return record

    def _cred_probe_due(self, name: str) -> bool:
        """True if ``name`` has a health_probe and its re-probe interval elapsed.

        Gates the keepalive sweep so a credential is re-checked at most once per
        MCP_CRED_PROBE_INTERVAL. Returns False when the periodic probe is
        disabled (interval <= 0) or the server declares no probe.
        """
        if MCP_CRED_PROBE_INTERVAL <= 0:
            return False
        if self._cred_probe_spec(name) is None:
            return False
        with self._lock:
            last = self._cred_probe_ts.get(name, 0.0)
        return (time.time() - last) >= MCP_CRED_PROBE_INTERVAL

    def _probe_cred_health_async(self, name: str) -> None:
        """Fire a one-shot credential probe on a daemon thread (non-blocking).

        Used at connect time so a fresh connection surfaces an expired cookie
        promptly WITHOUT blocking the connect/handshake path. No-op for servers
        with no declared ``health_probe``.
        """
        if MCP_CRED_PROBE_INTERVAL < 0:
            return
        if self._cred_probe_spec(name) is None:
            return

        def _worker():
            try:
                self._run_cred_probe(name)
            except Exception as e:  # defence in depth — _run_cred_probe swallows
                logger.debug('[MCP] async cred probe for %s failed: %s', name, e)

        threading.Thread(target=_worker, name=f'mcp-credprobe-{name}',
                         daemon=True).start()

    def _breaker_record_failure(self, name: str) -> float:
        """Record a failed reconnect and return the backoff delay applied.

        Bumps the consecutive-failure count and schedules the next allowed
        retry at ``min(BASE * 2**(failures-1), MAX)`` seconds from now.
        """
        with self._lock:
            failures = self._breaker.get(name, (0, 0.0))[0] + 1
            delay = min(
                MCP_BREAKER_BASE_BACKOFF * (2 ** (failures - 1)),
                MCP_BREAKER_MAX_BACKOFF,
            )
            self._breaker[name] = (failures, time.time() + delay)
        return delay

    def _reconnect_server(self, name: str) -> _MCPServerHandle:
        """Tear down and re-establish a single server using its stored config.

        Used by both recovery paths (reactive call_tool retry + proactive
        keepalive). Serialized per-server via ``_reconnect_locks`` so two
        callers racing on the same dropped server don't spawn duplicate
        subprocesses. Returns the fresh handle.

        Raises if the config is unknown or the reconnect itself fails — the
        caller decides how to surface that.
        """
        with self._lock:
            old = self._servers.get(name)
            # Prefer the live handle's config; fall back to the last-known-good
            # config so a server torn down by a previous FAILED reconnect can
            # still be retried.
            srv_cfg = dict(old.config) if old is not None else self._configs.get(name)
            srv_cfg = dict(srv_cfg) if srv_cfg is not None else None
            rlock = self._reconnect_locks.setdefault(name, threading.Lock())
        if srv_cfg is None:
            raise ValueError(f'cannot reconnect unknown MCP server: {name}')

        with rlock:
            # Re-check under the per-server lock: a racing caller may have
            # already reconnected while we waited. If the live handle has a
            # session and differs from the one we saw, reuse it.
            with self._lock:
                cur = self._servers.get(name)
            if cur is not None and cur is not old and cur.session is not None:
                logger.info('[MCP] %s already reconnected by another caller', name)
                return cur
            audit_log('mcp_reconnect', server=name)
            logger.info('[MCP] Reconnecting server %s', name)
            try:
                self.connect_server(name, srv_cfg)
            except Exception:
                # Record the failure so the circuit breaker backs off the
                # next keepalive sweep (connect_server clears the breaker on
                # success, so this only sticks for genuinely failing servers).
                delay = self._breaker_record_failure(name)
                with self._lock:
                    failures = self._breaker.get(name, (0, 0.0))[0]
                logger.warning(
                    '[MCP] Reconnect of %s failed (consecutive=%d) — '
                    'backing off %.0fs before next attempt',
                    name, failures, delay,
                )
                raise
            with self._lock:
                return self._servers[name]

    async def _async_start_owner(self, name: str, srv_cfg: dict):
        """Async: spawn the owner task for a server and await readiness.

        The owner task holds the ``AsyncExitStack`` open for the lifetime
        of the server (see ``_server_owner``). We return only after the
        session is initialized and the tool list has been fetched.
        """
        loop = asyncio.get_running_loop()
        handle = _MCPServerHandle(name, srv_cfg)
        handle._shutdown_event = asyncio.Event()
        handle._ready_future = loop.create_future()
        handle._closed_future = loop.create_future()

        handle._owner_task = loop.create_task(
            self._server_owner(handle),
            name=f'mcp-owner:{name}',
        )

        # Wait for the owner to finish connect+list_tools (or fail).
        # ``asyncio.shield`` prevents our wait_for timeout from cancelling
        # the owner task itself — if readiness hangs, we still want the
        # owner to complete its own cleanup cycle.
        try:
            tools = await asyncio.wait_for(
                asyncio.shield(handle._ready_future),
                # Generous readiness ceiling: connect handshake + list_tools
                # each have their own MCP_CONNECT_TIMEOUT inside the owner.
                timeout=MCP_CONNECT_TIMEOUT * 2 + 5,
            )
        except asyncio.TimeoutError:
            # Readiness stalled — tell the owner to shut down and re-raise
            # as a MCPConnectError so the route can surface a user-facing
            # message including any captured stderr.
            handle._shutdown_event.set()
            stderr_tail = _read_stderr_tail(handle._stderr_file)
            raise MCPConnectError(
                name,
                TimeoutError(
                    f'connection handshake did not complete within '
                    f'{MCP_CONNECT_TIMEOUT * 2 + 5}s'
                ),
                stderr_tail,
            )
        return handle, tools

    async def _server_owner(self, handle: _MCPServerHandle) -> None:
        """Long-lived owner task: opens the context stack, serves the
        session until shutdown is signaled, then closes the stack from
        within the same task.

        Invariant (the whole point of this refactor): ``aclose()`` on the
        ``AsyncExitStack`` is ALWAYS awaited inside this coroutine, never
        from a different caller. That sidesteps the anyio cancel-scope /
        task-mismatch error that previously caused ``aclose()`` to hang
        for the full ``MCP_CALL_TIMEOUT + 10`` budget (~130s).
        """
        from contextlib import AsyncExitStack

        name = handle.name
        srv_cfg = handle.config

        try:
            async with AsyncExitStack() as stack:
                transport = srv_cfg.get('transport', 'stdio')

                if transport == 'sse':
                    url = srv_cfg.get('url', '')
                    if not url:
                        raise ValueError(
                            f'MCP server {name}: SSE transport requires "url"'
                        )
                    from mcp.client.sse import sse_client
                    read, write = await stack.enter_async_context(sse_client(url))
                else:
                    # stdio transport (default)
                    command = srv_cfg.get('command', '')
                    args = srv_cfg.get('args', [])
                    if not command:
                        raise ValueError(
                            f'MCP server {name}: stdio transport requires "command"'
                        )

                    # Pre-flight: verify the launcher is resolvable. Without
                    # this we get a cryptic FileNotFoundError deep inside
                    # mcp.client.stdio. If it's not on PATH, try to self-heal
                    # by resolving a pip-installed console script that lives
                    # next to the running interpreter (the common "installed
                    # but not on the subprocess PATH" case) before giving up.
                    import shutil as _shutil
                    if not _shutil.which(command):
                        resolved = _resolve_launcher(command)
                        if not resolved:
                            # Last resort: if this is a vendored internal
                            # server, pip-install it into Tofu's own env now
                            # so onboarding is zero-touch (no PATH edits, no
                            # manual install). One attempt per process.
                            #
                            # CRITICAL: ``_try_autoinstall_launcher`` runs a
                            # BLOCKING ``subprocess.run`` (pip, up to 300s). We
                            # are on the shared MCP event loop here, so calling
                            # it inline would freeze EVERY other server's
                            # keepalive / tool-calls / connects for the whole
                            # install. Offload to a worker thread (mirrors
                            # ``_reconnect_server``'s ``run_in_executor``) so a
                            # cold install never stalls the loop. Normally this
                            # path isn't even reached — the install ROUTE
                            # pre-warms the launcher in a Flask worker thread
                            # first (see ``prewarm_vendored_launcher``).
                            owner_loop = asyncio.get_running_loop()
                            resolved = await owner_loop.run_in_executor(
                                None, _try_autoinstall_launcher, command)
                        if resolved:
                            command = resolved
                        else:
                            hint = _launcher_install_hint(command)
                            raise FileNotFoundError(
                                f'MCP server {name!r}: launcher {command!r} is not on PATH. '
                                f'{hint}'
                            )

                    from mcp import StdioServerParameters
                    from mcp.client.stdio import stdio_client

                    # Merge env: os.environ + custom env vars
                    env = dict(os.environ)
                    # Node.js does NOT read HTTP_PROXY / HTTPS_PROXY by default.
                    # Two mechanisms ensure proxy support for child processes:
                    #   1. NODE_USE_ENV_PROXY=1 — env-var flag (Node ≥ v22.21)
                    #   2. NODE_OPTIONS=--use-env-proxy — CLI flag via NODE_OPTIONS,
                    #      which propagates to ALL child node processes (including
                    #      those spawned by npx, which may not inherit env-var flags).
                    env.setdefault('NODE_USE_ENV_PROXY', '1')
                    existing_opts = env.get('NODE_OPTIONS', '')
                    if '--use-env-proxy' not in existing_opts:
                        env['NODE_OPTIONS'] = (
                            f'{existing_opts} --use-env-proxy'.strip()
                        )
                    # Redirect launcher caches (uv/uvx, npm/npx, pip) to a
                    # writable project-local dir when $HOME/.cache is read-only
                    # — otherwise uvx dies before the MCP handshake.
                    _ensure_writable_caches(env)
                    # Make the running interpreter's bin/ dir visible to the
                    # child. This is what lets a pip-installed MCP server find
                    # its sibling console tools (e.g. hope-mcp shelling out to
                    # `hope`) AND mirrors the _resolve_launcher fallback so the
                    # subprocess sees the same launcher we resolved. Prepended
                    # so the env Tofu runs in wins over a stale system copy.
                    _prepend_interpreter_bin_to_path(env)
                    extra_env = srv_cfg.get('env', {})
                    if extra_env:
                        env.update(extra_env)

                    params = StdioServerParameters(
                        command=command,
                        args=args,
                        env=env,
                    )

                    # Capture the child's stderr to a real on-disk tempfile so
                    # that if the launcher dies during the MCP handshake, we
                    # can surface the actual error (Python traceback, "module
                    # not found", auth failure, etc.) to the user instead of
                    # a useless "Connection closed". A real file (with
                    # ``fileno()``) is required: mcp.client.stdio passes
                    # ``errlog`` straight to ``anyio.open_process(stderr=...)``,
                    # which only accepts an fd-backed file or integer fd.
                    # The file is unlinked from disk on close (mode w+b,
                    # delete=True) so it never accumulates.
                    stderr_buf = tempfile.TemporaryFile(mode='w+b')
                    handle._stderr_file = stderr_buf
                    read, write = await stack.enter_async_context(
                        stdio_client(params, errlog=stderr_buf)
                    )

                # Create and initialize session
                from mcp import ClientSession

                session = await stack.enter_async_context(
                    ClientSession(read, write)
                )
                init_result = await asyncio.wait_for(
                    session.initialize(), timeout=MCP_CONNECT_TIMEOUT
                )
                # Harvest serverInfo.name / serverInfo.version so the UI
                # can surface the upstream MCP server's own version (not
                # Tofu's or the launcher's). This comes from the MCP
                # handshake — see mcp.types.Implementation.
                try:
                    srv_info = getattr(init_result, 'serverInfo', None)
                    if srv_info is not None:
                        handle.server_name = str(getattr(srv_info, 'name', '') or '')
                        handle.server_version = str(getattr(srv_info, 'version', '') or '')
                        if handle.server_version:
                            logger.info(
                                '[MCP] Server %s reports version %s (impl=%s)',
                                name, handle.server_version, handle.server_name or '?',
                            )
                except Exception as e:
                    logger.debug('[MCP] Could not parse serverInfo for %s: %s', name, e)

                # Discover tools
                response = await asyncio.wait_for(
                    session.list_tools(), timeout=MCP_CONNECT_TIMEOUT
                )

                handle.session = session
                handle.tools = response.tools

                # Signal readiness BEFORE blocking on the shutdown event.
                if not handle._ready_future.done():
                    handle._ready_future.set_result(response.tools)

                # Serve until shutdown is requested. call_tool runs on the
                # same event loop, just using handle.session directly — no
                # per-call coordination through this task is needed.
                try:
                    await handle._shutdown_event.wait()
                except asyncio.CancelledError:
                    # Someone cancelled the owner task directly (e.g. loop
                    # shutdown). Fall through to AsyncExitStack cleanup.
                    logger.debug('[MCP] Owner %s cancelled — proceeding to cleanup', name)
                # AsyncExitStack.__aexit__ fires here — same task that
                # opened the stack. No cancel-scope mismatch possible.
        except BaseException as e:
            # anyio / mcp wrap the real failure in nested ExceptionGroups —
            # unwrap before forwarding so callers see the actual cause
            # (e.g. ``McpError: Connection closed``, ``FileNotFoundError``)
            # instead of the unhelpful "unhandled errors in a TaskGroup".
            root = _unwrap_exception_group(e)
            stderr_tail = _read_stderr_tail(handle._stderr_file)
            if stderr_tail:
                logger.warning(
                    '[MCP] Server %s stderr tail (%d chars):\n%s',
                    name, len(stderr_tail), stderr_tail,
                )
            wrapped = MCPConnectError(name, root, stderr_tail)
            # Propagate failure to whoever is awaiting readiness.
            if handle._ready_future and not handle._ready_future.done():
                handle._ready_future.set_exception(wrapped)
            else:
                # Already ready — this was a runtime failure during the
                # shutdown-wait phase. Log with context so we can diagnose.
                logger.warning('[MCP] Owner %s exited with error: %s', name, wrapped)
            # Re-raise CancelledError so the event loop can finish its job.
            if isinstance(e, asyncio.CancelledError):
                raise
        finally:
            # Always resolve the closed_future so callers awaiting a
            # clean shutdown are unblocked.
            if handle._closed_future and not handle._closed_future.done():
                handle._closed_future.set_result(None)
            # Release the stderr capture buffer.
            if handle._stderr_file is not None:
                try:
                    handle._stderr_file.close()
                except OSError as e:
                    logger.debug('[MCP] stderr_file close failed: %s', e)
                handle._stderr_file = None

    def _disconnect_one(self, name: str, forget: bool = False) -> None:
        """Sync: request shutdown for a single server and wait (bounded).

        Safe to call from any thread. Runs entirely via ``_run_async``
        indirection so the event loop is touched from the loop thread only.

        Args:
            name: server id to disconnect.
            forget: when True (explicit user disconnect / removal), also drop
                the circuit-breaker state and stored config so the keepalive
                loop stops retrying it. The reconnect teardown path leaves
                this False so a transient reconnect doesn't erase recovery
                state.
        """
        with self._lock:
            handle = self._servers.pop(name, None)
            # Remove tool index entries eagerly — the server is gone as
            # far as callers are concerned, even if cleanup is still
            # draining.
            to_remove = [k for k, v in self._tool_index.items()
                         if v['server_name'] == name]
            for k in to_remove:
                del self._tool_index[k]
            if forget:
                self._breaker.pop(name, None)
                self._configs.pop(name, None)
            # Credential-health is transient live state — always drop it when
            # the handle goes away (a reconnect re-probes fresh on connect).
            self._cred_health.pop(name, None)
            self._cred_probe_ts.pop(name, None)
        if handle is None:
            return

        try:
            self._run_async_with_timeout(
                self._async_signal_shutdown(handle),
                timeout=self._DISCONNECT_TIMEOUT,
            )
        except (asyncio.TimeoutError, TimeoutError) as e:
            # Owner didn't exit in the budget. Force-cancel it on the loop
            # — AsyncExitStack.__aexit__ will still fire (handled via
            # CancelledError inside _server_owner) and clean up the
            # subprocess/pipes.
            logger.warning(
                '[MCP] Disconnect %s did not complete within %.1fs — '
                'force-cancelling owner task (%s)',
                name, self._DISCONNECT_TIMEOUT, e,
            )
            if handle._owner_task is not None and not handle._owner_task.done():
                loop = self._loop
                if loop is not None and loop.is_running():
                    loop.call_soon_threadsafe(handle._owner_task.cancel)

    async def _async_signal_shutdown(self, handle: _MCPServerHandle) -> None:
        """Async: set the shutdown event and await the owner task's exit."""
        if handle._shutdown_event is not None and not handle._shutdown_event.is_set():
            handle._shutdown_event.set()
        if handle._closed_future is not None:
            await handle._closed_future

    def _run_async_with_timeout(self, coro, timeout: float) -> Any:
        """Like ``_run_async`` but with a caller-supplied timeout.

        Use this for disconnect paths where we don't want to pay the
        default ``MCP_CALL_TIMEOUT + 10`` (~130s) — that budget is only
        appropriate for long-running tool calls.
        """
        loop = self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result(timeout=timeout)

    def disconnect_all(self) -> None:
        """Gracefully disconnect all MCP servers."""
        # Stop the keepalive loop first so it can't race with teardown by
        # reconnecting a server we're about to drop. Set the stop event AND
        # cancel the task: the task may be parked in ``wait_for(stop.wait())``
        # or mid-sweep, and we're about to stop the loop out from under it.
        loop = self._loop
        task = self._keepalive_task
        if loop is not None and loop.is_running():
            def _stop_keepalive():
                if self._keepalive_stop is not None:
                    self._keepalive_stop.set()
                if task is not None and not task.done():
                    task.cancel()
            loop.call_soon_threadsafe(_stop_keepalive)
        self._keepalive_task = None

        with self._lock:
            names = list(self._servers.keys())
        for name in names:
            try:
                self._disconnect_one(name)
                logger.info('[MCP] Disconnected server: %s', name)
            except Exception as e:
                logger.warning('[MCP] Error disconnecting %s: %s', name, e)
        with self._lock:
            # _disconnect_one already pops; this is belt-and-suspenders
            # in case a caller mutated _servers out from under us.
            self._servers.clear()
            self._tool_index.clear()
            self._breaker.clear()
            self._configs.clear()
            self._cred_health.clear()
            self._cred_probe_ts.clear()
            self._started = False

        # Shut down the event loop
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
            if self._loop_thread:
                self._loop_thread.join(timeout=5)
            self._loop = None
            self._loop_thread = None
        logger.info('[MCP] All servers disconnected')

    # ── Tool translation ──────────────────────────────────

    @staticmethod
    def _tool_to_openai(server_name: str, tool) -> dict[str, Any]:
        """Translate an MCP Tool to OpenAI function-calling format.

        MCP tool schema::

            Tool(name='search', description='...', inputSchema={...})

        OpenAI tool schema::

            {"type": "function", "function": {"name": "mcp__tavily__search",
             "description": "[MCP:tavily] ...", "parameters": {...}}}
        """
        ns_name = make_namespaced_name(server_name, tool.name)
        desc = tool.description or f'MCP tool: {tool.name}'
        # Prefix description with server name for disambiguation
        tagged_desc = f'[MCP:{server_name}] {desc}'
        # Clean up the input schema: ensure it has required fields
        schema = dict(tool.inputSchema) if tool.inputSchema else {'type': 'object', 'properties': {}}
        if 'type' not in schema:
            schema['type'] = 'object'

        return {
            'type': 'function',
            'function': {
                'name': ns_name,
                'description': tagged_desc,
                'parameters': schema,
            },
        }

    # ── Tool discovery (for LLM) ──────────────────────────

    def get_openai_tool_defs(self) -> list[dict[str, Any]]:
        """Get all MCP tools as OpenAI function-calling definitions.

        Returns:
            List of OpenAI tool dicts ready to append to the tool_list.

        Ordering is deterministic (sorted by namespaced name) rather than
        dict-insertion order.  Insertion order changes whenever a server
        reconnects or re-discovers its tools mid-conversation, which would
        reorder the tools array and break the prompt cache prefix even though
        the tool *content* is unchanged.  A stable sort keeps the bytes
        identical across rounds.
        """
        with self._lock:
            return [self._tool_index[ns]['openai_def']
                    for ns in sorted(self._tool_index)]

    def get_tool_safety(self) -> dict[str, bool]:
        """Map every discovered MCP tool's namespaced name → read-only flag.

        The flag is the tool's MCP ``annotations.readOnlyHint`` (default
        ``False`` when the server omits it). Consumers use this to partition
        MCP tools for concurrency / write-approval: a tool that is NOT
        read-only is treated as a write tool (serial + approval-eligible).
        """
        with self._lock:
            return {ns: bool(info.get('read_only_hint'))
                    for ns, info in self._tool_index.items()}

    def get_tool_info(self, namespaced_name: str) -> MCPToolInfo | None:
        """Look up tool info by namespaced name."""
        with self._lock:
            return self._tool_index.get(namespaced_name)

    def is_mcp_tool(self, fn_name: str) -> bool:
        """Check if a function name is a registered MCP tool."""
        with self._lock:
            return fn_name in self._tool_index

    @property
    def server_count(self) -> int:
        with self._lock:
            return len(self._servers)

    @property
    def tool_count(self) -> int:
        with self._lock:
            return len(self._tool_index)

    @property
    def connected(self) -> bool:
        return self._started and self.server_count > 0

    def list_servers(self) -> list[dict[str, Any]]:
        """List all connected servers with their tool counts.

        Returns:
            List of dicts with keys: name, tools_count, tool_names, description.
        """
        with self._lock:
            result = []
            for name, handle in self._servers.items():
                result.append({
                    'name': name,
                    'tools_count': len(handle.tools),
                    'tool_names': [t.name for t in handle.tools],
                    'description': handle.config.get('description', ''),
                    'transport': handle.config.get('transport', 'stdio'),
                    'server_version': handle.server_version,
                    'server_impl_name': handle.server_name,
                })
            return result

    # ── Tool execution ────────────────────────────────────

    def call_tool(self, namespaced_name: str, arguments: dict[str, Any]) -> str:
        """Execute an MCP tool call and return the result as a string.

        Args:
            namespaced_name: Full namespaced tool name (``mcp__{server}__{tool}``).
            arguments: Tool arguments dict.

        Returns:
            Tool result as a string (text content extracted from MCP response).

        Raises:
            ValueError: If the tool or server is not found.
            TimeoutError: If the call exceeds the timeout.
        """
        parsed = parse_namespaced_name(namespaced_name)
        if parsed is None:
            raise ValueError(f'Invalid MCP tool name: {namespaced_name}')
        server_name, tool_name = parsed

        with self._lock:
            handle = self._servers.get(server_name)
            if handle is None:
                raise ValueError(f'MCP server not connected: {server_name}')

        # Coerce LLM-provided strings to the schema's declared types.
        # LLMs that don't strictly honor the JSON schema (esp. weaker models)
        # frequently emit `"step_version": "1"` for an integer field, which
        # the MCP server's jsonschema validator then rejects with
        # `'1' is not of type 'integer'`. Best-effort coerce so the call
        # actually reaches the server.
        info = self._tool_index.get(namespaced_name)
        if info is not None:
            arguments = _coerce_args_to_schema(arguments, info['input_schema'])
            # Use the server's actual registered tool name. ``parse_namespaced_name``
            # only sees the post-dedupe view, but the MCP server itself was
            # registered with the original (possibly stuttering) name.
            tool_name = info['tool_name']

        timeout = handle.config.get('timeout', MCP_CALL_TIMEOUT)

        # ── Call-level health gate ──
        # If this server has already timed out MCP_DEGRADED_TIMEOUT_STREAK
        # times in a row, fast-fail with an actionable error instead of
        # blocking the model for another full ``timeout`` seconds (and then
        # likely auto-retrying). A single later success resets the streak.
        if MCP_DEGRADED_TIMEOUT_STREAK > 0:
            with self._lock:
                streak = self._timeout_streak.get(server_name, 0)
            if streak >= MCP_DEGRADED_TIMEOUT_STREAK:
                logger.warning(
                    '[MCP:Call] %s.%s SKIPPED — server degraded after %d '
                    'consecutive call timeouts (timeout=%ds). Not retried; '
                    'reconnect or raise the per-server timeout to recover.',
                    server_name, tool_name, streak, timeout,
                )
                audit_log('mcp_server_degraded', server=server_name,
                          tool=tool_name, consecutive_timeouts=streak,
                          timeout=timeout)
                return (
                    f'MCP Error: server {server_name!r} is degraded — '
                    f'{streak} consecutive call timeouts (each waited '
                    f'{timeout}s). The call was not attempted. This usually '
                    f'means the tool runs longer than the server\'s per-call '
                    f'timeout; do not retry blindly.'
                )

        logger.info('[MCP:Call] %s.%s(args=%s) timeout=%ds',
                    server_name, tool_name, str(arguments)[:200], timeout)

        t0 = time.time()
        try:
            result = self._run_async(
                self._async_call_tool(handle, tool_name, arguments, timeout),
                timeout=timeout + 10,
            )
        except Exception as e:
            elapsed = time.time() - t0
            # A call timeout (transport read-timeout OR the outer thread-future
            # budget) bumps the per-server streak and leaves an audit trail so
            # the mismatch between a long-poll tool and the transport cap is
            # greppable. NOT treated as a transport-dead error (reconnecting
            # wouldn't help a tool that simply runs longer than its budget).
            if _is_call_timeout_error(e):
                with self._lock:
                    new_streak = self._timeout_streak.get(server_name, 0) + 1
                    self._timeout_streak[server_name] = new_streak
                logger.warning(
                    '[MCP:Call] %s.%s TIMED OUT after %.1fs (budget=%ds, '
                    'consecutive=%d). If this tool legitimately runs longer '
                    'than %ds, raise its per-server "timeout" in '
                    'mcp_servers.json.',
                    server_name, tool_name, elapsed, timeout, new_streak, timeout,
                )
                audit_log('mcp_call_timeout', server=server_name,
                          tool=tool_name, timeout=timeout,
                          elapsed=round(elapsed, 1), consecutive=new_streak)
                raise
            # If the call failed because the transport is dead (subprocess
            # crashed / idle-dropped), transparently reconnect once and
            # retry — the user should never have to manually reconnect.
            if _is_transport_dead_error(e):
                logger.warning(
                    '[MCP:Call] %s.%s hit dead transport after %.1fs (%s) — '
                    'reconnecting and retrying once',
                    server_name, tool_name, elapsed, e,
                )
                try:
                    new_handle = self._reconnect_server(server_name)
                except Exception as re:
                    logger.error('[MCP:Call] reconnect of %s failed: %s',
                                 server_name, re, exc_info=True)
                    raise e from re
                try:
                    result = self._run_async(
                        self._async_call_tool(new_handle, tool_name, arguments, timeout),
                        timeout=timeout + 10,
                    )
                    elapsed = time.time() - t0
                    self._reset_timeout_streak(server_name)
                    logger.info(
                        '[MCP:Call] %s.%s succeeded after reconnect+retry '
                        '(%d chars in %.1fs)',
                        server_name, tool_name, len(result), elapsed,
                    )
                    return result
                except Exception as e2:
                    logger.error('[MCP:Call] %s.%s still failing after reconnect: %s',
                                 server_name, tool_name, e2, exc_info=True)
                    raise
            logger.error('[MCP:Call] %s.%s failed after %.1fs: %s',
                         server_name, tool_name, elapsed, e, exc_info=True)
            raise

        elapsed = time.time() - t0
        # Any successful call clears the degraded streak.
        self._reset_timeout_streak(server_name)
        logger.info('[MCP:Call] %s.%s returned %d chars in %.1fs',
                    server_name, tool_name, len(result), elapsed)
        return result

    def _reset_timeout_streak(self, server_name: str) -> None:
        """Clear a server's consecutive call-timeout streak (called on any
        successful call). Logs once when recovering from a degraded state."""
        with self._lock:
            prev = self._timeout_streak.pop(server_name, 0)
        if prev >= MCP_DEGRADED_TIMEOUT_STREAK > 0:
            logger.info('[MCP:Call] %s recovered from degraded state '
                        '(streak was %d)', server_name, prev)

    async def _async_call_tool(
        self,
        handle: _MCPServerHandle,
        tool_name: str,
        arguments: dict[str, Any],
        timeout: int,
    ) -> str:
        """Async: call a tool on an MCP server and extract text result."""
        from datetime import timedelta

        result = await handle.session.call_tool(
            tool_name,
            arguments=arguments,
            read_timeout_seconds=timedelta(seconds=timeout),
        )

        # Extract text from the MCP CallToolResult
        if result.isError:
            # MCP reports an error from the tool
            error_text = self._extract_text(result)
            return f'MCP Error: {error_text}'

        text = self._extract_text(result)

        # Truncate if too large
        if len(text) > MCP_MAX_RESULT_CHARS:
            text = text[:MCP_MAX_RESULT_CHARS] + f'\n\n[Truncated: {len(text):,} chars total, showing first {MCP_MAX_RESULT_CHARS:,}]'

        return text

    # ── Keepalive: proactive health-check + auto-reconnect ──

    def _start_keepalive(self) -> None:
        """Launch the background keepalive loop on the MCP event loop.

        Idempotent and a no-op when disabled
        (``TOFU_MCP_KEEPALIVE_INTERVAL=0``). Called after the first
        successful connect so idle deployments don't spin a loop for
        nothing.
        """
        if MCP_KEEPALIVE_INTERVAL <= 0:
            return
        loop = self._loop
        if loop is None or not loop.is_running():
            return
        if self._keepalive_task is not None and not self._keepalive_task.done():
            return

        def _spawn():
            self._keepalive_stop = asyncio.Event()
            self._keepalive_task = loop.create_task(
                self._keepalive_loop(), name='mcp-keepalive')

        loop.call_soon_threadsafe(_spawn)
        logger.info('[MCP] Keepalive loop armed (interval=%ds, ping_timeout=%ds)',
                    MCP_KEEPALIVE_INTERVAL, MCP_PING_TIMEOUT)

    async def _keepalive_loop(self) -> None:
        """Ping every connected server periodically; reconnect dead ones.

        Runs on the MCP event-loop thread. Pings are protocol-level
        (``ClientSession.send_ping``); a ping that errors or times out means
        the transport is gone, so we trigger a reconnect. The reconnect runs
        in a worker thread (``run_in_executor``) because ``_reconnect_server``
        is a sync method that re-enters this very loop via
        ``run_coroutine_threadsafe`` — calling it inline would deadlock.

        A per-server circuit breaker (``_breaker``) gates reconnects: after a
        reconnect FAILS, that server is skipped until its exponentially
        growing backoff elapses, so a permanently-broken server isn't
        respawned every sweep. Servers whose handle was torn down by a failed
        reconnect are still revisited via the breaker keys + stored config, so
        they self-heal if the server eventually recovers.
        """
        stop = self._keepalive_stop
        while stop is not None and not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=MCP_KEEPALIVE_INTERVAL)
                break  # stop was set → exit loop
            except asyncio.TimeoutError:
                pass  # normal: interval elapsed, run a health sweep

            # Candidate set = live servers ∪ servers in backoff (the latter
            # may have no live handle after a failed reconnect).
            with self._lock:
                live = dict(self._servers)
                candidates = set(live) | set(self._breaker)
            loop = asyncio.get_running_loop()

            for name in candidates:
                # Skip servers whose backoff window hasn't elapsed.
                if self._breaker_blocks(name):
                    continue

                handle = live.get(name)
                session = handle.session if handle is not None else None

                if session is not None:
                    # Live server: health-check it. A healthy ping is the
                    # common case → continue without touching the breaker.
                    try:
                        await asyncio.wait_for(session.send_ping(),
                                               timeout=MCP_PING_TIMEOUT)
                        # Transport is alive — but the stored CREDENTIALS may
                        # have expired underneath it. Re-probe at most once per
                        # MCP_CRED_PROBE_INTERVAL (offloaded to a worker thread:
                        # _run_cred_probe drives call_tool, which re-enters this
                        # very loop and would deadlock if run inline).
                        if self._cred_probe_due(name):
                            loop.run_in_executor(None, self._run_cred_probe, name)
                        continue
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        logger.warning(
                            '[MCP] Keepalive ping to %s failed (%s) — reconnecting',
                            name, _unwrap_exception_group(e),
                        )
                else:
                    # No live session but the breaker is tracking it (failed
                    # reconnect previously, backoff now elapsed) → retry.
                    logger.info('[MCP] Keepalive retrying backed-off server %s', name)

                try:
                    await loop.run_in_executor(None, self._reconnect_server, name)
                    logger.info('[MCP] Keepalive reconnected %s', name)
                except asyncio.CancelledError:
                    raise
                except Exception as re:
                    # _reconnect_server already recorded the breaker failure +
                    # logged the backoff; keep this terse to avoid double noise.
                    logger.debug('[MCP] Keepalive reconnect of %s still failing: %s',
                                 name, _unwrap_exception_group(re))
        logger.debug('[MCP] Keepalive loop exited')

    @staticmethod
    def _extract_text(result) -> str:
        """Extract text content from a CallToolResult.

        MCP results contain a list of content blocks (TextContent,
        ImageContent, etc.).  We extract all text blocks and join them.
        """
        parts = []
        for block in result.content:
            if hasattr(block, 'text'):
                parts.append(block.text)
            elif hasattr(block, 'data'):
                # ImageContent / AudioContent — describe but don't dump binary
                block_type = getattr(block, 'type', 'unknown')
                parts.append(f'[{block_type} content: {len(block.data)} bytes]')
            elif hasattr(block, 'uri'):
                # ResourceLink
                parts.append(f'[Resource: {block.uri}]')
            else:
                parts.append(str(block))
        return '\n'.join(parts)


# ══════════════════════════════════════════════════════════
#  Module-level singleton
# ══════════════════════════════════════════════════════════

_bridge: MCPBridge | None = None
_bridge_lock = threading.Lock()


def get_bridge() -> MCPBridge:
    """Get the global MCPBridge singleton (lazy-initialized)."""
    global _bridge
    if _bridge is not None:
        return _bridge
    with _bridge_lock:
        if _bridge is None:
            _bridge = MCPBridge()
    return _bridge
