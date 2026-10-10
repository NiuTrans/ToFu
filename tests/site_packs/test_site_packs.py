"""Site pack engine: loader layering, hot reload, interpreter behavior.

The interpreter tests run pipelines against a recording fake page and pin
the exact BrowserPage call sequence the built-in adapters used to make —
this is the equivalence proof for the xiaohongshu migration.
"""

import json
from pathlib import Path

import pytest

from lib.site_packs import load_packs, reset_cache_for_tests
from lib.site_packs._interpreter import (
    operation_capabilities,
    operation_handler,
    render_template,
)
from lib.site_packs._loader import _layer_directories
from lib.site_packs._manifest import PackAuth, PackManifest, PackOperation

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_loader_cache():
    reset_cache_for_tests()
    yield
    reset_cache_for_tests()


def _write_pack(directory: Path, pack_id: str, **overrides) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    doc = {
        'pack': 'tofu.site-pack/v1',
        'id': pack_id,
        'name': pack_id.title(),
        'domains': [f'{pack_id}.example.com'],
        'operations': [{
            'name': 'search',
            'description': 'demo',
            'steps': [{'do': 'navigate', 'url': 'https://example.com/'}],
        }],
    }
    doc.update(overrides)
    path = directory / f'{pack_id}.json'
    path.write_text(json.dumps(doc), encoding='utf-8')
    return path


def test_user_layer_overrides_core_and_invalid_files_are_skipped(
        tmp_path, monkeypatch):
    core = tmp_path / 'core'
    bundle = tmp_path / 'bundles' / 'acme' / 'site_packs'
    user = tmp_path / 'user'
    _write_pack(core, 'demo', version=1)
    _write_pack(bundle, 'demo', version=2)
    _write_pack(user, 'demo', version=3)
    (user / 'broken.json').write_text('{not json', encoding='utf-8')

    monkeypatch.setattr('lib.site_packs._loader._CORE_DIR', core)
    monkeypatch.setattr('lib.site_packs._loader._BUNDLES_DIR',
                        tmp_path / 'bundles')
    monkeypatch.setattr('lib.site_packs._loader.config_path',
                        lambda *parts: str(user))

    packs = load_packs()
    assert packs['demo'].version == 3
    assert packs['demo'].source_path.endswith('demo.json')


def test_hot_reload_picks_up_edits_without_restart(tmp_path, monkeypatch):
    core = tmp_path / 'core'
    _write_pack(core, 'demo', version=1)
    monkeypatch.setattr('lib.site_packs._loader._CORE_DIR', core)
    monkeypatch.setattr('lib.site_packs._loader._BUNDLES_DIR',
                        tmp_path / 'nope')
    monkeypatch.setattr('lib.site_packs._loader.config_path',
                        lambda *parts: str(tmp_path / 'user'))

    assert load_packs()['demo'].version == 1
    rewritten = _write_pack(core, 'demo', version=2)
    # Network filesystems may quantize mtimes; make the edit visible to the
    # loader signature regardless of tick granularity.
    import os
    stat = rewritten.stat()
    os.utime(rewritten, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))
    assert load_packs()['demo'].version == 2


def test_pack_auth_catalog_projection(tmp_path, monkeypatch):
    core = tmp_path / 'core'
    _write_pack(core, 'demo', auth={
        'login_url': 'https://demo.example.com/login',
        'access_strategy': 'cookies_replay',
        'fields': [{'name': 'sid', 'importance': 'required'}],
    })
    monkeypatch.setattr('lib.site_packs._loader._CORE_DIR', core)
    monkeypatch.setattr('lib.site_packs._loader._BUNDLES_DIR',
                        tmp_path / 'nope')
    monkeypatch.setattr('lib.site_packs._loader.config_path',
                        lambda *parts: str(tmp_path / 'user'))
    from lib.site_packs import pack_auth_catalog
    (row,) = pack_auth_catalog()
    assert row['domain'] == 'demo.example.com'
    assert row['access_strategy'] == 'cookies_replay'
    assert row['fields'] == [{'name': 'sid', 'importance': 'required'}]


# ── Interpreter ─────────────────────────────────────────────────────

class _FakePage:
    """Records the BrowserPage calls a pipeline makes."""

    def __init__(self):
        self.calls = []
        self.current_url = 'https://xiaohongshu.com/search_result?keyword=tofu'
        self.lease = type('Lease', (), {'owner_user_id': 'alice'})()

    def new_tab(self, url, **kw):
        self.calls.append(('new_tab', url))

    def wait(self, *, selector='', condition='visible', timeout=10, **kw):
        self.calls.append(('wait', selector, timeout))
        return {}

    def scroll(self, *, direction='down', amount=700, selector='',
               trusted_read=False, **kw):
        self.calls.append(('scroll', direction, amount, trusted_read))
        return {}

    def execute(self, expression, *, args=None, trusted_read=False, **kw):
        self.calls.append(('execute', args, trusted_read))
        return {'result': {'value': [{'title': 't', 'url': 'u'}]}}

    def fill(self, value, *, selector='', trusted_read=False, **kw):
        self.calls.append(('fill', value, selector, trusted_read))
        return {}

    def press(self, keys, *, selector='', trusted_read=False, **kw):
        self.calls.append(('press', keys, selector, trusted_read))
        return {}

    def click(self, *, selector='', trusted_read=False, **kw):
        self.calls.append(('click', selector, trusted_read))
        return {}


