"""Douyin core pack: shipped pipeline shape and acceptance through read_pipeline.

Extraction JavaScript runs browser-side; these tests pin the server-side
pipeline (navigation, waits, trusted reads, field budgets) and prove that the
site's own result cards survive acceptance. Fixtures are synthetic.
"""
from types import SimpleNamespace

import pytest

from lib.site_packs import load_packs, reset_cache_for_tests
from lib.site_packs._interpreter import operation_handler

pytestmark = pytest.mark.unit

SEARCH_URL = 'https://www.douyin.com/search/%E6%B9%96%E6%99%AF%20%E6%B0%91%E5%AE%BF?type=general'


@pytest.fixture(autouse=True)
def _core_only(tmp_path, monkeypatch):
    reset_cache_for_tests()
    monkeypatch.setattr('lib.site_packs._loader._BUNDLES_DIR', tmp_path / 'bundles')
    monkeypatch.setattr('lib.site_packs._loader.config_path', lambda *parts: str(tmp_path / 'user'))
    monkeypatch.setattr('lib.browser.access.is_read_allowed', lambda owner, url: str(owner) == '41')
    yield
    reset_cache_for_tests()


def _pack():
    return load_packs()['douyin']


def _operation(name):
    pack = _pack()
    return pack, next(op for op in pack.operations if op.name == name)


class _Page:
    def __init__(self, url, value, *, records=()):
        self.calls = []
        self.current_url = url
        self.value = value
        self.lease = SimpleNamespace(owner_user_id='41')
        self.records = list(records)

    def new_tab(self, url, **kw):
        self.calls.append(('new_tab', url))

    def wait(self, *, selector='', condition='visible', timeout=10, **kw):
        self.calls.append(('wait', selector, timeout))
        return {}

    def execute(self, expression, *, args=None, trusted_read=False, **kw):
        self.calls.append(('execute', trusted_read))
        return {'result': {'value': self.value}}

    def read(self, max_chars=60000):
        text = 'Search results ' + ' '.join(r['title'] for r in self.records)
        return {'result': {'url': self.current_url, 'title': '', 'text': text,
                           'contentSnapshot': {'text': text, 'records': self.records}},
                'page': {'url': self.current_url}}


def test_pack_is_core_and_declares_read_only_operations():
    pack = _pack()
    assert pack.domains == ('douyin.com',)
    assert {op.name: op.access for op in pack.operations} == {'search': 'read', 'detail': 'read'}


def test_search_returns_site_ranked_cards_for_a_compound_query():
    cards = [{'id': '101', 'title': '半山湖景小院实拍', 'snippet': '@房东 · 5天前 · 赞 13 · 视频',
              'url': 'https://www.douyin.com/video/101'},
             {'id': '102', 'title': '周末住进花园', 'snippet': '@旅人 · 9月22日 · 图文',
              'url': 'https://www.douyin.com/note/102'}]
    page = _Page(SEARCH_URL, cards, records=[{'title': '精选', 'url': '/jingxuan'}])
    pack, operation = _operation('search')
    rows = operation_handler(pack, operation)(page, {'query': '湖景 民宿', 'limit': 5})

    assert page.calls[0] == ('new_tab', SEARCH_URL)
    assert ('wait', '[id^="waterfall_item_"]', 12) in page.calls
    assert ('execute', True) in page.calls
    assert [r['title'] for r in rows] == ['半山湖景小院实拍', '周末住进花园']
    assert rows[1]['url'] == 'https://www.douyin.com/note/102'
    assert rows[0]['fields']['id'] == '101'
    assert rows[0]['source'] == 'Douyin' and rows[0]['metadata']['adapter'] == 'douyin'


def test_search_limit_refuses_before_browser_effects(monkeypatch):
    from lib.browser import adapters
    from lib.browser.adapter_errors import AdapterValidationError
    from lib.site_packs._projection import pack_to_adapter
    adapter = pack_to_adapter(_pack())
    monkeypatch.setattr(adapters, 'get_adapter', lambda name: adapter)
    monkeypatch.setattr(adapters, 'acquire_browser_lease',
                        lambda **kw: pytest.fail('invalid search acquired a page'))
    with pytest.raises(AdapterValidationError):
        adapters.invoke_adapter(adapter.id, 'search', {'query': '民宿', 'limit': 31},
                                owner_user_id='41', client_id='device')


def test_detail_bounds_every_declared_field(monkeypatch):
    captured = {}
    monkeypatch.setattr('lib.site_packs._interpreter.read_detail',
                        lambda page, params, *, fields=None: captured.update(fields=fields) or {'title': ''})
    raw = {'kind': 'video', 'author': 'a' * 300, 'desc': 'd' * 5000, 'likes': 29,
           'hashtags': ['#' + 'x' * 200] * 40, 'chapters': ['00:07 抵达'] * 40,
           'top_comments': ['c' * 2000] * 30, 'undeclared': 'dropped'}
    page = _Page('https://www.douyin.com/video/101', raw)
    pack, operation = _operation('detail')
    operation_handler(pack, operation)(page, {'url': 'https://www.douyin.com/video/101'})

    fields = captured['fields']
    assert page.calls[0] == ('new_tab', 'https://www.douyin.com/video/101')
    assert ('wait', '[data-e2e="user-info"]', 12) in page.calls
    assert fields['author'] == 'a' * 256 and fields['desc'] == 'd' * 4096 and fields['likes'] == '29'
    assert fields['hashtags'] == ['#' + 'x' * 127] * 30
    assert len(fields['chapters']) == 30 and len(fields['top_comments']) == 20
    assert all(len(c) == 1000 for c in fields['top_comments'])
    assert 'undeclared' not in fields and fields['comments'] == ''
