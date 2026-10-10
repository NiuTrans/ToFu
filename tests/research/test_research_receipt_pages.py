"""Unfinished receipt pages retain exact facts across bounds, faults and restarts."""
from copy import deepcopy

import pytest

from lib.research.receipt_pages import ReceiptPages, NAMESPACE, PAGE_BYTES, encoded, digest, empty_reference
from lib.research.program import _normalize_tool_receipts
from lib.storage.errors import StorageError
from tests.research.test_research_action_observations import MemoryRecords
from tests.research.test_research_workspace_evidence_guard import storage as storage

pytestmark = pytest.mark.unit


def repository(client, **overrides):
    arguments = dict(user_id=1, paper_hash='a' * 32, lang='en', run_id='long-experiment',
                     hypothesis_digest='b' * 64, protocol_digest='c' * 64)
    return ReceiptPages(client, **(arguments | overrides))


def fact(n, **overrides):
    return _normalize_tool_receipts([dict(tool='poll', call_id=f'call-{n}', source_task_id=f'task-{n}',
        status='done', result_digest=f'digest-{n}', result_excerpt='{}', observed_at=n,
        execution_observation={'state': 'running', 'job_id': 'job', 'provider_scope': 'provider:cluster'},
        ) | overrides])[0]


def restore_chronological(repo, reference):
    pages = list(repo.iter_pages(reference))
    return [receipt for page in reversed(pages) for receipt in page['receipts']]


def test_streamed_pages_keep_complete_history_and_old_heads():
    client = MemoryRecords(); repo = repository(client)
    first = repo.prepare(empty_reference(), (fact(n) for n in range(70)))
    final = repo.prepare(first, (fact(n) for n in range(70, 1000)))
    assert final['count'] == 1000 and first['count'] == 70
    assert restore_chronological(repo, final) == [fact(n) for n in range(1000)]
    assert restore_chronological(repo, first) == [fact(n) for n in range(70)]
    assert all(len(page['receipts']) <= 32 and len(encoded(page)) <= PAGE_BYTES for page in repo.iter_pages(final))
    assert repo.prepare(first, (fact(n) for n in range(70, 1000))) == final


def test_unicode_payloads_use_encoded_byte_boundary_without_losing_text():
    repo = repository(MemoryRecords())
    rows = [fact(n, result_excerpt='𐀀' * 4000, observation_excerpt='𐀀' * 4000) for n in range(20)]
    reference = repo.prepare(empty_reference(), iter(rows))
    pages = list(repo.iter_pages(reference))
    assert len(pages) > 1 and all(len(encoded(page)) <= PAGE_BYTES for page in pages)
    assert restore_chronological(repo, reference) == rows


def test_lost_ack_and_orphan_preparation_do_not_change_prior_reference():
    client = MemoryRecords(); client.lose_ack = True; repo = repository(client)
    prior = empty_reference(); candidate = repo.prepare(prior, [fact(1)])
    assert prior == empty_reference() and list(repo.iter_pages(prior)) == []
    assert repo.prepare(prior, [fact(1)]) == candidate
    assert repo.read_page(candidate)['receipts'] == [fact(1)]


@pytest.mark.parametrize('override', [dict(user_id=2), dict(paper_hash='d' * 32), dict(lang='zh'),
    dict(run_id='another'), dict(hypothesis_digest='d' * 64), dict(protocol_digest='d' * 64)])
def test_pages_cannot_cross_owner_run_or_scientific_binding(override):
    client = MemoryRecords(); reference = repository(client).prepare(empty_reference(), [fact(1)])
    with pytest.raises(StorageError, match='missing or invalid'):
        repository(client, **override).read_page(reference)


@pytest.mark.parametrize('mutation', ['hash', 'version', 'count', 'lineage'])
def test_corrupt_page_fails_closed(mutation):
    client = MemoryRecords(); repo = repository(client)
    reference = repo.prepare(empty_reference(), [fact(1)])
    row = client.rows[(NAMESPACE, repo.prefix + reference['head'])]
    if mutation == 'hash': row['value']['receipts'][0]['result_digest'] = 'changed'
    elif mutation == 'version': row['version'] = True
    else:
        changed = deepcopy(row['value'])
        if mutation == 'count': changed['count'] = True
        else: changed['previous'] = {'head': 'f' * 64, 'count': 2}
        reference = dict(head=digest(changed), count=1)
        client.rows[(NAMESPACE, repo.prefix + reference['head'])] = dict(version=1, value=changed)
    with pytest.raises(StorageError, match='missing or invalid'): repo.read_page(reference)


def test_missing_ancestor_is_detected_during_bounded_iteration():
    client = MemoryRecords(); repo = repository(client)
    first = repo.prepare(empty_reference(), [fact(1)])
    second = repo.prepare(first, [fact(2)])
    del client.rows[(NAMESPACE, repo.prefix + first['head'])]
    pages = repo.iter_pages(second)
    assert next(pages)['receipts'] == [fact(2)]
    with pytest.raises(StorageError, match='missing or invalid'): next(pages)


@pytest.mark.parametrize('reference', [dict(head='', count=1), dict(head='a'*64, count=0),
    dict(head='', count=0.0), dict(head='', count=False), dict(head='', count=0, unexpected=True)])
def test_invalid_page_references_are_rejected(reference):
    with pytest.raises(ValueError): repository(MemoryRecords()).prepare(reference, [])


def test_success_shaped_ack_without_persisted_page_is_rejected():
    client = MemoryRecords(); client.command = lambda *args: {'version': 1}
    with pytest.raises(StorageError, match='missing or invalid'):
        repository(client).prepare(empty_reference(), [fact(1)])


def test_refused_write_does_not_produce_an_authoritative_reference():
    client = MemoryRecords()
    def refuse(*args): raise StorageError('database_unavailable', 'refused')
    client.command = refuse
    with pytest.raises(StorageError, match='refused'):
        repository(client).prepare(empty_reference(), [fact(1)])
    assert not client.rows


def test_noncanonical_input_cannot_be_silently_projected():
    with pytest.raises(ValueError, match='canonical complete'):
        repository(MemoryRecords()).prepare(empty_reference(), [dict(fact(1), unexpected='lost')])


def test_pages_survive_selected_storage_restart(storage):
    supervisor, _ = storage
    repo = repository(supervisor.client)
    first = repo.prepare(empty_reference(), (fact(n) for n in range(90)))
    supervisor.stop(); supervisor.start()
    reopened = repository(supervisor.client)
    final = reopened.prepare(first, (fact(n) for n in range(90, 130)))
    assert restore_chronological(reopened, final) == [fact(n) for n in range(130)]
    assert restore_chronological(reopened, first) == [fact(n) for n in range(90)]
