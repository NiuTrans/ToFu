"""Durable swarm transcript access and empty-output-directory maintenance.

The per-agent ``<base>/<task_id>/<agent_id>.log`` files OUTLIVE the in-memory
``MasterOrchestrator`` session — they are the durable fallback that lets
``await_agents`` / ``get_agent_result`` return a completed sub-agent's full
output even after the session is gone (TTL eviction, recycle, restart).

Also owns output-dir resolution (``_resolve_output_dir`` / ``_swarm_base_dir``)
since that's rooted in the same on-disk layout. Depends only on ``_config`` for
the ``SWARM_OUTPUT_DIR`` override and launch-probed session capacity.
"""

from __future__ import annotations

import errno
import os
import threading

from lib.log import get_logger
from lib.swarm.integration._config import MAX_SESSIONS, SWARM_OUTPUT_DIR

logger = get_logger(__name__)

# Empty task directories carry no transcript or recovery value. Bound startup
# maintenance by the launch-probed session profile and a hard entry ceiling;
# the scan is streaming, so its memory does not grow with directory history.
_EMPTY_DIR_SWEEP_HARD_ENTRY_LIMIT = 16_384
_EMPTY_DIR_SWEEP_ENTRY_LIMIT = max(
    512,
    min(_EMPTY_DIR_SWEEP_HARD_ENTRY_LIMIT, MAX_SESSIONS * 1_024),
)
_output_cleanup_lock = threading.Lock()
_output_cleanup_thread: threading.Thread | None = None
_output_cleanup_cancel: threading.Event | None = None


# ── Output dir resolution ────────────────────────────────

def _resolve_output_dir(task_id: str) -> str:
    """Return absolute path to ``<base>/<task_id>/`` for sub-agent log streams."""
    return os.path.join(_swarm_base_dir(), task_id)


def _swarm_base_dir() -> str:
    """Root dir holding all ``<task_id>/`` sub-agent log folders.

    Honours the ``TOFU_SWARM_OUTPUT_DIR`` override, else ``<data_root>/swarm``.
    Uses ``lib.runtime_paths.data_root()`` — the single source of truth — so
    these sub-agent logs co-locate with the DB under the resolved writable root,
    not the code tree (which a fresh source checkout may place on a different
    mount).
    """
    if SWARM_OUTPUT_DIR:
        return SWARM_OUTPUT_DIR
    try:
        from lib.runtime_paths import data_root
        return os.path.join(data_root(), 'swarm')
    except Exception as e:  # pragma: no cover — defensive
        logger.warning('[swarm] runtime_paths.data_root() unavailable, '
                       'falling back to in-tree data/swarm: %s', e)
        return os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.dirname(os.path.abspath(__file__))))),
            'data', 'swarm',
        )


def _prune_empty_output_dirs(
    base_dir: str | None = None,
    *,
    entry_limit: int | None = None,
    cancel_event: threading.Event | None = None,
) -> dict[str, int | bool]:
    """Remove only empty immediate child directories under the swarm root.

    ``os.rmdir`` is the authority check: a concurrent log creation makes it
    fail with ``ENOTEMPTY`` and the directory is preserved. Files, nested
    content, and symlinks are never removed. Work is bounded by directory
    entries inspected, not merely successful removals.
    """
    root = base_dir or _swarm_base_dir()
    try:
        requested_limit = int(
            _EMPTY_DIR_SWEEP_ENTRY_LIMIT
            if entry_limit is None else entry_limit)
    except (TypeError, ValueError, OverflowError):
        requested_limit = _EMPTY_DIR_SWEEP_ENTRY_LIMIT
    limit = max(1, min(_EMPTY_DIR_SWEEP_HARD_ENTRY_LIMIT, requested_limit))
    scanned = 0
    removed = 0
    errors = 0
    cancelled = False
    capped = False
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                if cancel_event is not None and cancel_event.is_set():
                    cancelled = True
                    break
                if scanned >= limit:
                    capped = True
                    break
                scanned += 1
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    os.rmdir(entry.path)
                    removed += 1
                except OSError as exc:
                    if exc.errno not in (
                        errno.ENOENT,
                        errno.ENOTEMPTY,
                        errno.EEXIST,
                    ):
                        errors += 1
    except FileNotFoundError:
        pass
    except OSError:
        errors += 1
    return {
        'scanned': scanned,
        'removed': removed,
        'errors': errors,
        'cancelled': cancelled,
        'capped': capped,
        'entryLimit': limit,
    }


