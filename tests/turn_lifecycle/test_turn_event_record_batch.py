"""One transaction persists a queued run of structural task events.

``turn_lifecycle.record_task_events`` commits K ordinary ``turn.event.record``
steps through ``turn.event.record_batch``: K projection revisions, K
conversation-sync events and K task-event rows, exactly like K sequential
``record_task_event`` calls, and nothing at all when any entry is refused.
Both authorities run every specification.
"""
import base64
import random
from copy import deepcopy

import orjson
import pytest

from lib.storage.errors import StorageError
from lib.storage.protocol import wire_payload
from tests.turn_lifecycle.test_task_event_delivery import (
    _task, _discard, storage as storage, turn_service as turn_service,
)

pytestmark = pytest.mark.unit


def _round(round_num, status):
    return {'roundNum': round_num, 'toolCallId': f'call-{round_num}', 'toolName': 'read_file',
            'toolArgs': '{"path": "a.py"}', 'status': status,
            **({'toolContent': f'result {round_num}'} if status == 'done' else {})}


def _script():
    """Producer mutations and the structural frame each one emits."""
    def tool(round_num, status):
        def mutate(task):
            rows = [row for row in task['toolRounds'] if row['roundNum'] != round_num]
            task['toolRounds'] = rows + [_round(round_num, status)]
        return mutate
    return [
        (lambda task: task.update(phase={'phase': 'calling', 'detail': 'model'}),
         {'type': 'phase', 'phase': 'calling'}),
        (lambda task: task.update(content='Reading the file.'),
         {'type': 'round_end', 'roundNum': 1, 'reason': 'tool_calls'}),
        (tool(1, 'running'), {'type': 'tool_start', 'toolName': 'read_file', 'roundNum': 1}),
        (tool(1, 'done'), {'type': 'tool_result', 'toolName': 'read_file', 'roundNum': 1}),
        (lambda task: task.update(phase={'phase': 'thinking', 'detail': 'round 2'}),
         {'type': 'phase', 'phase': 'thinking'}),
        (tool(2, 'running'), {'type': 'tool_start', 'toolName': 'read_file', 'roundNum': 2}),
    ]


def _entries(turn_service, task, script, first_sequence=0):
    """Freeze each frame's projection inputs exactly like the delivery FIFO."""
    entries = []
    for offset, (mutate, event) in enumerate(script):
        mutate(task)
        wire = {**event, 'seq': first_sequence + offset, 'taskId': task['id'],
                'attemptId': task['_attemptId']}
        entries.append(turn_service.TaskEventBatchEntry(
            raw_event=wire, sequence=first_sequence + offset,
            projection_source=turn_service.capture_task_projection_source(task, wire)))
    return entries


def _sync_sequence(storage, task):
    return storage.client.query('turn.sync.snapshot', {
        'conversation_id': task['convId'], 'user_id': 1})['syncSequence']


def _sync_changes(storage, task, after):
    return storage.client.query('turn.sync.changes', {
        'conversation_id': task['convId'], 'user_id': 1, 'after': after})['events']


def _rows(storage, task):
    return storage.client.query('event.list', {
        'task_id': task['id'], 'after_sequence': -1, 'limit': 1000})


def _turn(turn_service, task):
    return turn_service.get_turn(task['convId'], task['_turnId'], user_id=1)


# Wall-clock activity facts differ between any two executions.
_CLOCK_FIELDS = {'timingTrace', 'activityTimeline'}


def _comparable_patch(change):
    event = change['payload']['event']
    return (change['type'], event['type'], event['payload']['status'],
            [operation for operation in event['payload']['projectionPatch']['operations']
             if not (operation['path'] and operation['path'][0] in _CLOCK_FIELDS)])


def _without_identity(value, task):
    """Replace one arm's task/attempt/conversation/turn ids with placeholders."""
    import json
    text = json.dumps(value, sort_keys=True)
    for key in ('_attemptId', 'id', 'convId', '_turnId'):
        text = text.replace(task[key], key.upper())
    return json.loads(text)


