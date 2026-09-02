"""Sidecar-authoritative durable task-event storage.

Responsibilities
----------------
* project event payloads into their durable form;
* append every cursor exactly once through the bounded Sidecar batcher;
* acknowledge persistence before a frame becomes client-visible;
* provide bounded cold replay and liveness reads;
* run tiered event, sync-log, conversation-trash, tool-result-artifact, and
  page maintenance.

There is deliberately no in-process SQL fallback. The production lifecycle
starts one storage Sidecar before request serving; losing that authority is a
durability failure, not a reason to activate a second database implementation.
"""

from __future__ import annotations

import atexit
import os
import threading
import time

from lib.conversation_sync.generated_contract import (
    STREAM_POLICY as CONVERSATION_SYNC_POLICY,
)
from lib.log import get_logger
from lib.storage_projection import (
    project_event_usage_for_storage,
    project_usage_container_for_storage,
    sanitize_usage_for_persist,
)
from lib.task_event_contract import (
    STRUCTURAL_EVENT_TYPES,
    TASK_EVENT_STREAMING_RETENTION_MS,
    TASK_EVENT_STRUCTURAL_RETENTION_MS,
    TERMINAL_EVENT_TYPES,
)
from runtime_guards import deployment_resource_default


logger = get_logger(__name__)

# Streaming frames remain available for reconnects; structural request
# snapshots remain available for the Request Inspector.
EVENT_TTL_MS = TASK_EVENT_STREAMING_RETENTION_MS
STRUCTURAL_TTL_MS = TASK_EVENT_STRUCTURAL_RETENTION_MS


def _env_seconds(name: str, default: float, minimum: float) -> float:
    raw = os.environ.get(name, default)
    try:
        return max(minimum, float(raw))
    except (TypeError, ValueError):
        logger.warning('[EventLog] invalid %s=%r; using %ss', name, raw, default)
        return float(default)


def _env_days_ms(name: str, default: float, minimum: float) -> int:
    raw = os.environ.get(name, default)
    try:
        days = max(minimum, float(raw))
    except (TypeError, ValueError):
        logger.warning(
            '[EventLog] invalid %s=%r; using %s days', name, raw, default)
        days = float(default)
    return int(days * 24 * 3600 * 1000)


