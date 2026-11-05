"""Receipt batches persist final trie nodes, retaining exact publication guards."""
from collections import Counter
from copy import deepcopy
from contextvars import ContextVar
from threading import Event, Lock

import pytest

from lib.research.progress_index import SOURCE_NAMESPACE, JOB_NAMESPACE, empty_progress
from lib.research.source_index import (ReceiptSourceIndex, receipt_digest, receipt_key,
                                       prepare_source_index, empty_reference, PREPARATION_WINDOW_RECEIPTS)
from lib.storage.errors import StorageError
from tests.research.test_research_action_observations import MemoryRecords
from tests.research.test_research_progress_index import observation, repo

pytestmark = pytest.mark.unit


class CountedRecords(MemoryRecords):
    def __init__(self):
        super().__init__()
        self.calls = Counter()
        self.calls_lock = Lock()

    def command(self, operation, payload, *args, **kwargs):
        with self.calls_lock:
            self.calls[operation] += 1
        return super().command(operation, payload, *args, **kwargs)

    def query(self, operation, payload, *args, **kwargs):
        with self.calls_lock:
            self.calls[operation] += 1
        return super().query(operation, payload, *args, **kwargs)


def reachable_records(client, indexes, progress):
    reachable = set()
    for index, reference in ((indexes.sources, progress['sources']), (indexes.jobs, progress['jobs'])):
        pending = [reference['root']] if reference['root'] else []
        while pending:
            key = (index.namespace, index.prefix + pending.pop())
            if key in reachable:
                continue
            reachable.add(key)
            node = client.rows[key]['value']
            pending.extend(node.get('children', {}).values())
    return reachable


def test_one_batch_persists_only_reachable_nodes_and_reads_each_back():
    client = CountedRecords()
    indexes = repo(client)
    rows = [observation(n, 'job', 'running' if n % 2 else 'completed') for n in range(1024)]
    final = indexes.advance(empty_progress(), iter(rows))
    reachable = reachable_records(client, indexes, final)
    assert set(client.rows) == reachable
    assert client.calls['record.put'] == len(reachable)
    assert client.calls['record.get'] == len(reachable)
    reopened = repo(client)
    assert all(reopened.lookup_source(final, row) == receipt_digest(row) for row in rows)
    assert final['receipts'] == 1024 and final['job_counts']['running'] == 1


def test_batch_matches_chronological_updates_and_keeps_old_roots():
    rows = [observation(n, f'job-{n % 13}', ('queued', 'running', 'completed', 'failed')[n % 4])
            for n in range(137)]
    client = MemoryRecords()
    indexes = repo(client)
    prior = indexes.advance(empty_progress(), rows[:3])
    frozen = deepcopy(prior)
    batched = indexes.advance(prior, iter(rows[3:]))
    sequential = empty_progress()
    oracle = repo(MemoryRecords())
    for row in rows:
        sequential = oracle.advance(sequential, [row])
    assert batched == sequential and prior == frozen
    assert repo(client).lookup_source(prior, rows[0]) == receipt_digest(rows[0])
    assert repo(client).lookup_source(prior, rows[-1]) is None


@pytest.mark.parametrize('namespace', [SOURCE_NAMESPACE, JOB_NAMESPACE])
def test_unstored_success_ack_cannot_publish_a_batched_root(namespace):
    client = MemoryRecords()
    indexes = repo(client)
    old = indexes.advance(empty_progress(), [observation(0, 'old', 'running')])
    command = client.command

    def drop(operation, payload, *args, **kwargs):
        if payload['namespace'] == namespace:
            return {'version': 1}
        return command(operation, payload, *args, **kwargs)

    client.command = drop
    rows = [observation(n, f'job-{n}', 'completed') for n in range(1, 12)]
    with pytest.raises(StorageError):
        indexes.advance(old, rows)
    assert repo(client).lookup_source(old, observation(0, 'old', 'running')) is not None
    assert repo(client).lookup_source(old, rows[-1]) is None
    client.command = command
    retried = indexes.advance(old, rows)
    assert repo(client).lookup_source(retried, rows[-1]) == receipt_digest(rows[-1])


def test_source_conflict_in_batch_cannot_replace_published_facts():
    client = MemoryRecords()
    indexes = repo(client)
    row = observation(0, 'job', 'running')
    prior = indexes.advance(empty_progress(), [row])
    with pytest.raises(ValueError, match='source identity conflicts'):
        indexes.advance(prior, [observation(1, 'other', 'completed'), dict(row, result_digest='changed')])
    assert repo(client).lookup_source(prior, row) == receipt_digest(row)


