"""lib/netmirrors.py — Package-mirror registry with health tracking.

One runtime registry for "which mirror serves this package ecosystem
best right now" — the knowledge that used to live frozen inside
install.sh.  ``run_command`` (via ``lib.project_mod.run_net``) consults
it before spawning ``pip``/``npm``/``conda`` subprocesses and on failure
diagnosis; the Settings UI can render it as-is.

Ecosystems: ``pypi``, ``npm``, ``conda``, ``github``.

Design rules:

- **No corporate infrastructure in the repo.**  Built-in seeds are
  public mirrors only; site-specific entries arrive via
  ``data/config/netpath_mirrors.json`` (user config) or the
  ``TOFU_NETMIRRORS_JSON`` env var (a JSON array, deployment-injected).
- **Health over hope.**  Every entry tracks consecutive failures +
  EWMA latency, fed by real subprocess outcomes
  (:func:`report_outcome`) and by on-demand active probes
  (:func:`probe`).  2 consecutive failures → 120s cooldown.
- **Standard mechanisms only.**  ``env_overlay`` emits the ecosystem's
  own configuration knobs (``PIP_INDEX_URL``, ``npm_config_registry``),
  never command rewrites.

Env knobs:
  ``TOFU_NETMIRRORS``       on/off master switch (default: on)
  ``TOFU_NETMIRRORS_JSON``  extra entries as a JSON array (see upsert)
"""

from __future__ import annotations

import json
import os
import threading
import time
from urllib.parse import urlparse

from lib.config_dir import config_path
from lib.json_store import read_json, write_json_atomic
from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'ECOSYSTEMS', 'UPSTREAM_HOSTS', 'entries', 'get', 'upsert', 'remove',
    'set_enabled', 'report_outcome', 'best', 'env_overlay', 'probe',
    'status_summary', 'reset_for_test',
]

ECOSYSTEMS = ('pypi', 'npm', 'conda', 'github')

#: The default upstream each ecosystem talks to when no mirror is in
#: play — netpath tracks THESE hosts, so "is upstream sick?" is a
#: netpath question (see lib.project_mod.run_net).
UPSTREAM_HOSTS = {
    'pypi': 'pypi.org',
    'npm': 'registry.npmjs.org',
    'conda': 'conda.anaconda.org',
    'github': 'github.com',
}

_FAIL_THRESHOLD = 2          # consecutive failures before cooldown
_COOLDOWN_S = 120.0
_EWMA_ALPHA = 0.3
_MAX_ENTRIES = 64

#: Public, well-known mirrors only.  Corp/site mirrors belong to the
#: config file or TOFU_NETMIRRORS_JSON — never hardcoded here.
_BUILTINS = (
    {'id': 'pypi-tuna', 'ecosystem': 'pypi',
     'url': 'https://pypi.tuna.tsinghua.edu.cn/simple',
     'label': 'Tsinghua TUNA'},
    {'id': 'pypi-ustc', 'ecosystem': 'pypi',
     'url': 'https://pypi.mirrors.ustc.edu.cn/simple',
     'label': 'USTC'},
    {'id': 'pypi-aliyun', 'ecosystem': 'pypi',
     'url': 'https://mirrors.aliyun.com/pypi/simple',
     'label': 'Aliyun'},
    {'id': 'npm-npmmirror', 'ecosystem': 'npm',
     'url': 'https://registry.npmmirror.com',
     'label': 'npmmirror.com'},
    {'id': 'conda-tuna', 'ecosystem': 'conda',
     'url': 'https://mirrors.tuna.tsinghua.edu.cn/anaconda/cloud/conda-forge',
     'label': 'Tsinghua TUNA (conda-forge)'},
    {'id': 'conda-ustc', 'ecosystem': 'conda',
     'url': 'https://mirrors.ustc.edu.cn/anaconda/cloud/conda-forge',
     'label': 'USTC (conda-forge)'},
)

_STORE_PATH = config_path('netpath_mirrors.json')
_STORE_VERSION = 1

_lock = threading.Lock()
# id → runtime entry: {id, ecosystem, url, label, enabled, preferred,
#                      source, fails, ewma_ms, samples, cooldown_until,
#                      last_ok, last_probe}
_entries: 'dict[str, dict]' = {}
_loaded = False


def _enabled() -> bool:
    return os.environ.get('TOFU_NETMIRRORS', 'on').strip().lower() not in (
        '0', 'off', 'false', 'no')


def _new_health() -> dict:
    return {'fails': 0, 'ewma_ms': None, 'samples': 0,
            'cooldown_until': 0.0, 'last_ok': 0.0, 'last_probe': 0.0}


