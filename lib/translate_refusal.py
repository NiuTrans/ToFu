"""lib/translate_refusal.py — On-disk refusal markers for the translate engine.

When a content-quality guard (wrong-language flip / no-op echo /
over-generation) refuses a chunk after the FULL retry budget, the verdict is
a property of the chunk's content shape, not of the model roster — re-running
the same roster on the next page load just burns 5 more dispatches to reach
the same refusal (observed: one 488-char chunk refused 36×/day, each time
after 35-58 s of retries).

This module persists a tiny marker per refused chunk, keyed on
``sha256(target | source | text)`` — the same keying as
:mod:`lib.translate_cache`, stored separately under
``data/translate_refusal/<aa>/<sha>.json`` so a refusal can never be mistaken
for a translation. On a later call for the same chunk the engine replays the
refusal instantly (zero dispatches, same typed ``TranslationContentRefused``
→ same 502 envelope).

Markers expire after ``TOFU_TRANSLATE_REFUSAL_TTL_DAYS`` (default 7) so a
healthier future model roster gets one fresh attempt. Kill switch:
``TOFU_TRANSLATE_REFUSAL=0``. Writes are atomic (tempfile + os.replace) and
every failure is best-effort at debug level — a broken store must never
break translation itself.
"""

import hashlib
import math
import os
import re
import sys
import tempfile
import time

from lib.env_compat import getenv_compat
from lib.json_store import read_json, write_json_atomic
from lib.log import get_logger

logger = get_logger(__name__)

from lib.runtime_paths import data_root
_DEFAULT_DIR = os.path.join(data_root(), 'translate_refusal')
_REFUSAL_DIR = _DEFAULT_DIR


def _effective_dir() -> str:
    """Directory the next get/put actually uses.

    Under pytest the default dir is redirected to a per-TEST tmp namespace
    (mirrors lib/log.py's _LOG_UNDER_PYTEST convention): a full-budget
    refusal recorded by one test suite must never become another suite's
    replay — several translate suites share the same _MIXED_SOURCE fixture,
    so a process-wide or production dir leaks verdicts across files. A test
    that monkeypatches ``_REFUSAL_DIR`` opts out of the redirection (the
    override is honoured verbatim).
    """
    if _REFUSAL_DIR is not _DEFAULT_DIR:
        return _REFUSAL_DIR
    if 'pytest' in sys.modules:
        current = os.environ.get('PYTEST_CURRENT_TEST', '')
        slug = re.sub(r'[^A-Za-z0-9_.-]+', '_', current.split(' ')[0]) or 'collection'
        # pid in the path: tmpfs outlives a pytest run, and a refusal recorded
        # by run N (e.g. a manual NEUTER experiment) must not replay in run N+1.
        return os.path.join(tempfile.gettempdir(),
                            f'tofu-translate-refusal-pytest-{os.getpid()}', slug)
    return _REFUSAL_DIR

_ENABLED = getenv_compat('TOFU_TRANSLATE_REFUSAL', default='1') != '0'
_TTL_SECONDS = int(getenv_compat('TOFU_TRANSLATE_REFUSAL_TTL_DAYS',
                                 default='7')) * 86400


def _key(text: str, source: str, target: str) -> str:
    """Stable sha256 key for (target, source, text) — same scheme as
    translate_cache but its own namespace prefix, so the two stores evolve
    independently."""
    h = hashlib.sha256()
    h.update(b'r1\x00')
    h.update((target or '').encode('utf-8'))
    h.update(b'\x00')
    h.update((source or '').encode('utf-8'))
    h.update(b'\x00')
    h.update((text or '').encode('utf-8'))
    return h.hexdigest()


def _path_for(key: str) -> str:
    return os.path.join(_effective_dir(), key[:2], key + '.json')


def get(text: str, source: str, target: str):
    """Return the stored refusal ``{verdict, reason, model, content_fails,
    ts}`` for this chunk, or ``None`` when absent / expired / unreadable."""
    if not _ENABLED or not text:
        return None
    path = _path_for(_key(text, source, target))
    data = read_json(path, default=None)

    if not isinstance(data, dict) or not data.get('verdict'):
        logger.debug('[TranslateRefusal] malformed payload at %s', path)
        return None
    ts = data.get('ts', 0)
    if (not isinstance(ts, (int, float)) or isinstance(ts, bool)
            or not math.isfinite(float(ts))):
        logger.debug('[TranslateRefusal] malformed timestamp at %s', path)
        return None
    if _TTL_SECONDS > 0 and (time.time() - ts) > _TTL_SECONDS:
        try:
            os.remove(path)
        except OSError as e:
            logger.debug('[TranslateRefusal] expired-remove failed for %s: %s',
                         path, e)
        return None
    return data


def put(text: str, source: str, target: str, *, verdict: str, reason: str,
        model: str = '', content_fails: int = 0):
    """Record a refusal marker for ``(text, source, target)``. Best-effort:
    write failures are logged at debug and never raised."""
    if not _ENABLED or not text or not verdict:
        return
    path = _path_for(_key(text, source, target))
    shard = os.path.dirname(path)
    payload = {
        'verdict': verdict,
        'reason': (reason or '')[:500],
        'model': model or '',
        'content_fails': int(content_fails),
        'ts': int(time.time()),
    }
    try:
        os.makedirs(shard, exist_ok=True)
        write_json_atomic(path, payload, indent=None)
    except (OSError, TypeError, ValueError) as e:
        logger.debug('[TranslateRefusal] write failed for %s: %s', path, e)
