"""Full paged receipt sources enter consumption exactly once, including migration."""
import pytest

from lib.research.receipt_pages import ReceiptPages, receipt_reader, empty_reference as empty_pages, NAMESPACE as PAGE_NAMESPACE
from lib.research.source_index import prepare_source_index, empty_reference, legacy_archive_replay, NAMESPACE
from lib.research.history import ResearchHistoryRepository, empty_history
from lib.research.persistence import research_direction_hash
from lib.research.program import normalize_run_evidence
from lib.research.workspace import empty_workspace
from lib.storage.errors import StorageError
from tests.research.test_research_source_index import index, fact, DIRECTION
from tests.research.test_research_action_observations import MemoryRecords
from tests.research.test_research_workspace_evidence_guard import storage as storage

pytestmark = pytest.mark.unit


def completed(client, count=130, run_id='completed-paged'):
    pager = ReceiptPages(client, user_id=1, paper_hash=research_direction_hash(DIRECTION), lang='en',
        run_id=run_id, hypothesis_digest='b'*64, protocol_digest='c'*64)
    reference = pager.prepare(empty_pages(), (fact(n) for n in range(count-1)))
    run = normalize_run_evidence([dict(id=run_id, status='inconclusive', execution_status='completed',
        hypothesis_digest='b'*64, protocol_digest='c'*64, receipt_history=reference,
        tool_receipts=[fact(count-1)])])[0]
    reader = receipt_reader(client, user_id=1, paper_hash=research_direction_hash(DIRECTION), lang='en')
    archive = ResearchHistoryRepository(client, user_id=1, paper_hash=research_direction_hash(DIRECTION), lang='en')
    return run, reader, archive


def test_complete_paged_sources_are_consumed_once_without_changing_prior_root():
    client = MemoryRecords(); repo = index(client)
    run, reader, archive = completed(client)
    prior = empty_reference()
    prepared = prepare_source_index(repo, prior, archive, None, [run], [], receipt_reader=reader)
    assert prepared['count'] == 130
    assert repo.replay(prepared, (fact(n) for n in range(130)))
    assert not repo.replay(prior, (fact(n) for n in range(130)))
    with pytest.raises(ValueError, match='already consumed'):
        prepare_source_index(repo, prepared, archive, None, [dict(run, id='renamed')], [],
            receipt_reader=lambda renamed: reader(run))
    assert prepare_source_index(repo, prepared, archive, None, [run], [run], receipt_reader=reader) == prepared


def test_missing_complete_reader_cannot_silently_register_only_inline_tail():
    client = MemoryRecords(); repo = index(client); run, _, archive = completed(client)
    with pytest.raises(ValueError, match='complete receipt reader'):
        prepare_source_index(repo, empty_reference(), archive, None, [run], [])
    assert not any(namespace == NAMESPACE for namespace, _ in client.rows)


def test_corrupt_ancestor_rejects_consumption_before_first_prepared_source():
    client = MemoryRecords(); repo = index(client); run, reader, archive = completed(client)
    page_key = next(key for namespace, key in client.rows if namespace == PAGE_NAMESPACE)
    del client.rows[(PAGE_NAMESPACE, page_key)]
    with pytest.raises(StorageError, match='missing or invalid'):
        prepare_source_index(repo, empty_reference(), archive, None, [run], [], receipt_reader=reader)
    assert not any(namespace == NAMESPACE for namespace, _ in client.rows)


def test_legacy_paged_archive_backfills_all_sources_and_matches_early_receipt():
    client = MemoryRecords(); repo = index(client); run, reader, archive = completed(client)
    workspace = empty_workspace(DIRECTION)
    history = archive.prepare(empty_history(workspace), workspace, [run])
    assert legacy_archive_replay(archive, history, iter([fact(0)]), receipt_reader=reader)
    assert not legacy_archive_replay(archive, history, iter([]), receipt_reader=reader)
    assert not legacy_archive_replay(archive, history, iter([dict(fact(0), source_task_id='')]), receipt_reader=reader)
    prepared = prepare_source_index(repo, empty_reference(0), archive, history, [], [], receipt_reader=reader)
    assert prepared['count'] == 130 and repo.replay(prepared, iter([fact(0), fact(129)]))
    changed = dict(fact(0), result_digest='conflict')
    with pytest.raises(ValueError, match='source identity conflicts'):
        legacy_archive_replay(archive, history, iter([changed]), receipt_reader=reader)


def test_streamed_replay_does_not_treat_empty_generator_as_consumed_evidence():
    repo = index(MemoryRecords())
    assert not repo.replay(empty_reference(), iter([]))


def test_conflicting_sources_in_a_new_stream_cannot_produce_a_published_root():
    client = MemoryRecords(); repo = index(client); run, reader, archive = completed(client, count=2)
    run['tool_receipts'] = [dict(fact(0), result_digest='conflicting-terminal')]
    prior = empty_reference()
    with pytest.raises(ValueError, match='source identity conflicts'):
        prepare_source_index(repo, prior, archive, None, [run], [], receipt_reader=reader)
    assert prior == empty_reference() and not repo.replay(prior, [fact(0)])


def test_complete_paged_source_index_survives_selected_storage_restart(storage):
    supervisor, _ = storage; run, reader, archive = completed(supervisor.client)
    repo = index(supervisor.client)
    prepared = prepare_source_index(repo, empty_reference(), archive, None, [run], [], receipt_reader=reader)
    supervisor.stop(); supervisor.start()
    reopened = index(supervisor.client)
    assert prepared['count'] == 130 and reopened.replay(prepared, (fact(n) for n in range(130)))
    assert not reopened.replay(empty_reference(), iter([fact(0)]))
