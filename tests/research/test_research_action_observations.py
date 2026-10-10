"""Original Research observations survive process-local state and ACK faults."""
from copy import deepcopy
import hashlib
import json

import pytest

from lib.research.action_observations import ActionObservations, NAMESPACE, PART_NAMESPACE, BINDING_NAMESPACE
from lib.storage.errors import StorageError
from tests.research.test_research_workspace_evidence_guard import storage as storage

pytestmark = pytest.mark.unit


class MemoryRecords:
    def __init__(self):
        self.rows = {}
        self.lose_ack = False
        self.refuse_manifest = False

    def query(self, operation, payload):
        if operation == 'record.list':
            keys = sorted(key for namespace, key in self.rows
                if namespace == payload['namespace'] and key.startswith(payload['prefix'])
                and key > payload.get('after_key', ''))
            return [dict(deepcopy(self.rows[(payload['namespace'], key)]), key=key)
                    for key in keys[:payload['limit']]]
        assert operation == 'record.get'
        return deepcopy(self.rows.get((payload['namespace'], payload['key'])))

    def command(self, operation, payload, idempotency_key):
        assert operation == 'record.put' and payload['expected_version'] == 0
        identity = payload['namespace'], payload['key']
        if self.refuse_manifest and payload['namespace'] == NAMESPACE:
            raise StorageError('database_unavailable', 'injected manifest refusal')
        if identity in self.rows:
            raise StorageError('database_conflict', 'already exists')
        self.rows[identity] = {'version': 1, 'value': deepcopy(payload['value'])}
        if self.lose_ack:
            raise StorageError('database_unavailable', 'injected lost acknowledgment')
        return {'version': 1}


def repository(client, **overrides):
    arguments = dict(user_id=1, task_id='research_action_observed', direction='Frozen basis',
                     lang='en', revision=7, basis_digest='a' * 64)
    return ActionObservations(client, **(arguments | overrides))


def capture(repo, content, ordinal=1):
    return repo.capture(ordinal=ordinal, call_id='call-1', tool='run_command',
        content=content, receipt={'result_digest': hashlib.sha256(content.encode()).hexdigest(),
                                 'status': 'done', 'result_excerpt': content[:4000]})


def test_schema_is_valid_and_part_bound_is_authoritative():
    import jsonschema
    from lib.research.action_observations import _validator
    for kind in ('binding', 'part', 'manifest', 'admission', 'recoveryPage', 'frozenBasis', 'effectIntent', 'effectState', 'effectsPage', 'modelIntent', 'modelUsage', 'modelUsageSnapshot', 'modelCallState', 'modelCallsPage', 'actionTerminal', 'actionTerminalResponse', 'requestContext', 'requestContextPart', 'requestMaterial', 'responseMaterial', 'modelResponse', 'loopMaterial', 'loopCheckpoint', 'processOwner', 'executionOwner'):
        jsonschema.Draft202012Validator.check_schema(_validator(kind).schema)
    assert repository(MemoryRecords()).part_chars == 30000


@pytest.mark.parametrize('content', ['', '汉字🧪' * 25000, 'x' * 100000])
def test_full_original_survives_new_repository_and_identical_retry(content):
    client = MemoryRecords()
    manifest = capture(repository(client), content)
    assert capture(repository(client), content) == manifest
    restored, original = repository(client).read(1)
    assert restored == manifest and original == content
    assert all(len(row['value']['content']) <= 30000 for (namespace, _), row
               in client.rows.items() if namespace == PART_NAMESPACE)


def test_lost_ack_is_resolved_only_by_exact_readback():
    client = MemoryRecords()
    client.lose_ack = True
    content = 'acknowledged original' * 3000
    capture(repository(client), content)
    assert repository(client).read(1)[1] == content


def test_unpublished_parts_grant_no_observation_and_retry_does_not_repeat_effect():
    client = MemoryRecords()
    client.refuse_manifest = True
    content = 'finished external experiment' * 3000
    with pytest.raises(StorageError):
        capture(repository(client), content)
    assert any(namespace == PART_NAMESPACE for namespace, _ in client.rows)
    assert repository(client).read(1) is None
    client.refuse_manifest = False
    capture(repository(client), content)
    assert repository(client).read(1)[1] == content