def _xhs_pack():
    packs = load_packs()
    assert 'xiaohongshu' in packs
    return packs['xiaohongshu']


def _operation(pack, name):
    return next(op for op in pack.operations if op.name == name)


def test_render_template_quotes_params_and_honors_raw():
    assert render_template('https://x/?q={query}', {'query': 'a b&c'}) \
        == 'https://x/?q=a%20b%26c'
    assert render_template('{url|raw}', {'url': 'https://x/y?z=1'}) \
        == 'https://x/y?z=1'


def test_xhs_search_pipeline_matches_legacy_call_sequence(monkeypatch):
    captured = {}

    def fake_read_search_records(page, query, limit, source_id, source_name,
                                 *, candidates=None, payload=None):
        captured.update(query=query, limit=limit, source_id=source_id,
                        source_name=source_name, candidates=candidates)
        return [{'title': 't', 'source': source_name}]

    monkeypatch.setattr('lib.site_packs._interpreter.read_search_records',
                        fake_read_search_records)
    pack = _xhs_pack()
    operation = _operation(pack, 'search')
    handler = operation_handler(pack, operation)
    page = _FakePage()
    result = handler(page, {'query': 'tofu 豆腐', 'limit': 10, 'pages': 3})

    assert result == [{'title': 't', 'source': 'Xiaohongshu'}]
    assert page.calls[0] == (
        'new_tab',
        'https://www.xiaohongshu.com/search_result?keyword='
        'tofu%20%E8%B1%86%E8%85%90&source=web_search_result_notes')
    assert ('wait', 'a[href*="/explore/"],a[href*="/search_result/"]', 12) \
        in page.calls
    scrolls = [c for c in page.calls if c[0] == 'scroll']
    assert scrolls == [('scroll', 'down', 1200, True)] * 2  # pages=3 → 2
    executes = [c for c in page.calls if c[0] == 'execute']
    assert executes == [('execute', None, True)]
    assert captured['query'] == 'tofu 豆腐'
    assert captured['limit'] == 10
    assert captured['source_id'] == 'xiaohongshu'
    assert captured['source_name'] == 'Xiaohongshu'
    assert captured['candidates'] == [{'title': 't', 'url': 'u'}]


def test_xhs_search_limit_clamps_to_pack_bound(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        'lib.site_packs._interpreter.read_search_records',
        lambda page, query, limit, source_id, source_name, **kw: captured
        .setdefault('limit', limit) or [])
    pack = _xhs_pack()
    handler = operation_handler(pack, _operation(pack, 'search'))
    handler(_FakePage(), {'query': 'tofu', 'limit': 99})
    assert captured['limit'] == 30  # legacy: max(1, min(30, limit))


def test_xhs_detail_pipeline_truncates_fields(monkeypatch):
    captured = {}

    def fake_read_detail(page, params, *, fields=None):
        captured['fields'] = fields
        return {'title': 't', 'fields': fields}

    monkeypatch.setattr('lib.site_packs._interpreter.read_detail',
                        fake_read_detail)

    class _DetailPage(_FakePage):
        def execute(self, expression, *, args=None, trusted_read=False, **kw):
            return {'result': {'value': {
                'author': 'a' * 600, 'likes': 7,
                'tags': ['x' * 200] * 40, 'ignored': 'y'}}}

    pack = _xhs_pack()
    handler = operation_handler(pack, _operation(pack, 'detail'))
    page = _DetailPage()
    result = handler(page, {'url': 'https://www.xiaohongshu.com/explore/abc'})

    assert page.calls[0] == ('new_tab', 'https://www.xiaohongshu.com/explore/abc')
    fields = captured['fields']
    assert fields['author'] == 'a' * 512
    assert fields['likes'] == '7'
    assert fields['tags'] == ['x' * 128] * 30
    assert set(fields) == {'author', 'published_at', 'likes', 'comments',
                           'tags'}
    assert result['title'] == 't'


def test_capability_derivation_from_steps():
    operation = PackOperation(
        name='search', description='d', access='read',
        input_schema={'type': 'object'}, output_schema={'type': 'object'},
        session='ephemeral', window_mode='background', timeout=30,
        min_interval_s=0, cooldown_s=0,
        steps=(
            {'do': 'navigate', 'url': 'https://x/'},
            {'do': 'wait', 'selector': 'a'},
            {'do': 'paginate', 'mode': 'scroll', 'times_from_param': 'pages',
             'max_times': 4},
            {'do': 'extract', 'js': '1', 'into': 'candidates'},
            {'do': 'normalize', 'kind': 'search_records',
             'query_from_param': 'q', 'limit_from_param': 'l',
             'source_name': 'X'},
        ))
    assert sorted(operation_capabilities(operation)) == sorted(
        ('tabs', 'wait', 'scroll', 'execute', 'read', 'snapshot'))


