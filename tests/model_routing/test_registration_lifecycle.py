"""Repository admission, routing exclusion and bounded sourced research."""
from copy import deepcopy
import time

import pytest

from lib.model_routing import InMemoryModelRoutingRepository, OwnerBoundary, ModelRef, NativeModelSelection, compile_candidates
from lib.model_routing.domain import ModelRoutingError
from lib.model_catalog import registration_research as research
from tests.model_routing.test_model_routing_contract import _full_document
from tests.support.model_registration import complete_registration

pytestmark = pytest.mark.unit


def test_repository_cannot_bypass_admission_and_draft_has_no_routes():
    repo, owner = InMemoryModelRoutingRepository(), OwnerBoundary.create(401)
    installed = repo.compare_and_swap(owner, _full_document(), expected_revision=0)
    assert all(m['registration_state'] == 'pending_metadata' for m in installed.document['models'])
    selection = NativeModelSelection(ModelRef('creator', 'alpha'), None, '')
    assert compile_candidates(installed.document, selection) == []
    forged = deepcopy(installed.document)
    forged['models'][0]['registration_state'] = 'ready'
    with pytest.raises(ModelRoutingError, match='Cannot activate incomplete'):
        repo.compare_and_swap(owner, forged, expected_revision=installed.revision)
    assert repo.get(owner).revision == installed.revision
    completed = complete_registration(installed.document)
    ready = repo.compare_and_swap(owner, completed, expected_revision=installed.revision)
    assert compile_candidates(ready.document, selection)


def test_invalid_refresh_keeps_last_good_and_explicit_disable_is_respected():
    repo, owner = InMemoryModelRoutingRepository(), OwnerBoundary.create(402)
    ready = repo.compare_and_swap(owner, complete_registration(_full_document()), expected_revision=0)
    broken = deepcopy(ready.document)
    broken['models'][0].pop('registration_metadata')
    with pytest.raises(ModelRoutingError):
        repo.compare_and_swap(owner, broken, expected_revision=ready.revision)
    assert repo.get(owner).document == ready.document
    disabled = deepcopy(ready.document)
    disabled['models'][0]['registration_state'] = 'disabled'
    installed = repo.compare_and_swap(owner, disabled, expected_revision=ready.revision)
    assert not compile_candidates(installed.document, NativeModelSelection(ModelRef('creator', 'alpha'), None, ''))


def research_fixture(monkeypatch):
    from tests.model_routing.test_model_registration_admission import document
    doc = document()
    for field in ('registration_metadata', 'list_pricing', 'inference_profile', 'registration_state'):
        doc['models'][0].pop(field)
    repo, owner = InMemoryModelRoutingRepository(), OwnerBoundary.create(403)
    repo.compare_and_swap(owner, doc, expected_revision=0)
    monkeypatch.setattr(research, '_official_domains', lambda *_: ['official.example'])
    text = 'Released 2026-09-10. Input USD 1, output USD 2 per million tokens. Reasoning is unsupported.'
    url = 'https://official.example/model'
    proposal = {'release_date': '2026-09-10', 'list_pricing': {'input': 1, 'output': 2, 'currency': 'USD', 'unit': 'per_million_tokens'},
                'inference_profile': {'reasoning': {'support': 'unsupported'}},
                'citations': [{'url': url, 'quote': text,
                    'fields': ['release_date', 'list_pricing', 'inference_profile']}]}
    search = lambda *a, **k: [{'url': url, 'full_content': text}]
    return repo, owner, search, proposal


def test_research_fills_only_verified_draft_then_activates_via_same_repository(monkeypatch):
    repo, owner, search, proposal = research_fixture(monkeypatch)
    report = research.run_registration_research(repo, owner, deadline=time.monotonic()+60,
        search=search, extract=lambda *a, **k: proposal)
    row = repo.get(owner).document['models'][0]
    assert report['ready'] == 1 and row['registration_state'] == 'ready'
    assert row['registration_metadata']['field_sources']['list_pricing'] == ['https://official.example/model']


@pytest.mark.parametrize('fault', ['fake_url', 'fake_quote', 'identity', 'partial'])
def test_unverified_research_never_activates_or_persists_partial_facts(monkeypatch, fault):
    repo, owner, search, proposal = research_fixture(monkeypatch)
    before = repo.get(owner)
    if fault == 'fake_url': proposal['citations'][0]['url'] = 'https://unfetched.example/model'
    if fault == 'fake_quote': proposal['citations'][0]['quote'] = 'Invented release and pricing'
    if fault == 'identity': proposal['model_id'] = 'different-model'
    if fault == 'partial': proposal.pop('list_pricing')
    report = research.run_registration_research(repo, owner, deadline=time.monotonic()+60,
        search=search, extract=lambda *a, **k: proposal)
    assert report['ready'] == 0
    assert repo.get(owner).document == before.document


def test_owner_edit_during_research_wins(monkeypatch):
    repo, owner, search, proposal = research_fixture(monkeypatch)
    def extract(*args, **kwargs):
        current = repo.get(owner)
        current.document['models'][0]['display_name'] = 'Owner edited'
        repo.compare_and_swap(owner, current.document, expected_revision=current.revision)
        return proposal
    report = research.run_registration_research(repo, owner, deadline=time.monotonic()+60, search=search, extract=extract)
    assert report['ready'] == 0
    assert repo.get(owner).document['models'][0]['display_name'] == 'Owner edited'


def test_research_deadline_and_process_flight_are_bounded(monkeypatch):
    repo, owner, _, _ = research_fixture(monkeypatch)
    def unexpected(*a, **k): pytest.fail('research exceeded budget')
    assert research.run_registration_research(repo, owner, deadline=0, search=unexpected)['researched'] == 0
    with research._flight:
        assert research.run_registration_research(repo, owner, deadline=time.monotonic()+60, search=unexpected)['status'] == 'busy'