@pytest.mark.parametrize('change', ['content', 'ordinal', 'binding', 'version', 'missing'])
def test_modified_or_missing_parts_never_restore_an_original(change):
    client = MemoryRecords()
    capture(repository(client), 'original' * 10000)
    key = next(key for key in client.rows if key[0] == PART_NAMESPACE)
    row = client.rows[key]
    if change == 'content':
        row['value']['content'] = 'z' * len(row['value']['content'])
    elif change == 'ordinal':
        row['value']['ordinal'] = 2
    elif change == 'binding':
        row['value']['binding']['user_id'] = 2
    elif change == 'version':
        row['version'] = 2
    else:
        del client.rows[key]
    with pytest.raises(StorageError):
        repository(client).read(1)


def test_owner_and_task_isolation_and_basis_immutability():
    client = MemoryRecords()
    capture(repository(client), 'original')
    assert repository(client, user_id=2).read(1) is None
    assert repository(client, task_id='other-task').read(1) is None
    for changes in ({'revision': 8}, {'basis_digest': 'b' * 64}, {'lang': 'zh'},
                    {'direction': 'other direction'}):
        with pytest.raises(StorageError):
            repository(client, **changes).read(1)
        with pytest.raises(StorageError):
            capture(repository(client, **changes), 'original')
        with pytest.raises(StorageError):
            capture(repository(client, **changes), 'other ordinal', ordinal=2)
    with pytest.raises(StorageError):
        capture(repository(client), 'replacement')
    assert repository(client).read(1)[1] == 'original'


def test_native_and_sqlite_restore_after_storage_restart(storage):
    supervisor, backend = storage
    content = json.dumps({'scientific_observations': {'samples': list(range(1000))},
                          'log': '汉🧪' * 50000}, ensure_ascii=False)
    capture(repository(supervisor.client), content)
    with pytest.raises(StorageError):
        capture(repository(supervisor.client, basis_digest='b' * 64), content, ordinal=2)
    supervisor.stop()
    supervisor.start()
    reopened = ActionObservations.open(supervisor.client, user_id=1, task_id='research_action_observed')
    assert reopened.page()['observations'][0]['ordinal'] == 1
    manifest, original = reopened.read(1)
    assert original == content
    assert manifest['binding']['revision'] == 7
    assert manifest['result_digest'] == hashlib.sha256(content.encode()).hexdigest()


def test_missing_task_basis_cannot_restore_an_observation():
    client = MemoryRecords()
    repo = repository(client)
    capture(repo, 'original')
    del client.rows[(BINDING_NAMESPACE, repo.prefix)]
    with pytest.raises(StorageError, match='basis binding is missing'):
        repository(client).read(1)


def test_live_capture_adapter_binds_exact_workspace_and_preserves_full_result(monkeypatch):
    from lib.research.action import capture_research_result
    from lib.research.action_observations import capture_action_observation, _digest
    client = MemoryRecords()
    monkeypatch.setattr('lib.storage.get_storage_client', lambda **kwargs: client)
    workspace = {'lang': 'zh', 'revision': 9, 'hypothesis': 'specific hypothesis',
                 'protocol': {'random_seeds': [1, 2], 'baseline': 'frozen'}}
    task = {'user_id': 1, 'task_id': 'adapter-task', 'direction': 'Frozen basis'}
    row = {'roundNum': 3, 'toolCallId': 'completed-command', 'toolName': 'run_command',
           'status': 'done'}
    content = 'original output' * 10000
    capture_research_result(row, content)
    manifest = capture_action_observation(task, workspace, row, content)
    basis = _digest({'hypothesis': workspace['hypothesis'], 'protocol': workspace['protocol']})
    repo = repository(client, task_id='adapter-task', revision=9, lang='zh', basis_digest=basis)
    assert repo.read(3)[1] == content
    assert manifest['receipt'] == row['_researchReceipt']
    assert manifest['call_id'] == 'completed-command'


def test_reopen_recovers_saved_basis_and_pages_without_writes_or_original_reads():
    client = MemoryRecords()
    for ordinal in range(1, 74):
        capture(repository(client), f'original-{ordinal}', ordinal=ordinal)
    reads = []
    original_query = client.query
    def query(operation, payload):
        reads.append((operation, deepcopy(payload)))
        assert payload['namespace'] != PART_NAMESPACE
        return original_query(operation, payload)
    client.query = query
    client.command = lambda *args: pytest.fail('Recovery must not write or replay effects')
    reopened = ActionObservations.open(client, user_id=1, task_id='research_action_observed')
    assert reopened.binding == repository(client).binding
    cursor, observed = '', []
    while True:
        page = reopened.page(limit=8, cursor=cursor)
        observed.extend(row['ordinal'] for row in page['observations'])
        cursor = page['next_cursor']
        if not cursor:
            break
    assert len(observed) == 73 and set(observed) == set(range(1, 74))
    assert len([operation for operation, _ in reads if operation == 'record.list']) == 10
    assert all(payload['limit'] == 9 for operation, payload in reads if operation == 'record.list')
    client.query = original_query
    assert reopened.read(65)[1] == 'original-65'