def _commit_batch(storage, payload):
    """Commit a valid batch; native TofuDB may refuse one composite commit
    (``database_resource_exhausted``, nothing written), so record FIFO halves."""
    try:
        return storage.client.command('turn.event.record_batch', payload, None, priority='event')
    except StorageError as exc:
        entries = payload['entries']
        if exc.code != 'database_resource_exhausted' or len(entries) < 2:
            raise
        half = len(entries) // 2
        return (_commit_batch(storage, {**payload, 'entries': entries[:half]})
                + _commit_batch(storage, {**payload, 'entries': entries[half:]}))


def _capture_batch(storage, monkeypatch, fail_with=None, after_commit=None, singles=None):
    """Record every batch request; optionally fail before or after its commit."""
    calls = []
    original = storage.client.command

    def command(operation, payload, *args, **kwargs):
        if operation == 'turn.event.record' and singles is not None:
            singles.append(payload)
        if operation == 'turn.event.record_batch':
            # The request the clients send: a frozen payload's wire body.
            calls.append(orjson.loads(orjson.dumps(wire_payload(payload))))
            if fail_with is not None and len(calls) == 1:
                raise fail_with
            result = original(operation, payload, *args, **kwargs)
            if after_commit is not None and len(calls) == 1:
                raise after_commit
            return result
        return original(operation, payload, *args, **kwargs)

    monkeypatch.setattr(storage.client, 'command', command)
    return calls


def test_batch_commits_k_revisions_sync_events_and_rows_like_sequential_records(
        turn_service, storage, monkeypatch):
    from lib.tasks_pkg.event_log import project_persistent_event
    monkeypatch.setenv('TOFU_TURN_STRUCTURAL_RECORD_MS', '600000')
    _, sequential = _task(turn_service, storage, 'batch-sequential')
    _, batched = _task(turn_service, storage, 'batch-batched')
    try:
        for task in (sequential, batched):
            # Both arms start from one committed live baseline.
            assert turn_service.record_task_event(task, {'type': 'phase', 'phase': 'starting'})
        before = {id(task): (_turn(turn_service, task)['projectionRevision'],
                             _sync_sequence(storage, task)) for task in (sequential, batched)}
        script = _script()
        for offset, (mutate, event) in enumerate(script):
            mutate(sequential)
            wire = {**event, 'seq': offset, 'taskId': sequential['id'],
                    'attemptId': sequential['_attemptId']}
            assert turn_service.record_task_event(sequential, wire, task_event={
                'task_id': sequential['id'], 'sequence': offset,
                'event': project_persistent_event(sequential['id'], wire)}) == 'carried'
        singles = []
        calls = _capture_batch(storage, monkeypatch, singles=singles)
        entries = _entries(turn_service, batched, script)
        assert turn_service.record_task_events(batched, entries) == ['carried'] * len(script)
        assert len(calls) == 1 and len(calls[0]['entries']) == len(script)
        assert singles == [], 'the authority must commit the batch itself, not the per-event path'
        results = {}
        for task in (sequential, batched):
            revision, sync_sequence = before[id(task)]
            turn = _turn(turn_service, task)
            changes = _sync_changes(storage, task, sync_sequence)
            assert turn['projectionRevision'] == revision + len(script)
            assert [change['type'] for change in changes] == ['attempt.event'] * len(script)
            rows = _rows(storage, task)
            assert [row['sequence'] for row in rows] == list(range(len(script)))
            results[id(task)] = _without_identity((
                {key: value for key, value in turn['projection'].items()
                 if key not in _CLOCK_FIELDS},
                [_comparable_patch(change) for change in changes],
                [row['event'] for row in rows]), task)
        assert results[id(batched)] == results[id(sequential)]
        state = batched['_turnProjectionState']
        assert state['projectionRevision'] == before[id(batched)][0] + len(script)
        assert all(entry.outcome == 'carried' for entry in entries)
    finally:
        _discard(sequential)
        _discard(batched)


