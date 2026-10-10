"""Public search outcomes: relevance, bounded recovery and honest diagnostics."""
import pytest

from tests.skills.test_skill_online_catalog import _Response, _verification

pytestmark = pytest.mark.unit


def test_local_installed_bonus_cannot_create_a_match(monkeypatch):
    from lib.skills import catalog, registry
    from lib.skills.discovery import search_skill_catalog
    monkeypatch.setattr(catalog, 'get_catalog', lambda: [])
    monkeypatch.setattr(registry, 'list_skills', lambda *a, **kw: [
        {'id': 'friday', 'name': 'Friday', 'description': 'Calendar helper'},
        {'id': 'frida-tools', 'name': 'Frida tools',
         'description': 'Android dynamic instrumentation'},
    ])
    assert search_skill_catalog('jadx') == []
    assert [row['skill_id'] for row in search_skill_catalog('frida')] == ['frida-tools']
    assert search_skill_catalog(
        'Android reverse engineering Frida jadx dynamic instrumentation')[0]['skill_id'] == 'frida-tools'


def test_online_filters_prefix_noise_before_verification():
    from lib.skills.online_catalog import search_online_skills
    verified = []

    def getter(url, **kwargs):
        if url.endswith('/search'):
            return _Response({'results': [
                {'slug': 'friday', 'ownerHandle': 'alice', 'summary': 'Calendar'},
                {'slug': 'frida-tools', 'ownerHandle': 'alice'},
            ]}, url=url)
        slug = url.split('/')[-2]
        verified.append(slug)
        return _Response(_verification(slug=slug), url=url)

    result = search_online_skills('frida', http_get_fn=getter)
    assert verified == ['frida-tools']
    assert result['online']['relevance_rejected_count'] == 1
    assert result['catalog'][0]['source_revision'] == '1.2.3'


def test_long_query_recovers_with_bounded_original_terms():
    from lib.skills.online_catalog import search_online_skills
    queries = []

    def getter(url, **kwargs):
        if url.endswith('/search'):
            query = kwargs['params']['q']
            queries.append(query)
            rows = [{'slug': 'frida', 'ownerHandle': 'alice'}] if query == 'frida' else []
            return _Response({'results': rows}, url=url)
        return _Response(_verification(slug='frida'), url=url)

    query = 'Android reverse engineering Frida jadx dynamic instrumentation'
    result = search_online_skills(query, http_get_fn=getter)
    assert queries == [query.lower(), 'android', 'frida']
    assert len(result['catalog']) == 1
    assert result['online']['queries'] == queries


def test_verification_failure_is_not_silent_no_match():
    from lib.skills.online_catalog import search_online_skills
    from lib.skills.discovery import render_skill_search

    def getter(url, **kwargs):
        if url.endswith('/search'):
            return _Response({'results': [{'slug': 'frida', 'ownerHandle': 'alice'}]}, url=url)
        return _Response(b'limited', url=url, status=429,
                         headers={'Retry-After': '17'})

    result = search_online_skills('frida', include_unverified=False, http_get_fn=getter)
    assert result['catalog'] == []
    assert result['online']['verification_errors'] == ['online_rate_limited']
    rendered = render_skill_search('frida', [], online_status=result['online'])
    assert 'online_rate_limited' in rendered
    assert 'retry_after=17s' in rendered
    assert 'unavailable_count=1' in rendered


def test_disabled_discovery_is_explicit(monkeypatch):
    from lib.skills.online_catalog import search_online_skills
    from lib.skills.discovery import render_skill_search
    monkeypatch.setenv('TOFU_SKILLS_ONLINE_DISCOVERY', '0')
    result = search_online_skills('frida', http_get_fn=lambda *a, **kw: pytest.fail('network'))
    assert 'online_discovery_disabled' in render_skill_search(
        'frida', [], online_status=result['online'])


@pytest.mark.parametrize('matches,online,badge', [
    ([], {'attempted': True, 'ok': True}, '🔎 no matches'),
    ([], {'attempted': True, 'ok': False}, '⚠️ partial'),
    ([{'name': 'Frida', 'installed': True, 'skill_id': 'frida'}],
     {'attempted': True, 'ok': True}, '🔎 1 matches'),
])
def test_handler_reports_search_outcome(monkeypatch, matches, online, badge):
    import threading
    from lib.skills import discovery
    from lib.tasks_pkg.handlers.skills import _handle_skill_tool
    monkeypatch.setattr(discovery, 'search_skills', lambda *a, **kw: {
        'matches': matches, 'online': online})
    task = {'id': 'skill-search-test', '_userId': 1, 'toolRounds': [],
            'events': [], 'events_lock': threading.Lock(), '_suppressEvents': True}
    entry = {'query': 'frida'}
    _handle_skill_tool(task, None, 'search_skills', 'tc1', {'query': 'frida'},
                       0, entry, {}, None, False)
    assert entry['status'] == 'done'
    assert entry['results'][0]['badge'] == badge


def test_search_rate_limit_never_fans_out():
    from lib.skills.online_catalog import search_online_skills
    calls = []

    def getter(url, **kwargs):
        calls.append(url)
        return _Response(b'limited', url=url, status=429)

    result = search_online_skills(
        'Android reverse engineering Frida jadx', http_get_fn=getter)
    assert len(calls) == 1
    assert result['online']['error'] == 'online_rate_limited'


def test_verifies_tail_candidate_without_exceeding_twelve():
    from lib.skills.online_catalog import search_online_skills
    verified = []

    def getter(url, **kwargs):
        if url.endswith('/search'):
            return _Response({'results': [
                {'slug': f'frida-{i}', 'ownerHandle': 'alice'}
                for i in range(20)]}, url=url)
        slug = url.split('/')[-2]
        verified.append(slug)
        return _Response(_verification(slug=slug, ok=(slug == 'frida-11')), url=url)

    result = search_online_skills('frida', limit=1, include_unverified=False,
                                  http_get_fn=getter)
    assert len(verified) == 12
    assert result['catalog'][0]['catalog_id'] == 'clawhub.alice.frida-11'


def test_transient_verification_uses_short_failure_cache(monkeypatch):
    from lib.skills import online_catalog as online
    from lib.ttl_cache import TTLCache
    monkeypatch.setattr(online, '_SEARCH_CACHE', TTLCache(300, max_size=64))
    monkeypatch.setattr(online, '_VERIFY_CACHE', TTLCache(300, max_size=256))
    monkeypatch.setattr(online, '_FAILURE_CACHE', TTLCache(30, max_size=32))
    checks = []

    def getter(url, **kwargs):
        if url.endswith('/search'):
            return _Response({'results': [{'slug': 'frida', 'ownerHandle': 'alice'}]}, url=url)
        checks.append(url)
        if len(checks) == 1:
            return _Response(b'limited', url=url, status=429)
        return _Response(_verification(slug='frida'), url=url)

    monkeypatch.setattr(online, 'http_get', getter)
    assert online.search_online_skills('frida', include_unverified=False)['catalog'] == []
    assert online.search_online_skills('frida', include_unverified=False)['catalog'] == []
    assert len(checks) == 1
    online._FAILURE_CACHE.clear()  # Simulate short failure expiry, not success-cache expiry.
    assert online.search_online_skills('frida', include_unverified=False)['catalog']
    assert len(checks) == 2