def test_optional_wait_degrades_to_debug_log():
    class _TimeoutPage(_FakePage):
        def wait(self, **kw):
            raise TimeoutError('not found')

    pack = _xhs_pack()
    handler = operation_handler(pack, _operation(pack, 'search'))
    page = _TimeoutPage()
    # Must not raise: the optional wait only logs.
    import lib.site_packs._interpreter as interp
    import lib.browser.read_pipeline as pipeline
    # bypass the acceptance tail
    try:
        handler(page, {'query': 'tofu'})
    except Exception as exc:  # only the normalize tail may fail, not wait
        assert 'not found' not in str(exc)


def test_ensure_auth_raises_login_required_off_domain():
    from lib.browser.adapter_errors import AdapterExecutionError

    pack = _xhs_pack()
    handler = operation_handler(pack, _operation(pack, 'search'))
    page = _FakePage()
    page.current_url = 'https://sso.example.com/login'
    with pytest.raises(AdapterExecutionError) as exc:
        handler(page, {'query': 'tofu'})
    assert exc.value.code == 'login_required'
    assert exc.value.details['login_url'].endswith('/explore')


def test_adapter_registry_projects_pack_and_resolves_alias():
    from lib.browser.adapters import get_adapter

    adapter = get_adapter('xhs')
    assert adapter is not None and adapter.id == 'xiaohongshu'
    assert adapter.builtin is False
    search = adapter.command('search')
    assert search.min_interval_s == 12 and search.cooldown_s == 300
    assert search.timeout == 35
    assert sorted(search.required_capabilities) == sorted(
        ('tabs', 'wait', 'scroll', 'execute', 'read', 'snapshot'))


# ── P4: mutation pipelines ─────────────────────────────────────

def _write_pack_manifest():
    from lib.site_packs._manifest import parse_manifest
    return parse_manifest({
        'pack': 'tofu.site-pack/v1',
        'id': 'intranet-editor',
        'name': 'Intranet Editor',
        'domains': ['wiki.internal.example'],
        'auth': {'login_url': 'https://wiki.internal.example/login'},
        'operations': [{
            'name': 'edit-page',
            'description': '编辑内网 wiki 页面标题并读回确认',
            'access': 'write',
            'input_schema': {
                'type': 'object',
                'properties': {'url': {'type': 'string'},
                               'title': {'type': 'string'}},
                'required': ['url', 'title'],
            },
            'steps': [
                {'do': 'navigate', 'url': '{url|raw}'},
                {'do': 'ensure_auth'},
                {'do': 'fill', 'value': '{title}', 'selector': '#title'},
                {'do': 'click', 'selector': '#save'},
                # Read-back verify: extract AFTER the mutation, then accept.
                {'do': 'extract',
                 'js': '(() => document.querySelector("#title")?.value || "")()',
                 'into': 'fields'},
                {'do': 'normalize', 'kind': 'detail',
                 'fields': {'author': 512}},
            ],
        }],
    }, source='test')


class _WritePage(_FakePage):
    def __init__(self):
        super().__init__()
        self.current_url = 'https://wiki.internal.example/page/1'

    def read(self, *, max_chars=60000, **kw):
        return {'result': {'url': self.current_url},
                'page': {'url': self.current_url}}


def test_write_operation_mutations_are_not_trusted_reads(monkeypatch):
    monkeypatch.setattr(
        'lib.site_packs._interpreter.read_detail',
        lambda page, params, *, fields=None: {'fields': fields})
    pack = _write_pack_manifest()
    operation = pack.operations[0]
    handler = operation_handler(pack, operation)
    page = _WritePage()
    handler(page, {'url': 'https://wiki.internal.example/page/1',
                   'title': '新标题'})

    assert ('fill', '新标题', '#title', False) in page.calls
    assert ('click', '#save', False) in page.calls
    # Read-back: extraction runs after the mutation completes.
    kinds = [c[0] for c in page.calls]
    assert kinds.index('execute') > kinds.index('click')


def test_write_operation_derives_mutation_capabilities():
    pack = _write_pack_manifest()
    caps = operation_capabilities(pack.operations[0])
    assert 'click' in caps and 'fill' in caps


def test_write_pack_projects_write_access_command():
    from lib.browser.adapters import AdapterCommand, AdapterValidationError
    from lib.site_packs._projection import pack_to_adapter
    adapter = pack_to_adapter(_write_pack_manifest())
    command = adapter.command('edit-page')
    assert command.access == 'write'
    # The same command shape is what invoke_adapter gates through
    # require_access(access='write') before the handler ever runs.
    with pytest.raises(AdapterValidationError):
        AdapterCommand('edit-page', 'bad', access='mutate')
