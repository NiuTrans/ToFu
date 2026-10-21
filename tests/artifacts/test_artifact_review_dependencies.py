"""Review evidence revalidates through the shared owned fact graph on both stores."""
import json
import statistics
import time

import pytest

from tests.memory.test_working_facts import (facts, artifact, storage, turn_service,
    PRINCIPAL, record, owner, dependency)
from tests.artifacts.test_artifact_review import read, requirements, decide
from lib.artifacts import core, review
from lib.memory.working_facts import repository, service, sources

pytestmark = pytest.mark.unit


def ref(fact):
    return {key: fact[key] for key in ('id', 'revision')}


def test_owned_snapshot_never_suppresses_existing_artifacts(facts, storage, turn_service):
    turn_service.create_turn_pair('hint-conv', command_id='hint-pair',
        input_projection={'content': 'Produce'}, config={'model': 'fixture'}, user_id=1,
        conversation_defaults={'allowCreate': True, 'title': 'Hint', 'settings': {}})
    payload = {'conversation_id': 'hint-conv', 'user_id': 1, 'turn_limit': 100,
               'include_artifact_hint': True}
    assert storage.client.query('turn.sync.snapshot', payload)['hasArtifacts'] is False
    artifact = core.create_artifact(conv_id='hint-conv', content='Output', format='markdown', source='write_file')
    snapshot = storage.client.query('turn.sync.snapshot', payload)
    assert snapshot.get('hasArtifacts') is not False
    core.delete_artifact(artifact['id'])
    assert storage.client.query('turn.sync.snapshot', payload).get('hasArtifacts') is not True
    without_hint = storage.client.query('turn.sync.snapshot', {**payload, 'include_artifact_hint': False})
    assert 'hasArtifacts' not in without_hint


def test_bound_fact_correction_invalidates_one_review_and_survives_restart(facts, storage):
    requirements(facts)
    first = record(facts)['facts'][0]
    unrelated = record(facts)['facts'][0]
    decide(facts, factBindings=[ref(first)])
    accepted = decide(facts, index=1, factBindings=[ref(unrelated)])
    assert accepted['status'] == 'accepted'
    owner(facts, first, 'correct', text='Corrected source total')
    changed = read(facts)
    assert [item['state'] for item in changed['criteria']] == ['stale', 'pass']
    evidence = changed['criteria'][0]
    assert evidence['evidence']['factBindings'][0]['text'] == '# Report 1'
    assert evidence['factDependencies'][0]['currentRevision'] == 2
    assert evidence['factDependencies'][0]['reasons'] == ['fact_revision_changed']
    with pytest.raises(review.ReviewConflict):
        decide(facts, factBindings=[ref(first)])
    storage.stop(); storage.start()
    assert read(facts) == changed
    current = service.query_facts(facts['conv_id'], principal=PRINCIPAL, ids=[first['id']])['facts'][0]
    assert decide(facts, factBindings=[ref(current)])['status'] == 'accepted'


def test_transitive_source_change_invalidates_unchanged_fact_revision(facts):
    requirements(facts)
    source = record(facts)['facts'][0]
    middle = record(facts, evidence=[dependency(source)])['facts'][0]
    final = record(facts, evidence=[dependency(middle)])['facts'][0]
    decide(facts, factBindings=[ref(final)]); decide(facts, index=1)
    owner(facts, source, 'retract')
    changed = read(facts)
    assert [item['state'] for item in changed['criteria']] == ['stale', 'pass']
    binding = changed['criteria'][0]['factDependencies'][0]
    assert binding['boundRevision'] == binding['currentRevision'] == 1
    assert binding['reasons'] == ['source_dependency_changed']