def test_projection_state_is_remembered_once_for_the_whole_batch(
        turn_service, storage, monkeypatch):
    _, task = _task(turn_service, storage, 'batch-state-once')
    try:
        assert turn_service.record_task_event(task, {'type': 'phase', 'phase': 'starting'})
        remembered = []
        original = turn_service._remember_task_projection_state
        monkeypatch.setattr(turn_service, '_remember_task_projection_state',
                            lambda *args, **kwargs: remembered.append(kwargs['projection_revision'])
                            or original(*args, **kwargs))
        base = _turn(turn_service, task)['projectionRevision']
        entries = _entries(turn_service, task, _script())
        assert turn_service.record_task_events(task, entries) == ['carried'] * len(entries)
        assert remembered == [base + len(entries)]
    finally:
        _discard(task)


@pytest.mark.parametrize('invalid', [
    'terminal', 'interaction', 'background_ack', 'tool_effect', 'foreign_task',
    'descending', 'missing_carrier', 'other_attempt', 'stale_base', 'conflicting_row',
    'count_limit',
])
def test_any_invalid_entry_rolls_back_every_entry(turn_service, storage, monkeypatch, invalid):
    _, task = _task(turn_service, storage, 'batch-rollback')
    try:
        assert turn_service.record_task_event(task, {'type': 'phase', 'phase': 'starting'})
        calls = _capture_batch(storage, monkeypatch, fail_with=StorageError(
            'database_unavailable', 'capture the exact producer batch', True))
        with pytest.raises(StorageError):
            turn_service.record_task_events(task, _entries(turn_service, task, _script()))
        payload = deepcopy(calls[0])
        entries = payload['entries']
        middle = entries[2]
        if invalid == 'terminal':
            middle.update(terminal=True, status='completed')
        elif invalid == 'interaction':
            middle['event_type'] = 'interaction_request'
        elif invalid == 'background_ack':
            middle['background_command_ack'] = {'block_id': 'bg-1', 'deliveries': []}
        elif invalid == 'tool_effect':
            middle['event_payload']['updateKind'] = 'tool_effect'
            middle['task_event']['event']['type'] = 'tool_effect'
        elif invalid == 'foreign_task':
            middle['task_event']['task_id'] = 'another-task'
        elif invalid == 'descending':
            middle['task_event']['sequence'] = 0
        elif invalid == 'missing_carrier':
            middle.pop('task_event')
        elif invalid == 'other_attempt':
            middle['attempt_id'] = 'another-attempt'
        elif invalid == 'stale_base':
            patch = middle['projection_patch']
            patch['baseRevision'] -= 1
            patch['targetRevision'] -= 1
        elif invalid == 'conflicting_row':
            storage.client.command('event.append', {
                'task_id': task['id'], 'sequence': 2,
                'event': {'type': 'delta', 'content': 'existing different value'}}, None)
        else:
            # Five structural rows plus one progress entry carrying 500 rows:
            # 505 rows exceed the existing 500-event RPC composition bound.
            boundary = deepcopy(payload)
            for candidate, preceding in ((payload, 499), (boundary, 494)):
                last = candidate['entries'][-1]['task_event']
                last['event'] = {'type': 'delta', 'content': 'x'}
                last['preceding_events'] = [
                    {'task_id': task['id'], 'sequence': 5 + index,
                     'event': {'type': 'delta', 'content': 'x'}} for index in range(preceding)]
                last['sequence'] = 5 + preceding
        before_turn = _turn(turn_service, task)
        before_sync = _sync_sequence(storage, task)
        before_rows = [row['sequence'] for row in _rows(storage, task)]
        with pytest.raises(StorageError):
            storage.client.command('turn.event.record_batch', payload, None, priority='event')
        after_turn = _turn(turn_service, task)
        assert after_turn['projectionRevision'] == before_turn['projectionRevision']
        assert after_turn['projection'] == before_turn['projection']
        assert _sync_sequence(storage, task) == before_sync
        assert [row['sequence'] for row in _rows(storage, task)] == before_rows
        assert before_rows == ([2] if invalid == 'conflicting_row' else [])
        # The unmodified producer batch (for the count fence, the batch at
        # exactly 500 rows) is valid on the same authority state.
        if invalid != 'conflicting_row':
            valid = boundary if invalid == 'count_limit' else calls[0]
            result = _commit_batch(storage, valid)
            assert [item['applied'] for item in result] == [True] * len(valid['entries'])
            assert len(_rows(storage, task)) == (500 if invalid == 'count_limit' else 6)
    finally:
        _discard(task)


