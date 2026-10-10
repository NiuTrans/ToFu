"""lib/translate_cache.py — On-disk cache for translation results.

Keyed on ``sha256(target | source | text)``.  Stored as small JSON files
under ``data/translate_cache/<aa>/<sha>.json`` (sharded by the first two
hex chars to keep any single dir small).

Hits skip both the MT-provider HTTP call AND the LLM dispatch in
``_translate_one_chunk()``. Opt out with ``TOFU_TRANSLATE_CACHE=0``.

Eviction is lazy and bounded:
  - Each entry has a TTL of ``TOFU_TRANSLATE_CACHE_TTL_DAYS`` (default 30).
  - On every ~1/256 lookup we sweep the shard the key falls in, removing
    entries past the TTL (cheap — typical shards have a few hundred files).
  - ``TOFU_TRANSLATE_CACHE_MAX_MIB`` is a launch-probed whole-cache ceiling.
    Its exact bytes are divided across all 256 hash shards; every write evicts
    oldest reconstructible entries from its shard until the hard share fits.
  - There is no global eviction loop and no in-memory path index.

The cache lives under ``data/translate_cache/`` which is already covered
by ``ALWAYS_EXCLUDE_DIRS`` in ``export.py`` (``data/`` is excluded), so
exported copies start with an empty cache.
"""

import hashlib
import json
import os
import random
import threading
import time

from lib.json_store import locked_path, write_json_atomic
from lib.log import get_logger
from runtime_guards import resolve_resource_budget

logger = get_logger(__name__)

from lib.runtime_paths import data_root
_CACHE_DIR = os.path.join(data_root(), 'translate_cache')

_ENABLED = os.environ.get('TOFU_TRANSLATE_CACHE', '1') != '0'
_TTL_SECONDS = int(os.environ.get('TOFU_TRANSLATE_CACHE_TTL_DAYS', '30')) * 86400
_SWEEP_PROBABILITY = 1.0 / 256  # one sweep per ~256 lookups, on the shard touched
_MIB = 1024 * 1024
_SHARD_COUNT = 256
_MAX_CACHE_BYTES = resolve_resource_budget(
    'TOFU_TRANSLATE_CACHE_MAX_MIB',
    os.environ,
    minimum=1,
    maximum=4096,
) * _MIB

_init_lock = threading.Lock()
_initialized = False


def _ensure_dir():
    global _initialized
    if _initialized:
        return
    with _init_lock:
        if _initialized:
            return
        try:
            os.makedirs(_CACHE_DIR, exist_ok=True)
            _initialized = True
        except Exception as e:
            logger.warning('[TranslateCache] Failed to create cache dir %s: %s',
                           _CACHE_DIR, e)


def _key(text: str, source: str, target: str) -> str:
    """Stable sha256 key for (target, source, text).

    Note: system prompts are NOT part of the key.  ``_build_translate_prompt``
    in ``routes/translate.py`` is a pure function of ``(target, source)``,
    so the key already captures it transitively.  If the prompt contents
    are changed, bump the version prefix below to invalidate old entries.
    """
    h = hashlib.sha256()
    h.update(b'v1\x00')
    h.update((target or '').encode('utf-8'))
    h.update(b'\x00')
    h.update((source or '').encode('utf-8'))
    h.update(b'\x00')
    h.update((text or '').encode('utf-8'))
    return h.hexdigest()


def _path_for(key: str) -> str:
    return os.path.join(_CACHE_DIR, key[:2], key + '.json')


def _shard_budget_bytes(shard_prefix: str) -> int:
    """Return this shard's exact share of the process-wide byte ceiling."""
    shard_index = int(shard_prefix, 16)
    base, remainder = divmod(max(1, int(_MAX_CACHE_BYTES)), _SHARD_COUNT)
    return base + (1 if shard_index < remainder else 0)


def _serialized_payload_size(payload: dict) -> int:
    """Mirror ``write_json_atomic(..., indent=None)`` without writing."""
    return len((json.dumps(payload, indent=None, ensure_ascii=False) + '\n')
               .encode('utf-8'))


def _enforce_shard_budget(shard: str, *, keep_path: str) -> int:
    """Evict oldest JSON entries until ``shard`` fits its hard byte share."""
    shard_prefix = os.path.basename(shard)
    budget = _shard_budget_bytes(shard_prefix)
    try:
        names = os.listdir(shard)
    except OSError as exc:
        logger.debug('[TranslateCache] budget list failed for %s: %s', shard, exc)
        return 0

    rows = []
    total = 0
    for name in names:
        if not name.endswith('.json'):
            continue
        path = os.path.join(shard, name)
        try:
            stat = os.stat(path)
        except OSError:
            continue
        total += int(stat.st_size)
        rows.append((float(stat.st_mtime), path, int(stat.st_size)))
    if total <= budget:
        return 0

    removed = 0
    # Keep the just-written result when older entries can satisfy the bound.
    # It is already known to fit by itself from the pre-write size check.
    rows.sort(key=lambda row: (row[1] == keep_path, row[0], row[1]))
    for _mtime, path, size in rows:
        if total <= budget:
            break
        try:
            os.remove(path)
        except OSError as exc:
            logger.debug('[TranslateCache] budget remove failed for %s: %s',
                         path, exc)
            continue
        total = max(0, total - size)
        removed += 1
    if removed:
        logger.debug(
            '[TranslateCache] shard=%s evicted=%d retained_bytes=%d budget=%d',
            shard_prefix,
            removed,
            total,
            budget,
        )
    return removed


