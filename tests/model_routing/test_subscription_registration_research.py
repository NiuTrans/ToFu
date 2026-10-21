"""Subscription candidates become callable only after complete sourced admission."""
from copy import deepcopy
import time

import pytest

from lib.model_catalog import registration_research as research
from lib.model_routing import (
    InMemoryModelRoutingRepository, ModelRef, NativeModelSelection,
    OwnerBoundary, compile_candidates, empty_document, public_projection,
)
from tests.model_routing.test_model_routing_contract import _full_document

pytestmark = pytest.mark.unit


def test_search_activates_the_host_bridge_before_importing_tofu_search(monkeypatch):
    """Registration research must not import tofu-search raw.

    2026-10-21 boot: the research worker imported ``tofu_search`` directly, so
    the library kept its own defaults with NO proxy. On a host whose only
    egress is the configured pool every engine died on DNS
    (``0 final results ... via direct: Name or service not known``) while the
    rest of the app searched fine. The bridge install is what injects the
    proxy, the LLM filter and the fetch providers.
    """
    calls = []
    from lib.search import search_runtime
    from lib.model_catalog import registration_research as research

    monkeypatch.setattr(search_runtime, 'ensure_search_runtime',
                        lambda: calls.append('ensure'))

    captured = {}

    def fake_search(query, **kwargs):
        captured['query'] = query
        captured['kwargs'] = kwargs
        return [{'url': 'https://official.example/x', 'full_content': 'body'}]

    import tofu_search
    monkeypatch.setattr(tofu_search, 'search', fake_search)

    results = research._search('gpt-future-test official', seconds=12)

    assert calls == ['ensure'], 'the host bridge was not activated first'
    assert results and captured['query'] == 'gpt-future-test official'
    assert captured['kwargs']['search_deadline_secs'] <= 12


def pending_supply(monkeypatch):
    doc = _full_document()
    doc['models'] = []
    doc['creators'] = [{'creator_id': 'openai', 'name': 'OpenAI'}]
    for collection in ('providers', 'provider_accesses', 'connections', 'credentials', 'offerings', 'deployments'):
        doc[collection] = doc[collection][:1]
    offering = doc['offerings'][0]
    offering.pop('model')
    offering.update(identity_state='pending_identity', pending_model_id='gpt-future-test',
                    capabilities=['text'], context_window=32768,
                    inherited_model_fields=['context_window', 'capabilities'])
    doc['credentials'][0]['authorization']['models'] = []
    doc['deployments'][0].update(wire_model_id='gpt-future-test', identity_confidence='pending')
    repo, owner = InMemoryModelRoutingRepository(), OwnerBoundary.create(491)
    repo.compare_and_swap(owner, doc, expected_revision=0)
    monkeypatch.setattr(research, '_official_domains', lambda *_: ['official.example'])
    url = 'https://official.example/gpt-future-test'
    text = 'GPT Future Test released 2026-09-29. USD 2 input and 10 output per million. Text only, 1050000 context, reasoning unsupported.'
    proposal = {'release_date': '2026-09-29',
        'list_pricing': {'input': 2, 'output': 10, 'currency': 'USD', 'unit': 'per_million_tokens'},
        'capabilities': ['text'], 'context_window': 1050000,
        'inference_profile': {'reasoning': {'support': 'unsupported'}},
        'citations': [{'url': url, 'quote': text, 'fields': [
            'release_date', 'list_pricing', 'capabilities', 'context_window', 'inference_profile']}]}
    search = lambda *a, **k: [{'url': url, 'full_content': text}]
    return repo, owner, search, proposal


def run(repo, owner, search, proposal, **kwargs):
    return research.run_registration_research(repo, owner, deadline=time.monotonic()+60,
        search=search, extract=lambda *a, **k: deepcopy(proposal), **kwargs)