def test_physical_file_change_after_restart_invalidates_only_bound_review(facts, tmp_path, storage):
    from tests.memory.test_working_file_sources import read_source, save
    requirements(facts)
    task, _, source = read_source(facts, tmp_path)
    fact = save(facts, task, source)
    decide(facts, factBindings=[ref(fact)])
    accepted = decide(facts, index=1)
    storage.stop(); storage.start()
    task.pop('_working_file_sources')
    assert read(facts) == accepted
    (tmp_path / 'basis.txt').write_text('Budget: 200\nRegion: east\n')
    current = read(facts)
    assert [item['state'] for item in current['criteria']] == ['stale', 'pass']
    assert current['criteria'][0]['factDependencies'][0]['currentRevision'] == fact['revision']
    assert current['criteria'][0]['evidence']['factBindings'][0]['text'] == 'Budget: 100'


def test_same_owner_other_conversation_and_tenant_cannot_supply_a_binding(facts, turn_service):
    from lib.identity import PrincipalContext
    requirements(facts)
    fact = record(facts)['facts'][0]
    turn_service.create_turn_pair('other-review', command_id='other-review-pair',
        input_projection={'content': 'Separate task'}, config={'model': 'fixture'}, user_id=1,
        conversation_defaults={'allowCreate': True, 'title': 'Other', 'settings': {}})
    other = core.create_artifact(conv_id='other-review', content='# Other', format='markdown',
                                source='write_file', source_ref={'path': 'other.md'})
    requirements(other)
    with pytest.raises(review.ReviewConflict):
        decide(other, factBindings=[ref(fact)])
    tenant = PrincipalContext.user(subject_id='reviewer', owner_user_id=1, tenant_id='other-tenant')
    current = review.update_review(facts['id'], {'action': 'requirements', 'requirementsRevision': 0,
        'objective': 'Tenant scope', 'criteria': [{'id': 'a', 'text': 'Correct', 'kind': 'requirement'}]},
        principal=tenant)
    with pytest.raises(review.ReviewConflict):
        review.update_review(facts['id'], {'action': 'decision', 'requirementsRevision': 1,
            'decisionsRevision': 0, 'artifactDigest': current['artifactDigest'],
            'criterionId': 'a', 'criterionDigest': current['criteria'][0]['digest'],
            'verdict': 'pass', 'note': 'Foreign fact', 'factBindings': [ref(fact)]}, principal=tenant)


def test_criterion_source_reads_generations_without_recursive_acceptance(facts):
    view = requirements(facts)
    criterion = view['criteria'][0]
    fact = record(facts, text=criterion['text'], evidence=[{'kind': 'criterion',
        'id': facts['id'], 'criterionId': criterion['id'], 'version': criterion['digest'],
        'quote': criterion['text']}])['facts'][0]
    decide(facts, factBindings=[ref(fact)])
    assert decide(facts, index=1, factBindings=[ref(fact)])['status'] == 'accepted'
    criteria = [{key: item[key] for key in ('id', 'text', 'kind')} for item in view['criteria']]
    criteria[0]['text'] = 'New source totals'
    changed = requirements(facts, criteria=criteria)
    assert [item['state'] for item in changed['criteria']] == ['stale', 'stale']
    assert changed['criteria'][1]['factDependencies'][0]['status'] == 'changed'


def test_source_correction_racing_review_cas_cannot_publish_a_current_pass(facts, monkeypatch):
    requirements(facts)
    fact = record(facts)['facts'][0]
    original = review._Review.put
    def interleave(self, kind, value, revision):
        if kind == 'decisions':
            owner(facts, fact, 'correct', text='Changed during decision')
        return original(self, kind, value, revision)
    monkeypatch.setattr(review._Review, 'put', interleave)
    result = decide(facts, factBindings=[ref(fact)])
    assert result['criteria'][0]['state'] == 'stale'
    assert result['criteria'][0]['evidence']['factBindings'][0]['revision'] == 1


def test_derived_missing_duplicate_and_foreign_evidence_are_not_admitted(facts):
    requirements(facts)
    derived = record(facts, text='An inference', kind='derived')['facts'][0]
    for bindings in ([ref(derived)], [{'id': 'unknown-or-foreign', 'revision': 1}],
                     [{**ref(derived), 'revision': 99}]):
        with pytest.raises(review.ReviewConflict):
            decide(facts, factBindings=bindings)
    with pytest.raises(ValueError, match='unique'):
        decide(facts, factBindings=[ref(derived), ref(derived)])
    confirmed = owner(facts, derived, 'confirm')['facts'][0]
    result = decide(facts, factBindings=[ref(confirmed)])
    assert result['criteria'][0]['evidence']['factBindings'][0]['statusAtReview'] == 'owner_asserted'
    assert read(facts)['decisionsRevision'] == 1