def test_lost_batch_ack_and_repeated_preparation_are_exact():
    client = MemoryRecords()
    client.lose_ack = True
    rows = [observation(n, f'job-{n % 5}', 'completed') for n in range(70)]
    first = repo(client).advance(empty_progress(), rows)
    assert repo(client).advance(empty_progress(), rows) == first
    assert all(repo(client).lookup_source(first, row) == receipt_digest(row) for row in rows)


def test_receipt_windows_continue_without_history_truncation():
    client = MemoryRecords()
    indexes = repo(client)
    rows = [observation(n, 'job', 'running') for n in range(2 * PREPARATION_WINDOW_RECEIPTS + 3)]
    progress = indexes.advance(empty_progress(), iter(rows))
    assert progress['receipts'] == len(rows) and progress['sources']['count'] == len(rows)
    reopened = repo(client)
    for n in (0, PREPARATION_WINDOW_RECEIPTS - 1, PREPARATION_WINDOW_RECEIPTS, len(rows) - 1):
        assert reopened.lookup_source(progress, rows[n]) == receipt_digest(rows[n])


def test_settled_source_preparation_uses_the_same_reachable_node_boundary():
    client = MemoryRecords()
    index = ReceiptSourceIndex(client, user_id=1, paper_hash='a' * 32, lang='en', cache_entries=65)
    rows = [observation(n, 'job', 'completed') for n in range(80)]
    run = {'id': 'run', 'execution_status': 'completed', 'tool_receipts': rows}
    reference = prepare_source_index(index, empty_reference(), None, None, [run], [])
    pending = [reference['root']]
    reachable = set()
    while pending:
        key = (index.namespace, index.prefix + pending.pop())
        reachable.add(key)
        pending.extend(client.rows[key]['value'].get('children', {}).values())
    assert set(client.rows) == reachable
    assert all(index.lookup(reference, receipt_key(row)) == receipt_digest(row) for row in rows)
    with pytest.raises(ValueError, match='already consumed'):
        prepare_source_index(index, reference, None, None, [dict(run, id='replay')], [run])


@pytest.mark.parametrize('ack', [None, {'version': True}, {'version': 1}])
def test_consumed_source_index_never_trusts_an_unstored_ack(ack):
    client = MemoryRecords()
    index = ReceiptSourceIndex(client, user_id=1, paper_hash='a' * 32, lang='en', cache_entries=65)
    client.command = lambda *args, **kwargs: deepcopy(ack)
    row = observation(0, 'job', 'completed')
    with pytest.raises(StorageError):
        index.insert(empty_reference(), receipt_key(row), receipt_digest(row))


def test_lost_source_ack_requires_storage_readback_even_with_a_cached_node():
    client = MemoryRecords()
    index = ReceiptSourceIndex(client, user_id=1, paper_hash='a' * 32, lang='en', cache_entries=65)
    row = observation(0, 'job', 'completed')
    index.insert(empty_reference(), receipt_key(row), receipt_digest(row))
    client.rows.clear()

    def unavailable(*args, **kwargs):
        raise StorageError('database_unavailable', 'injected write refusal')

    client.command = unavailable
    with pytest.raises(StorageError):
        index.insert(empty_reference(), receipt_key(row), receipt_digest(row))


def test_independent_trie_branches_overlap_storage_waits_with_caller_context():
    client = MemoryRecords()
    indexes = repo(client)
    original = indexes.sources._write
    context = ContextVar('research_preparation_test_owner', default=None)
    token = context.set('owned-research')
    second_entered = Event()
    lock = Lock()
    entered = []

    def write(node):
        assert context.get() == 'owned-research'
        if node['kind'] == 'leaf':
            with lock:
                entered.append(node['key'])
                ordinal = len(entered)
            if ordinal == 1:
                assert second_entered.wait(10), 'independent branch was serialized behind storage'
            elif ordinal == 2:
                second_entered.set()
        return original(node)

    indexes.sources._write = write
    try:
        result = indexes.advance(empty_progress(), [observation(n, 'job', 'completed') for n in range(80)])
        assert result['sources']['count'] == 80 and len(entered) == 80
    finally:
        context.reset(token)


def test_failed_branch_joins_started_siblings_before_discarding_scratch():
    indexes = repo(MemoryRecords())
    original = indexes.sources._write
    release = Event()
    lock = Lock()
    active = 0
    entered = 0

    def write(node):
        nonlocal active, entered
        if node['kind'] != 'leaf':
            return original(node)
        with lock:
            active += 1
            entered += 1
            ordinal = entered
        try:
            if ordinal == 1:
                assert release.wait(10)
            elif ordinal == 2:
                release.set()
                raise StorageError('database_unavailable', 'injected sibling failure')
            return original(node)
        finally:
            with lock:
                active -= 1

    indexes.sources._write = write
    with pytest.raises(StorageError, match='injected sibling failure'):
        indexes.advance(empty_progress(), [observation(n, 'job', 'completed') for n in range(80)])
    assert active == 0 and entered >= 2
