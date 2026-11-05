"""Diagnostic evidence comes from the same accepted responses as page content."""

import json
from copy import deepcopy

import pytest

pytestmark = pytest.mark.unit

PAGE = 'https://site.test/search'
ENDPOINT = 'https://site.test/api/data'


@pytest.fixture(autouse=True)
def policy(monkeypatch):
    monkeypatch.setattr('lib.browser.access.is_read_allowed',
                        lambda owner, url: str(owner) == '41' and 'denied.test' not in url)


def response(title='coffee guide', *, url=ENDPOINT, status=200):
    return {'url': url, 'status': status, 'contentType': 'application/json',
            'responsePreview': json.dumps({'data': {'items': [{
                'id': 'record', 'title': title, 'description': 'A practical guide',
                'url': '/articles/coffee', 'accessToken': 'never-visible',
            }]}})}


def payload(*rows):
    return {'url': PAGE, 'text': 'Search', 'network': {'responses': list(rows)}}


@pytest.mark.parametrize('entry', ['analysis', 'render', 'content'])
def test_public_research_projection_parses_each_response_once(monkeypatch, entry):
    from lib.browser.research import analyze_research_payload, render_research_payload

    row = response()
    source = payload(row)
    original_loads = json.loads
    parsed = []
    def loads(value, *args, **kwargs):
        if value == row['responsePreview']:
            parsed.append(value)
        return original_loads(value, *args, **kwargs)
    monkeypatch.setattr(json, 'loads', loads)
    target = {'kind': 'list', 'query': 'coffee'}
    if entry == 'analysis':
        result = analyze_research_payload(source, owner_user_id='41', target=target)
        assert result['network']['candidate_count'] == 1
    else:
        result = render_research_payload(source, owner_user_id='41', target=target,
                                         mode='content' if entry == 'content' else 'both')
        assert 'coffee guide' in result and 'never-visible' not in result
    assert len(parsed) == 1


def test_same_endpoint_does_not_admit_an_unrelated_response():
    from lib.browser.network_evidence import analyze_network_evidence

    report = analyze_network_evidence(payload(response(), response('unrelated heading')),
                                      owner_user_id='41', target={'kind': 'list', 'query': 'coffee'})
    assert report['candidate_count'] == 1
    assert report['candidates'][0]['observations'] == 1
    assert 'accessToken' not in json.dumps(report)


def test_capture_counts_include_unrelated_and_denied_rows():
    from lib.browser.network_evidence import analyze_network_evidence

    report = analyze_network_evidence(payload(
        response(), response('unrelated heading', url='https://site.test/api/other'),
        response(url='https://denied.test/private')),
        owner_user_id='41', target={'kind': 'list', 'query': 'coffee'})
    assert report['capture']['response_count'] == 3
    assert report['denied_response_count'] == 1
    assert report['candidate_count'] == 1


def test_research_reports_authentication_failure_without_claiming_accepted_data():
    from lib.browser.research import analyze_research_payload

    source = payload(response(), response(url=ENDPOINT + '?query=coffee', status=401))
    source['research'] = {'stopReason': 'max_pages'}
    analysis = analyze_research_payload(source, owner_user_id='41',
                                        target={'kind': 'list', 'query': 'coffee'})
    assert analysis['strategy'] == 'token_gated_api'
    assert 'sign-in' in analysis['recommended_next_step']
    assert analysis['network']['capture']['response_count'] == 2
    assert analysis['network']['candidates'] == []


def test_content_only_skips_endpoint_and_anti_bot_diagnostics(monkeypatch):
    from lib.browser.research import render_research_payload

    def unexpected(*args, **kwargs):
        pytest.fail('Content-only rendering must not build research diagnostics')
    monkeypatch.setattr('lib.browser.network_evidence._infer_shape', unexpected)
    monkeypatch.setattr('lib.browser.research._anti_bot_report', unexpected)
    rendered = render_research_payload(payload(response()), owner_user_id='41',
                                       mode='content', target={'kind': 'list', 'query': 'coffee'})
    assert 'coffee guide' in rendered
    assert 'Collection diagnostics' not in rendered