def test_reopen_unknown_owner_or_task_has_no_record():
    client = MemoryRecords()
    capture(repository(client), 'original')
    assert ActionObservations.open(client, user_id=2, task_id='research_action_observed') is None
    assert ActionObservations.open(client, user_id=1, task_id='other') is None


@pytest.mark.parametrize('field,value', [('user_id', 2), ('task_id', 'other'),
    ('revision', True), ('basis_digest', 'unknown')])
def test_reopen_refuses_corrupted_binding(field, value):
    client = MemoryRecords()
    capture(repository(client), 'original')
    binding = client.rows[(BINDING_NAMESPACE, repository(client).prefix)]['value']['binding']
    binding[field] = value
    with pytest.raises(StorageError):
        ActionObservations.open(client, user_id=1, task_id='research_action_observed')


def test_binding_with_unpublished_parts_discovers_no_observation():
    client = MemoryRecords()
    client.refuse_manifest = True
    with pytest.raises(StorageError):
        capture(repository(client), 'completed tool original')
    reopened = ActionObservations.open(client, user_id=1, task_id='research_action_observed')
    assert reopened.page() == {'observations': [], 'next_cursor': ''}


@pytest.mark.parametrize('arguments', [{'limit': 0}, {'limit': 17}, {'limit': True},
    {'cursor': 'foreign:task:1'}, {'cursor': None}])
def test_invalid_pages_are_rejected_before_io(arguments):
    repo = repository(MemoryRecords())
    repo.client.query = lambda *args: pytest.fail('Invalid pagination must not query storage')
    with pytest.raises(ValueError):
        repo.page(**arguments)


@pytest.mark.parametrize('change', ['duplicate', 'unordered', 'owner', 'key', 'version', 'too_many'])
def test_manifest_discovery_refuses_invalid_storage_rows(change):
    client = MemoryRecords()
    repo = repository(client)
    capture(repo, 'one', ordinal=1)
    capture(repo, 'two', ordinal=2)
    rows = client.query('record.list', {'namespace': NAMESPACE, 'prefix': repo.prefix, 'limit': 3})
    if change == 'duplicate':
        rows[1] = deepcopy(rows[0])
    elif change == 'unordered':
        rows.reverse()
    elif change == 'owner':
        rows[0]['value']['binding']['user_id'] = 2
    elif change == 'key':
        rows[0]['key'] = 'foreign-key'
    elif change == 'version':
        rows[0]['version'] = True
    else:
        rows *= 10
    client.query = lambda *args: rows
    with pytest.raises(StorageError):
        repo.page(limit=2)


@pytest.mark.parametrize('content', ['', '汉🧪' * 50000])
def test_verified_export_is_complete_utf8_and_file_is_caller_owned(content):
    client = MemoryRecords()
    repo = repository(client)
    capture(repo, content)
    manifest, spool, length = repo.prepare_original_export(1)
    try:
        assert spool.read() == content.encode('utf-8')
        assert length == len(content.encode('utf-8'))
        assert manifest['result_digest'] == hashlib.sha256(content.encode('utf-8')).hexdigest()
    finally:
        spool.close()
    assert spool.closed


def test_export_missing_late_part_closes_spool_and_never_returns_partial_bytes(monkeypatch):
    import tempfile
    client = MemoryRecords()
    repo = repository(client)
    capture(repo, 'original' * 10000)
    parts = [key for key in client.rows if key[0] == PART_NAMESPACE]
    del client.rows[parts[-1]]
    spools = []
    original = tempfile.TemporaryFile
    def temporary(*args, **kwargs):
        spool = original(*args, **kwargs); spools.append(spool); return spool
    monkeypatch.setattr('lib.research.action_observations.tempfile.TemporaryFile', temporary)
    with pytest.raises(StorageError):
        repo.prepare_original_export(1)
    assert len(spools) == 1 and spools[0].closed


def test_unconsumed_response_body_closes_verified_spool():
    import asyncio
    import tempfile
    from routes.api_v1.research import _VerifiedObservationBody
    spool = tempfile.TemporaryFile()
    async def request():
        async with _VerifiedObservationBody(spool):
            pass
    asyncio.run(request())
    assert spool.closed


