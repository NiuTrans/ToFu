"""lib/search_settings.py — Single source of truth for search/fetch settings.

Three consumers share this module so they can never drift apart:

  * **Settings UI** (``routes/config.py`` GET) projects :func:`status_payload`
    — the LIVE backend state (tofu-search version, extension reachability,
    engine count, filter model/mode) next to the effective knob values.
  * **The ``update_search_settings`` agent tool** mutates settings through
    :func:`apply_updates` — validated, clamped, persisted, hot-reloaded,
    audit-logged. The model can thus fix a bad knob mid-conversation
    ("抓取太慢" → raise timeout / lower top_n) instead of describing the
    Settings page to the user.
  * **The Daily Optimizer** (``block_search_domain`` action) reuses
    :func:`normalise_domain` so "what is a domain" has exactly one answer.

Priority chain (unchanged, see ``lib/__init__.py``): ENV VAR > saved
server_config.json ``search`` section > built-in default. A save whose env
var is ALSO set persists fine but does not take effect — :func:`apply_updates`
reports that honestly in ``notes`` rather than letting the caller believe
the change landed.
"""

from __future__ import annotations

import os

import lib as _lib
from lib.config_dir import config_path as _config_path
from lib.json_store import read_json, write_json_atomic
from lib.log import audit_log, get_logger

logger = get_logger(__name__)

_CONFIG_FILE = _config_path('server_config.json')

_MB = 1024 * 1024

# ── Tunable knobs ────────────────────────────────────────────────────
# key → (type, min, max, lib attribute, env override). ``int`` values are
# CLAMPED into [min, max] (never silently stored out of range); ``bool``
# accepts only real booleans. max_chars_pdf keeps 0 = unlimited.
KNOBS: dict[str, dict] = {
    'fetch_top_n':        {'type': 'int',  'min': 1,     'max': 20,
                           'attr': 'FETCH_TOP_N',            'env': 'FETCH_TOP_N'},
    'fetch_timeout':      {'type': 'int',  'min': 5,     'max': 120,
                           'attr': 'FETCH_TIMEOUT',          'env': 'FETCH_TIMEOUT'},
    'max_chars_search':   {'type': 'int',  'min': 1000,  'max': 500_000,
                           'attr': 'FETCH_MAX_CHARS_SEARCH', 'env': 'FETCH_MAX_CHARS_SEARCH'},
    'max_chars_direct':   {'type': 'int',  'min': 1000,  'max': 1_000_000,
                           'attr': 'FETCH_MAX_CHARS_DIRECT', 'env': 'FETCH_MAX_CHARS_DIRECT'},
    'max_chars_pdf':      {'type': 'int',  'min': 0,     'max': 2_000_000,
                           'attr': 'FETCH_MAX_CHARS_PDF',    'env': 'FETCH_MAX_CHARS_PDF'},
    'max_bytes':          {'type': 'int',  'min': _MB,   'max': 500 * _MB,
                           'attr': 'FETCH_MAX_BYTES',        'env': 'FETCH_MAX_BYTES'},
    'llm_content_filter': {'type': 'bool',
                           'attr': 'LLM_CONTENT_FILTER_ENABLED', 'env': 'FETCH_LLM_FILTER'},
}


def normalise_domain(domain: str) -> str:
    """Canonical domain normaliser (scheme/www/path/port stripped, lowercased).

    Shared by the optimizer's block_search_domain action and the
    update_search_settings tool so "what counts as the same domain" has one
    answer across every writer of ``skip_domains``.
    """
    dom = (domain or '').strip().lower()
    if dom.startswith('http://') or dom.startswith('https://'):
        dom = dom.split('://', 1)[1]
    if dom.startswith('www.'):
        dom = dom[4:]
    if '/' in dom:
        dom = dom.split('/', 1)[0]
    if ':' in dom:
        dom = dom.split(':', 1)[0]
    return dom