def test_source_unavailable_preserves_original_review_and_propagates_store_failure(facts, monkeypatch):
    requirements(facts)
    source = core.create_artifact(conv_id=facts['conv_id'], content='Original ledger', format='markdown',
                                 source='write_file', source_ref={'path': 'ledger.md'})
    fact = record(facts, text='Original ledger', evidence=[{'kind': 'artifact', 'id': source['id'],
        'version': source['content_sha256'], 'quote': 'Original ledger'}])['facts'][0]
    prior = decide(facts, factBindings=[ref(fact)], verdict='fail')
    core.delete_artifact(source['id'])
    current = read(facts)
    assert current['status'] == 'pending'
    assert current['criteria'][0]['state'] == 'stale'
    assert current['criteria'][0]['evidence'] == prior['criteria'][0]['evidence']
    assert current['criteria'][0]['factDependencies'][0]['status'] == 'unavailable'
    def fail(_self):
        raise TimeoutError('Storage did not confirm current facts')
    monkeypatch.setattr(repository.FactRepository, 'read', fail)
    with pytest.raises(TimeoutError):
        read(facts)


def test_evidence_removal_is_explicit_and_system_cannot_attest(facts):
    requirements(facts)
    fact = record(facts)['facts'][0]
    decide(facts, factBindings=[ref(fact)])
    with pytest.raises(ValueError, match='Explicit'):
        decide(facts)
    assert decide(facts, factBindings=[])['criteria'][0]['factDependencies'] == []
    from lib.identity import PrincipalContext
    with pytest.raises(PermissionError):
        review.update_review(facts['id'], {'action': 'requirements', 'requirementsRevision': 1,
            'objective': 'Synthetic review', 'criteria': []}, principal=PrincipalContext.system(
                subject_id='virtual-reviewer', owner_user_id=1, scopes=['chat']))


def test_shared_dependency_reads_are_selected_and_coalesced(facts, monkeypatch, tmp_path):
    criteria = [{'id': str(index), 'text': f'Criterion {index}', 'kind': 'requirement'} for index in range(16)]
    requirements(facts, criteria=criteria)
    selected = record(facts)['facts'][0]
    # Independent records are not resolved merely because a review is opened.
    for _ in range(31):
        record(facts)
    reads = {'record': 0, 'source': 0}
    original_read, original_resolve = repository.FactRepository.read, sources.SourceResolver.resolve
    def count_read(self):
        reads['record'] += 1
        return original_read(self)
    def count_source(self, source, **kwargs):
        reads['source'] += 1
        return original_resolve(self, source, **kwargs)
    monkeypatch.setattr(repository.FactRepository, 'read', count_read)
    monkeypatch.setattr(sources.SourceResolver, 'resolve', count_source)
    read(facts)
    assert reads == {'record': 0, 'source': 0}
    for index in range(16):
        decide(facts, index=index, factBindings=[ref(selected)])
    samples = []
    for _ in range(10):
        reads.update(record=0, source=0)
        start = time.perf_counter(); result = read(facts); samples.append((time.perf_counter()-start)*1000)
        assert reads == {'record': 1, 'source': 1}
        assert result['status'] == 'accepted'
    measurement = {'criteria': 16, 'facts': 32, 'selectedFacts': 1, 'factReadsPerView': reads,
                   'medianMs': statistics.median(samples), 'maxMs': max(samples),
                   'responseBytes': len(json.dumps(result).encode())}
    (tmp_path / 'review-dependency-measurement.json').write_text(json.dumps(measurement, indent=2))
    print('review_dependency_measurement=' + json.dumps(measurement))