def test_extraction_has_one_dispatch_attempt_and_explicit_owner(monkeypatch):
    import lib.llm_dispatch
    captured = {}
    def dispatch(messages, **kwargs):
        captured.update(kwargs)
        assert messages[0]['role'] == 'system'
        return '{}', {}
    monkeypatch.setattr(lib.llm_dispatch, 'dispatch_chat', dispatch)
    assert research._extract({'model_id': 'example'}, [], owner_user_id=412, seconds=10) == {}
    assert captured['max_retries'] == 1  # dispatch uses attempts, not extra retries
    assert captured['max_tokens'] == research.MAX_OUTPUT_TOKENS
    assert captured['owner_user_id'] == 412 and captured['timeout'] == 10
    assert captured['exclude_models'] == ['example']
    assert captured['defer_on_shared_contention'] is True


def test_last_wire_can_be_removed_without_fabricating_model_id():
    from lib.model_routing import normalize_document
    document = complete_registration(_full_document())
    document['deployments'] = []
    normalized = normalize_document(document)
    assert normalized['offerings'] and not normalized['deployments']
    assert not compile_candidates(normalized, NativeModelSelection(ModelRef('creator', 'alpha'), None, ''))


def test_daily_research_has_fixed_model_and_page_budgets(monkeypatch):
    repo, owner, _, _ = research_fixture(monkeypatch)
    current = repo.get(owner)
    seed = current.document['models'][0]
    current.document['models'] = [dict(deepcopy(seed), model_id=f'model-{chr(97+i)}', display_name=f'Model {chr(97+i)}') for i in range(10)]
    repo.compare_and_swap(owner, current.document, expected_revision=current.revision)
    calls = []
    def search(query, **kwargs):
        calls.append((query, kwargs))
        return []
    report = research.run_registration_research(repo, owner, deadline=time.monotonic()+60, search=search)
    assert report['researched'] == len(calls) == research.MAX_MODELS_PER_PASS, (report, [(r['model_id'], r.get('registration_state')) for r in repo.get(owner).document['models']])
    assert all(row[1]['seconds'] <= 15 for row in calls)


def test_unknown_provider_override_is_quarantined_even_when_model_is_ready():
    repo, owner = InMemoryModelRoutingRepository(), OwnerBoundary.create(413)
    document = complete_registration(_full_document())
    document['offerings'][0]['inference_profile'] = {'reasoning': {'support': 'unknown'}}
    installed = repo.compare_and_swap(owner, document, expected_revision=0)
    bad_ids = {row['deployment_id'] for row in installed.document['deployments'] if row.get('registration_issues')}
    assert bad_ids
    candidates = compile_candidates(installed.document, NativeModelSelection(ModelRef('creator', 'alpha'), None, ''))
    assert candidates and all(row.deployment['deployment_id'] not in bad_ids for row in candidates)
    forged = deepcopy(installed.document)
    for row in forged['deployments']: row.pop('registration_issues', None)
    updated = repo.compare_and_swap(owner, forged, expected_revision=installed.revision)
    assert {row['deployment_id'] for row in updated.document['deployments'] if row.get('registration_issues')} == bad_ids


def test_invalid_provider_update_preserves_working_revision():
    repo, owner = InMemoryModelRoutingRepository(), OwnerBoundary.create(414)
    current = repo.compare_and_swap(owner, complete_registration(_full_document()), expected_revision=0)
    draft = deepcopy(current.document)
    draft['offerings'][0]['inference_profile'] = {'reasoning': {'support': 'unknown'}}
    with pytest.raises(ModelRoutingError, match='Provider inference metadata'):
        repo.compare_and_swap(owner, draft, expected_revision=current.revision)
    assert repo.get(owner).document == current.document


def test_repeated_wire_names_share_bounded_compilation_work(monkeypatch):
    import lib.llm.reasoning
    doc = complete_registration(_full_document())
    seed = doc['deployments'][0]
    doc['deployments'] += [dict(seed, deployment_id=f'budget-{i}', wire_model_id=f'wire-{i}') for i in range(300)]
    original = lib.llm.reasoning.compile_reasoning
    calls = []
    def compile(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(lib.llm.reasoning, 'compile_reasoning', compile)
    repo = InMemoryModelRoutingRepository()
    installed = repo.compare_and_swap(OwnerBoundary.create(415), doc, expected_revision=0)
    assert len(installed.document['deployments']) >= 300
    assert len(calls) < 20


def test_research_preserves_known_profile_and_evidence(monkeypatch):
    repo, owner, search, proposal = research_fixture(monkeypatch)
    model = repo.get(owner).document['models'][0]
    model['registration_metadata'] = {'release_date': '2026-09-01', 'checked_at': '2026-09-18',
        'source_urls': ['https://official.example/release'],
        'field_sources': {'release_date': ['https://official.example/release']}}
    model['release_date'] = '2026-09-01'
    model['inference_profile'] = {'reasoning': {'support': 'unknown'}, 'sampling': {'temperature': {'support': 'unsupported'}}}
    proposal['inference_profile']['sampling'] = {'temperature': {'support': 'supported'}}
    evidence = [{'url': row['url'], 'text': row['full_content']} for row in search()]
    candidate = research._candidate(model, proposal, evidence)
    assert candidate['inference_profile']['sampling'] == model['inference_profile']['sampling']
    assert candidate['registration_metadata']['release_date'] == '2026-09-01'
    assert candidate['registration_metadata']['field_sources']['release_date'] == ['https://official.example/release']
    assert candidate['registration_metadata']['field_sources']['list_pricing'] == ['https://official.example/model']
