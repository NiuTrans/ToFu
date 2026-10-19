"""Durable source bindings, dependency changes and explicit owner corrections."""
import pytest

from tests.artifacts.test_artifact_review import artifact as artifact, storage as storage, turn_service, PRINCIPAL, requirements
from lib.artifacts import core
from lib.memory.working_facts import repository, service, sources

pytestmark = pytest.mark.unit


@pytest.fixture
def facts(artifact, storage, monkeypatch):
    monkeypatch.setattr(repository, 'get_storage_client', lambda write=False: storage.client)
    return artifact


def query(artifact, **kwargs):
    from lib.memory.working_facts.contract import validate
    return validate(service.query_facts(artifact['conv_id'], principal=PRINCIPAL, **kwargs), 'WorkingFactsView')


def record(artifact, text='# Report 1', kind='observation', evidence=None, **changes):
    evidence = evidence or [{'kind': 'artifact', 'id': artifact['id'],
                            'version': artifact['content_sha256'], 'quote': '# Report 1'}]
    command = {'action': 'record', 'revision': query(artifact)['revision'],
               'text': text, 'kind': kind, 'sources': evidence, **changes}
    return service.record_fact(artifact['conv_id'], command, principal=PRINCIPAL, task_id='producer')


def owner(artifact, fact, action, **changes):
    return service.owner_decision(artifact['conv_id'], {'action': action, 'id': fact['id'],
        'revision': query(artifact)['revision'], 'note': 'Checked against source evidence', **changes}, principal=PRINCIPAL)


def dependency(fact):
    return {'kind': 'fact', 'id': fact['id'], 'version': fact['revision']}


def test_observation_needs_exact_quote_inference_needs_owner_review(facts):
    with pytest.raises(ValueError, match='observation'):
        record(facts, text='The report is correct')
    with pytest.raises(ValueError, match='quote'):
        record(facts, evidence=[{'kind': 'artifact', 'id': facts['id'],
               'version': facts['content_sha256'], 'quote': 'invented evidence'}])
    observed = record(facts)['facts'][0]
    assert observed['status'] == 'observed'
    inferred = record(facts, text='The report is suitable for publication', kind='derived')['facts'][0]
    assert inferred['status'] == 'needs_review'
    confirmed = owner(facts, inferred, 'confirm')['facts'][0]
    assert confirmed['status'] == 'owner_asserted'
    assert confirmed['sources'] == inferred['sources']
    with pytest.raises(PermissionError):
        record(facts, id=confirmed['id'])


def test_user_correction_invalidates_transitive_dependents_only(facts):
    a = record(facts)['facts'][0]
    b = record(facts, evidence=[dependency(a)])['facts'][0]
    c = record(facts, evidence=[dependency(b)])['facts'][0]
    unrelated = record(facts)['facts'][0]
    owner(facts, a, 'correct', text='The customer needs Report 2')
    result = {item['id']: item for item in query(facts)['facts']}
    assert result[a['id']]['status'] == 'owner_asserted'
    assert result[b['id']]['status'] == result[c['id']]['status'] == 'stale'
    assert result[unrelated['id']]['status'] == 'observed'
    assert result[b['id']]['text'] == result[c['id']]['text'] == '# Report 1'
    with pytest.raises(PermissionError):
        record(facts, id=a['id'])


def test_requirement_change_preserves_independent_observations(facts):
    brief = requirements(facts)
    criterion = brief['criteria'][0]
    a = record(facts, text=criterion['text'], evidence=[{'kind': 'criterion', 'id': facts['id'],
        'criterionId': criterion['id'], 'version': criterion['digest'], 'quote': criterion['text']}])['facts'][0]
    b = record(facts, text='Use ledger totals in the delivery', kind='derived', evidence=[dependency(a)])['facts'][0]
    owner(facts, b, 'confirm')
    independent = record(facts)['facts'][0]
    requirements(facts, objective='Changed by the owner')
    statuses = {item['id']: item['status'] for item in query(facts)['facts']}
    assert statuses[a['id']] == statuses[b['id']] == 'stale'
    assert statuses[independent['id']] == 'observed'


def test_cycles_stale_edits_and_unknown_versions_are_refused(facts):
    a = record(facts)['facts'][0]
    b = record(facts, evidence=[dependency(a)])['facts'][0]
    with pytest.raises(ValueError, match='acyclic'):
        record(facts, id=a['id'], evidence=[dependency(b)])
    with pytest.raises(repository.FactConflict):
        record(facts, revision=0)
    with pytest.raises(repository.FactConflict):
        record(facts, evidence=[{**dependency(a), 'version': 999}])
    assert len(query(facts)['facts']) == 2


def test_restart_preserves_bindings_and_query_does_not_reread_bodies(facts, storage, monkeypatch):
    record(facts)
    before = query(facts)
    storage.stop(); storage.start()
    def forbidden(*args, **kwargs):
        raise AssertionError('A retained source must be checked through immutable metadata')
    monkeypatch.setattr(sources, 'get_artifact', forbidden)
    for _ in range(3):
        assert query(facts) == before


def test_foreign_sources_and_deleted_sources_never_become_current(facts, storage):
    foreign = core.create_artifact(conv_id='different-conversation', content='# Report 1',
                                   format='markdown', source='write_file')
    with pytest.raises(repository.FactConflict):
        record(facts, evidence=[{'kind': 'artifact', 'id': foreign['id'],
               'version': foreign['content_sha256'], 'quote': '# Report 1'}])
    fact = record(facts)['facts'][0]
    storage.client.command('artifact.delete', {'artifact_id': facts['id'], 'deleted_at': 123}, 'remove-source')
    result = query(facts)['facts'][0]
    assert result['status'] == 'unavailable'
    assert result['text'] == fact['text']
    with pytest.raises(repository.FactConflict):
        owner(facts, fact, 'confirm')


def test_lost_ack_and_uncommitted_failure_preserve_truthful_revision(facts, storage, monkeypatch):
    from lib.storage import StorageError
    original = storage.client.command
    def lost_ack(*args, **kwargs):
        original(*args, **kwargs)
        raise StorageError('database_timeout', 'Injected lost ACK')
    monkeypatch.setattr(storage.client, 'command', lost_ack)
    assert record(facts)['revision'] == 1
    def before_commit(*args, **kwargs):
        raise StorageError('database_unavailable', 'Injected before commit')
    monkeypatch.setattr(storage.client, 'command', before_commit)
    with pytest.raises(StorageError):
        record(facts)
    assert query(facts)['revision'] == 1


def test_owner_retraction_cannot_be_undone_by_agent_and_stales_dependents(facts):
    a = record(facts)['facts'][0]
    b = record(facts, evidence=[dependency(a)])['facts'][0]
    owner(facts, a, 'retract')
    states = {row['id']: row['status'] for row in query(facts)['facts']}
    assert states[a['id']] == 'withdrawn' and states[b['id']] == 'stale'
    with pytest.raises(PermissionError):
        record(facts, id=a['id'])
    with pytest.raises(repository.FactConflict):
        owner(facts, a, 'confirm')
    assert owner(facts, a, 'correct', text='Restored explicitly by the owner')['facts'][0]['active']
