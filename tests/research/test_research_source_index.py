"""Published immutable source lookup survives archival, restart and orphan writes."""
from copy import deepcopy

import pytest

from lib.research.source_index import (ReceiptSourceIndex, NAMESPACE, empty_reference,
    receipt_key, receipt_digest, legacy_archive_replay)
from lib.research.persistence import research_direction_hash
from lib.research.history import ResearchHistoryRepository
from lib.research.action import apply_action_result
from lib.research.workspace import save_workspace, load_workspace
from tests.research.test_research_action_observations import MemoryRecords
from tests.research.test_research_history import draft_with_runs, DIRECTION
from tests.research.test_research_workspace_evidence_guard import storage as storage

pytestmark = pytest.mark.unit


def index(client, user_id=1, cache_entries=65):
    return ReceiptSourceIndex(client, user_id=user_id, paper_hash=research_direction_hash(DIRECTION),
                              lang='en', cache_entries=cache_entries)


def fact(number):
    return {'source_task_id': f'source-{number}', 'call_id': 'call', 'tool': 'run_command',
            'status': 'done', 'result_digest': f'digest-{number}', 'result_excerpt': '{}',
            'artifact_ref': '', 'observed_at': 1}


def test_many_sources_lookup_without_materializing_history_or_changing_old_roots():
    client = MemoryRecords(); repo = index(client); reference = empty_reference()
    first = None
    for number in range(1200):
        row = fact(number); reference = repo.insert(reference, receipt_key(row), receipt_digest(row))
        if number == 0: first = deepcopy(reference)
    assert reference['count'] == 1200 and len(repo.cache) <= 65
    reopened = index(client)
    queries = []
    original_query = client.query
    def query(operation, payload):
        queries.append(payload); return original_query(operation, payload)
    client.query = query
    for number in (0, 500, 1199):
        queries.clear()
        assert reopened.replay(reference, [fact(number)])
        assert len(queries) <= 65
    assert not reopened.replay(first, [fact(1199)])
    assert reopened.replay(first, [fact(0)])
    assert not reopened.replay(reference, [fact(9999)])


def test_orphan_preparation_and_lost_ack_do_not_publish_consumption():
    client = MemoryRecords(); client.lose_ack = True; repo = index(client); row = fact(1)
    candidate = repo.insert(empty_reference(), receipt_key(row), receipt_digest(row))
    assert not repo.replay(empty_reference(), [row])
    assert repo.replay(candidate, [row])
    assert repo.insert(candidate, receipt_key(row), receipt_digest(row)) == candidate


def test_conflicting_source_facts_are_refused():
    repo = index(MemoryRecords()); row = fact(1)
    reference = repo.insert(empty_reference(), receipt_key(row), receipt_digest(row))
    changed = dict(row, result_digest='changed')
    with pytest.raises(ValueError, match='source identity conflicts'): repo.replay(reference, [changed])
    with pytest.raises(ValueError): repo.insert(reference, receipt_key(changed), receipt_digest(changed))


@pytest.mark.parametrize('mutation', ['missing', 'corrupt', 'foreign'])
def test_invalid_source_nodes_fail_closed(mutation):
    client = MemoryRecords(); repo = index(client); row = fact(1)
    reference = repo.insert(empty_reference(), receipt_key(row), receipt_digest(row))
    if mutation == 'missing': client.rows.pop((NAMESPACE, repo.prefix + reference['root']))
    elif mutation == 'corrupt': client.rows[NAMESPACE, repo.prefix + reference['root']]['value']['facts'] = '0'*64
    reader = index(client, user_id=2 if mutation == 'foreign' else 1)
    from lib.storage.errors import StorageError
    with pytest.raises(StorageError): reader.replay(reference, [row])


def test_published_index_survives_selected_storage_restart_and_archival(storage):
    supervisor, _ = storage
    draft = draft_with_runs(24)
    for number, run in enumerate(draft['runs']):
        run['tool_receipts'][0]['source_task_id'] = f'source-{number}'
        run['tool_receipts'][0]['call_id'] = 'call'
    saved = save_workspace(DIRECTION, 'en', draft, expected_revision=0, user_id=1)
    assert all(run['id'] != 'run-0' for run in saved['runs'])
    reference = saved['history']['source_index']
    assert reference['version'] == 1 and reference['count'] == 24
    supervisor.stop(); supervisor.start()
    restored = load_workspace(DIRECTION, 'en', user_id=1); repo = index(supervisor.client)
    # Normalization supplies the canonical fields that the original tool capture carries.
    from lib.research.program import normalize_run_evidence
    receipt = normalize_run_evidence([draft['runs'][0]])[0]['tool_receipts'][0]
    replay = apply_action_result(action='experiment', workspace=restored, task_id='source-0',
        payload={'run': {'id': 'renamed-archived'}}, receipts=[receipt], bindings=[],
        consumed_source_lookup=lambda rows: repo.replay(reference, rows))
    assert replay['history'] == restored['history'] and replay['runs'] == restored['runs']
    assert repo.replay(reference, [receipt])