@pytest.mark.parametrize('committed', [True, False])
def test_unknown_outcome_reconciles_before_rebasing_without_duplicate_sync_events(
        turn_service, storage, monkeypatch, committed):
    from lib.tasks_pkg.segments import record_injected_note
    _, task = _task(turn_service, storage, 'batch-lost-ack')
    try:
        assert turn_service.record_task_event(task, {'type': 'phase', 'phase': 'starting'})
        base = _turn(turn_service, task)['projectionRevision']
        sync_before = _sync_sequence(storage, task)
        lost = StorageError('database_timeout', 'injected lost batch acknowledgement', True)
        calls = _capture_batch(storage, monkeypatch, **(
            {'after_commit': lost} if committed else {'fail_with': lost}))
        note = record_injected_note(task, llm_round=1, kind='system-intervention',
                                    text='accepted before the frozen batch')
        entries = _entries(turn_service, task, _script())
        # The live producer can advance while this detached FIFO run persists.
        # Its later note must not leak into retry or replay of earlier frames.
        record_injected_note(task, llm_round=2, kind='system-intervention',
                             text='accepted after the frozen batch')
        with pytest.raises(StorageError, match='lost batch acknowledgement'):
            turn_service.record_task_events(task, entries)
        assert all(entry.outcome is None for entry in entries)
        frozen = [deepcopy(entry.task_event) for entry in entries]
        assert turn_service.record_task_events(task, entries) == ['carried'] * len(entries)
        assert [entry.task_event for entry in entries] == frozen
        turn = _turn(turn_service, task)
        changes = _sync_changes(storage, task, sync_before)
        assert turn['projectionRevision'] == base + len(entries)
        assert len(changes) == len(entries)
        revisions = [change['payload']['event']['payload']['projectionPatch']['targetRevision']
                     for change in changes]
        assert revisions == list(range(base + 1, base + len(entries) + 1))
        assert [row['sequence'] for row in _rows(storage, task)] == list(range(len(entries)))
        # The retry reads the exact rows first: a committed unknown outcome is
        # never resent, and an uncommitted one is resent on the same base.
        if committed:
            assert len(calls) == 1
        else:
            assert len(calls) == 2
            assert ([entry['projection_patch']['baseRevision'] for entry in calls[1]['entries']]
                    == [entry['projection_patch']['baseRevision'] for entry in calls[0]['entries']])
        assert task['_turnProjectionState']['projectionRevision'] == base + len(entries)
        assert turn['projection']['toolRounds'][-1]['roundNum'] == 2
        notes = [row for row in turn['projection']['segments'] if row['type'] == 'system_note']
        assert [(row['blockId'], row['text']) for row in notes] == [(note['blockId'], note['text'])]
    finally:
        _discard(task)


def test_partially_durable_batch_fails_closed_without_rebasing(
        turn_service, storage, monkeypatch):
    _, task = _task(turn_service, storage, 'batch-partial')
    try:
        assert turn_service.record_task_event(task, {'type': 'phase', 'phase': 'starting'})
        calls = _capture_batch(storage, monkeypatch, fail_with=StorageError(
            'database_unavailable', 'capture the exact producer batch', True))
        entries = _entries(turn_service, task, _script())
        with pytest.raises(StorageError):
            turn_service.record_task_events(task, entries)
        # Another writer committed only the first entry behind this producer's back.
        first = calls[0]['entries'][0]
        assert storage.client.command('turn.event.record', first, None, priority='event')['applied']
        revision = _turn(turn_service, task)['projectionRevision']
        with pytest.raises(StorageError) as refused:
            turn_service.record_task_events(task, entries)
        assert refused.value.code == 'database_conflict'
        assert 'partially durable' in refused.value.message
        assert _turn(turn_service, task)['projectionRevision'] == revision
        assert [row['sequence'] for row in _rows(storage, task)] == [0]
        assert all(entry.outcome is None for entry in entries)
    finally:
        _discard(task)


