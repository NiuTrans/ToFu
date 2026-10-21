"""Executable site-capability boundaries; no live browser or account requests."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from lib.browser import adapters
from lib.browser.site_tool import site_tool

# The modelplaza site pack ships with bundles/meituan (internal builds only);
# these boundary tests name it explicitly, so the module skips when the pack
# is absent (opensource tree) instead of misreporting adapter behavior.
if not (Path(__file__).resolve().parents[2]
        / 'bundles' / 'meituan' / 'site_packs' / 'modelplaza.json').is_file():
    pytest.skip('modelplaza site pack is not shipped in this build',
                allow_module_level=True)

pytestmark = pytest.mark.unit


@pytest.fixture
def runtime(monkeypatch):
    monkeypatch.setattr('lib.browser.access.is_read_allowed', lambda owner, url: owner == '41')
    monkeypatch.setattr(adapters, 'adapter_health', lambda *a, **k: {'healthy': True})
    return SimpleNamespace(owner_user_id='41', client_id='device-a')


def test_discover_inspect_and_invoke_share_adapter_schema(runtime, monkeypatch):
    sources = json.loads(site_tool({}, runtime))['sources']
    assert any(row['site'] == 'modelplaza' for row in sources)
    schema = json.loads(site_tool({'site': 'modelplaza'}, runtime))
    command = next(c for c in schema['operations'] if c['name'] == 'search')
    assert command['input_schema'] == adapters.get_adapter('modelplaza').command('search').input_schema
    seen = []
    monkeypatch.setattr(adapters, 'invoke_adapter', lambda *a, **k: seen.append((a, k)) or {'ok': True, 'result': []})
    assert json.loads(site_tool({'site': 'modelplaza', 'operation': 'search', 'params': {'query': 'Claude'}}, runtime))['ok']
    assert seen[0][1] == {'owner_user_id': '41', 'client_id': 'device-a'}


@pytest.mark.parametrize('query', ['site:platform.claude.com/docs thinking', 'after:2026-01-01', 'intext:signature'])
def test_native_web_operators_rejected_before_any_lease(query, monkeypatch):
    monkeypatch.setattr(adapters, 'acquire_browser_lease', lambda **k: pytest.fail('navigated'))
    with pytest.raises(adapters.AdapterValidationError):
        adapters.invoke_adapter('xiaohongshu', 'search', {'query': query}, owner_user_id='41', client_id='device-a')


def test_schema_bounds_and_unknown_operation_fail_without_execution(monkeypatch):
    monkeypatch.setattr(adapters, 'acquire_browser_lease', lambda **k: pytest.fail('navigated'))
    for site, operation, params in [('modelplaza', 'unknown', {}), ('modelplaza', 'search', {'query': 'hi', 'freshness': 'day'}), ('xiaohongshu', 'search', {'query': 'coffee', 'pages': 99})]:
        with pytest.raises(adapters.AdapterValidationError):
            adapters.invoke_adapter(site, operation, params, owner_user_id='41', client_id='device-a')
    # A model name is a semantic contract, not an English-only regex.
    adapters._validate_params(adapters.get_adapter('modelplaza').command('search').input_schema, {'query': '通义千问'})


def test_denied_site_and_upgrade_are_typed(runtime, monkeypatch):
    from lib.browser.protocol import BrowserUpgradeRequired
    monkeypatch.setattr(adapters, 'invoke_adapter', lambda *a, **k: (_ for _ in ()).throw(BrowserUpgradeRequired(['read'])))
    result = site_tool({'site': 'modelplaza', 'operation': 'search', 'params': {'query': 'Claude'}}, runtime)
    assert result.error.code == 'browser_upgrade_required'
    runtime.owner_user_id = '42'
    assert site_tool({'site': 'modelplaza'}, runtime).error.code == 'browser_access_denied'


def test_site_output_never_slices_json(runtime, monkeypatch):
    monkeypatch.setattr(adapters, 'invoke_adapter', lambda *a, **k: {'ok': True, 'result': '"' * 90000})
    result = site_tool({'site': 'modelplaza', 'operation': 'search', 'params': {'query': 'Claude'}}, runtime)
    assert result.error.code == 'output_budget_exhausted'


def test_native_pacing_is_owner_scoped_expiring_without_route_count_quota(monkeypatch):
    from lib.browser import site_pacing as pacing
    monkeypatch.setattr(pacing, '_ROUTES', {})
    clock = [100.0]
    monkeypatch.setattr(pacing.time, 'monotonic', lambda: clock[0])
    ticket = pacing.admit('41', 'a', 'xhs', interval_s=12, cooldown_s=300)
    with pytest.raises(pacing.SiteReadThrottled):
        pacing.admit('41', 'a', 'xhs', interval_s=12, cooldown_s=300)
    pacing.admit('42', 'a', 'xhs', interval_s=12, cooldown_s=300)
    pacing.settle(ticket, blocked=True)
    clock[0] += 13
    with pytest.raises(pacing.SiteReadThrottled):
        pacing.admit('41', 'a', 'xhs', interval_s=12, cooldown_s=300)
    for owner in range(43, 343):
        pacing.admit(str(owner), 'a', 'xhs', interval_s=12, cooldown_s=300)
    assert len(pacing._ROUTES) == 302
    clock[0] += 901
    pacing.admit('43', 'a', 'xhs', interval_s=12, cooldown_s=300)
    assert len(pacing._ROUTES) == 1
    for operation in ('search', 'detail'):
        assert adapters.get_adapter('xhs').command(operation).min_interval_s == 12


def test_native_invoke_cannot_bypass_pacing(monkeypatch):
    from lib.browser import site_pacing as pacing
    monkeypatch.setattr(pacing, '_ROUTES', {})
    monkeypatch.setattr(adapters, 'audit_log', lambda *a, **k: None)
    monkeypatch.setattr(adapters, 'acquire_browser_lease', lambda **k: SimpleNamespace(owner_user_id='41', client_id='a'))
    class Page:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
    monkeypatch.setattr(adapters, 'BrowserPage', Page)
    seen = []
    adapter = adapters.SiteAdapter(id='paced-fixture', name='Fixture', domains=('example.com',), commands=(adapters.AdapterCommand('search', 'test', min_interval_s=12, cooldown_s=300, output_schema={'type': 'array'}, handler=lambda *a: seen.append(1) or []),))
    adapters.register_adapter(adapter)
    try:
        adapters.invoke_adapter(adapter.id, 'search', {}, owner_user_id='41', client_id='a')
        with pytest.raises(adapters.AdapterExecutionError) as exc:
            adapters.invoke_adapter(adapter.id, 'search', {}, owner_user_id='41', client_id='a')
        assert exc.value.code == 'rate_limited' and seen == [1]
    finally:
        adapters.unregister_adapter(adapter.id)


def test_incomplete_fields_do_not_train_successful_observations():
    from lib.browser.research import _content_observation_outcome
    receipt = {'status': 'partial', 'content': 'Real content', 'records': [], 'coverage': {'missing_fields': ['price'], 'stop_reason': 'observed_content'}}
    assert _content_observation_outcome(receipt, {'network': {}}, None) == 'not_observed'


def test_content_modes_preserve_target(runtime):
    from lib.browser.tool_runtime import BrowserToolRuntime
    from lib.browser.handlers._page import _handle_read_page
    payload = {'url': 'https://example.com/catalog', 'title': 'Catalog', 'contentSnapshot': {'text': 'Needle and unrelated item', 'records': [{'title': 'Needle', 'url': '/n'}, {'title': 'Unrelated', 'url': '/u'}]}}
    bridge = BrowserToolRuntime(owner_user_id='41', client_id='a', sender=lambda *a, **k: (payload, None))
    for mode in ('auto', 'text'):
        result = _handle_read_page({'tabId': 1, 'mode': mode, 'target': {'kind': 'list', 'query': 'Needle', 'fields': ['price']}}, bridge)
        assert 'Needle' in result and 'Unrelated' not in result and 'missing_fields' in result and 'price' in result


def test_host_provider_refusal_is_terminal_not_unavailable(monkeypatch):
    import tofu_search
    from lib.search.search_bridge import _ChatuiSiteSearchProvider
    monkeypatch.setattr(adapters, 'invoke_adapter', lambda *a, **k: (_ for _ in ()).throw(adapters.AdapterExecutionError('rate_limited', 'private-sentinel')))
    provider = _ChatuiSiteSearchProvider(user_id='41', client_id='a', bound=True)
    with pytest.raises(tofu_search.SiteSearchTerminalError) as exc:
        provider.search('xiaohongshu', 'coffee')
    assert 'private-sentinel' not in str(exc.value)


def test_typed_site_error_survives_task_display(monkeypatch):
    import lib.tasks_pkg.handlers.browser as handler
    from lib.tools.result_envelope import typed_tool_error
    error = typed_tool_error('rate_limited', retryable=True, next_action='Wait')
    monkeypatch.setattr('lib.browser.dispatch.execute_browser_tool', lambda *a, **k: error)
    finished = []
    monkeypatch.setattr(handler, '_finalize_tool_round', lambda *a, **k: finished.append(k))
    result = handler._handle_browser_tool({'_userId': '41'}, {}, 'browser_site', 'id', {}, 1, {}, {}, '', False)
    assert result[1] is error and finished[0]['status'] == 'error'


def test_site_approval_metadata_names_read_and_bounds_redacted_params():
    from lib.tasks_pkg.tool_dispatch._approval import _APPROVAL_META_ENRICHERS
    meta = {}
    _APPROVAL_META_ENRICHERS['browser_site'](meta, {
        'site': 'modelplaza', 'operation': 'search',
        'params': {'query': 'Claude', 'accessToken': 'private-sentinel', 'extra': 'x' * 90000},
    })
    encoded = json.dumps(meta)
    assert all(text in encoded for text in ('modelplaza', 'search', 'Claude'))
    assert 'private-sentinel' not in encoded and len(encoded) < 5000
    assert 'truncated' in encoded
