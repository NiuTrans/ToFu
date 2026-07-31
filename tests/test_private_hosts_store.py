"""tests/test_private_hosts_store.py — Internal-host allowlist: store, wiring, gates.

Three things are asserted here, and the second and third are the ones that
matter long-term:

1. INPUT NORMALIZATION — a pasted URL / port / uppercase / trailing dot all
   reduce to the bare hostname, and a BARE IP is refused. The IP refusal is
   not fussiness: an internal load balancer rotates its address between
   lookups (one host answered as both 10.176.18.71 and 10.192.19.176 minutes
   apart), so an IP entry silently stops matching — and it would also be a
   direct way to name a cloud-metadata endpoint.

2. TWO GATES STAY SEPARATE — the allowlist grants REACHABILITY and never
   credentials; auth-sources grants credentials and never an SSRF exemption.
   The original defect was exactly this conflation (a connected auth-source
   implicitly bypassed the SSRF guard), so a guard has to hold them apart.

3. EXPORT SURVIVAL — the feature must not depend on anything ``export.py``
   strips. The env var alone would have been broken in an exported copy;
   the Settings store is the source of truth and its CODE ships.
"""

import os
import tempfile
from unittest import mock

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolated_store():
    """Point the store at a temp file — never touch the shared data/config."""
    import lib.private_hosts as ph
    prev_path, prev_cache, prev_loaded = ph._STORE_PATH, list(ph._cache), ph._cache_loaded
    ph._STORE_PATH = os.path.join(tempfile.mkdtemp(), 'private_hosts.json')
    ph._cache.clear()
    ph._cache_loaded = False
    yield ph
    ph._STORE_PATH = prev_path
    ph._cache.clear()
    ph._cache.extend(prev_cache)
    ph._cache_loaded = prev_loaded


# ── 1. Normalization + validation ──

@pytest.mark.parametrize('raw,expected', [
    ('https://api.openai.com/ml/modelPlaza?a=1', 'your-llm-gateway.example.com'),
    ('AIGC.Sankuai.COM', 'your-llm-gateway.example.com'),
    ('your-llm-gateway.example.com:443', 'your-llm-gateway.example.com'),
    ('your-llm-gateway.example.com.', 'your-llm-gateway.example.com'),
    ('  example-corp.com  ', 'example-corp.com'),
    ('http://user:pw@host.example.com/x', 'host.example.com'),
])
def test_normalize_accepts_what_users_paste(_isolated_store, raw, expected):
    assert _isolated_store.normalize_host(raw) == expected


@pytest.mark.parametrize('bad', [
    '', '   ', None,
    '10.192.19.176',      # RFC1918 — the rotating-LB case
    '10.176.18.71',       # the SAME host's other address
    '127.0.0.1',
    '169.254.169.254',    # cloud metadata
    '[::1]',
    '::1',
    'http://10.0.0.5/x',  # IP smuggled inside a URL
    'intranet',           # single label — too broad / a typo
])
def test_normalize_refuses_unusable_input(_isolated_store, bad):
    with pytest.raises(ValueError):
        _isolated_store.normalize_host(bad)


def test_bare_ip_refusal_message_explains_the_reason(_isolated_store):
    """The error must teach the rule, not just say 'invalid'."""
    with pytest.raises(ValueError, match='(?i)hostname'):
        _isolated_store.normalize_host('10.0.0.1')


# ── CRUD ──

def test_upsert_then_enabled_hosts(_isolated_store):
    ph = _isolated_store
    ph.upsert_host('https://api.openai.com/ml/', label='YourProvider internal')
    assert ph.enabled_hosts() == {'your-llm-gateway.example.com'}
    rows = ph.list_hosts()
    assert len(rows) == 1
    assert rows[0]['host'] == 'your-llm-gateway.example.com'
    assert rows[0]['label'] == 'YourProvider internal'
    assert rows[0]['enabled'] is True


def test_new_entry_defaults_to_enabled(_isolated_store):
    """Adding a host IS the statement of intent — no second switch to flip."""
    ph = _isolated_store
    assert ph.upsert_host('example-corp.com')['enabled'] is True


def test_toggle_and_delete_are_normalization_insensitive(_isolated_store):
    ph = _isolated_store
    ph.upsert_host('your-llm-gateway.example.com')
    assert ph.set_enabled('AIGC.SANKUAI.COM.', False) is True
    assert ph.enabled_hosts() == set()
    assert ph.set_enabled('https://api.openai.com/x', True) is True
    assert ph.enabled_hosts() == {'your-llm-gateway.example.com'}
    assert ph.delete_host('  your-llm-gateway.example.com  ') is True
    assert ph.list_hosts() == []


def test_toggle_unknown_host_returns_false(_isolated_store):
    assert _isolated_store.set_enabled('nope.example.com', True) is False
    assert _isolated_store.delete_host('nope.example.com') is False


def test_disabled_entry_is_not_handed_to_the_fetcher(_isolated_store):
    ph = _isolated_store
    ph.upsert_host('example-corp.com', enabled=False)
    assert ph.list_hosts()[0]['enabled'] is False
    assert ph.enabled_hosts() == set(), 'a disabled row must not exempt anything'


def test_survives_reload_from_disk(_isolated_store):
    ph = _isolated_store
    ph.upsert_host('example-corp.com')
    ph._cache_loaded = False
    ph._cache.clear()
    assert ph.enabled_hosts() == {'example-corp.com'}