def _env_int(
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = os.environ.get(name, default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning('[EventLog] invalid %s=%r; using %s', name, raw, default)
        value = int(default)
    return max(minimum, min(maximum, value))


_PRUNE_BATCH_ROWS = _env_int(
    'TOFU_EVENT_PRUNE_BATCH_ROWS', 25, 10, 10_000)
_PRUNE_MAX_BATCHES = _env_int(
    'TOFU_EVENT_PRUNE_BATCHES', 16, 1, 64)
_TASK_EVENT_PRUNE_INTERVAL_S = _env_seconds(
    'TOFU_TASK_EVENT_PRUNE_INTERVAL', 300, 30)
_MAINTENANCE_INTERVAL_S = _env_seconds(
    'TOFU_EVENT_MAINTENANCE_INTERVAL', 15, 5)
_BACKLOG_MAINTENANCE_INTERVAL_S = _env_seconds(
    'TOFU_STORAGE_BACKLOG_MAINTENANCE_INTERVAL', 30, 5)

_ATTEMPT_EVENT_TTL_MS = _env_days_ms(
    'TOFU_ATTEMPT_EVENT_TTL_DAYS',
    deployment_resource_default(
        'TOFU_ATTEMPT_EVENT_TTL_DAYS', os.environ),
    1)
_ATTEMPT_EVENT_PRUNE_INTERVAL_S = _env_seconds(
    'TOFU_ATTEMPT_EVENT_PRUNE_INTERVAL', 300, 30)
_ATTEMPT_EVENT_PRUNE_MAX_ROWS = _env_int(
    'TOFU_ATTEMPT_EVENT_PRUNE_MAX_ROWS', 256, 16, 200_000)
_ATTEMPT_EVENT_PRUNE_MAX_ATTEMPTS = _env_int(
    'TOFU_ATTEMPT_EVENT_PRUNE_MAX_ATTEMPTS', 16, 1, 256)

_CONVERSATION_SYNC_REPLAY_TTL_MS = _env_days_ms(
    'TOFU_CONVERSATION_SYNC_REPLAY_TTL_DAYS',
    float(CONVERSATION_SYNC_POLICY.get('replayRetentionMs', 604_800_000))
    / (24 * 3600 * 1000),
    1,
)
_CONVERSATION_SYNC_PRUNE_INTERVAL_S = _env_seconds(
    'TOFU_CONVERSATION_SYNC_PRUNE_INTERVAL', 300, 30)
_CONVERSATION_SYNC_PRUNE_MAX_ROWS = _env_int(
    'TOFU_CONVERSATION_SYNC_PRUNE_MAX_ROWS', 512, 16, 20_000)

# A delete remains recoverable well beyond the six-second UI undo window.
# Retention is code-owned so deployments cannot accidentally turn an ordinary
# delete into an immediate irreversible purge.
_CONVERSATION_TRASH_TTL_MS = 30 * 24 * 3600 * 1000
_CONVERSATION_TRASH_PRUNE_INTERVAL_S = 3600.0
_CONVERSATION_TRASH_PRUNE_MAX_CONVERSATIONS = 2

_TOOL_RESULT_ARTIFACT_PRUNE_INTERVAL_S = _env_seconds(
    'TOFU_TOOL_RESULT_ARTIFACT_PRUNE_INTERVAL', 300, 30)
_TOOL_RESULT_ARTIFACT_PRUNE_BATCH_ROWS = _env_int(
    'TOFU_TOOL_RESULT_ARTIFACT_PRUNE_BATCH_ROWS', 512, 16, 5_000)
_TOOL_RESULT_ARTIFACT_PRUNE_MAX_BATCHES = _env_int(
    'TOFU_TOOL_RESULT_ARTIFACT_PRUNE_BATCHES', 8, 1, 32)

_RECLAIM_INTERVAL_S = _env_seconds(
    'TOFU_STORAGE_RECLAIM_INTERVAL', 300, 30)
_RECLAIM_PAGES = _env_int(
    'TOFU_STORAGE_RECLAIM_PAGES', 8192, 0, 1_048_576)
_RECLAIM_MIN_FREE_PAGES = _env_int(
    'TOFU_STORAGE_RECLAIM_MIN_FREE_PAGES', 1024, 0, 100_000_000)
_RECLAIM_BUDGET_MS = _env_int(
    'TOFU_STORAGE_RECLAIM_BUDGET_MS', 250, 10, 60_000)

_PERSIST_TIMEOUT_S = 5.0
_SIDECAR_BATCHER_LOCK = threading.Lock()
_SIDECAR_BATCHER = None
_MAINTENANCE_LOCK = threading.Lock()
_SIDECAR_MAINTENANCE_STOP = threading.Event()
_SIDECAR_MAINTENANCE_THREAD = None


class EventDurabilityError(RuntimeError):
    """An event could not cross the required durability boundary."""


def _invalidate_event_read_caches(task_ids) -> None:
    """Evict task projections at enqueue and at the batch commit boundary."""
    if isinstance(task_ids, str):
        task_ids = (task_ids,)
    for task_id in task_ids or ():
        if not task_id:
            continue
        try:
            from lib.tasks_pkg.request_inspector import invalidate_task_cache
            invalidate_task_cache(task_id)
        except Exception as exc:
            logger.debug(
                '[EventLog] inspector cache invalidation skipped: %s', exc)
        try:
            from lib.tasks_pkg.turn_trace import invalidate_trace_cache
            invalidate_trace_cache(task_id)
        except Exception as exc:
            logger.debug('[EventLog] trace cache invalidation skipped: %s', exc)


def _ensure_sidecar_batcher():
    """Lazily construct the one process-wide Sidecar event batcher."""
    global _SIDECAR_BATCHER
    batcher = _SIDECAR_BATCHER
    if batcher is not None:
        return batcher
    with _SIDECAR_BATCHER_LOCK:
        if _SIDECAR_BATCHER is None:
            from lib.storage import StorageEventBatcher
            _SIDECAR_BATCHER = StorageEventBatcher(
                on_commit=_invalidate_event_read_caches)
        return _SIDECAR_BATCHER


def stop_sidecar_batcher(timeout: float = 3.0) -> bool:
    """Drain and close the process-wide event batcher."""
    global _SIDECAR_BATCHER
    batcher = _SIDECAR_BATCHER
    if batcher is None:
        return True
    try:
        wait_s = max(0.1, float(timeout))
    except (TypeError, ValueError):
        wait_s = 3.0
    try:
        stopped = batcher.close(timeout=wait_s)
    except Exception as exc:
        logger.warning('[EventLog] Sidecar batcher close failed: %s', exc)
        return False
    if stopped:
        with _SIDECAR_BATCHER_LOCK:
            if _SIDECAR_BATCHER is batcher:
                _SIDECAR_BATCHER = None
    return bool(stopped)


def _usage_without_wire_diagnostics(usage):
    return sanitize_usage_for_persist(usage)


def _project_usage_container_for_storage(container):
    return project_usage_container_for_storage(container)


def _project_usage_diagnostics_for_storage(event):
    """Remove transient wire diagnostics from every persisted usage shape."""
    return project_event_usage_for_storage(event)


def append_persistent_event(task_id, event_id, event):
    """Persist one event and wait for its Sidecar transaction to commit.

    Batching amortizes concurrent streams, while the per-call acknowledgement
    preserves the actual durable-before-visible contract. There is no
    fire-and-forget failure state and no second sequence authority.
    """
    if not task_id:
        raise EventDurabilityError('task_id is required for durable events')
    if event_id is None:
        raise EventDurabilityError(
            'event_id is required; TaskRuntime owns sequence allocation')
    if not isinstance(event, dict):
        raise EventDurabilityError('durable task events must be objects')

    projected = _project_usage_diagnostics_for_storage(event)
    if projected.get('type') == 'messages_snapshot':
        try:
            from lib.tasks_pkg.snapshot_delta import get_projector
            projected = get_projector().project(task_id, projected)
        except Exception as exc:
            logger.warning(
                '[EventLog] snapshot projection failed task=%s: %s; '
                'persisting the full snapshot', task_id[:8], exc)

    _invalidate_event_read_caches(task_id)
    try:
        return _ensure_sidecar_batcher().append(
            task_id=str(task_id),
            sequence=int(event_id),
            event=projected,
            wait=True,
            timeout=_PERSIST_TIMEOUT_S,
        )
    except Exception:
        # The batch command may fail for a connection-specific reason. One
        # independent semantic command is safe because the natural event key
        # deduplicates an ambiguous earlier commit.
        try:
            from lib.storage import get_storage_client
            result = get_storage_client(write=True).command(
                'event.append', {
                    'task_id': str(task_id),
                    'sequence': int(event_id),
                    'event': projected,
                }, None, priority='event', deadline=_PERSIST_TIMEOUT_S)
            _invalidate_event_read_caches(task_id)
            return result
        except Exception as retry_exc:
            raise EventDurabilityError(
                f'event {event_id} for task {str(task_id)[:8]} was not durable'
            ) from retry_exc


def flush_pending(task_id=None) -> bool:
    """Wait until every event accepted before this call is durable."""
    batcher = _SIDECAR_BATCHER
    if batcher is None:
        return True
    try:
        return batcher.flush(timeout=_PERSIST_TIMEOUT_S)
    except Exception as exc:
        raise EventDurabilityError(
            f'event flush failed for task {str(task_id or "")[:8]}') from exc


def read_events(task_id, since_event_id=None, limit=10_000):
    """Read a bounded, ordered cold-replay page from the Sidecar authority."""
    if not task_id:
        return []
    try:
        requested = max(1, min(int(limit), 100_000))
    except (TypeError, ValueError):
        requested = 10_000
    after = int(since_event_id) if since_event_id is not None else -1
    out = []
    try:
        from lib.storage import get_storage_client
        client = get_storage_client()
        while len(out) < requested:
            page_limit = min(1000, requested - len(out))
            rows = client.query(
                'event.list', {
                    'task_id': str(task_id),
                    'after_sequence': after,
                    'limit': page_limit,
                }) or []
            if not rows:
                break
            out.extend({
                'event_id': int(row.get('sequence', 0)),
                'payload': row.get('event') or {},
            } for row in rows)
            after = int(rows[-1].get('sequence', after))
            if len(rows) < page_limit:
                break
        return out
    except Exception as exc:
        logger.warning(
            '[EventLog] cold replay failed task=%s: %s', str(task_id)[:8], exc)
        return []


def has_terminal_event(task_id) -> bool:
    """Return whether the latest persisted event is terminal."""
    if not task_id:
        return False
    try:
        from lib.storage import get_storage_client
        row = get_storage_client().query(
            'event.latest', {'task_id': str(task_id)})
        return bool(
            row and (row.get('event') or {}).get('type') in TERMINAL_EVENT_TYPES)
    except Exception as exc:
        logger.debug(
            '[EventLog] terminal probe failed task=%s: %s', str(task_id)[:8], exc)
        return False


def _backlog_cadence(normal_interval_s, has_backlog):
    normal = float(normal_interval_s)
    return min(normal, _BACKLOG_MAINTENANCE_INTERVAL_S) if has_backlog else normal


def _prune_sidecar_event_backlog(client, cutoff_ms, *, retention_class):
    """Drain one retention tier in separately committed, writer-fair pages."""
    deleted_total = 0
    batches = 0
    remaining = False
    for _ in range(_PRUNE_MAX_BATCHES):
        if _SIDECAR_MAINTENANCE_STOP.is_set():
            break
        response = client.command(
            'event.prune', {
                'created_before_ms': int(cutoff_ms),
                'limit': _PRUNE_BATCH_ROWS,
                'retention_class': retention_class,
            }, None, priority='maintenance', deadline=30)
        response = response or {}
        if response.get('deferred'):
            return {
                'deleted': deleted_total,
                'batches': batches + 1,
                'remaining': False,
                'deferred': True,
                'reason': str(response.get('reason') or 'unavailable'),
                'required_index': str(response.get('required_index') or ''),
            }
        deleted = int(response.get('deleted') or 0)
        deleted_total += deleted
        batches += 1
        remaining = (
            bool(response.get('has_more'))
            if 'has_more' in response
            else deleted >= _PRUNE_BATCH_ROWS
        )
        if not remaining:
            break
    return {'deleted': deleted_total, 'batches': batches,
            'remaining': remaining}


def _prune_tool_result_artifact_backlog(client, now_ms):
    """Delete expired reconstructible tool overflow in bounded commits."""
    deleted_total = 0
    batches = 0
    remaining = False
    for _ in range(_TOOL_RESULT_ARTIFACT_PRUNE_MAX_BATCHES):
        if _SIDECAR_MAINTENANCE_STOP.is_set():
            break
        result = client.maintenance(
            'tool_result_artifact.prune', {
                'now_ms': int(now_ms),
                'limit': _TOOL_RESULT_ARTIFACT_PRUNE_BATCH_ROWS,
            }, deadline=30) or {}
        deleted = int(result.get('deleted') or 0)
        deleted_total += deleted
        batches += 1
        remaining = bool(result.get('hasMore'))
        if not remaining:
            break
    return {'deleted': deleted_total, 'batches': batches,
            'remaining': remaining}


def _maintenance_timeout_opens_circuit(operation, exc):
    """Classify a writer timeout that must stop optional online maintenance."""
    if getattr(exc, 'code', '') != 'database_timeout':
        return False
    logger.warning(
        '[EventLog] online storage maintenance disabled until restart: '
        '%s exceeded the shared writer budget (%s)', operation, exc)
    return True


def _sidecar_maintenance_loop() -> None:
    last_task_event_prune = 0.0
    last_attempt_prune = 0.0
    last_sync_prune = 0.0
    last_trash_prune = 0.0
    last_tool_artifact_prune = 0.0
    last_reclaim = 0.0
    attempt_backlog = True
    sync_backlog = True
    trash_backlog = True
    tool_artifact_backlog = True
    reclaim_backlog = True
    reclaim_offline_required = False
    task_event_retention_enabled = True
    task_event_backlog = True
    maintenance_circuit_open = False

    while not _SIDECAR_MAINTENANCE_STOP.wait(_MAINTENANCE_INTERVAL_S):
        # Retention and reclamation are reconstructible housekeeping. Once a
        # bounded unit times out on the single SQLite writer, more online
        # probes can only extend the interactive outage. Keep the owner thread
        # alive (so start_storage_maintenance remains idempotent), but defer all
        # optional writes until a process restart supplies one fresh probe.
        if maintenance_circuit_open:
            continue
        now_ms = int(time.time() * 1000)
        now_mono = time.monotonic()
        task_event_interval = _backlog_cadence(
            _TASK_EVENT_PRUNE_INTERVAL_S, task_event_backlog)
        if (task_event_retention_enabled
                and now_mono - last_task_event_prune >= task_event_interval):
            last_task_event_prune = now_mono
            try:
                from lib.storage import get_storage_client
                client = get_storage_client(write=True)
                stream = _prune_sidecar_event_backlog(
                    client, now_ms - EVENT_TTL_MS,
                    retention_class='streaming')
                structural = (
                    {'deleted': 0, 'remaining': False}
                    if (stream.get('deferred') or stream.get('remaining'))
                    else _prune_sidecar_event_backlog(
                        client, now_ms - STRUCTURAL_TTL_MS,
                        retention_class='structural')
                )
                deferred = stream if stream.get('deferred') else structural
                if deferred.get('deferred'):
                    task_event_retention_enabled = False
                    logger.warning(
                        '[EventLog] task-event retention disabled until restart: '
                        'reason=%s required_index=%s',
                        deferred.get('reason'), deferred.get('required_index'))
                    task_event_backlog = False
                else:
                    task_event_backlog = bool(
                        stream.get('remaining')
                        or structural.get('remaining'))
                reclaim_backlog = bool(
                    reclaim_backlog or stream['deleted'] or structural['deleted'])
            except Exception as exc:
                if _maintenance_timeout_opens_circuit(
                        'task-event retention', exc):
                    maintenance_circuit_open = True
                    continue
                task_event_backlog = True
                logger.warning('[EventLog] task-event retention failed: %s', exc)

        attempt_interval = _backlog_cadence(
            _ATTEMPT_EVENT_PRUNE_INTERVAL_S, attempt_backlog)
        if (now_mono - last_attempt_prune >= attempt_interval
                and _ATTEMPT_EVENT_TTL_MS > 0):
            last_attempt_prune = now_mono
            try:
                from lib.storage import get_storage_client
                result = get_storage_client(write=True).command(
                    'turn.events.prune', {
                        'settled_before_ms': now_ms - _ATTEMPT_EVENT_TTL_MS,
                        'max_attempts': _ATTEMPT_EVENT_PRUNE_MAX_ATTEMPTS,
                        'max_rows': _ATTEMPT_EVENT_PRUNE_MAX_ROWS,
                    }, None, priority='maintenance', deadline=60) or {}
                attempt_backlog = bool(result.get('remaining'))
                reclaim_backlog = bool(
                    reclaim_backlog or result.get('deleted_rows'))
            except Exception as exc:
                if _maintenance_timeout_opens_circuit(
                        'attempt-event retention', exc):
                    maintenance_circuit_open = True
                    continue
                attempt_backlog = True
                logger.warning('[EventLog] attempt-event retention failed: %s', exc)

        sync_interval = _backlog_cadence(
            _CONVERSATION_SYNC_PRUNE_INTERVAL_S, sync_backlog)
        if (now_mono - last_sync_prune >= sync_interval
                and _CONVERSATION_SYNC_REPLAY_TTL_MS > 0):
            last_sync_prune = now_mono
            try:
                from lib.storage import get_storage_client
                result = get_storage_client(write=True).command(
                    'turn.sync.prune', {
                        'created_before_ms': (
                            now_ms - _CONVERSATION_SYNC_REPLAY_TTL_MS),
                        'max_rows': _CONVERSATION_SYNC_PRUNE_MAX_ROWS,
                    }, None, priority='maintenance', deadline=60) or {}
                sync_backlog = bool(result.get('remaining'))
                reclaim_backlog = bool(
                    reclaim_backlog or result.get('deletedRows'))
            except Exception as exc:
                if _maintenance_timeout_opens_circuit(
                        'sync-log retention', exc):
                    maintenance_circuit_open = True
                    continue
                sync_backlog = True
                logger.warning('[EventLog] sync-log retention failed: %s', exc)

        trash_interval = _backlog_cadence(
            _CONVERSATION_TRASH_PRUNE_INTERVAL_S, trash_backlog)
        if now_mono - last_trash_prune >= trash_interval:
            last_trash_prune = now_mono
            try:
                from lib.storage import get_storage_client
                result = get_storage_client(write=True).command(
                    'conversation.trash.prune', {
                        'deleted_before_ms': (
                            now_ms - _CONVERSATION_TRASH_TTL_MS),
                        'max_conversations': (
                            _CONVERSATION_TRASH_PRUNE_MAX_CONVERSATIONS),
                    }, None, priority='maintenance', deadline=60) or {}
                trash_backlog = bool(result.get('remaining'))
                reclaim_backlog = bool(
                    reclaim_backlog or result.get('purgedConversations'))
            except Exception as exc:
                if _maintenance_timeout_opens_circuit(
                        'conversation-trash retention', exc):
                    maintenance_circuit_open = True
                    continue
                trash_backlog = True
                logger.warning(
                    '[EventLog] conversation-trash retention failed: %s', exc)

        tool_artifact_interval = _backlog_cadence(
            _TOOL_RESULT_ARTIFACT_PRUNE_INTERVAL_S, tool_artifact_backlog)
        if now_mono - last_tool_artifact_prune >= tool_artifact_interval:
            last_tool_artifact_prune = now_mono
            try:
                from lib.storage import get_storage_client
                result = _prune_tool_result_artifact_backlog(
                    get_storage_client(write=True), now_ms)
                tool_artifact_backlog = bool(result.get('remaining'))
                reclaim_backlog = bool(
                    reclaim_backlog or result.get('deleted'))
            except Exception as exc:
                if _maintenance_timeout_opens_circuit(
                        'tool-result-artifact retention', exc):
                    maintenance_circuit_open = True
                    continue
                tool_artifact_backlog = True
                logger.warning(
                    '[EventLog] tool-result-artifact retention failed: %s',
                    exc)

        reclaim_interval = _backlog_cadence(
            _RECLAIM_INTERVAL_S, reclaim_backlog)
        if (_RECLAIM_PAGES > 0
                and not reclaim_offline_required
                and now_mono - last_reclaim >= reclaim_interval):
            last_reclaim = now_mono
            try:
                from lib.storage import get_storage_client
                result = get_storage_client(write=True).command(
                    'system.reclaim', {
                        'max_pages': _RECLAIM_PAGES,
                        'min_free_pages': _RECLAIM_MIN_FREE_PAGES,
                        'budget_ms': _RECLAIM_BUDGET_MS,
                    }, None, priority='maintenance', deadline=90) or {}
                reclaimed = int(result.get('reclaimed') or 0)
                freelist = int(result.get('freelist') or 0)
                if result.get('offline_required'):
                    reclaim_offline_required = True
                    reclaim_backlog = False
                    if (result.get('reason_code')
                            == 'unsupported_storage_topology'):
                        logger.warning(
                            '[EventLog] online page reclaim disabled until '
                            'restart: authority storage is %s (%s); automatic '
                            'SQLite page relocation never enters the shared '
                            'writer on this topology. Use the documented '
                            'offline storage_deep_clean workflow.',
                            result.get('storage_class') or 'unknown',
                            result.get('filesystem_type') or 'unknown',
                        )
                    else:
                        logger.warning(
                            '[EventLog] online page reclaim disabled until '
                            'restart: the SQLite freelist is bulk-compaction '
                            'sized (%.1f GiB, %.1f%% of %.1f GiB); run the '
                            'documented offline storage_deep_clean workflow',
                            int(result.get('freelist_bytes') or 0) / 1024 ** 3,
                            float(result.get('freelist_ratio') or 0) * 100,
                            int(result.get('file_bytes') or 0) / 1024 ** 3,
                        )
                else:
                    reclaim_backlog = bool(
                        reclaimed and freelist >= _RECLAIM_MIN_FREE_PAGES)
            except Exception as exc:
                if _maintenance_timeout_opens_circuit(
                        'storage reclaim', exc):
                    maintenance_circuit_open = True
                    reclaim_backlog = False
                else:
                    reclaim_backlog = True
                    logger.warning('[EventLog] storage reclaim failed: %s', exc)


def start_storage_maintenance():
    """Start the idempotent Sidecar retention owner."""
    global _SIDECAR_MAINTENANCE_THREAD
    thread = _SIDECAR_MAINTENANCE_THREAD
    if thread is not None and thread.is_alive():
        return thread
    with _MAINTENANCE_LOCK:
        thread = _SIDECAR_MAINTENANCE_THREAD
        if thread is not None and thread.is_alive():
            return thread
        _SIDECAR_MAINTENANCE_STOP.clear()
        thread = threading.Thread(
            target=_sidecar_maintenance_loop,
            name='sidecar-event-maintenance', daemon=True)
        thread.start()
        _SIDECAR_MAINTENANCE_THREAD = thread
        return thread


def stop_storage_maintenance(timeout: float = 3.0) -> bool:
    """Stop the Sidecar retention owner with a bounded join."""
    global _SIDECAR_MAINTENANCE_THREAD
    _SIDECAR_MAINTENANCE_STOP.set()
    thread = _SIDECAR_MAINTENANCE_THREAD
    if thread is None:
        return True
    try:
        wait_s = max(0.0, float(timeout))
    except (TypeError, ValueError):
        wait_s = 3.0
    if thread is not threading.current_thread():
        thread.join(wait_s)
    if thread.is_alive():
        logger.warning(
            '[EventLog] maintenance thread did not stop within %.1fs', wait_s)
        return False
    with _MAINTENANCE_LOCK:
        if _SIDECAR_MAINTENANCE_THREAD is thread:
            _SIDECAR_MAINTENANCE_THREAD = None
    return True


def _shutdown_event_storage() -> None:
    try:
        stop_storage_maintenance(timeout=3.0)
        stop_sidecar_batcher(timeout=3.0)
    except Exception as exc:
        logger.debug('[EventLog] shutdown drain failed: %s', exc)


atexit.register(_shutdown_event_storage)


__all__ = [
    'EVENT_TTL_MS',
    'STRUCTURAL_EVENT_TYPES',
    'STRUCTURAL_TTL_MS',
    'EventDurabilityError',
    'append_persistent_event',
    'flush_pending',
    'has_terminal_event',
    'read_events',
    'start_storage_maintenance',
    'stop_sidecar_batcher',
    'stop_storage_maintenance',
]