def read_effective() -> dict:
    """The values the fetch pipeline will actually use RIGHT NOW."""
    return {
        'fetch_top_n': int(getattr(_lib, 'FETCH_TOP_N', 6)),
        'fetch_timeout': int(getattr(_lib, 'FETCH_TIMEOUT', 15)),
        'max_chars_search': int(getattr(_lib, 'FETCH_MAX_CHARS_SEARCH', 60000)),
        'max_chars_direct': int(getattr(_lib, 'FETCH_MAX_CHARS_DIRECT', 200000)),
        'max_chars_pdf': int(getattr(_lib, 'FETCH_MAX_CHARS_PDF', 0)),
        'max_bytes': int(getattr(_lib, 'FETCH_MAX_BYTES', 20 * _MB)),
        'llm_content_filter': bool(getattr(_lib, 'LLM_CONTENT_FILTER_ENABLED', True)),
        'skip_domains': sorted(getattr(_lib, 'SKIP_DOMAINS', set())),
    }


def _validate(key: str, value) -> tuple:
    """Validate + normalise one knob. Returns (ok, coerced_value, error)."""
    spec = KNOBS.get(key)
    if spec is None:
        return False, None, f'unknown setting: {key!r} (allowed: {", ".join(KNOBS)})'
    if spec['type'] == 'bool':
        if not isinstance(value, bool):
            return False, None, f'{key} must be a boolean, got {type(value).__name__}'
        return True, value, None
    # int knobs: accept int, or a digit string (models sometimes send "8")
    if isinstance(value, bool):  # bool is an int subclass — reject explicitly
        return False, None, f'{key} must be an integer, got a boolean'
    if isinstance(value, str):
        value = value.strip()
        if not value.lstrip('-').isdigit():
            return False, None, f'{key} must be an integer, got {value!r}'
        value = int(value)
    if not isinstance(value, int):
        return False, None, f'{key} must be an integer, got {type(value).__name__}'
    clamped = max(spec['min'], min(spec['max'], value))
    return True, clamped, None


