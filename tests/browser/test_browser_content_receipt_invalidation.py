"""Discarded or unusable browser content cannot claim an accepted source."""

import pytest

from lib.browser.content import collect_content
from lib.browser.network_evidence import analyze_network_evidence

pytestmark = pytest.mark.unit


@pytest.fixture
def source(monkeypatch):
    monkeypatch.setattr('lib.browser.access.is_read_allowed', lambda owner, url: owner == '41')
    return {'url': 'https://site.test/search', 'text': 'Search', 'network': {'responses': [{
        'url': 'https://site.test/api/data?query=coffee',
        'status': 200, 'contentType': 'application/json',
        'body': {'title': 'coffee guide', 'description': 'A practical guide'},
    }]}}


@pytest.mark.parametrize('transport_facts', [
    {}, {'research': {'stopReason': 'max_pages'}}, {'truncated': True},
])
def test_final_auth_failure_discards_content_and_its_acceptance_evidence(source, transport_facts):
    target = {'kind': 'list', 'query': 'coffee'}
    assert collect_content(source, owner_user_id='41', target=target)['records']
    source['network']['responses'].append({
        'url': 'https://site.test/api/data?query=coffee', 'status': 401,
    })
    source.update(transport_facts)
    receipt = collect_content(source, owner_user_id='41', target=target)
    assert receipt['status'] == 'blocked'
    assert receipt['coverage']['stop_reason'] == 'authentication_required'
    assert receipt['records'] == [] and receipt['content'] == ''
    assert receipt['evidence'] == []
    report = analyze_network_evidence(source, owner_user_id='41', target=target)
    assert report['candidates'] == []


def test_article_without_accepted_text_has_no_acceptance_evidence(source):
    source['network']['responses'][0]['body']['description'] = ''
    receipt = collect_content(source, owner_user_id='41',
                              target={'kind': 'article', 'query': 'coffee'})
    assert receipt['status'] == 'unavailable'
    assert receipt['content'] == '' and receipt['records'] == []
    assert receipt['evidence'] == []