def test_stale_attempt_revokes_the_whole_batch(turn_service, storage, monkeypatch):
    _, task = _task(turn_service, storage, 'batch-stale')
    try:
        assert turn_service.record_task_event(task, {'type': 'phase', 'phase': 'starting'})
        # The attempt settles behind this executor's cached projection state.
        stale = {**task}
        stale.pop('_turnProjectionState')
        stale.pop('_turnProjectionStateLock', None)
        stale['status'] = 'done'
        assert turn_service.record_task_event(stale, {'type': 'done', 'finishReason': 'stop'})
        calls = _capture_batch(storage, monkeypatch)
        entries = _entries(turn_service, task, _script())
        assert turn_service.record_task_events(task, entries) == [False] * len(entries)
        assert len(calls) == 1
        assert task['aborted'] and task['_abort_reason'] == 'turn_attempt_stale_fence'
        assert '_turnProjectionState' not in task
        assert _rows(storage, task) == []
    finally:
        _discard(task)


@pytest.mark.parametrize('refusal', [
    StorageError('unknown_operation', 'unknown_operation'),
    StorageError('database_protocol_error', 'Unknown storage operation'),
])
def test_authority_without_the_batch_command_uses_the_per_event_path(
        turn_service, storage, monkeypatch, refusal):
    _, task = _task(turn_service, storage, 'batch-old-authority')
    try:
        assert turn_service.record_task_event(task, {'type': 'phase', 'phase': 'starting'})
        base = _turn(turn_service, task)['projectionRevision']
        single = []
        original = storage.client.command

        def command(operation, payload, *args, **kwargs):
            if operation == 'turn.event.record_batch':
                raise refusal
            if operation == 'turn.event.record':
                single.append(payload['task_event']['sequence'])
            return original(operation, payload, *args, **kwargs)

        monkeypatch.setattr(storage.client, 'command', command)
        entries = _entries(turn_service, task, _script())
        assert turn_service.record_task_events(task, entries) == ['carried'] * len(entries)
        assert single == list(range(len(entries)))
        assert _turn(turn_service, task)['projectionRevision'] == base + len(entries)
        assert [row['sequence'] for row in _rows(storage, task)] == list(range(len(entries)))
        # Committed entries are frozen: a repeated call writes nothing.
        assert turn_service.record_task_events(task, entries) == ['carried'] * len(entries)
        assert single == list(range(len(entries)))
    finally:
        _discard(task)


def test_runs_beyond_the_existing_rpc_budget_commit_in_ordered_transactions(
        turn_service, storage, monkeypatch):
    from dataclasses import replace
    from lib.agent_core import storage_event_policy
    budget = replace(storage_event_policy.resolve_storage_event_budget(), batch_max_events=4)
    monkeypatch.setattr(storage_event_policy, 'resolve_storage_event_budget',
                        lambda *args, **kwargs: budget)
    _, task = _task(turn_service, storage, 'batch-budget')
    try:
        assert turn_service.record_task_event(task, {'type': 'phase', 'phase': 'starting'})
        base = _turn(turn_service, task)['projectionRevision']
        calls = _capture_batch(storage, monkeypatch)
        entries = _entries(turn_service, task, _script())
        assert turn_service.record_task_events(task, entries) == ['carried'] * len(entries)
        assert [len(call['entries']) for call in calls] == [4, 2]
        assert _turn(turn_service, task)['projectionRevision'] == base + len(entries)
    finally:
        _discard(task)