def test_pending_supply_is_researched_registered_bound_and_routable(monkeypatch):
    repo, owner, search, proposal = pending_supply(monkeypatch)
    before = repo.get(owner).document
    assert before['models'] == []
    report = run(repo, owner, search, proposal, provider_id='provider-a')
    after = repo.get(owner).document
    assert report['ready'] == 1
    model = after['models'][0]
    assert model['registration_state'] == 'ready'
    assert model['context_window'] == 1050000
    assert model['registration_metadata']['field_sources']['context_window'] == [proposal['citations'][0]['url']]
    assert after['offerings'][0]['identity_state'] == 'confirmed'
    assert after['offerings'][0]['context_window'] == 1050000
    assert after['offerings'][0]['actual_pricing'] == before['offerings'][0]['actual_pricing']
    assert after['deployments'][0]['probe_status'] == before['deployments'][0]['probe_status']
    assert compile_candidates(after, NativeModelSelection(ModelRef('openai', 'gpt-future-test'), None, ''))
    revision = repo.get(owner).revision
    def unexpected(*args, **kwargs):
        pytest.fail('a ready registration must not invoke research again')
    report = research.run_registration_research(repo, owner, deadline=time.monotonic()+60,
                                               search=unexpected, extract=unexpected)
    assert report['researched'] == 0 and repo.get(owner).revision == revision


@pytest.mark.parametrize('fault', ['context', 'capabilities', 'field_citation', 'fake_quote'])
def test_incomplete_supply_never_creates_a_registry_draft(monkeypatch, fault):
    repo, owner, search, proposal = pending_supply(monkeypatch)
    before = repo.get(owner)
    if fault == 'context':
        proposal.pop('context_window')
    elif fault == 'capabilities':
        proposal.pop('capabilities')
    elif fault == 'field_citation':
        proposal['citations'][0]['fields'].remove('context_window')
    else:
        proposal['citations'][0]['quote'] = 'An invented official model card'
    report = run(repo, owner, search, proposal)
    assert report['ready'] == 0 and report['unresolved'] == 1
    assert repo.get(owner).document == before.document


def test_disabled_or_other_provider_supply_is_not_researched(monkeypatch):
    repo, owner, search, proposal = pending_supply(monkeypatch)
    assert run(repo, owner, search, proposal, provider_id='provider-b')['researched'] == 0
    current = repo.get(owner)
    current.document['offerings'][0]['enabled'] = False
    repo.compare_and_swap(owner, current.document, expected_revision=current.revision)
    assert run(repo, owner, search, proposal)['researched'] == 0
    assert repo.get(owner).document['models'] == []


@pytest.mark.parametrize('change', ['delete', 'bind', 'cancel', 'deadline'])
def test_research_cannot_publish_after_supply_removal_owner_binding_or_cancellation(monkeypatch, change):
    repo, owner, search, proposal = pending_supply(monkeypatch)
    cancelled = [False]
    clock = [100.0]
    monkeypatch.setattr(research.time, 'monotonic', lambda: clock[0])
    def extract(*args, **kwargs):
        if change == 'cancel':
            cancelled[0] = True
        elif change == 'deadline':
            clock[0] = 200.0
        else:
            current = repo.get(owner)
            if change == 'delete':
                current.document['offerings'] = []
                current.document['deployments'] = []
            else:
                current.document['offerings'][0]['pending_model_id'] = 'gpt-owner-choice'
            repo.compare_and_swap(owner, current.document, expected_revision=current.revision)
        return proposal
    report = research.run_registration_research(repo, owner, deadline=160.0,
        search=search, extract=extract, cancelled=lambda: cancelled[0])
    assert report['ready'] == 0 and repo.get(owner).document['models'] == []


