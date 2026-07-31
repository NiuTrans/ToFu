"""tests/test_search_bridge_security_wiring.py

Two invariants that together killed a whole class of mis-diagnosis:

1. Every SearchConfig knob chatui needs MUST be reachable from chatui. A field
   with neither an env fallback in ``tofu_search.configure()`` nor a bridge
   kwarg is un-tunable without editing library source — ``searxng_instances``
   sat that way while its own docstring told operators to override it.

2. A fetch failure MUST carry WHY. ``fetch_page_content`` returning bare
   ``str | None`` collapsed SSRF-blocked / skip-domain / timeout / SPA-shell
   into one indistinguishable "Failed to fetch", which is what made an
   internal-host block look like a broken fetcher.
"""

import os
from unittest import mock

import pytest

pytestmark = pytest.mark.unit


# ── 1. Bridge wiring ──

def _sync_kwargs(env=None):
    """Run sync_search_config() and capture the kwargs handed to configure()."""
    import lib.search_bridge as sb
    with mock.patch.dict(os.environ, env or {}, clear=False), \
         mock.patch.object(sb.tofu_search, 'configure') as cfg:
        sb.sync_search_config()
    assert cfg.called, 'sync_search_config() never called configure()'
    return cfg.call_args.kwargs


def test_security_knobs_are_wired_through_the_bridge():
    kw = _sync_kwargs()
    for field in ('allow_private_hosts', 'block_private_addresses',
                  'allow_insecure_ssl_fallback', 'min_request_interval_ms'):
        assert field in kw, f'{field} is not reachable from chatui'


def test_shipped_default_posture_is_fail_safe():
    """Wiring a knob must not weaken it: the guard stays ON, allowlist empty."""
    kw = _sync_kwargs()
    assert kw['block_private_addresses'] is True
    assert kw['allow_insecure_ssl_fallback'] is False
    assert set(kw['allow_private_hosts']) == set()


def test_allow_private_hosts_is_populated_from_env():
    kw = _sync_kwargs({'TOFU_SEARCH_ALLOW_PRIVATE_HOSTS': 'your-llm-gateway.example.com, other.internal'})
    assert set(kw['allow_private_hosts']) == {'your-llm-gateway.example.com', 'other.internal'}


def test_empty_proxy_url_is_not_passed_so_env_survives():
    """An explicit '' would SHADOW TOFU_SEARCH_PROXY_URL.

    configure() applies its env default only to fields ABSENT from kwargs, so
    passing '' means "no proxy" instead of "fall back to the environment".
    """
    import lib.search_bridge as sb
    with mock.patch.object(sb, '_resolve_proxy_url', return_value=''):
        kw = _sync_kwargs()
    assert 'proxy_url' not in kw

    with mock.patch.object(sb, '_resolve_proxy_url', return_value='http://p:8080'):
        kw2 = _sync_kwargs()
    assert kw2['proxy_url'] == 'http://p:8080'


def test_searxng_instances_only_overrides_when_asked():
    assert 'searxng_instances' not in _sync_kwargs()
    kw = _sync_kwargs({'TOFU_SEARCH_SEARXNG_INSTANCES': 'https://a.example https://b.example'})
    assert kw['searxng_instances'] == ['https://a.example', 'https://b.example']


# ── 2. Failure-reason propagation ──

def test_fetch_failure_reason_reaches_the_tool_surface():
    """A refused fetch must yield a typed reason AND a human error_msg."""
    from lib.tasks_pkg.handlers.search import _core

    diag_payload = {
        'reason': 'ssrf_blocked',
        'detail': "Blocked by the SSRF guard: host 'x.internal' resolves to a private address.",
    }

    def _fake_fetch(url, max_chars=None, pdf_max_chars=None, diag=None, **kw):
        if diag is not None:
            diag.update(diag_payload)
        return None

    fake_facade = mock.Mock(fetch_page_content=_fake_fetch)
    with mock.patch.object(_core, '_facade_mod', return_value=fake_facade), \
         mock.patch.object(_core, '_stage_binary_asset', return_value=None):
        item = _core._fetch_url_one('https://x.internal/p', 'q', fetch_reason='t')

    assert item['page_content'] is None
    assert item['reason'] == 'fetch_failed:ssrf_blocked'
    assert 'SSRF guard' in item['error_msg']
    # The model-facing string must actually carry the cause.
    rendered = f"Failed to fetch {item['url']}." + (
        f" ({item['error_msg']})" if item['error_msg'] else '')
    assert 'SSRF guard' in rendered


def test_distinct_failures_stay_distinguishable():
    """Different causes must NOT collapse into one opaque message."""
    from lib.tasks_pkg.handlers.search import _core
    seen = {}
    for token, detail in (('timeout', 'Host did not respond within 15s.'),
                          ('spa_shell', 'Page is a JavaScript shell.'),
                          ('http_404', 'Server returned HTTP 404.')):
        def _fake(url, max_chars=None, pdf_max_chars=None, diag=None, _t=token, _d=detail, **kw):
            if diag is not None:
                diag.update({'reason': _t, 'detail': _d})
            return None
        with mock.patch.object(_core, '_facade_mod', return_value=mock.Mock(fetch_page_content=_fake)), \
             mock.patch.object(_core, '_stage_binary_asset', return_value=None):
            it = _core._fetch_url_one(f'https://e.example/{token}', 'q')
        seen[token] = (it['reason'], it['error_msg'])
    assert len({v[0] for v in seen.values()}) == 3, seen
    assert len({v[1] for v in seen.values()}) == 3, seen


def test_library_without_diag_param_still_fetches():
    """Older tofu-search has no `diag` kwarg — that must not become a failure."""
    from lib.tasks_pkg.handlers.search import _core

    def _legacy_fetch(url, max_chars=None, pdf_max_chars=None):
        return 'real page text ' * 20

    with mock.patch.object(_core, '_facade_mod',
                           return_value=mock.Mock(fetch_page_content=_legacy_fetch)), \
         mock.patch.object(_core, 'filter_web_content', side_effect=lambda c, *a, **k: c):
        item = _core._fetch_url_one('https://ok.example/p', 'q')

    assert item['page_content'], 'legacy signature must still return content'
    assert item['reason'] == 'extracted_ok'