def _sanitize(raw: dict, source: str) -> 'dict | None':
    """Normalize one entry; None when malformed. Never raises."""
    try:
        eco = str(raw.get('ecosystem') or '').strip().lower()
        url = str(raw.get('url') or '').strip().rstrip('/')
        entry_id = str(raw.get('id') or '').strip().lower()
        if eco not in ECOSYSTEMS or not url:
            return None
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname:
            return None
        if not entry_id:
            entry_id = '%s-%s' % (eco, (parsed.hostname or 'mirror')
                                  .replace('.', '-'))
        return {
            'id': entry_id[:48],
            'ecosystem': eco,
            'url': url,
            'label': str(raw.get('label') or entry_id)[:60],
            'enabled': bool(raw.get('enabled', True)),
            # 'preferred' pins the mirror as the ecosystem default even
            # while upstream is healthy (site policy, e.g. corp mirror).
            'preferred': bool(raw.get('preferred', False)),
            'source': source,
        }
    except Exception as e:
        logger.debug('[Netmirrors] skipped malformed entry: %s', e)
        return None


def _load() -> None:
    """Merge builtins + config file + env JSON, once per process."""
    global _loaded
    with _lock:
        if _loaded:
            return
        _loaded = True
        for raw in _BUILTINS:
            e = _sanitize(raw, 'builtin')
            if e:
                _entries[e['id']] = {**e, **_new_health()}
        payload = read_json(_STORE_PATH, default=None)
        saved_health = {}
        if isinstance(payload, dict):
            for raw in payload.get('entries') or ():
                e = _sanitize(raw, 'config')
                if e:
                    _entries[e['id']] = {**e, **_new_health()}
            saved_health = payload.get('health') or {}
        env_json = os.environ.get('TOFU_NETMIRRORS_JSON', '').strip()
        if env_json:
            try:
                for raw in json.loads(env_json):
                    e = _sanitize(raw, 'env')
                    if e:
                        _entries[e['id']] = {**e, **_new_health()}
            except (ValueError, TypeError) as e:
                logger.warning('[Netmirrors] TOFU_NETMIRRORS_JSON ignored: %s', e)
        # Restore health for surviving ids (a mirror's past reliability is
        # still meaningful across restarts).
        if isinstance(saved_health, dict):
            for mid, h in saved_health.items():
                ent = _entries.get(mid)
                if not ent or not isinstance(h, dict):
                    continue
                try:
                    ent['fails'] = max(0, int(h.get('fails') or 0))
                    ent['samples'] = max(0, int(h.get('samples') or 0))
                    lat = h.get('ewma_ms')
                    ent['ewma_ms'] = float(lat) if lat is not None else None
                    ent['last_ok'] = float(h.get('last_ok') or 0.0)
                except (TypeError, ValueError) as e:
                    logger.debug('[Netmirrors] skipped malformed health for '
                                 '%s: %s', mid, e)
        logger.info('[Netmirrors] %d mirror entries loaded (%d builtin)',
                    len(_entries), len(_BUILTINS))


def _save() -> None:
    with _lock:
        entries = [dict({k: v for k, v in e.items()
                         if k in ('id', 'ecosystem', 'url', 'label',
                                  'enabled', 'preferred')})
                   for e in _entries.values() if e['source'] != 'builtin']
        health = {mid: {'fails': e['fails'], 'samples': e['samples'],
                        'ewma_ms': e['ewma_ms'], 'last_ok': e['last_ok']}
                  for mid, e in _entries.items()}
        payload = {'version': _STORE_VERSION, 'saved_at': time.time(),
                   'entries': entries, 'health': health}
    try:
        write_json_atomic(_STORE_PATH, payload, fsync=False, indent=2)
    except Exception as e:
        logger.warning('[Netmirrors] save failed: %s', e)


# ═════════════════════════════════════════════════════════════
#  Reads
# ═════════════════════════════════════════════════════════════

def entries(ecosystem: 'str | None' = None) -> list:
    """All registered entries (optionally one ecosystem), config view."""
    _load()
    with _lock:
        out = []
        for e in _entries.values():
            if ecosystem and e['ecosystem'] != ecosystem:
                continue
            view = {k: e[k] for k in ('id', 'ecosystem', 'url', 'label',
                                      'enabled', 'preferred', 'source')}
            view['health'] = {
                'fails': e['fails'],
                'ewma_ms': e['ewma_ms'],
                'samples': e['samples'],
                'cooling': _is_cooling(e),
            }
            out.append(view)
        return out


def get(entry_id: str) -> 'dict | None':
    _load()
    with _lock:
        e = _entries.get((entry_id or '').strip().lower())
        return dict(e) if e else None


def _is_cooling(e: dict) -> bool:
    return (e['fails'] >= _FAIL_THRESHOLD
            and time.monotonic() < e.get('cooldown_until', 0.0))


def best(ecosystem: str) -> 'dict | None':
    """The healthiest enabled entry for *ecosystem* (None = registry empty).

    A ``preferred`` entry wins while healthy (site policy); otherwise the
    lowest measured latency wins, with never-probed entries ranked last
    in registration order.  Entries in failure cooldown are skipped.
    """
    if not _enabled():
        return None
    _load()
    with _lock:
        candidates = [dict(e) for e in _entries.values()
                      if e['ecosystem'] == ecosystem and e['enabled']
                      and not _is_cooling(e)]
    if not candidates:
        return None
    preferred = [e for e in candidates if e['preferred']]
    if preferred:
        candidates = preferred
    candidates.sort(key=lambda e: (
        float('inf') if e['ewma_ms'] is None else e['ewma_ms']))
    return candidates[0]