def test_exact_catalog_facts_activate_stored_draft_without_search(monkeypatch):
    from lib.model_routing.domain import normalize_document
    doc = empty_document()
    doc['creators'] = [{'creator_id': 'openai', 'name': 'OpenAI'}]
    doc['models'] = [{'creator_id': 'openai', 'model_id': 'gpt-6.1-sol',
        'display_name': 'My Sol', 'capabilities': ['text', 'vision', 'thinking'],
        'context_window': 1050000, 'quality_rank': 0, 'registration_state': 'pending_metadata'}]
    repo, owner = InMemoryModelRoutingRepository(), OwnerBoundary.create(492)
    # Emulate a previously stored draft read after installing new exact rules.
    repo.compare_and_swap(owner, doc, expected_revision=0)
    current = repo.get(owner)
    current.document['models'][0]['registration_state'] = 'pending_metadata'
    # In-memory repository deliberately exposes the same normalized read
    # seam as a persisted old revision, without bypassing production writes.
    from lib.model_routing.repository import StoredAuthority
    old = StoredAuthority(owner, current.revision, normalize_document(current.document), current.updated_at)
    original_get = repo.get
    monkeypatch.setattr(repo, 'get', lambda boundary: deepcopy(old) if original_get(boundary).revision == old.revision else original_get(boundary))
    def unexpected(*args, **kwargs):
        pytest.fail('exact maintained facts must run before a search/LLM call')
    report = research.run_registration_research(repo, owner, deadline=time.monotonic()+60,
                                               search=unexpected, extract=unexpected)
    assert report['ready'] == 1 and report['researched'] == 0
    assert repo.get(owner).document['models'][0]['display_name'] == 'My Sol'
    assert repo.get(owner).document['models'][0]['registration_state'] == 'ready'


def test_openai_documentation_hosts_and_sol_reference_price_are_exact():
    assert 'developers.openai.com' in research._official_domains(empty_document(), {'creator_id': 'openai'})
    from lib.provider.provider_template_recipes import compile_provider_template_bundle
    bundle = compile_provider_template_bundle('openai', selected_model_ids=['gpt-6.1-sol'])
    doc = empty_document()
    doc['creators'] = bundle['creators']
    doc['models'] = bundle['models']
    model = public_projection(doc)['models'][0]
    assert model['release_date'] == '2026-09-29'
    assert model['context_window'] == 1050000
    assert model['official_pricing']['cache_read'] == 0.10
    reasoning = model['inference_profile']['reasoning']
    assert reasoning['can_disable'] is False
    assert reasoning['supported_efforts'] == ['low', 'medium', 'high', 'xhigh', 'max']
    assert research.registration_missing_fields(model) == []


@pytest.mark.parametrize('model_id', ['gpt-6.1-sol', 'gpt-future-test'])
def test_codex_catalog_import_uses_exact_registration_or_defers_to_research(monkeypatch, model_id):
    from lib.oauth import codex_catalog, outbound
    from lib.oauth import codex  # Install its refresh hooks before patching token I/O.
    assert codex is not None
    rows = codex_catalog._normalise_models({'models': [{
        'slug': model_id, 'visibility': 'list',
        'input_modalities': ['text', 'image'] if model_id == 'gpt-6.1-sol' else ['text'],
        'default_reasoning_level': 'medium',
        'supported_reasoning_levels': ([{'effort': x} for x in ['low', 'medium', 'high', 'xhigh', 'max']]
                                      if model_id == 'gpt-6.1-sol' else []),
    }]})
    monkeypatch.setattr(codex_catalog, 'cached_codex_provider_models', lambda: codex_catalog._provider_models(rows))
    repo, owner = InMemoryModelRoutingRepository(), OwnerBoundary.create(493)
    entry = outbound._managed_oauth_entry('codex', plan_type='test')
    assert outbound._upsert_oauth_provider(entry, owner_user_id=owner.owner_user_id,
                                         tenant_id=None, repository=repo)
    doc = repo.get(owner).document
    if model_id == 'gpt-6.1-sol':
        assert doc['models'][0]['registration_state'] == 'ready'
        selection = NativeModelSelection(ModelRef('openai', model_id), None, '')
        candidates = compile_candidates(doc, selection)
        assert candidates and not candidates[0].deployment.get('registration_issues')
    else:
        assert doc['models'] == []
        assert doc['offerings'][0]['identity_state'] == 'pending_identity'
        assert research._pending_supply_models(doc, provider_id='oauth_codex')[0]['context_window'] is None
