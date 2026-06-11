"""Access-matrix cell-probe engine — server-owned background reachability test.

Moved out of ``routes/config.py`` (2026-06). Sends a 1-token completion to
every (key × concrete-model-id) cell of a provider to learn which pairs the
gateway actually routes. Because each alias on a gateway can route to a
genuinely DIFFERENT upstream model, every alias is probed independently.

The probe is a long-running fan-out: it runs in a background thread and its
progress is persisted to disk (``data/config/probe_cache/``) as a secret-free
snapshot, so closing Settings or restarting the server doesn't lose progress.

``routes/config.py`` re-exports every public name here under its legacy
private alias (``_probe_one_cell``, ``_probe_cell_multi``,
``_run_cell_probe_task``, ``_probe_cache_path``, ``_probe_cell_key``,
``_persist_probe_task``, ``_public_probe_snapshot``, ``CELL_PROBE_TASKS``,
``CELL_PROBE_LOCK``, ``_time``) so existing call sites and tests keep working.

NOTE for tests: ``probe_cell_multi`` / ``run_cell_probe_task`` call
``probe_one_cell`` THROUGH THIS MODULE'S global, so
``mock.patch.object(lib.provider_probe, 'probe_one_cell', ...)`` (or patching
the re-exported name) takes effect. Same for ``probe_cache_path``.
"""

import hashlib
import threading
import time as _time
from concurrent.futures import ThreadPoolExecutor, as_completed

from lib.config_dir import config_path as _config_path
from lib.json_store import read_json, write_json_atomic  # noqa: F401  (read_json re-used by routes)
from lib.log import get_logger

logger = get_logger(__name__)


def probe_one_cell(base_url, api_key, model_id, extra_headers, timeout,
                   protocol='openai'):
    """Send a minimal completion to test one (key, model) pair.

    Returns one of: 'ok', 'rate_limited', 'unauthorized', 'not_found',
    'unavailable', 'error' plus a short human-readable detail string.

    A 200 OR an HTTP 400 both count as ``ok`` — a 400 means the gateway
    accepted the (key, model) routing and only rejected the (deliberately
    tiny) request shape, which still proves the pair is reachable.

    ``protocol='anthropic'`` probes the Anthropic Messages API
    (``POST /v1/messages`` with ``x-api-key`` + ``anthropic-version``)
    instead of OpenAI Chat Completions. The status→verdict table is
    identical for both protocols.
    """
    from lib.http_client import http_post

    # ``max_tokens: 1`` is the floor — the probe only needs to learn whether
    # the gateway accepts the (key, model) routing, never the completion
    # itself, so output cost is held to a single token per attempt.
    if protocol == 'anthropic':
        from lib.llm.anthropic_outbound import (
            anthropic_headers, anthropic_messages_url,
        )
        url = anthropic_messages_url(base_url)
        headers = anthropic_headers(api_key, extra_headers)
        payload = {
            'model': model_id,
            'messages': [{'role': 'user', 'content': '.'}],
            'max_tokens': 1,
        }
    else:
        url = base_url.rstrip('/') + '/chat/completions'
        headers = {'Authorization': 'Bearer %s' % api_key} if api_key else {}
        if extra_headers:
            headers.update(extra_headers)
        payload = {
            'model': model_id,
            'messages': [{'role': 'user', 'content': 'hi'}],
            'max_tokens': 1,
            'stream': False,
        }
    try:
        resp = http_post(url, json=payload, headers=headers, timeout=timeout)
    except Exception as e:
        logger.warning('[CellProbe] %s @ %s network error: %s', model_id, base_url, e)
        return 'unavailable', 'network: %s' % str(e)[:120]

    code = resp.status_code
    try:
        body = resp.text[:400]
    except (UnicodeDecodeError, ValueError, OSError) as e:
        logger.debug('[CellProbe] %s @ %s: could not read response body: %s',
                     model_id, base_url, e)
        body = ''
    lower = body.lower()

    if code == 200 or code == 400:
        return 'ok', 'HTTP %d' % code
    if code == 429 or code == 402:
        return 'rate_limited', 'HTTP %d %.120s' % (code, body)
    if code in (401, 403):
        return 'unauthorized', 'HTTP %d %.120s' % (code, body)
    if code == 404 or 'model_not_found' in lower or 'does not exist' in lower or 'no such model' in lower:
        return 'not_found', 'HTTP %d %.120s' % (code, body)
    if code in (500, 502, 503, 504, 529):
        return 'unavailable', 'HTTP %d %.120s' % (code, body)
    return 'error', 'HTTP %d %.120s' % (code, body)


# Verdicts that warrant a retry (could be a transient blip), versus ones
# that are definitive on the first attempt (no point re-asking).
_PROBE_TRANSIENT = {'rate_limited', 'unavailable', 'error'}
_PROBE_DEFINITIVE = {'unauthorized', 'not_found'}