def apply_updates(changes: dict) -> dict:
    """Validate, persist, and hot-apply search/fetch setting changes.

    Args:
        changes: knob → value. Aliases understood:
          * ``max_download_mb`` (int, MB) → ``max_bytes`` (bytes) — humans and
            models think in MB; the pipeline stores bytes.
          * ``block_domain`` / ``unblock_domain`` (str) → add/remove one host
            in ``skip_domains`` (normalised via :func:`normalise_domain`).

    Returns:
        ``{'ok', 'applied', 'errors', 'notes', 'effective'}`` where
        ``applied`` maps knob → stored value, ``notes`` carries env-override
        warnings (saved but shadowed), and ``effective`` is the post-change
        :func:`read_effective` snapshot. With NO changes the function is a
        pure read (nothing written, no reload, no audit).
    """
    changes = dict(changes or {})
    result = {'ok': True, 'applied': {}, 'errors': {}, 'notes': [],
              'effective': read_effective()}

    # ── Alias folding ──
    if 'max_download_mb' in changes:
        mb = changes.pop('max_download_mb')
        if isinstance(mb, bool) or not isinstance(mb, (int, float)):
            result['errors']['max_download_mb'] = (
                f'max_download_mb must be a number, got {type(mb).__name__}')
        elif mb <= 0:
            result['errors']['max_download_mb'] = 'max_download_mb must be > 0'
        else:
            changes['max_bytes'] = int(mb * _MB)

    domains_to_block: list[str] = []
    domains_to_unblock: list[str] = []
    for alias, bucket in (('block_domain', domains_to_block),
                          ('unblock_domain', domains_to_unblock)):
        raw = changes.pop(alias, None)
        if raw is None:
            continue
        items = raw if isinstance(raw, list) else [raw]
        for item in items:
            dom = normalise_domain(str(item or ''))
            if dom and '.' in dom:
                bucket.append(dom)
            else:
                result['errors'][alias] = f'invalid domain: {item!r}'

    # ── Validate the plain knobs ──
    validated: dict[str, object] = {}
    for key, value in changes.items():
        ok, coerced, err = _validate(key, value)
        if ok:
            validated[key] = coerced
        else:
            result['errors'][key] = err

    if not validated and not domains_to_block and not domains_to_unblock:
        if result['errors']:
            result['ok'] = False
            logger.warning('[SearchSettings] apply rejected: %s', result['errors'])
        return result   # pure read when called with no changes at all

    # ── Persist into server_config.json (merge, never clobber) ──
    data = read_json(_CONFIG_FILE, default={})
    if not isinstance(data, dict):
        data = {}
    search_cfg = data.get('search')
    if not isinstance(search_cfg, dict):
        search_cfg = {}
        data['search'] = search_cfg

    for key, value in validated.items():
        search_cfg[key] = value
        result['applied'][key] = value

    if domains_to_block or domains_to_unblock:
        current = search_cfg.get('skip_domains')
        if not isinstance(current, list):
            # Seed from the in-memory defaults so we never SHRINK the set.
            current = sorted(_lib.SKIP_DOMAINS)
        current = set(current)
        current |= set(domains_to_block)
        current -= set(domains_to_unblock)
        search_cfg['skip_domains'] = sorted(current)
        if domains_to_block:
            result['applied']['block_domain'] = sorted(set(domains_to_block))
        if domains_to_unblock:
            result['applied']['unblock_domain'] = sorted(set(domains_to_unblock))

    try:
        write_json_atomic(_CONFIG_FILE, data)
    except Exception as e:
        logger.error('[SearchSettings] persist failed: %s', e, exc_info=True)
        result['ok'] = False
        result['errors']['persist'] = str(e)
        return result

    # ── Hot-apply (re-reads the file, updates lib.*, re-syncs tofu-search) ──
    try:
        _lib.reload_config()
    except Exception as e:
        logger.error('[SearchSettings] reload_config failed: %s', e, exc_info=True)
        result['ok'] = False
        result['errors']['reload'] = str(e)
        return result

    # ── Honesty: a saved knob shadowed by an env var did NOT take effect ──
    for key in validated:
        env_key = KNOBS[key].get('env')
        if env_key and os.environ.get(env_key) not in (None, ''):
            result['notes'].append(
                f'{key} saved, but env var {env_key} is set and OVERRIDES it '
                f'until the process is restarted without it')

    result['effective'] = read_effective()
    audit_log('search_settings_update',
              applied=result['applied'],
              rejected=result['errors'] or None,
              skip_domains_size=result['effective'].get('skip_domains')
              and len(result['effective']['skip_domains']))
    logger.info('[SearchSettings] applied=%s errors=%s', result['applied'],
                result['errors'] or 'none')
    return result


def status_payload() -> dict:
    """Live backend state for the Settings UI status strip.

    Everything here answers "what will the backend ACTUALLY do on the next
    search" — the piece the Settings page never showed (it only echoed saved
    values, leaving the frontend/backend relationship opaque).
    """
    payload = {'ok': False}
    try:
        import tofu_search
        cfg = tofu_search.get_config()
        payload.update({
            'ok': True,
            'tofu_search_version': str(getattr(tofu_search, '__version__', '?')),
            'searxng_instances': len(getattr(cfg, 'searxng_instances', []) or []),
            'filter_mode': str(getattr(cfg, 'filter_mode', 'gate')),
            'filter_model': os.environ.get('FETCH_FILTER_MODEL', '') or 'dispatch-default',
            'search_deadline_secs': int(getattr(cfg, 'search_deadline_secs', 45)),
            'fetch_url_deadline_secs': int(getattr(cfg, 'fetch_url_deadline_secs', 25)),
        })
    except Exception as e:
        logger.warning('[SearchSettings] tofu-search status unavailable: %s', e)
        # error_transparency ratchet: error fields are envelope-produced,
        # never a bare str(e) (loses kind/severity/hint). The status strip
        # reads a STRING, so take the envelope's classified message.
        from lib.error_envelope import from_exception
        payload['error'] = from_exception(
            e, context='status-probe', source='search-settings').get(
                'message', str(e))
    try:
        from lib.browser import is_extension_connected
        payload['extension_connected'] = bool(is_extension_connected())
    except Exception as e:
        logger.debug('[SearchSettings] extension probe failed: %s', e)
        payload['extension_connected'] = False
    return payload