@pytest.mark.parametrize("cancellations", [1, 3])
def test_disconnect_settles_inflight_read_before_closing_spool(cancellations):
    import asyncio
    import tempfile
    import threading
    from routes.api_v1.research import _VerifiedObservationBody
    entered, release = threading.Event(), threading.Event()
    original = tempfile.TemporaryFile(); original.write(b'complete'); original.seek(0)
    class Controlled:
        closed = False
        def read(self, length):
            entered.set()
            assert release.wait(3), 'read was not released'
            return original.read(length)
        def close(self):
            self.closed = True; original.close()
    spool = Controlled()
    async def request():
        async with _VerifiedObservationBody(spool) as body:
            async for _ in body:
                pass
    async def run():
        task = asyncio.create_task(request())
        assert await asyncio.to_thread(entered.wait, 2)
        for _ in range(cancellations):
            task.cancel()
            await asyncio.sleep(0)
            assert not spool.closed
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert spool.closed
    try:
        asyncio.run(run())
    finally:
        release.set(); original.close()


def test_frozen_basis_is_exact_immutable_and_legacy_missing_is_not_fabricated():
    from lib.research.action_observations import _digest
    basis = {'hypothesis': 'specific testable scope', 'protocol': {'random_seeds': [7, 11],
             'decision_rule': {'minimum_effect': 0.25}}}
    client = MemoryRecords(); repo = repository(client, basis_digest=_digest(basis))
    assert repo.load_basis() is None
    repo.freeze_basis(basis)
    repo.freeze_basis(deepcopy(basis))
    assert repo.load_basis() == basis
    basis['protocol']['random_seeds'].append(99)
    assert repo.load_basis()['protocol']['random_seeds'] == [7, 11]
    with pytest.raises(ValueError, match='does not match'):
        repo.freeze_basis(basis)
    reopened = ActionObservations.open(client, user_id=1, task_id='research_action_observed')
    assert reopened.load_basis() == repo.load_basis()


def test_modified_basis_or_missing_binding_never_recovers_as_original():
    from lib.research.action_observations import _digest, BASIS_NAMESPACE
    basis = {'hypothesis': 'original', 'protocol': {'baseline': 'frozen'}}
    client = MemoryRecords(); repo = repository(client, basis_digest=_digest(basis)); repo.freeze_basis(basis)
    row = client.rows[(BASIS_NAMESPACE, repo.prefix)]
    row['value']['basis']['hypothesis'] = 'replacement'
    with pytest.raises(StorageError, match='digest is invalid'):
        repo.load_basis()
    row['value']['basis']['hypothesis'] = 'original'
    del client.rows[(BINDING_NAMESPACE, repo.prefix)]
    with pytest.raises(StorageError, match='binding is missing'):
        repo.load_basis()


def test_frozen_basis_survives_selected_storage_restart(storage):
    from lib.research.action_observations import _digest
    supervisor, _ = storage
    basis = {'hypothesis': '冻结的研究依据', 'protocol': {'random_seeds': [3, 17],
             'dataset': 'held-out', 'baseline': 'reference'}}
    repo = repository(supervisor.client, basis_digest=_digest(basis)); repo.freeze_basis(basis)
    capture(repo, 'observed original')
    supervisor.stop(); supervisor.start()
    reopened = ActionObservations.open(supervisor.client, user_id=1, task_id='research_action_observed')
    assert reopened.load_basis() == basis
    assert reopened.read(1)[1] == 'observed original'


def intent_repository(client):
    from lib.research.action_observations import _digest
    basis = {'hypothesis': 'declared', 'protocol': {'baseline': 'reference'}}
    repo = repository(client, basis_digest=_digest(basis)); repo.freeze_basis(basis)
    return repo


def test_unresolved_intent_is_durable_and_same_slot_cannot_replay():
    client = MemoryRecords(); repo = intent_repository(client)
    repo.record_intent(ordinal=1, call_id='call-1', tool='run_command', arguments={'command': 'experiment'})
    reopened = ActionObservations.open(client, user_id=1, task_id='research_action_observed')
    effect = reopened.effects_page()['effects'][0]
    assert effect['state'] == 'unresolved_effect' and effect['result_digest'] is None
    assert effect['replay_allowed'] is False
    with pytest.raises(StorageError, match='replay is refused'):
        reopened.record_intent(ordinal=1, call_id='call-1', tool='run_command', arguments={'command': 'experiment'})
    capture(repo, 'completed original')
    effect = reopened.effects_page()['effects'][0]
    assert effect['state'] == 'observation_published' and effect['tool_status'] == 'done'
    assert effect['replay_allowed'] is False


