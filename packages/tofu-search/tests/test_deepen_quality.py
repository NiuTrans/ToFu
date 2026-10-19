"""One-hop search expansion preserves original scope and timing authority."""

import threading
import time

import pytest

from tofu_search.search import deepen

pytestmark = pytest.mark.unit


def parent(links, relevance=None):
    result = {'url': 'https://vendor.test/docs/index', 'title': 'S5M2 Firmware',
              'full_content': 'S5M2 Firmware information.\n\n--- Page Links ---\n' +
              '\n'.join(f'- [{anchor}]({url})' for anchor, url in links)}
    if relevance:
        result['relevance'] = relevance
    return result


@pytest.mark.parametrize('scope,url,allowed', [
    ('site:vendor.test', 'https://vendor.test/s5m2', True),
    ('site:vendor.test', 'https://docs.vendor.test/s5m2', True),
    ('site:vendor.test', 'https://vendor.test.evil.test/s5m2', False),
    ('site:vendor.test/docs', 'https://vendor.test/docs/s5m2', True),
    ('site:vendor.test/docs', 'https://vendor.test/docs-evil/s5m2', False),
    ('site:vendor.test -site:private.vendor.test', 'https://private.vendor.test/s5m2', False),
])
def test_deep_links_enforce_site_before_fetch(monkeypatch, scope, url, allowed):
    fetched = []
    def fetch(target, **kw):
        fetched.append(target)
        return 'S5M2 Firmware substantive version details. ' * 4
    monkeypatch.setattr(deepen, 'fetch_page_content', fetch)
    out = deepen.deepen_results(scope + ' "S5M2" Firmware',
        [parent([('S5M2 Firmware', url)])])
    assert bool(out) is allowed
    assert fetched == ([url] if allowed else [])


def test_site_word_and_domain_tokens_are_not_deep_subjects(monkeypatch):
    monkeypatch.setattr(deepen, 'fetch_page_content', lambda *a, **kw: pytest.fail('Unrelated link fetched'))
    out = deepen.deepen_results('site:vendor.test "S5M2" Firmware',
        [parent([('site vendor', 'https://vendor.test/site')])])
    assert out == []


@pytest.mark.parametrize('status', ['weak_match', 'unverified'])
def test_uncertain_parent_cannot_amplify_outbound_junk(monkeypatch, status):
    monkeypatch.setattr(deepen, 'fetch_page_content', lambda *a, **kw: pytest.fail('Uncertain parent crawled'))
    assert deepen.deepen_results('S5M2 Firmware',
        [parent([('S5M2 Firmware', 'https://vendor.test/s5m2')], status)]) == []


def test_expired_deadline_starts_no_fetch(monkeypatch):
    monkeypatch.setattr(deepen, 'fetch_page_content', lambda *a, **kw: pytest.fail('Expired fetch started'))
    assert deepen.deepen_results('S5M2 Firmware',
        [parent([('S5M2 Firmware', 'https://vendor.test/s5m2')])],
        deadline_ts=time.monotonic() - 1) == []


def test_deep_deadline_returns_without_joining_slow_loser(monkeypatch):
    release = threading.Event()
    finished = threading.Event()
    passed = []
    def fetch(url, **kw):
        passed.append(kw['deadline_secs'])
        if url.endswith('/slow'):
            try:
                release.wait(2)
            finally:
                finished.set()
        return 'S5M2 Firmware actual source version details. ' * 4
    monkeypatch.setattr(deepen, 'fetch_page_content', fetch)
    started = time.monotonic()
    try:
        results = deepen.deepen_results('S5M2 Firmware', [parent([
            ('S5M2 Firmware quick', 'https://vendor.test/quick'),
            ('S5M2 Firmware slow', 'https://vendor.test/slow')])],
            deadline_ts=started + 0.1)
        assert time.monotonic() - started < 0.7
        assert [r['url'] for r in results] == ['https://vendor.test/quick']
        assert all(0 < timeout <= 0.1 for timeout in passed)
    finally:
        release.set()
        assert finished.wait(1)
    # Late completion is not allowed to mutate the returned corpus.
    assert len(results) == 1
