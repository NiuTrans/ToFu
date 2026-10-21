"""Ordinary Turn requirement generations participate in the common fact graph."""
import pytest

from lib.acceptance import turns
from lib.memory.working_facts import repository, service
from lib.memory.working_facts.repository import FactConflict
from lib.storage import StorageError
from tests.artifacts.test_turn_review import (
    subject, storage, turn_service, configure, read, PRINCIPAL,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def fact_storage(storage, monkeypatch):
    monkeypatch.setattr(repository, 'get_storage_client', lambda write=False: storage.client)


def source(subject, index=1):
    item = read(subject)['criteria'][index]
    return {'kind': 'criterion', 'targetKind': 'turn', 'id': subject.turn_id,
        'criterionId': item['id'], 'version': item['digest'], 'quote': item['text']}


def query():
    return service.query_facts('turn-review', principal=PRINCIPAL)


def record(binding):
    return service.record_fact('turn-review', {'action': 'record', 'revision': query()['revision'],
        'kind': 'observation', 'text': binding['quote'], 'sources': [binding]},
        principal=PRINCIPAL, task_id='turn-review-task')['facts'][0]


def test_turn_requirement_change_invalidates_only_its_exact_fact_and_survives_restart(subject, storage):
    configure(subject)
    first, changed = record(source(subject, 0)), record(source(subject))
    criteria = [{key: row[key] for key in ('id', 'kind', 'text')} for row in read(subject)['criteria']]
    criteria[1]['text'] = 'Report totals include the revised tax rule'
    configure(subject, criteria=criteria)
    view = query()
    by_id = {row['id']: row for row in view['facts']}
    assert by_id[first['id']]['status'] == 'observed'
    assert by_id[changed['id']]['status'] == 'stale'
    assert by_id[changed['id']]['revision'] == changed['revision']
    assert by_id[changed['id']]['text'] == changed['text']
    storage.stop(); storage.start()
    assert query() == view


def test_turn_sources_use_requirement_port_once_without_recursive_review(subject, monkeypatch):
    configure(subject)
    record(source(subject, 0)); record(source(subject))
    calls = []
    original = turns.read_requirements
    def observe(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)
    monkeypatch.setattr(turns, 'read_requirements', observe)
    monkeypatch.setattr(turns, 'read_review', lambda *a, **kw: pytest.fail('Recursive owner review read'))
    assert all(row['status'] == 'observed' for row in query()['facts'])
    assert len(calls) == 1


@pytest.mark.parametrize('fault', ['quote', 'version', 'missing_turn', 'artifact_as_turn', 'missing_criterion', 'target_kind'])
def test_invalid_or_unavailable_turn_sources_never_enter_facts(subject, fault):
    configure(subject)
    binding = source(subject)
    if fault == 'quote': binding['quote'] = 'Invented requirement'
    elif fault == 'version': binding['version'] = '0'*64
    elif fault == 'missing_turn': binding['id'] = 'missing-turn'
    elif fault == 'artifact_as_turn': binding['id'] = subject.artifact['id']
    elif fault == 'missing_criterion': binding['criterionId'] = 'missing'
    elif fault == 'target_kind': binding['targetKind'] = 'guess'
    with pytest.raises((ValueError, FactConflict)):
        record(binding)
    assert query()['facts'] == []


def test_unavailable_requirement_store_is_not_a_current_or_missing_source(subject, monkeypatch):
    configure(subject); record(source(subject))
    def unavailable(*args, **kwargs):
        raise StorageError('database_busy', 'fixture requirement read unavailable')
    monkeypatch.setattr(turns, 'read_requirements', unavailable)
    with pytest.raises(StorageError): query()


@pytest.mark.parametrize('scope', ['human_turn', 'other_conversation', 'foreign_owner', 'other_tenant'])
def test_turn_source_cannot_cross_authority_scope(subject, scope):
    from lib.identity import PrincipalContext
    configure(subject)
    binding = source(subject)
    if scope == 'human_turn':
        binding['id'] = subject.input_id
    if scope == 'other_conversation':
        from tests.turn_lifecycle.test_turn_event_carried_task_event import _running_task
        pair, _ = _running_task(subject.service, None, 'other-conversation', 'other-task')
        binding['id'] = pair['turn']['turnId']
    if scope in {'human_turn', 'other_conversation'}:
        with pytest.raises((ValueError, FactConflict)):
            record(binding)
        assert query()['facts'] == []
    else:
        from lib.memory.working_facts.sources import SourceResolver
        outsider = PrincipalContext.user(subject_id='outsider', owner_user_id=8 if scope == 'foreign_owner' else 1,
            tenant_id='elsewhere' if scope == 'other_tenant' else None)
        resolver = SourceResolver('turn-review', outsider, {'facts': {}})
        state, _, quote = resolver.resolve(binding, capture=True)
        assert state in {'unavailable', 'stale'} and quote == ''


def test_removed_and_readded_turn_criterion_never_revives_the_original_fact(subject):
    original = configure(subject)
    fact = record(source(subject))
    criteria = [{key: row[key] for key in ('id', 'kind', 'text')} for row in original['criteria']]
    configure(subject, criteria=criteria[:1])
    assert query()['facts'][0]['status'] == 'stale'
    configure(subject, criteria=criteria)
    assert query()['facts'][0]['status'] == 'stale'
    assert query()['facts'][0]['revision'] == fact['revision']