def test_unusable_stored_row_is_dropped_on_load(_isolated_store):
    """A hand-edited bare-IP row must not reach the fetch guard."""
    ph = _isolated_store
    from lib.json_store import write_json_atomic
    write_json_atomic(ph._STORE_PATH, {'version': 1, 'hosts': [
        {'host': '10.0.0.9', 'enabled': True},
        {'host': 'good.example.com', 'enabled': True},
    ]})
    ph._cache_loaded = False
    ph._cache.clear()
    assert ph.enabled_hosts() == {'good.example.com'}


# ── 2. Bridge wiring: store is the source of truth, env is only a fallback ──

def _sync_kwargs(env=None):
    import lib.search_bridge as sb
    with mock.patch.dict(os.environ, env or {}, clear=False), \
         mock.patch.object(sb.tofu_search, 'configure') as cfg:
        sb.sync_search_config()
    return cfg.call_args.kwargs


def test_store_feeds_the_search_config(_isolated_store):
    _isolated_store.upsert_host('example-corp.com')
    assert set(_sync_kwargs()['allow_private_hosts']) == {'example-corp.com'}


def test_store_wins_over_env(_isolated_store):
    """Settings is authoritative; env must not override a configured store."""
    _isolated_store.upsert_host('example-corp.com')
    kw = _sync_kwargs({'TOFU_SEARCH_ALLOW_PRIVATE_HOSTS': 'other.example.com'})
    assert set(kw['allow_private_hosts']) == {'example-corp.com'}


def test_env_still_works_as_a_bootstrap_fallback(_isolated_store):
    """An empty store falls back to env so CI / first boot can still set it."""
    kw = _sync_kwargs({'TOFU_SEARCH_ALLOW_PRIVATE_HOSTS': 'boot.example.com'})
    assert set(kw['allow_private_hosts']) == {'boot.example.com'}


def test_empty_store_and_no_env_means_everything_blocked(_isolated_store):
    kw = _sync_kwargs()
    assert set(kw['allow_private_hosts']) == set()
    assert kw['block_private_addresses'] is True


def test_unreadable_store_fails_closed(_isolated_store):
    """An unreadable allowlist must block, never silently widen the boundary."""
    import lib.search_bridge as sb
    with mock.patch('lib.private_hosts.enabled_hosts', side_effect=OSError('boom')):
        assert sb._store_private_hosts() == set()


# ── 3. Two gates stay separate ──

def test_allowlisting_a_host_grants_no_credentials(_isolated_store):
    """Reachability must not imply a login."""
    from lib.auth_sources import match_source
    _isolated_store.upsert_host('example-corp.com')
    assert match_source('https://api.openai.com/ml/x') is None, (
        'an allowlist entry must NOT register an auth source')


def test_private_host_store_exposes_no_credential_surface(_isolated_store):
    """The store's API must have no cookie/credential vocabulary at all."""
    import lib.private_hosts as ph
    banned = ('cookie', 'credential', 'token', 'password', 'proxy')
    for name in ph.__all__:
        low = name.lower()
        assert not any(b in low for b in banned), f'{name} mixes gates'
    for row in (ph.upsert_host('example-corp.com'), *ph.list_hosts()):
        for key in row:
            assert not any(b in key.lower() for b in banned), f'{key} mixes gates'


def test_auth_source_does_not_grant_an_ssrf_exemption(_isolated_store):
    """The mirror direction: connecting an account must not allowlist a host."""
    from lib.auth_sources import DEFAULT_SOURCES
    catalog = {d['domain'] for d in DEFAULT_SOURCES}
    assert 'example-corp.com' in catalog, 'precondition: the domain is in the auth catalog'
    # It is in the CREDENTIAL catalog, yet grants no reachability by itself.
    assert _isolated_store.enabled_hosts() == set()
    assert set(_sync_kwargs()['allow_private_hosts']) == set()


# ── 4. Export survival (charter #13/#14: the export artifact is a first-class target) ──

def _export_excluded_dirs():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        '_export_probe',
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'export.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return set(mod.ALWAYS_EXCLUDE_DIRS)


@pytest.mark.parametrize('path', [
    'lib/private_hosts.py',
    'routes/api_v1/private_hosts.py',
    'static/js/settings/private_hosts.js',
])
def test_feature_code_survives_export(path):
    """Every file the feature needs must live OUTSIDE an excluded directory."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    assert os.path.isfile(os.path.join(root, path)), f'{path} is missing'
    top = path.split('/')[0]
    assert top not in _export_excluded_dirs(), (
        f'{path} lives under {top!r}, which export.py strips — '
        'the feature would be broken in an exported copy')


def test_the_data_file_is_export_excluded_on_purpose():
    """The allowlist DATA is per-install intent: a fresh copy starts closed.

    This is the fail-safe direction, and it is why the CODE (asserted above)
    is what must survive rather than the JSON.
    """
    assert 'data' in _export_excluded_dirs()


def test_feature_is_not_env_only():
    """The capability must not require an env var that export.py cannot carry."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'lib/search_bridge.py'), encoding='utf-8') as f:
        src = f.read()
    idx = src.find('allow_private_hosts =')
    assert idx > 0, 'allow_private_hosts assignment not found'
    window = src[idx:idx + 700]
    assert '_store_private_hosts()' in window, (
        'the allowlist must be read from the Settings store, not env alone')