def test_legacy_archive_replay_checks_only_committed_chain(storage):
    supervisor, _ = storage
    draft = draft_with_runs(24)
    draft['runs'][0]['tool_receipts'][0].update(source_task_id='legacy-source', call_id='call')
    saved = save_workspace(DIRECTION, 'en', draft, expected_revision=0, user_id=1)
    archive = ResearchHistoryRepository(supervisor.client, user_id=1, paper_hash=research_direction_hash(DIRECTION), lang='en')
    from lib.research.program import normalize_run_evidence
    receipt = normalize_run_evidence([draft['runs'][0]])[0]['tool_receipts'][0]
    assert legacy_archive_replay(archive, saved['history'], [receipt])
    assert not legacy_archive_replay(archive, saved['history'], [fact(999)])


def test_workspace_put_cannot_duplicate_already_consumed_source(storage):
    supervisor, _ = storage
    draft = draft_with_runs(1)
    draft['runs'][0]['tool_receipts'][0].update(source_task_id='source', call_id='call')
    saved = save_workspace(DIRECTION, 'en', draft, expected_revision=0, user_id=1)
    candidate = deepcopy(saved)
    candidate['runs'].append(dict(deepcopy(candidate['runs'][0]), id='duplicated-label'))
    with pytest.raises(ValueError, match='already consumed'):
        save_workspace(DIRECTION, 'en', candidate, expected_revision=saved['revision'], user_id=1)
    assert load_workspace(DIRECTION, 'en', user_id=1) == saved


def test_legacy_backfill_preserves_existing_working_and_archived_sources(storage):
    supervisor, _ = storage
    from lib.research.source_index import prepare_source_index
    draft = draft_with_runs(24)
    for n, run in enumerate(draft['runs']):
        run['tool_receipts'][0].update(source_task_id=f'source-{n}', call_id='call')
    saved = save_workspace(DIRECTION, 'en', draft, expected_revision=0, user_id=1)
    repo = index(supervisor.client)
    archive = ResearchHistoryRepository(supervisor.client, user_id=1, paper_hash=research_direction_hash(DIRECTION), lang='en')
    migrated = prepare_source_index(repo, empty_reference(0), archive, saved['history'], saved['runs'], saved['runs'])
    assert migrated['version'] == 1 and migrated['count'] == 24
    assert migrated['root'] == saved['history']['source_index']['root']


def test_source_cache_scales_from_cached_capacity_probe(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr('runtime_guards.probe_system_resources', lambda: SimpleNamespace(
        effective_memory_available_mb=8192, effective_cpu_count=2))
    repo = ReceiptSourceIndex(MemoryRecords(), user_id=1, paper_hash=research_direction_hash(DIRECTION), lang='en')
    assert repo.capacity > 65
    monkeypatch.setattr('runtime_guards.probe_system_resources', lambda: SimpleNamespace(
        effective_memory_available_mb=None, effective_cpu_count=2))
    lean = ReceiptSourceIndex(MemoryRecords(), user_id=1, paper_hash=research_direction_hash(DIRECTION), lang='en')
    assert lean.capacity == 65


@pytest.mark.parametrize('capacity', [None, (-1, 2), (8192, 0), (True, 2), (8192, 1.5)])
def test_probe_failure_keeps_source_persistence_available(monkeypatch, capacity):
    from types import SimpleNamespace
    def probe():
        if capacity is None:
            raise OSError('capacity probe unavailable')
        return SimpleNamespace(effective_memory_available_mb=capacity[0], effective_cpu_count=capacity[1])
    monkeypatch.setattr('runtime_guards.probe_system_resources', probe)
    repo = ReceiptSourceIndex(MemoryRecords(), user_id=1, paper_hash=research_direction_hash(DIRECTION), lang='en')
    assert repo.capacity == 65
    row = fact(1)
    reference = repo.insert(empty_reference(), receipt_key(row), receipt_digest(row))
    assert repo.replay(reference, [row])


def test_explicit_cache_capacity_bypasses_probe_and_remains_validated(monkeypatch):
    def probe():
        pytest.fail('explicit capacity must bypass resource probing')
    monkeypatch.setattr('runtime_guards.probe_system_resources', probe)
    assert index(MemoryRecords(), cache_entries=100).capacity == 100
    with pytest.raises(ValueError, match='cache capacity'):
        index(MemoryRecords(), cache_entries=True)
