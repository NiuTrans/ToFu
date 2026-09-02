"""lib/translate_cache.py — On-disk cache for translation results.

Keyed on ``sha256(target | source | text)``.  Stored as small JSON files
under ``data/translate_cache/<aa>/<sha>.json`` (sharded by the first two
hex chars to keep any single dir small).

Hits skip both the MT-provider HTTP call AND the LLM dispatch in
``_translate_one_chunk()``.  Cache is purely additive — opt out with
``TOFU_TRANSLATE_CACHE=0``.

Eviction is lazy and bounded:
  - Each entry has a TTL of ``TOFU_TRANSLATE_CACHE_TTL_DAYS`` (default 30).
  - On every ~1/256 lookup we sweep the shard the key falls in, removing
    entries past the TTL (cheap — typical shards have a few hundred files).
  - There is no global eviction loop; under steady state the lazy sweep
    keeps each shard bounded.

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

from lib.json_store import write_json_atomic
from lib.log import get_logger

logger = get_logger(__name__)

from lib.runtime_paths import data_root
_CACHE_DIR = os.path.join(data_root(), 'translate_cache')

_ENABLED = os.environ.get('TOFU_TRANSLATE_CACHE', '1') != '0'
_TTL_SECONDS = int(os.environ.get('TOFU_TRANSLATE_CACHE_TTL_DAYS', '30')) * 86400
_SWEEP_PROBABILITY = 1.0 / 256  # one sweep per ~256 lookups, on the shard touched

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
    try:
        write_json_atomic(path, payload, indent=None)
    except (OSError, TypeError, ValueError) as e:
        logger.debug('[TranslateCache] write failed for %s: %s', path, e)


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