def start_swarm_output_cleanup(base_dir: str | None = None) -> bool:
    """Start at most one bounded daemon sweep; return whether one launched."""
    global _output_cleanup_cancel, _output_cleanup_thread
    with _output_cleanup_lock:
        current = _output_cleanup_thread
        if current is not None and current.is_alive():
            return False
        cancel = threading.Event()
        root = base_dir or _swarm_base_dir()

        def _run() -> None:
            global _output_cleanup_cancel, _output_cleanup_thread
            try:
                stats = _prune_empty_output_dirs(
                    root, cancel_event=cancel)
                log = logger.warning if stats['errors'] else logger.info
                if stats['removed'] or stats['errors']:
                    log(
                        '[Swarm] Empty output-dir cleanup scanned=%d '
                        'removed=%d errors=%d capped=%s cancelled=%s',
                        stats['scanned'], stats['removed'], stats['errors'],
                        stats['capped'], stats['cancelled'])
            finally:
                with _output_cleanup_lock:
                    if _output_cleanup_thread is threading.current_thread():
                        _output_cleanup_thread = None
                        _output_cleanup_cancel = None

        worker = threading.Thread(
            target=_run,
            name='swarm-output-cleanup',
            daemon=True,
        )
        _output_cleanup_cancel = cancel
        _output_cleanup_thread = worker
        try:
            worker.start()
        except Exception:
            _output_cleanup_cancel = None
            _output_cleanup_thread = None
            raise
        return True


def stop_swarm_output_cleanup(timeout: float = 2.0) -> bool:
    """Cancel and bounded-join startup output maintenance."""
    with _output_cleanup_lock:
        worker = _output_cleanup_thread
        cancel = _output_cleanup_cancel
        if cancel is not None:
            cancel.set()
    if worker is None:
        return True
    try:
        wait_seconds = max(0.0, float(timeout))
    except (TypeError, ValueError, OverflowError):
        wait_seconds = 2.0
    if worker is not threading.current_thread():
        worker.join(timeout=wait_seconds)
    return not worker.is_alive()


def swarm_output_cleanup_snapshot() -> dict[str, int | bool]:
    """Return bounded process-local cleanup diagnostics."""
    with _output_cleanup_lock:
        worker = _output_cleanup_thread
        return {
            'running': bool(worker is not None and worker.is_alive()),
            'entryLimit': _EMPTY_DIR_SWEEP_ENTRY_LIMIT,
            'hardEntryLimit': _EMPTY_DIR_SWEEP_HARD_ENTRY_LIMIT,
        }


def _read_log_file(path: str, task_id: str) -> str | None:
    try:
        with open(path, encoding='utf-8') as fp:
            return fp.read()
    except FileNotFoundError:
        logger.debug('[Swarm:%s] agent log not found: %s', task_id, path)
        return None
    except OSError as e:
        logger.debug('[Swarm:%s] could not read agent log %s: %s',
                     task_id, path, e)
        return None


def _read_agent_log(task_id: str, agent_id: str) -> tuple[str, str] | None:
    """Read a finished sub-agent's full streamed transcript from disk.

    Each sub-agent streams its raw output (thinking + content) to
    ``<base>/<task_id>/<agent_id>.log`` (see ``lib/swarm/agent.py``). That
    file OUTLIVES the in-memory session — it is never deleted on session
    teardown / TTL eviction / recycling. It is the durable fallback for
    ``get_agent_result`` when the live ``MasterOrchestrator`` is gone.

    Lookup is two-stage because the agent's log lives under the task_id of
    the turn that SPAWNED it, while ``get_agent_result`` is frequently
    called from a LATER turn in the same conversation (each user message
    gets a fresh task_id). So:

      1. Fast path — try ``<base>/<task_id>/<agent_id>.log``.
      2. Cross-task path — glob ``<base>/*/<agent_id>.log`` (agent ids are
         globally near-unique 8-char tokens). On multiple hits, pick the
         most recently modified.

    Returns ``(text, source_path)`` or None if not found anywhere.
    """
    fast = os.path.join(_resolve_output_dir(task_id), f'{agent_id}.log')
    text = _read_log_file(fast, task_id)
    if text is not None:
        return text, fast

    import glob
    base = _swarm_base_dir()
    try:
        matches = glob.glob(os.path.join(base, '*', f'{agent_id}.log'))
    except OSError as e:
        logger.debug('[Swarm:%s] cross-task glob failed for %s: %s',
                     task_id, agent_id, e)
        return None
    matches = [m for m in matches if m != fast]
    if not matches:
        return None
    if len(matches) > 1:
        try:
            matches.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        except OSError as e:
            logger.debug('[Swarm:%s] mtime sort failed: %s', task_id, e)
        logger.info('[Swarm:%s] agent %s log found in %d dirs — using newest %s',
                    task_id, agent_id, len(matches), matches[0])
    text = _read_log_file(matches[0], task_id)
    if text is None:
        return None
    return text, matches[0]
