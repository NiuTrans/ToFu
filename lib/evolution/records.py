"""Owner-scoped record plumbing for the self-evolution module.

Responsibility: key discipline, size bounds, and acknowledged writes with a
bounded retry on transient storage failure, over the existing Sidecar
``record.*`` operations. Entry points: ``put_record`` / ``get_record`` /
``list_records`` / ``entity_key``. Dependencies: ``lib.identity`` and
``lib.storage`` (imported lazily); no domain policy lives here.
"""
from __future__ import annotations

from copy import deepcopy
import json
import re
import time
import uuid

from lib.identity import require_user_id
from lib.log import get_logger
from lib.storage.errors import StorageError

logger = get_logger(__name__)

NAMESPACE_GOALS = 'evolution.goals.v1'
NAMESPACE_PLANS = 'evolution.plans.v1'
NAMESPACE_EPISODES = 'evolution.episodes.v1'
NAMESPACE_COST_PRIORS = 'evolution.cost_priors.v1'
NAMESPACE_STRATEGIES = 'evolution.strategies.v1'
NAMESPACE_ANTI_PATTERNS = 'evolution.anti_patterns.v1'
NAMESPACE_MUTATIONS = 'evolution.mutations.v1'
NAMESPACE_CAPABILITY_GAPS = 'evolution.capability_gaps.v1'
NAMESPACE_HOLDOUT = 'evolution.holdout.v1'
NAMESPACE_REGRESSIONS = 'evolution.regressions.v1'
NAMESPACE_SKILLS = 'evolution.skills.v1'
NAMESPACE_HYPOTHESES = 'evolution.hypotheses.v1'
NAMESPACE_GATE_POLICY = 'evolution.gate_policy.v1'
NAMESPACE_DISTILLATIONS = 'evolution.distillations.v1'
NAMESPACE_POLICY = 'evolution.policy.v1'
NAMESPACE_RUNS = 'evolution.runs.v1'
NAMESPACE_LIBRARIES = 'evolution.libraries.v1'
NAMESPACE_OBSERVATIONS = 'evolution.observations.v1'

#: Episodes are small structured records; bulky evidence stays in the existing
#: owner-scoped artifact stores and is referenced, never copied.
MAX_RECORD_BYTES = 1024 * 1024
MAX_LIST_LIMIT = 200

#: Same posture as research run records: an idempotent replace-in-place write
#: that the storage layer itself marked retryable earns a bounded second
#: chance; durable refusals propagate.
_COMMIT_RETRY_DELAYS_S = (1.0, 2.0)

_ID_RE = re.compile(r'[A-Za-z0-9_.-]{1,160}')

# Injectable so tests never sleep.
_sleep = time.sleep


def _storage(*, write: bool = False):
    from lib.storage import get_storage_client
    return get_storage_client(write=write)


def entity_key(entity_id: str, user_id, *, kind: str) -> str:
    uid = require_user_id(user_id, context=f'evolution {kind} owner')
    if not isinstance(entity_id, str) or not _ID_RE.fullmatch(entity_id):
        raise ValueError(f'invalid evolution {kind} id')
    return f'{uid}:{entity_id}'


def put_record(namespace: str, key: str, value: dict, *, kind: str,
               expected_version: int | None = None) -> int:
    if len(json.dumps(value, ensure_ascii=False).encode('utf-8')) > MAX_RECORD_BYTES:
        raise ValueError(f'evolution {kind} exceeds {MAX_RECORD_BYTES} bytes')
    payload = {'namespace': namespace, 'key': key, 'value': value}
    if expected_version is not None:
        payload['expected_version'] = expected_version
    command_id = f'evolution:{kind}:{uuid.uuid4().hex}'
    for attempt in range(len(_COMMIT_RETRY_DELAYS_S) + 1):
        try:
            result = _storage(write=True).command('record.put', payload, command_id)
            if not isinstance(result, dict):
                raise StorageError('database_internal',
                                   f'evolution {kind} write was not acknowledged')
            return int(result['version'])
        except StorageError as exc:
            if not exc.retryable or attempt >= len(_COMMIT_RETRY_DELAYS_S):
                raise
            delay = max(_COMMIT_RETRY_DELAYS_S[attempt],
                        (exc.retry_after_ms or 0) / 1000.0)
            logger.warning('[Evolution:Records] transient %s write failure '
                           '(attempt %d/%d): %s — retrying in %.1fs',
                           kind, attempt + 1, len(_COMMIT_RETRY_DELAYS_S) + 1,
                           exc, delay)
            _sleep(delay)