# ═════════════════════════════════════════════════════════════
#  Writes
# ═════════════════════════════════════════════════════════════

def upsert(raw: dict, source: str = 'config') -> 'dict | None':
    """Add or replace one entry (persisted for config-sourced writes)."""
    _load()
    e = _sanitize(raw, source)
    if e is None:
        return None
    with _lock:
        if len(_entries) >= _MAX_ENTRIES and e['id'] not in _entries:
            logger.warning('[Netmirrors] registry full (%d) — %s dropped',
                           _MAX_ENTRIES, e['id'])
            return None
        old = _entries.get(e['id'])
        health = ({k: old[k] for k in _new_health()} if old
                  else _new_health())
        _entries[e['id']] = {**e, **health}
    if source == 'config':
        _save()
    return get(e['id'])


def remove(entry_id: str) -> bool:
    _load()
    with _lock:
        gone = _entries.pop((entry_id or '').strip().lower(), None)
    if gone:
        _save()
    return gone is not None


def set_enabled(entry_id: str, enabled: bool) -> bool:
    _load()
    with _lock:
        e = _entries.get((entry_id or '').strip().lower())
        if not e:
            return False
        e['enabled'] = bool(enabled)
        if not enabled:
            e['fails'] = 0
            e['cooldown_until'] = 0.0
    _save()
    return True


def report_outcome(entry_id: str, ok: bool,
                   latency_ms: 'float | None' = None) -> None:
    """Feed one real (or probe) outcome to an entry's health."""
    if not _enabled():
        return
    _load()
    with _lock:
        e = _entries.get((entry_id or '').strip().lower())
        if not e:
            return
        if ok:
            e['fails'] = 0
            e['cooldown_until'] = 0.0
            e['last_ok'] = time.time()
            if latency_ms is not None and 0 < latency_ms <= 30000:
                e['samples'] += 1
                if e['ewma_ms'] is None:
                    e['ewma_ms'] = float(latency_ms)
                else:
                    e['ewma_ms'] = (_EWMA_ALPHA * float(latency_ms)
                                    + (1 - _EWMA_ALPHA) * e['ewma_ms'])
        else:
            e['fails'] += 1
            if e['fails'] == _FAIL_THRESHOLD:
                e['cooldown_until'] = time.monotonic() + _COOLDOWN_S
                logger.warning('[Netmirrors] %s cooling for %ds (%d '
                               'consecutive failures)',
                               entry_id, int(_COOLDOWN_S), e['fails'])


# ═════════════════════════════════════════════════════════════
#  Env overlays + active probing
# ═════════════════════════════════════════════════════════════

def env_overlay(ecosystem: str, entry: dict) -> dict:
    """Subprocess env vars that point *ecosystem*'s tooling at *entry*.

    Standard configuration mechanisms only — no command rewriting:
      pypi → PIP_INDEX_URL (+ PIP_TRUSTED_HOST for plain-http mirrors)
      npm  → npm_config_registry
      conda → CONDA_CHANNELS (channel URL; conda ≥4.4 honours it)
      github → none (diagnosis text only)
    """
    url = str(entry.get('url') or '')
    if not url:
        return {}
    if ecosystem == 'pypi':
        overlay = {'PIP_INDEX_URL': url}
        parsed = urlparse(url)
        if parsed.scheme == 'http' and parsed.hostname:
            overlay['PIP_TRUSTED_HOST'] = parsed.hostname
        return overlay
    if ecosystem == 'npm':
        return {'npm_config_registry': url}
    if ecosystem == 'conda':
        return {'CONDA_CHANNELS': url}
    return {}


def probe(entry_id: 'str | None' = None, ecosystem: 'str | None' = None,
          timeout: float = 4.0) -> dict:
    """Actively probe entries (one, or one ecosystem, or all) and feed health.

    Uses ``lib.http_client.http_get`` so probe traffic itself rides the
    adaptive proxy layer.  Returns ``{entry_id: ok}``.
    """
    _load()
    with _lock:
        targets = [dict(e) for e in _entries.values()
                   if (entry_id is None or e['id'] == entry_id)
                   and (ecosystem is None or e['ecosystem'] == ecosystem)
                   and e['enabled']]
    results = {}
    for e in targets:
        t0 = time.monotonic()
        ok = False
        try:
            from lib.http_client import http_get
            resp = http_get(e['url'] + '/', timeout=timeout)
            resp.close()
            ok = True
        except Exception as exc:
            logger.debug('[Netmirrors] probe %s failed: %s', e['id'], exc)
        latency = (time.monotonic() - t0) * 1000.0
        report_outcome(e['id'], ok, latency if ok else None)
        results[e['id']] = ok
    _save()
    return results


def status_summary() -> dict:
    """Settings-UI friendly view grouped by ecosystem."""
    _load()
    out = {'enabled': _enabled(), 'ecosystems': {}}
    for eco in ECOSYSTEMS:
        out['ecosystems'][eco] = entries(eco)
    return out


def reset_for_test() -> None:
    """Drop all entries and re-seed from builtins. Test-only."""
    global _loaded
    with _lock:
        _entries.clear()
        _loaded = False
    _load()