def probe_cell_multi(base_url, api_key, model_id, extra_headers, timeout,
                     attempts=3, retry_delay=0.8, protocol='openai'):
    """Probe a cell up to ``attempts`` times to filter out FALSE 429s.

    Rationale: gateways routinely return a transient 429 / 5xx even for a
    (key, model) pair the key is fully entitled to. Flagging it after one
    shot would wrongly recommend disabling a working model. So:

      * A single ``ok`` on ANY attempt wins immediately — the earlier
        rate-limit was transient.
      * ``unauthorized`` / ``not_found`` are definitive → return at once.
      * Transient failures are retried after ``retry_delay`` seconds; if
        every attempt fails we return the LAST transient verdict with an
        ``(N/N attempts)`` note so the UI can show it was persistent.

    Returns ``(status, detail)`` like :func:`probe_one_cell`.
    """
    attempts = max(1, int(attempts))
    last_status, last_detail = 'error', ''
    for i in range(attempts):
        # Call through the module global so tests can patch probe_one_cell.
        status, detail = probe_one_cell(base_url, api_key, model_id, extra_headers,
                                        timeout, protocol)
        if status == 'ok':
            note = '' if i == 0 else ' (ok on attempt %d/%d)' % (i + 1, attempts)
            return 'ok', detail + note
        if status in _PROBE_DEFINITIVE:
            return status, detail
        last_status, last_detail = status, detail
        if i < attempts - 1:
            _time.sleep(retry_delay)
    suffix = ' (%d/%d attempts failed)' % (attempts, attempts) if attempts > 1 else ''
    return last_status, '%.120s%s' % (last_detail, suffix)


# ══════════════════════════════════════════════════════
#  Background task state
# ══════════════════════════════════════════════════════
CELL_PROBE_TASKS: dict = {}
CELL_PROBE_LOCK = threading.Lock()
_PROBE_DISABLE_STATUSES = {'rate_limited', 'unauthorized', 'not_found', 'unavailable'}


def probe_cache_path(provider_id: str) -> str:
    """Disk path for a provider's persisted probe snapshot."""
    safe = hashlib.sha1((provider_id or '').encode('utf-8')).hexdigest()[:16]
    return _config_path('probe_cache', '%s.json' % safe)


def probe_cell_key(key_idx, model_id) -> str:
    return '%s::%s' % (key_idx, model_id)


def persist_probe_task(task: dict):
    """Atomically write a public (key-free) snapshot of the task to disk."""
    try:
        write_json_atomic(probe_cache_path(task['provider_id']),
                          public_probe_snapshot(task), fsync=False)
    except Exception as e:
        logger.warning('[CellProbe] persist failed for %s: %s',
                       task.get('provider_id'), e)


def public_probe_snapshot(task: dict) -> dict:
    """The serialisable, secret-free view of a probe task (for poll + disk)."""
    return {
        'provider_id': task['provider_id'],
        'status': task['status'],
        'started_at': task['started_at'],
        'finished_at': task['finished_at'],
        'total': task['total'],
        'done_count': task['done_count'],
        'attempts': task.get('attempts', 1),
        'cells': task['cells'],
        'summary': task['summary'],
        'error': task['error'],
    }


def run_cell_probe_task(task: dict, work: list, timeout: int):
    """Background worker: fan out cell probes, updating + persisting progress."""
    provider_id = task['provider_id']
    base_url = task['_base_url']
    extra_headers = task['_extra_headers']
    protocol = task.get('_protocol', 'openai')
    attempts = task.get('attempts', 3)
    logger.info('[CellProbe] Started background probe for %s — %d cell(s), '
                'up to %d attempt(s) each (protocol=%s)', provider_id, len(work),
                attempts, protocol)

    def _run(item):
        key_idx, api_key, root, mid = item
        # Multi-attempt so a FALSE 429 / transient 5xx doesn't wrongly flag a
        # reachable cell. A single ok on any attempt wins.
        status, detail = probe_cell_multi(base_url, api_key, mid, extra_headers,
                                          timeout, attempts=attempts,
                                          protocol=protocol)
        return {
            'key_idx': key_idx,
            'model_id': mid,
            'root_model_id': root,
            'status': status,
            'detail': detail,
            'recommend_disable': status in _PROBE_DISABLE_STATUSES,
        }

    last_persist = 0.0
    try:
        workers = min(8, len(work))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='cell-probe') as pool:
            futures = [pool.submit(_run, it) for it in work]
            for fut in as_completed(futures):
                if task.get('_abort'):
                    logger.info('[CellProbe] %s aborted', provider_id)
                    break
                try:
                    cell = fut.result()
                except Exception as e:
                    logger.error('[CellProbe] cell task raised: %s', e, exc_info=True)
                    continue
                with CELL_PROBE_LOCK:
                    task['cells'][probe_cell_key(cell['key_idx'], cell['model_id'])] = cell
                    task['done_count'] = len(task['cells'])
                    n_disable = sum(1 for c in task['cells'].values() if c['recommend_disable'])
                    task['summary'] = {'ok': task['done_count'] - n_disable, 'disable': n_disable}
                # Throttle disk writes: at most every ~1.5s during the run.
                now = _time.monotonic()
                if now - last_persist > 1.5:
                    last_persist = now
                    persist_probe_task(task)
        with CELL_PROBE_LOCK:
            task['status'] = 'done'
            task['finished_at'] = _time.time()
        persist_probe_task(task)
        logger.info('[CellProbe] %s done: %d cells, %d flagged',
                    provider_id, task['done_count'], task['summary']['disable'])
    except Exception as e:
        logger.error('[CellProbe] background worker crashed for %s: %s',
                     provider_id, e, exc_info=True)
        with CELL_PROBE_LOCK:
            task['status'] = 'error'
            task['error'] = str(e)[:300]
            task['finished_at'] = _time.time()
        persist_probe_task(task)


__all__ = [
    'probe_one_cell', 'probe_cell_multi', 'run_cell_probe_task',
    'probe_cache_path', 'probe_cell_key', 'persist_probe_task',
    'public_probe_snapshot', 'CELL_PROBE_TASKS', 'CELL_PROBE_LOCK',
]