def get(text: str, source: str, target: str):
    """Return cached translation dict ``{translated, model}`` or ``None``."""
    if not _ENABLED or not text:
        return None
    _ensure_dir()
    key = _key(text, source, target)
    path = _path_for(key)
    try:
        st = os.stat(path)
    except FileNotFoundError as _e_audit:
        logger.debug('[translate_cache] get caught %s: %s', type(_e_audit).__name__, _e_audit)
        return None
    except OSError as e:
        logger.debug('[TranslateCache] stat failed for %s: %s', path, e)
        return None

    if _TTL_SECONDS > 0 and (time.time() - st.st_mtime) > _TTL_SECONDS:
        try:
            os.remove(path)
        except OSError as e:
            logger.debug('[TranslateCache] expired-remove failed for %s: %s', path, e)
        return None

    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.debug('[TranslateCache] read failed for %s: %s', path, e)
        return None

    if (not isinstance(data, dict)
            or not isinstance(data.get('translated'), str)
            or not data.get('translated')
            or not isinstance(data.get('model', ''), str)):
        logger.debug('[TranslateCache] malformed payload at %s', path)
        return None

    if random.random() < _SWEEP_PROBABILITY:
        _lazy_sweep_shard(key[:2])

    return data


def put(text: str, source: str, target: str, translated: str, model: str = ''):
    """Store ``translated`` under the key for ``(text, source, target)``.

    Atomic: uses the repository's unique-temp + fsync + replace primitive.
    Failures are logged at debug level — caching is best-effort.
    """
    if not _ENABLED or not text or not translated:
        return
    _ensure_dir()
    key = _key(text, source, target)
    path = _path_for(key)
    shard = os.path.dirname(path)
    try:
        os.makedirs(shard, exist_ok=True)
    except OSError as e:
        logger.debug('[TranslateCache] mkdir %s failed: %s', shard, e)
        return

    payload = {
        'translated': translated,
        'model': model or '',
        'len_in': len(text),
        'len_out': len(translated),
        'ts': int(time.time()),
    }
    shard_budget = _shard_budget_bytes(key[:2])
    payload_size = _serialized_payload_size(payload)
    if payload_size > shard_budget:
        logger.debug(
            '[TranslateCache] skip oversized entry bytes=%d shard_budget=%d',
            payload_size,
            shard_budget,
        )
        return
    budget_lock_path = os.path.join(shard, '.translate-cache-budget')
    # ``locked_path`` is already both thread- and process-safe and is keyed by
    # this shard. Independent shards therefore remain concurrent instead of
    # queueing every cache write behind one process-wide mutex.
    with locked_path(budget_lock_path):
        try:
            write_json_atomic(path, payload, indent=None)
        except (OSError, TypeError, ValueError) as e:
            logger.debug('[TranslateCache] write failed for %s: %s', path, e)
            return
        _enforce_shard_budget(shard, keep_path=path)


def remove(text: str, source: str, target: str) -> bool:
    """Discard one reconstructible entry after caller-side validation fails."""
    if not _ENABLED or not text:
        return False
    _ensure_dir()
    key = _key(text, source, target)
    path = _path_for(key)
    shard = os.path.dirname(path)
    budget_lock_path = os.path.join(shard, '.translate-cache-budget')
    with locked_path(budget_lock_path):
        try:
            os.remove(path)
        except FileNotFoundError:
            return False
        except OSError as exc:
            logger.debug('[TranslateCache] remove failed for %s: %s', path, exc)
            return False
    return True


def _lazy_sweep_shard(shard_prefix: str):
    """Remove expired files in a single shard dir.  Called probabilistically
    from ``get()``; never raises."""
    if _TTL_SECONDS <= 0:
        return
    shard = os.path.join(_CACHE_DIR, shard_prefix)
    cutoff = time.time() - _TTL_SECONDS
    try:
        names = os.listdir(shard)
    except OSError as _e_audit:
        logger.debug('[translate_cache] _lazy_sweep_shard caught %s: %s', type(_e_audit).__name__, _e_audit)
        return
    removed = 0
    for name in names:
        p = os.path.join(shard, name)
        try:
            if os.stat(p).st_mtime < cutoff:
                os.remove(p)
                removed += 1
        except OSError as _e_audit:
            logger.debug('[translate_cache] _lazy_sweep_shard caught %s: %s', type(_e_audit).__name__, _e_audit)
            continue
    if removed:
        logger.debug('[TranslateCache] swept shard %s: removed %d expired entries',
                     shard_prefix, removed)