def test_a_refused_composite_commit_is_recorded_in_fifo_halves(turn_service, storage, monkeypatch):
    """A definitive refusal splits that interval into independently tried halves."""
    _, task = _task(turn_service, storage, 'batch-split')
    try:
        assert turn_service.record_task_event(task, {'type': 'phase', 'phase': 'starting'})
        base = _turn(turn_service, task)['projectionRevision']
        sync_before = _sync_sequence(storage, task)
        sizes = []
        original = storage.client.command

        def command(operation, payload, *args, **kwargs):
            if operation == 'turn.event.record_batch':
                sizes.append(len(payload['entries']))
                if len(payload['entries']) > 3:
                    raise StorageError('database_resource_exhausted',
                                       'Turn event batch exceeds one ordinary commit')
            return original(operation, payload, *args, **kwargs)

        monkeypatch.setattr(storage.client, 'command', command)
        entries = _entries(turn_service, task, _script())
        assert turn_service.record_task_events(task, entries) == ['carried'] * len(entries)
        assert sizes == [6, 3, 3]
        assert _turn(turn_service, task)['projectionRevision'] == base + len(entries)
        assert [row['sequence'] for row in _rows(storage, task)] == list(range(len(entries)))
        assert len(_sync_changes(storage, task, sync_before)) == len(entries)
    finally:
        _discard(task)


@pytest.mark.parametrize('lost_ack', [False, True])
def test_a_large_head_does_not_force_small_followers_into_single_event_commits(
        turn_service, storage, monkeypatch, lost_ack):
    _, task = _task(turn_service, storage, 'batch-heterogeneous')
    try:
        assert turn_service.record_task_event(task, {'type': 'phase', 'phase': 'starting'})
        original = storage.client.command
        accepted = []
        lose_ack = [lost_ack]

        def command(operation, payload, *args, **kwargs):
            if operation != 'turn.event.record_batch':
                return original(operation, payload, *args, **kwargs)
            sequences = [entry['task_event']['sequence'] for entry in payload['entries']]
            # One independently admissible large entry needs its own ordinary
            # commit. This says nothing about the later small entries.
            if sequences[0] == 0 and len(sequences) > 1:
                raise StorageError('database_resource_exhausted',
                                   'Turn event batch exceeds one ordinary commit')
            result = original(operation, payload, *args, **kwargs)
            accepted.append(sequences)
            if lose_ack and lose_ack.pop():
                raise StorageError('database_timeout', 'injected lost split acknowledgement', True)
            return result

        monkeypatch.setattr(storage.client, 'command', command)
        script = [(lambda task: None, {'type': 'phase', 'phase': 'calling'})] * 32
        entries = _entries(turn_service, task, script)
        if lost_ack:
            with pytest.raises(StorageError, match='lost split acknowledgement'):
                turn_service.record_task_events(task, entries)
        assert turn_service.record_task_events(task, entries) == ['carried'] * len(entries)
        assert accepted[0] == [0]
        assert any(len(part) > 1 for part in accepted[1:])
        assert [seq for part in accepted for seq in part] == list(range(32))
        assert [row['sequence'] for row in _rows(storage, task)] == list(range(32))
    finally:
        _discard(task)