# Reads are always replay-safe, so eligibility is classified by code, not by
# the server-supplied retryable flag: the authority marks ``deadline_elapsed``
# (``database_timeout``) non-retryable because that flag also gates command
# replay; a read replay never duplicates work.
_READ_RETRYABLE_CODES = {'database_timeout', 'database_busy', 'database_unavailable'}


def is_record_page_resource_refusal(error):
    """A bounded range can shrink under native resource pressure.

    The wire can erase the specific byte-limit reason. Existing transport
    backoff remains; callers may try fewer rows, never relax authority limits.
    """
    return (isinstance(error, StorageError) and error.code == 'database_unavailable'
            and (str(error) == 'database_resource_exhausted'
                 or 'versioned document page exceeds 8 MiB' in str(error)))


def _read_with_retry(operation: str, payload: dict, *, kind: str, shrinkable_page: bool = False):
    """Reads earn the same bounded second chance as writes.

    A transient authority stall (``deadline_elapsed`` on a cold or contended
    store) previously raised straight out of a single attempt, killing
    caller flows — evolution run advancement included — even though the
    write path already retried.
    """
    for attempt in range(len(_COMMIT_RETRY_DELAYS_S) + 1):
        try:
            return _storage().query(operation, payload)
        except StorageError as exc:
            if (exc.code not in _READ_RETRYABLE_CODES
                    or (shrinkable_page and operation == 'record.list'
                        and is_record_page_resource_refusal(exc))
                    or attempt >= len(_COMMIT_RETRY_DELAYS_S)):
                raise
            delay = max(_COMMIT_RETRY_DELAYS_S[attempt],
                        (exc.retry_after_ms or 0) / 1000.0)
            logger.warning('[Evolution:Records] transient %s read failure '
                           '(attempt %d/%d): %s — retrying in %.1fs',
                           kind, attempt + 1, len(_COMMIT_RETRY_DELAYS_S) + 1,
                           exc, delay)
            _sleep(delay)


def get_record(namespace: str, key: str) -> dict | None:
    row = _read_with_retry('record.get', {'namespace': namespace, 'key': key},
                           kind='record')
    if not isinstance(row, dict) or row.get('value') is None:
        return None
    return deepcopy(row['value'])


def get_versioned_record(namespace: str, key: str) -> tuple[dict | None, int]:
    row = _read_with_retry('record.get', {'namespace': namespace, 'key': key},
                           kind='record')
    if not isinstance(row, dict) or row.get('value') is None:
        return None, 0
    return deepcopy(row['value']), int(row['version'])


def list_records(namespace: str, user_id, *, kind: str, limit: int = 50) -> list[dict]:
    uid = require_user_id(user_id, context=f'evolution {kind} owner')
    bounded = max(1, min(int(limit or 50), MAX_LIST_LIMIT))
    rows = _read_with_retry('record.list', {
        'namespace': namespace, 'prefix': f'{uid}:', 'limit': bounded,
    }, kind=kind)
    if not isinstance(rows, list):
        return []
    return [deepcopy(row['value']) for row in rows
            if isinstance(row, dict) and isinstance(row.get('value'), dict)]


def list_record_page(namespace: str, user_id, *, kind: str, limit: int,
                     after_key: str = '', before_key: str = '', entity_prefix: str = '') -> list[dict]:
    """One owner-key range, including the caller's bounded lookahead row."""
    uid = require_user_id(user_id, context=f'evolution {kind} owner')
    if not isinstance(entity_prefix,str) or (entity_prefix and not _ID_RE.fullmatch(entity_prefix)):
        raise ValueError('invalid evolution entity prefix')
    prefix = f'{uid}:{entity_prefix}'
    if (type(limit) is not int or not 1 <= limit <= MAX_LIST_LIMIT
            or (after_key and before_key)
            or any(not isinstance(key, str) or len(key) > 512
                   or (key and not key.startswith(prefix)) for key in (after_key, before_key))):
        raise ValueError('invalid evolution record page')
    rows = _read_with_retry('record.list', {'namespace': namespace, 'prefix': prefix,
        'limit': limit, 'after_key': after_key, 'before_key': before_key}, kind=kind, shrinkable_page=True)
    if not isinstance(rows, list) or len(rows) > limit:
        raise StorageError('database_protocol_error', 'Invalid evolution record page response')
    previous = None
    for row in rows:
        key = row.get('key') if isinstance(row, dict) else None
        if (not isinstance(key, str) or not key.startswith(prefix)
                or not isinstance(row.get('value'), dict)
                or (after_key and key <= after_key) or (before_key and key >= before_key)
                or (previous is not None and (key >= previous if before_key else key <= previous))):
            raise StorageError('database_protocol_error', 'Invalid evolution record page boundary')
        previous = key
    return rows