def test_inventory_rechecks_current_response_permission(monkeypatch):
    from lib.browser.content import collect_content_and_network

    checks = []
    def allowed(owner, url):
        assert owner == '41'
        if url == ENDPOINT:
            checks.append(url)
            return len(checks) == 1
        return True
    monkeypatch.setattr('lib.browser.access.is_read_allowed', allowed)
    receipt, report = collect_content_and_network(
        payload(response()), owner_user_id='41',
        target={'kind': 'list', 'query': 'coffee'}, max_entries=12)
    assert receipt['records'][0]['title'] == 'coffee guide'
    assert report['candidates'] == []
    assert report['denied_response_count'] == 1


@pytest.mark.parametrize('entry', ['network', 'research'])
@pytest.mark.parametrize('owner,title', [('42', 'coffee guide'), ('41', 'unrelated')])
def test_prior_receipt_cannot_admit_a_new_owner_or_response(entry, owner, title):
    from lib.browser.content import collect_content
    from lib.browser.network_evidence import analyze_network_evidence
    from lib.browser.research import analyze_research_payload

    target = {'kind': 'list', 'query': 'coffee'}
    prior = collect_content(payload(response()), owner_user_id='41', target=target)
    assert prior['records']
    analyze = analyze_network_evidence if entry == 'network' else analyze_research_payload
    report = analyze(payload(response(title)), owner_user_id=owner,
                     target=target, content_receipt=prior)
    if entry == 'research':
        report = report['network']
    assert report['candidates'] == []


@pytest.mark.parametrize('legacy_transport', [False, True])
def test_shared_projection_keeps_input_and_partial_receipt_intact(monkeypatch, legacy_transport):
    from lib.browser import network_evidence
    from lib.browser.content import collect_content, collect_content_and_network

    row = response()
    row['bodyTruncated'] = True
    source = payload(row)
    if legacy_transport:
        source['networkResponses'] = source.pop('network')['responses']
    before = deepcopy(source)
    target = {'kind': 'list', 'query': 'coffee'}
    expected = collect_content(source, owner_user_id='41', target=target)
    counts = []
    original = network_evidence._shape_counts
    def shape_counts(body):
        counts.append(body)
        return original(body)
    monkeypatch.setattr(network_evidence, '_shape_counts', shape_counts)
    receipt, report = collect_content_and_network(
        source, owner_user_id='41', target=target, max_entries=12)
    assert source == before
    assert receipt == expected
    assert receipt.evidence_json == expected.evidence_json
    assert receipt['status'] == 'partial'
    assert report['capture']['response_count'] == 1
    assert report['candidates'][0]['body_truncated'] is True
    assert len(counts) == 1
    assert 'never-visible' not in json.dumps(counts)


def test_research_handler_collects_one_owner_device_capture_once(monkeypatch):
    from lib.browser.research import research_page
    from lib.browser.tool_runtime import BrowserToolRuntime

    row = response()
    source = payload(row)
    monkeypatch.setattr('lib.browser.protocol.require_capabilities', lambda *a, **k: {})
    monkeypatch.setattr('lib.browser.site_observations.load_site_observation', lambda *a, **k: None)
    observations = []
    monkeypatch.setattr('lib.browser.site_observations.record_site_observation',
                        lambda *a, **k: observations.append((a, k)))
    requests, parsed = [], []
    original_loads = json.loads
    def loads(value, *args, **kwargs):
        if value == row['responsePreview']:
            parsed.append(value)
        return original_loads(value, *args, **kwargs)
    monkeypatch.setattr(json, 'loads', loads)
    def sender(command, params=None, timeout=30, **route):
        requests.append((command, route))
        return source, None
    runtime = BrowserToolRuntime(owner_user_id='41', client_id='research-device', sender=sender)
    rendered = research_page({'url': PAGE, 'target': {'kind': 'list', 'query': 'coffee'}}, runtime)
    assert requests == [('research_url', {'client_id': 'research-device', 'owner_user_id': '41'})]
    assert len(parsed) == 1
    assert observations[0][0] == ('41', PAGE)
    assert observations[0][1]['outcome'] == 'success'
    assert 'coffee guide' in rendered and 'Collection diagnostics' in rendered
    assert 'never-visible' not in rendered