def test_intent_ack_loss_accepts_exact_own_commit_but_no_fresh_retry():
    client = MemoryRecords(); repo = intent_repository(client); client.lose_ack = True
    repo.record_intent(ordinal=1, call_id='call-1', tool='run_command', arguments={})
    assert repo.load_intent(1)['admission_id']
    with pytest.raises(StorageError):
        repo.record_intent(ordinal=1, call_id='call-1', tool='run_command', arguments={})


def test_observation_cannot_publish_under_different_intent_identity():
    client = MemoryRecords(); repo = intent_repository(client)
    repo.record_intent(ordinal=1, call_id='other-call', tool='run_command', arguments={})
    with pytest.raises(StorageError, match='does not match its effect intent'):
        capture(repo, 'original')
    assert repo.read(1) is None


def test_concurrent_intent_admission_and_unresolved_recovery_on_selected_store(storage):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from lib.research.action_observations import INTENT_NAMESPACE
    supervisor, _ = storage
    repo = intent_repository(supervisor.client)
    barrier = threading.Barrier(2)
    lock = threading.Lock(); initial_reads = [0]
    class RacingClient:
        def query(self, operation, payload):
            result = supervisor.client.query(operation, payload)
            if operation == 'record.get' and payload['namespace'] == INTENT_NAMESPACE:
                with lock:
                    initial_reads[0] += 1
                    wait = initial_reads[0] <= 2
                if wait: barrier.wait(timeout=5)
            return result
        def command(self, *args):
            return supervisor.client.command(*args)
    def admit():
        raced = repository(RacingClient(), basis_digest=repo.binding['basis_digest'])
        try:
            raced.record_intent(ordinal=1, call_id='call-1', tool='run_command', arguments={})
            return True
        except StorageError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: admit(), range(2)))
    assert sorted(results) == [False, True]
    supervisor.stop(); supervisor.start()
    reopened = ActionObservations.open(supervisor.client, user_id=1, task_id='research_action_observed')
    assert reopened.effects_page()['effects'][0]['state'] == 'unresolved_effect'
    with pytest.raises(StorageError):
        reopened.record_intent(ordinal=1, call_id='call-1', tool='run_command', arguments={})


def test_effect_pages_cover_all_slots_without_original_reads_and_reject_bad_status():
    client = MemoryRecords(); repo = intent_repository(client)
    for ordinal in range(1, 22):
        repo.record_intent(ordinal=ordinal, call_id='call-1', tool='run_command', arguments={})
    queried = []; original_query = client.query
    def query(operation, payload):
        assert payload['namespace'] != PART_NAMESPACE
        queried.append((operation, deepcopy(payload)))
        return original_query(operation, payload)
    client.query = query
    cursor = ''; slots = []
    while True:
        page = repo.effects_page(limit=8, cursor=cursor)
        slots.extend(effect['intent']['ordinal'] for effect in page['effects'])
        cursor = page['next_cursor']
        if not cursor: break
    assert len(slots) == 21 and set(slots) == set(range(1, 22))
    assert sum(operation == 'record.list' for operation, _ in queried) == 3
    client.query = original_query
    capture(repo, 'original')
    client.query = query
    client.rows[(NAMESPACE, repo.prefix + '1')]['value']['receipt']['status'] = 'invented'
    with pytest.raises(StorageError, match='effect state is invalid'):
        repo.effects_page()


@pytest.mark.parametrize('fault', ['missing', 'changed'])
def test_normal_success_ack_requires_exact_selected_authority_record(fault):
    class FalseAcknowledgment(MemoryRecords):
        def command(self, operation, payload, idempotency_key):
            if fault == 'missing':
                return {'version':1}
            result = super().command(operation, payload, idempotency_key)
            identity = payload['namespace'], payload['key']
            if 'content' in self.rows[identity]['value']:
                self.rows[identity]['value']['content'] += 'changed after write'
            return result
    client = FalseAcknowledgment()
    with pytest.raises(StorageError, match='not exactly acknowledged'):
        capture(repository(client), 'original measured output')
    assert not any(namespace == NAMESPACE for namespace, key in client.rows)


def test_new_immutable_records_each_have_one_confirmation_read():
    class CountedRecords(MemoryRecords):
        def __init__(self):
            super().__init__(); self.writes = 0; self.reads = 0
        def command(self, *args):
            result = super().command(*args)
            self.writes += 1
            return result
        def query(self, operation, payload):
            self.reads += 1
            return super().query(operation, payload)
    client = CountedRecords()
    capture(repository(client), 'x' * 60001)
    assert client.writes == 5
    assert client.reads == 7  # Five confirmations plus intent and prior-manifest lookups.