def test_a_run_beyond_one_ordinary_commit_never_spills(turn_service, storage, monkeypatch, request):
    """Native TofuDB refuses a composite commit that would drain the overlay
    or synchronize loose blocks under authority; SQL commits it whole."""
    backend = request.node.callspec.params['storage']
    _, task = _task(turn_service, storage, 'batch-bulky')
    try:
        assert turn_service.record_task_event(task, {'type': 'phase', 'phase': 'starting'})
        base = _turn(turn_service, task)['projectionRevision']
        sync_before = _sync_sequence(storage, task)
        sizes, refusals = [], []
        original = storage.client.command

        def command(operation, payload, *args, **kwargs):
            if operation != 'turn.event.record_batch':
                return original(operation, payload, *args, **kwargs)
            sizes.append(len(payload['entries']))
            try:
                return original(operation, payload, *args, **kwargs)
            except StorageError as exc:
                refusals.append(exc.code)
                raise

        monkeypatch.setattr(storage.client, 'command', command)

        def spills():
            return (storage.client.metrics()['durability']['flushDeferred']
                    if backend == 'tofudb' else 0)

        # 40 inline evidence frames of 48 KiB each: about 3.7 MiB of new
        # attempt-event and row bytes, beyond one WAL-carried commit.
        script = [(lambda task: None, {
            'type': 'tool_compacted', 'roundNum': index,
            'summary': base64.b64encode(random.Random(index).randbytes(36 * 1024)).decode()})
            for index in range(40)]
        spills_before = spills()
        entries = _entries(turn_service, task, script)
        assert turn_service.record_task_events(task, entries) == ['carried'] * len(entries)
        assert spills() == spills_before
        if backend == 'tofudb':
            assert refusals and set(refusals) == {'database_resource_exhausted'}
            assert sizes[0] == len(entries) and len(sizes) - len(refusals) >= 2
        else:
            assert (sizes, refusals) == ([len(entries)], [])
        assert _turn(turn_service, task)['projectionRevision'] == base + len(entries)
        assert [row['sequence'] for row in _rows(storage, task)] == list(range(len(entries)))
        assert len(_sync_changes(storage, task, sync_before)) == len(entries)
    finally:
        _discard(task)


def test_large_inline_diagnostics_follow_authority_admission_per_entry(
        turn_service, storage, monkeypatch, request):
    _, task = _task(turn_service, storage, 'batch-evidence')
    try:
        assert turn_service.record_task_event(task, {'type': 'phase', 'phase': 'starting'})
        calls = _capture_batch(storage, monkeypatch)
        small = {'type': 'tool_compacted', 'roundNum': 1, 'summary': 'kept inline'}
        # Both authorities retain admitted evidence. Native also accepts a real
        # frame beyond the retired producer estimate; SQL retains its declared
        # durable transport envelope. Do not invent a smaller producer limit.
        size = 9 * 1024 * 1024 if request.node.callspec.params['storage'] == 'tofudb' else 128 * 1024
        large = {'type': 'tool_compacted', 'roundNum': 2, 'summary': 'x' * size}
        script = [(lambda task: None, small), (lambda task: None, large)]
        entries = _entries(turn_service, task, script)
        assert turn_service.record_task_events(task, entries) == ['carried', 'carried']
        kept, large_entry = calls[0]['entries']
        assert kept['event_payload']['event']['summary'] == 'kept inline'
        assert large_entry['event_payload']['event']['summary'] == large['summary']
        assert large_entry['task_event']['event']['summary'] == large['summary']
        assert _rows(storage, task)[-1]['event']['summary'] == large['summary']
        changes = _sync_changes(storage, task, 0)
        assert changes[-1]['payload']['event']['payload']['event']['summary'] == large['summary']
        assert not task.get('_debugEvidenceOmittedCount')
    finally:
        _discard(task)


@pytest.mark.parametrize('ineligible', [
    {'type': 'done', 'finishReason': 'stop'},
    {'type': 'approval_required', 'detail': 'continue?'},
    {'type': 'background_command_inject', 'previews': []},
    {'type': 'tool_effect', 'roundNum': 1},
])
def test_producer_refuses_ineligible_entries_before_any_write(
        turn_service, storage, monkeypatch, ineligible):
    _, task = _task(turn_service, storage, 'batch-ineligible')
    try:
        calls = _capture_batch(storage, monkeypatch)
        entries = _entries(turn_service, task, _script()[:1])
        entries.append(turn_service.TaskEventBatchEntry(raw_event={**ineligible, 'seq': 1},
                                                        sequence=1))
        with pytest.raises(ValueError):
            turn_service.record_task_events(task, entries)
        descending = _entries(turn_service, task, _script()[:2])
        descending[1].sequence = 0
        with pytest.raises(ValueError):
            turn_service.record_task_events(task, descending)
        assert calls == []
        assert _rows(storage, task) == []
    finally:
        _discard(task)
