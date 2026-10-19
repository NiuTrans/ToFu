"""Cross-backend application producers, including durable terminal delivery.

Unlike raw wire differential scripts, these tests run the real storage adapter
and domain facade against a disposable supervised authority for each backend.
"""
from __future__ import annotations

from pathlib import Path

import random
import threading
import time
import uuid

import pytest

pytestmark = pytest.mark.api


@pytest.fixture
def application_storage_owner_id():
    return 1


@pytest.fixture(params=['sqlite', 'tofudb',
                        pytest.param('tofudb_embedded',
                                     marks=pytest.mark.serial)])
def application_storage(request, tmp_path, monkeypatch, application_storage_owner_id):
    from lib.storage import StorageRuntime, StorageSupervisor
    from lib.storage import service

    monkeypatch.setenv('TOFUDB_OWNER_ID', str(application_storage_owner_id))
    monkeypatch.setenv('TOFUDB_TENANT_ID', '1')
    backend = request.param
    if backend == 'tofudb_embedded':
        # Same native authority as 'tofudb', in-process transport: the
        # application contract must hold without the daemon and its socket.
        monkeypatch.setenv('TOFUDB_TRANSPORT', 'embedded')
        backend = 'tofudb'
    elif backend == 'tofudb':
        # Keep the daemon stage honest: TOFUDB_TRANSPORT defaults to auto,
        # which resolves embedded on the local volumes tmp_path lives on.
        monkeypatch.setenv('TOFUDB_TRANSPORT', 'daemon')
    if backend == 'tofudb':
        from scripts.check_tofudb_engine_drift import resolve_deployment_binary
        repository_root = Path(__file__).resolve().parents[2]
        monkeypatch.setenv('TOFUDB_BIN', resolve_deployment_binary(repository_root))
    # Model/provider preparation reads the same deployment contract as storage.
    monkeypatch.setenv('TOFU_PERSONAL_STORAGE_BACKEND', backend)
    runtime = StorageRuntime(StorageSupervisor(
        project_root=tmp_path, backend=backend, startup_timeout=60),
        auto_restart=False)
    monkeypatch.setattr(service, 'storage_runtime', lambda: runtime)
    try:
        runtime.start()
    except RuntimeError as exc:
        if request.param == 'tofudb' and 'tofu-db binary' in str(exc):
            # The native engine is an optional build artifact (cargo); lanes
            # without it keep the sqlite coverage and skip the engine runs,
            # same optional-dependency contract as the browser/node guards.
            runtime.stop()
            pytest.skip('tofu-db engine binary not built (packages/tofu-db)')
        raise
    try:
        yield runtime.client()
    finally:
        runtime.stop()


@pytest.mark.parametrize("continuation_bytes", [128, 16 * 1024])
def test_terminal_event_persists_turn_attempt_and_replay(application_storage, monkeypatch, continuation_bytes):
    from lib.turn_lifecycle import create_turn_pair, bind_task, record_task_event, get_turn, get_attempt
    from lib.tasks_pkg.manager._persist import persist_task_result

    conv_id = f'contract-{uuid.uuid4().hex}'
    task_id = f'contract-{uuid.uuid4().hex}'
    pair = create_turn_pair(conv_id, command_id=f'create-{conv_id}',
        input_projection={'content': 'question'}, config={'model': 'gpt-4o'}, user_id=1,
        conversation_defaults={'allowCreate': True, 'title': 'Contract test', 'settings': {}})
    attempt_id = pair['attempt']['attemptId']
    bind_task(attempt_id, task_id, user_id=1)
    task = {
        'id': task_id, 'convId': conv_id, '_attemptId': attempt_id, '_userId': 1,
        'status': 'running', 'content': 'final answer', 'thinking': '',
        'toolRounds': [], 'segments': [], 'model': 'gpt-4o',
        'config': {'model': 'gpt-4o'}, 'created_at': time.time(),
        'abort_event': threading.Event(),
    }
    before = get_turn(conv_id, pair['turn']['turnId'], user_id=1)
    assert record_task_event(task, {'type': 'phase', 'phase': 'text'})
    after = get_turn(conv_id, pair['turn']['turnId'], user_id=1)
    events = application_storage.query('turn.events.list', {
        'attempt_id': attempt_id, 'user_id': 1, 'projection_mode': 'patch'})
    patch = events[-1]['payload']['projectionPatch']
    from lib.turn_projection_patch import apply_projection_patch
    assert patch['baseRevision'] == before['projectionRevision']
    assert patch['targetRevision'] == after['projectionRevision']
    assert apply_projection_patch(before['projection'], patch) == after['projection']
    import lib.tasks_pkg.model_replay as replay
    monkeypatch.setattr(replay, 'prepare_completed_task', lambda task: {
        'content': 'final answer',
        'projection': {
            'messages': [{'role': 'system', 'content': 'x' * continuation_bytes},
                         {'role': 'assistant', 'content': 'final answer'}],
            'turns': [[pair['turn']['turnId'], 'a' * 64]],
        },
    })
    task.update(status='done', finishReason='stop')
    terminal = {'type': 'done', 'finishReason': 'stop'}
    assert record_task_event(task, terminal, task_event={
        'task_id': task_id, 'sequence': 1, 'event': terminal}) == 'carried'
    assert persist_task_result(task)
    turn = get_turn(conv_id, pair['turn']['turnId'], user_id=1)
    assert turn['status'] == 'completed'
    assert turn['projection']['content'] == 'final answer'
    assert get_attempt(attempt_id, user_id=1)['status'] == 'completed'
    result = application_storage.query('task_results.replay_get', {'key': task_id, 'user_id': 1})
    assert result['status'] == 'done'
    events = application_storage.query('event.list', {'task_id': task_id, 'after': 0, 'limit': 10})
    assert [row['sequence'] for row in events] == [1]
    assert events[0]['event']['type'] == 'done'
    continuation = application_storage.query('model_continuation.get', {
        'conversation_id': conv_id, 'lane_id': 'main', 'user_id': 1})
    assert continuation['messages'][0]['content'] == 'x' * continuation_bytes
    assert continuation['turns'][0][0] == pair['turn']['turnId']


@pytest.mark.parametrize('ending', ['completed', 'restart'])
@pytest.mark.parametrize('initial_thinking', ['thought', ''], ids=['populated', 'empty'])
def test_text_only_patch_replays_carried_frame_and_settles_across_backends(
        application_storage, monkeypatch, ending, initial_thinking):
    from lib.turn_lifecycle import create_turn_pair, bind_task, get_turn, record_task_event
    from lib.turn_projection_patch import apply_projection_patch

    monkeypatch.setenv('TOFU_TURN_DELTA_RECORD_MS', '0')
    conv_id, task_id = uuid.uuid4().hex, uuid.uuid4().hex
    pair = create_turn_pair(conv_id, command_id=uuid.uuid4().hex, user_id=1,
        input_projection={'content': 'question'}, config={'model': 'benchmark'},
        conversation_defaults={'allowCreate': True, 'title': 'Text', 'settings': {}})
    attempt_id = pair['attempt']['attemptId']
    bind_task(attempt_id, task_id, user_id=1)
    task = {'id': task_id, 'convId': conv_id, '_attemptId': attempt_id, '_userId': 1,
            'status': 'running', 'content': 'base汉😀' * 16384, 'thinking': initial_thinking,
            'toolRounds': [], 'segments': [], 'model': 'benchmark',
            'config': {'model': 'benchmark'}, 'abort_event': threading.Event()}
    assert record_task_event(task, {'type': 'phase', 'phase': 'text'})
    before = get_turn(conv_id, pair['turn']['turnId'], user_id=1)
    task['content'] += ' suffix😀'
    wire = {'type': 'delta', 'content': ' suffix😀', 'seq': 1}
    assert record_task_event(task, wire, task_event={
        'task_id': task_id, 'sequence': 1, 'event': wire}) == 'carried'
    after = get_turn(conv_id, pair['turn']['turnId'], user_id=1)
    events = application_storage.query('turn.events.list', {
        'attempt_id': attempt_id, 'user_id': 1, 'projection_mode': 'patch'})
    patch = events[-1]['payload']['projectionPatch']
    assert patch['operations'] == [
        {'op': 'append_text', 'path': ['content'], 'value': ' suffix😀'}]
    assert apply_projection_patch(before['projection'], patch) == after['projection']
    replay = application_storage.query('event.list', {
        'task_id': task_id, 'after_sequence': 0, 'limit': 10})
    assert replay[0]['event'] == {'v': 1, **wire}
    if not initial_thinking:
        # Opening reasoning after cached text progress is a structural boundary.
        task['thinking'] = 'late thought😀'
        assert record_task_event(task, {'type': 'delta', 'thinking': task['thinking']})
    # Several text frames leave durable segment mirrors behind their roots.
    # The next structural patch must repair that exact wire base once.
    for suffix in (' second😀', ' third汉'):
        task['content'] += suffix
        assert record_task_event(task, {'type': 'delta', 'content': suffix})
    structural_before = get_turn(conv_id, pair['turn']['turnId'], user_id=1)
    assert record_task_event(task, {'type': 'phase', 'phase': 'text'})
    structural_after = get_turn(conv_id, pair['turn']['turnId'], user_id=1)
    structural_events = application_storage.query('turn.events.list', {
        'attempt_id': attempt_id, 'user_id': 1, 'projection_mode': 'patch'})
    assert apply_projection_patch(
        structural_before['projection'],
        structural_events[-1]['payload']['projectionPatch']) == structural_after['projection']
    from lib.turn_projection_segments import projection_with_stable_segments
    assert structural_after['projection']['segments'] == projection_with_stable_segments(
        structural_after['projection'], actor='assistant', status='running')['segments']
    task['content'] += ' terminal tail😀'
    assert record_task_event(task, {'type': 'delta', 'content': ' terminal tail😀'})
    if ending == 'restart':
        result = application_storage.command('turn.recover', {
            'created_before_ms': int(time.time() * 1000) + 1000,
        }, uuid.uuid4().hex)
        assert result['recovered'] == 1
    else:
        task.update(status='done', finishReason='stop')
        assert record_task_event(task, {'type': 'done', 'finishReason': 'stop'})
    settled = get_turn(conv_id, pair['turn']['turnId'], user_id=1)
    assert settled['status'] == ('interrupted' if ending == 'restart' else 'completed')
    assert settled['projection']['content'] == task['content']
    assert settled['projection']['thinking'] == task['thinking']
    assert settled['projection']['segments'] == projection_with_stable_segments(
        settled['projection'], actor='assistant', status=settled['status'])['segments']


def test_stream_progress_wakes_committed_event_observers(application_storage):
    from lib.storage.commit_events import subscribe_committed_events
    from lib.turn_lifecycle import create_turn_pair, bind_task, record_task_event

    conv_id = f'wake-{uuid.uuid4().hex}'
    pair = create_turn_pair(conv_id, command_id=f'create-{conv_id}',
        input_projection={'content': 'question'}, config={}, user_id=1,
        conversation_defaults={'allowCreate': True, 'title': 'Wake test', 'settings': {}})
    task_id = f'task-{uuid.uuid4().hex}'
    attempt_id = pair['attempt']['attemptId']
    bind_task(attempt_id, task_id, user_id=1)
    notices = []
    unsubscribe = subscribe_committed_events(notices.extend)
    try:
        task = {'id': task_id, 'convId': conv_id, '_attemptId': attempt_id,
                '_userId': 1, 'status': 'running', 'content': '', 'thinking': '',
                'toolRounds': [], 'segments': [], 'config': {}}
        assert record_task_event(task, {'type': 'phase', 'phase': 'text'})
        committed = tuple(notices)
        notices.clear()
        application_storage.query('system.schema_version', {})
        assert not notices, 'a later read must not replay a previous command wakeup'
        from lib.storage.errors import StorageError
        with pytest.raises(StorageError):
            application_storage.command('turn.event.record', {'user_id': 1},
                                        f'invalid-notice-{uuid.uuid4().hex}')
        assert not notices, 'a rejected command must not publish a wakeup'
    finally:
        unsubscribe()
    matching = [notice for notice in committed
                if notice.get('contract') == 'storage.conversation-commit/v1'
                and notice.get('userId') == 1
                and notice.get('event', {}).get('conversationId') == conv_id]
    assert matching, 'a committed stream update must wake readers before the heartbeat'
    assert all(notice['event']['syncSeq'] > 0 for notice in matching)


def test_startup_cleanup_accepts_application_default(application_storage):
    from lib.turn_lifecycle import cleanup_superseded_attempts
    assert cleanup_superseded_attempts() == 0


def test_system_schema_query_works_through_application_adapter(application_storage):
    from lib.integration_state_repository import initialize_store
    initialize_store()
    assert application_storage.query('system.schema_version', {})['version'] > 0


@pytest.mark.parametrize('snapshot_bytes', [16 * 1024, 256 * 1024, 1024 * 1024, 3 * 1024 * 1024])
def test_live_agent_message_snapshots_survive_replay(application_storage, snapshot_bytes):
    """A live provider/tool round emits full diagnostic history before settling.

    Settled-Turn inserts do not exercise this producer or its durable event
    carrier. Use the real facade and require every accepted snapshot to replay.
    """
    from lib.turn_lifecycle import create_turn_pair, bind_task, record_task_event

    conv_id = f'snapshots-{uuid.uuid4().hex}'
    pair = create_turn_pair(conv_id, command_id=f'create-{conv_id}',
        input_projection={'content': 'inspect tool output'}, config={}, user_id=1,
        conversation_defaults={'allowCreate': True, 'title': 'Snapshots', 'settings': {}})
    task_id = f'task-{uuid.uuid4().hex}'
    attempt_id = pair['attempt']['attemptId']
    bind_task(attempt_id, task_id, user_id=1)
    task = {'id': task_id, 'convId': conv_id, '_attemptId': attempt_id,
            '_userId': 1, 'status': 'running', 'content': '', 'thinking': '',
            'toolRounds': [], 'segments': [], 'config': {}}
    # Hex-encoded seeded entropy prevents tiny compressed fixtures from hiding
    # WAL/blob amplification seen with real tool and provider histories.
    content = random.Random(731).randbytes(snapshot_bytes // 2).hex()
    message = {'role': 'user', 'content': content}
    event = {'type': 'messages_snapshot', 'messages': [message], 'round': 1}
    assert record_task_event(task, event), 'live snapshot must persist before push'
    from lib.storage import service
    runtime = service.storage_runtime()
    runtime.stop()
    runtime.start()
    result = runtime.client().query('turn.events.list', {
        'attempt_id': attempt_id, 'after': 0, 'limit': 100, 'user_id': 1})
    rows = result['events'] if isinstance(result, dict) else result
    snapshots = [row['payload']['event'] for row in rows
                 if row.get('payload', {}).get('updateKind') == 'messages_snapshot']
    assert snapshots == [event], 'replay must preserve the complete accepted snapshot'


@pytest.mark.parametrize('verify_before_restart', [False, True])
def test_live_structural_event_preserves_large_projection(application_storage, verify_before_restart):
    """A structural event checkpoints accumulated output below the event cap."""
    from lib.turn_lifecycle import create_turn_pair, bind_task, record_task_event, get_turn

    conv_id = f'projection-{uuid.uuid4().hex}'
    pair = create_turn_pair(conv_id, command_id=f'create-{conv_id}',
        input_projection={'content': 'analyze a large tool result'}, config={}, user_id=1,
        conversation_defaults={'allowCreate': True, 'title': 'Large projection', 'settings': {}})
    task_id = f'task-{uuid.uuid4().hex}'
    bind_task(pair['attempt']['attemptId'], task_id, user_id=1)
    content = random.Random(117).randbytes(640 * 1024).hex()
    task = {'id': task_id, 'convId': conv_id, '_attemptId': pair['attempt']['attemptId'],
            '_userId': 1, 'status': 'running', 'content': content, 'thinking': '',
            'toolRounds': [], 'segments': [], 'config': {}}
    assert record_task_event(task, {'type': 'messages_snapshot', 'messages': [
        {'role': 'user', 'content': 'next provider round'}]})
    turn = get_turn(conv_id, pair['turn']['turnId'], user_id=1)
    assert turn['projection']['content'] == content
    # Cross the compact-head budget again after it already owns a checkpoint.
    task['content'] += random.Random(118).randbytes(640 * 1024).hex()
    assert record_task_event(task, {'type': 'messages_snapshot', 'messages': []})
    # A small patch following the fold must retain the same exact large base.
    task['content'] += '\ncontinued'
    assert record_task_event(task, {'type': 'messages_snapshot', 'messages': []})
    if verify_before_restart:
        current = get_turn(conv_id, pair['turn']['turnId'], user_id=1)
        assert current['projection']['content'] == task['content'], 'live projection lost the acknowledged suffix'
    from lib.storage import service
    runtime = service.storage_runtime()
    runtime.stop()
    runtime.start()
    recovered = get_turn(conv_id, pair['turn']['turnId'], user_id=1)
    assert recovered['projection']['content'] == task['content']
    assert recovered['projectionRevision'] > turn['projectionRevision']
    task['status'] = 'done'
    assert record_task_event(task, {'type': 'done', 'finishReason': 'stop'})
    settled = get_turn(conv_id, pair['turn']['turnId'], user_id=1)
    assert settled['status'] == 'completed'
    assert settled['projection']['content'] == task['content']


def test_concurrent_agent_rounds_retain_exact_independent_state(application_storage):
    """Three live producers and a reader share one authority without lost writes."""
    from concurrent.futures import ThreadPoolExecutor
    from lib.turn_lifecycle import create_turn_pair, bind_task, record_task_event, get_turn, read_events

    agents = []
    for index in range(3):
        conv = f'parallel-{uuid.uuid4().hex}'
        pair = create_turn_pair(conv, command_id=f'create-{conv}',
            input_projection={'content': 'parallel task'}, config={}, user_id=1,
            conversation_defaults={'allowCreate': True, 'title': 'Parallel', 'settings': {}})
        task_id = f'task-{uuid.uuid4().hex}'
        bind_task(pair['attempt']['attemptId'], task_id, user_id=1)
        task = {'id': task_id, 'convId': conv, '_attemptId': pair['attempt']['attemptId'],
                '_userId': 1, 'status': 'running', 'content': '', 'thinking': '',
                'toolRounds': [], 'segments': [], 'config': {}}
        agents.append((pair, task, random.Random(index).randbytes(32 * 1024).hex()))
    barrier = threading.Barrier(4, timeout=30)

    def writer(agent):
        pair, task, payload = agent
        barrier.wait()
        for round_number in range(8):
            task['content'] += f'round {round_number}: {payload[:1024]}\n'
            assert record_task_event(task, {'type': 'messages_snapshot',
                'round': round_number, 'messages': [{'role': 'user', 'content': payload}]})
        return task['content']

    def reader():
        barrier.wait()
        revisions = [0] * len(agents)
        for _ in range(12):
            for index, (pair, task, _) in enumerate(agents):
                row = get_turn(task['convId'], pair['turn']['turnId'], user_id=1)
                assert row['projectionRevision'] >= revisions[index]
                revisions[index] = row['projectionRevision']

    with ThreadPoolExecutor(max_workers=4) as pool:
        writes = [pool.submit(writer, agent) for agent in agents]
        reads = pool.submit(reader)
        contents = [future.result(timeout=90) for future in writes]
        reads.result(timeout=90)
    for (pair, task, payload), content in zip(agents, contents):
        row = get_turn(task['convId'], pair['turn']['turnId'], user_id=1)
        assert row['projection']['content'] == content, 'acknowledged live projection differs before restart'
    from lib.storage import service
    runtime = service.storage_runtime()
    runtime.stop()
    runtime.start()
    for (pair, task, payload), content in zip(agents, contents):
        row = get_turn(task['convId'], pair['turn']['turnId'], user_id=1)
        assert row['projection']['content'] == content
        events = read_events(task['_attemptId'], user_id=1)
        snapshots = [event['payload']['event'] for event in events
                     if event.get('payload', {}).get('updateKind') == 'messages_snapshot']
        assert [event['round'] for event in snapshots] == list(range(8))
        assert all(event['messages'] == [{'role': 'user', 'content': payload}]
                   for event in snapshots)


def test_sidebar_catalog_page_uses_real_application_payload(application_storage):
    """Sidebar reads are required even when the browser can open a chat directly."""
    from lib.conversations.repository import ConversationRepository
    conv_id = f'catalog-{uuid.uuid4().hex}'
    application_storage.command('conversation.create', {
        'conv_id': conv_id, 'user_id': 1, 'title': 'Catalog contract',
        'settings': {'pinned': True, 'projectPath': '/contract'}}, f'create-{conv_id}')
    # Match the production sidebar request, including its larger default page.
    page = ConversationRepository().list_catalog_page(
        user_id=1, limit=500, before_id='', settings_keys=['pinned', 'projectPath'])
    assert page.total_count == 1
    assert not page.has_more
    assert len(page.items) == 1
    assert page.items[0].metadata['id'] == conv_id


def test_online_reclaim_reports_capability_and_preserves_conversations(application_storage, request):
    """The public port reports real reclaim capability and preserves live records."""
    from lib.conversations.repository import ConversationRepository
    conv_id = f'reclaim-{uuid.uuid4().hex}'
    application_storage.command('conversation.create', {
        'conv_id': conv_id, 'user_id': 1, 'title': 'Retain me', 'settings': {}},
        f'create-{conv_id}')
    result = application_storage.command('system.reclaim', {
        'max_pages': 32, 'min_free_pages': 0, 'budget_ms': 10}, f'reclaim-{conv_id}')
    assert result['reclaimed'] >= 0
    if request.node.callspec.params['application_storage'] in (
            'tofudb', 'tofudb_embedded'):
        assert result['backend'] == 'tofu-db'
        assert result['budget_ms'] == 10
        assert type(result['more_candidates']) is bool
        # Online reclaim (docs/TOFUDB_ONLINE_GC.md): the RPC is an
        # observation + doorbell boundary over the online collector, never a
        # physical lane — the offline CLI remains the exclusive GC path.
        assert result['offline_required'] is False
        assert result['reason_code'] is None
        assert type(result['statistics_complete']) is bool
        assert result['reclaimed'] >= 0 and result['reclaimed_bytes'] >= 0
        assert result['more_candidates'] == result['epoch_in_progress']
    page = ConversationRepository().list_catalog_page(
        user_id=1, limit=500, before_id='', settings_keys=[])
    assert page.total_count == 1
    assert page.items[0].metadata['id'] == conv_id


def test_large_snapshot_carrier_is_atomic_replayable_and_survives_restart(application_storage):
    from lib.turn_lifecycle import create_turn_pair, bind_task, record_task_event, get_turn, read_events
    from lib.storage import service
    conv = 'large-carrier-' + uuid.uuid4().hex
    pair = create_turn_pair(conv, command_id=conv, input_projection={'content': 'request'},
        config={}, user_id=1, conversation_defaults={'allowCreate': True, 'settings': {}})
    task_id = 'task-' + uuid.uuid4().hex
    bind_task(pair['attempt']['attemptId'], task_id, user_id=1)
    task = {'id': task_id, 'convId': conv, '_attemptId': pair['attempt']['attemptId'],
            '_userId': 1, 'status': 'running', 'content': 'committed answer', 'thinking': '',
            'toolRounds': [], 'segments': [], 'config': {}}
    body = random.Random(73).randbytes(1100000).hex()
    event = {'type': 'messages_snapshot', 'round': 0,
             'messages': [{'role': 'user', 'content': body}]}
    carrier = {'task_id': task_id, 'sequence': 1, 'event': event}
    assert record_task_event(task, event, task_event=carrier)
    task.update(status='done', finishReason='stop')
    terminal = {'type': 'done', 'finishReason': 'stop'}
    assert record_task_event(task, terminal, task_event={
        'task_id': task_id, 'sequence': 2, 'event': terminal})
    runtime = service.storage_runtime()
    runtime.stop(); runtime.start()
    row = get_turn(conv, pair['turn']['turnId'], user_id=1)
    assert row['status'] == 'completed' and row['projection']['content'] == 'committed answer'
    snapshots = [e['payload']['event'] for e in read_events(task['_attemptId'], user_id=1)
                 if e.get('payload', {}).get('updateKind') == 'messages_snapshot']
    assert snapshots == [event]
    rows = runtime.client().query('event.list', {'user_id': 1, 'task_id': task_id, 'after': 0, 'limit': 10})
    assert len(rows) == 2 and rows[0]['event']['messages'][0]['content'] == body
    replay = runtime.client().command('event.append', carrier | {'user_id': 1}, uuid.uuid4().hex)
    assert replay['inserted'] is False


def test_repeated_concurrent_agent_restart_keeps_every_acknowledged_round(application_storage):
    for _ in range(5):
        test_concurrent_agent_rounds_retain_exact_independent_state(application_storage)


def test_stable_segment_patch_replays_from_exact_committed_base(application_storage):
    from lib.turn_lifecycle import create_turn_pair, bind_task, record_task_event, get_turn
    from lib.turn_projection_patch import apply_projection_patch
    conv_id = f'patch-base-{uuid.uuid4().hex}'
    pair = create_turn_pair(conv_id, command_id=f'create-{conv_id}',
        input_projection={'content': 'question'}, config={}, user_id=1,
        conversation_defaults={'allowCreate': True, 'title': 'Patch base', 'settings': {}})
    attempt_id = pair['attempt']['attemptId']
    task_id = f'task-{uuid.uuid4().hex}'
    bind_task(attempt_id, task_id, user_id=1)
    task = {'id': task_id, 'convId': conv_id, '_attemptId': attempt_id,
            '_userId': 1, 'status': 'running', 'content': '', 'thinking': '',
            'toolRounds': [], 'segments': [], 'config': {}}
    for text in ['first answer', 'first answer continued']:
        before = get_turn(conv_id, pair['turn']['turnId'], user_id=1)
        task['content'] = text
        assert record_task_event(task, {'type': 'phase', 'phase': 'text'})
        after = get_turn(conv_id, pair['turn']['turnId'], user_id=1)
        events = application_storage.query('turn.events.list', {
            'attempt_id': attempt_id, 'user_id': 1, 'projection_mode': 'patch'})
        patch = events[-1]['payload']['projectionPatch']
        assert patch['baseRevision'] == before['projectionRevision']
        assert patch['targetRevision'] == after['projectionRevision']
        assert apply_projection_patch(before['projection'], patch) == after['projection']
        assert after['projection']['content'] == text


def test_conversation_inspector_api_reads_real_owner_scoped_projection(application_storage, monkeypatch):
    """Compose the real HTTP handlers with each authority and the diagnostic reader."""
    import asyncio
    import json
    from urllib.parse import urlsplit
    from quart import g
    from lib.app.app_factory import create_base_app
    from debug import inspect_conversation as inspector
    from lib.api_keys import local_admin_context
    from lib.api_v4 import success_envelope
    from lib.tasks_pkg.persistence_store import DefaultConversationStore
    from lib.turn_lifecycle import create_turn_pair
    import routes.conversations as conversations
    import routes.conversations_compaction as compactions

    conv_id = 'inspect-' + uuid.uuid4().hex
    create_turn_pair(conv_id, command_id=conv_id, input_projection={'content': 'native question'},
        config={}, user_id=1, conversation_defaults={'allowCreate': True, 'title': 'Inspect me', 'settings': {}})
    store = DefaultConversationStore()
    archive_id = store.archive_transcript(conv_id, [{'role': 'user', 'content': 'before compaction'}],
        user_id=1, summary='durable diagnostic summary', receipt={'status': 'completed'})
    assert archive_id
    monkeypatch.setattr(compactions, 'get_conversation_store', lambda: store)
    app = create_base_app(__name__)

    class RouteAPI(inspector._DiagnosticAPI):
        def __init__(self):
            super().__init__('http://127.0.0.1:15000', None)
            self.paths = []

        def get(self, path, *, missing_ok=False):
            self.requests += 1
            self.paths.append(path)
            async def invoke():
                async with app.test_request_context(path):
                    g.auth_ctx = local_admin_context()
                    resource = urlsplit(path).path
                    if resource.endswith('/diagnostic'):
                        response, status = await conversations.get_conv_diagnostic(conv_id)
                    elif resource.endswith('/compactions'):
                        response, status = await compactions.list_compactions(conv_id)
                    elif '/compactions/' in resource:
                        response, status = await compactions.get_compaction(conv_id, archive_id)
                    else:
                        response, status = await conversations.get_conv(conv_id)
                    assert status == 200
                    wire = success_envelope(await response.get_json(), request_id='test', server_time_ms=1)
                    self.bytes_read += len(json.dumps(wire, ensure_ascii=False).encode('utf-8'))
                    return wire['data']
            return asyncio.run(invoke())

    api = RouteAPI()
    document, archives = inspector._load_api_document(api, conv_id, full=True)
    assert api.bytes_read > 0 and api.requests == len(api.paths)
    assert any('kind=chunk' in path for path in api.paths)
    assert all('/diagnostic?' in path or '/compactions' in path for path in api.paths)
    assert api.reserved_bytes == api.reserved_requests == 0
    assert document['metadata']['title'] == 'Inspect me'
    assert document['messages'][0]['content'] == 'native question'
    assert len(document['messages']) == document['metadata']['msg_count'] == 2
    assert archives[0]['id'] == archive_id
    assert archives[0]['summary'] == 'durable diagnostic summary'
    assert archives[0]['receipt']['status'] == 'completed'


def test_flow_recovery_receipts_survive_authority_and_fence_late_publish(application_storage):
    """The same recovery evidence passes native and SQL operation boundaries."""
    from lib.orchestration.execution_journal import ExecutionJournal
    from lib.orchestration.sidecar_run_store import SidecarOrchestrationRunStore
    from lib.orchestration.startup_recovery import retire_interrupted_orchestration_runs
    from lib.orchestration.run_service import OrchestrationRunService
    from lib.orchestration.run_recovery import load_recovery_records, record_recovery_claim

    from lib.storage.service import storage_runtime
    runtime = storage_runtime()
    client = lambda **_: runtime.client()
    store = SidecarOrchestrationRunStore(1, client=client)
    run_id = store.new_run_id()
    definition = {'name': 'Recovery boundary', 'nodes': [], 'edges': []}
    assert store.create_run(run_id, definition=definition, input_text='seed')
    sequence = 0
    def emit(event):
        nonlocal sequence
        assert store.project_event(run_id, sequence, event)
        sequence += 1
    journal = ExecutionJournal(emit)
    journal.begin(definition, 'seed')
    assert journal.call('human', 'decision', 0, {'prompt': 'confirm'},
                        lambda: {'context': 'approved', 'aborted': False})['context'] == 'approved'
    # Restart the actual authority process over its committed WAL/database.
    runtime.stop()
    runtime.start()
    interrupted = {'kind': 'worker_lost', 'source': 'orchestration.startup_recovery',
                   'message': 'test process loss'}
    assert retire_interrupted_orchestration_runs(error=interrupted,
        client=client) >= 1
    runs = OrchestrationRunService(store)
    _, records = load_recovery_records(runs, run_id)
    assert len(records) == 1
    assert store.project_event(run_id, sequence, {'type': 'execution_effect_started'}) is False
    assert len(store.get_events(run_id)) == sequence  # rejected write rolled back
    other = SidecarOrchestrationRunStore(2, client=client)
    from lib.orchestration.run_store_port import OrchestrationRunStoreError
    try:
        foreign_result = other.get_run(run_id)
    except OrchestrationRunStoreError as exc:
        # Native authorities reject mismatched owner scopes at admission; SQL
        # authorities return an empty owner-filtered result. Neither leaks data.
        foreign_result = ('forbidden', exc.__cause__.code)
    assert foreign_result is None or (
        isinstance(foreign_result, tuple)
        and foreign_result == ('forbidden', 'database_forbidden'))
    assert other.append_event(run_id, sequence, {'type': 'execution_recovery_claim'}) is False
    record_recovery_claim(runs, run_id, 'child')
    assert store.get_events(run_id)[-1]['successor_run_id'] == 'child'
    assert store.delete_run(run_id)


def test_durable_human_wait_restarts_and_resumes_once(application_storage, monkeypatch):
    from types import SimpleNamespace
    from lib.orchestration.sidecar_run_store import SidecarOrchestrationRunStore
    from lib.orchestration.run_service import OrchestrationRunService
    from lib.orchestration.runtime_start_service import OrchestrationRuntimeStartService
    from lib.orchestration.startup_recovery import retire_interrupted_orchestration_runs
    from lib.orchestration.continuation import history
    from lib.orchestration.engine import FlowExecutor
    import lib.orchestration.runtime_service as service
    from lib.storage.service import storage_runtime
    from tests.orchestration.test_orchestration_execution_recovery import graph, role
    authority = storage_runtime()
    store = SidecarOrchestrationRunStore(1, client=lambda **_: authority.client())
    runs = OrchestrationRunService(store)
    calls = []
    def executor(definition, **options):
        options['agent_runner'] = lambda node, *_: calls.append(node['id']) or {'output': node['id']}
        return FlowExecutor(definition, **options)
    monkeypatch.setattr(service, 'create_flow_executor', executor)
    class Runtime:
        def __init__(self):
            self.tasks, self.spawns = {}, 0
        def create(self, *, task_id, **kwargs):
            task = {'id': task_id, 'abort_event': threading.Event(), 'events': []}
            self.tasks[task_id] = task
            return task
        def append_event(self, task_id, event):
            events = self.tasks[task_id]['events']
            events.append(event)
            return len(events) - 1
        def finish(self, task_id, **kwargs):
            self.tasks[task_id]['finished'] = kwargs
            return True
        def spawn(self, task_id, fn):
            self.spawns += 1
            fn()
    runtime = Runtime()
    starts = OrchestrationRuntimeStartService(runtime,
        definition_service=lambda: SimpleNamespace(get_definition=lambda _: None),
        run_service=lambda: runs)
    definition = graph(role('a'), {'id': 'h', 'type': 'control', 'kind': 'human',
                                  'params': {'mode': 'input'}}, role('b'),
                       {'id': 'h2', 'type': 'control', 'kind': 'human',
                        'params': {'mode': 'approve'}}, role('c'))
    run_id = starts.start('durable', definition, owner_user_id=1)
    assert runs.get(run_id)['status'] == 'paused'
    assert runtime.tasks[run_id]['finished']['result']['suspended']
    assert calls == ['a']
    events, _ = history(runs, run_id)
    request = events[-1]['request_id']
    authority.stop()
    authority.start()
    retire_interrupted_orchestration_runs(error={'kind': 'worker_lost'},
                                          client=lambda **_: authority.client())
    assert runs.get(run_id)['status'] == 'paused'
    foreign = OrchestrationRuntimeStartService(runtime,
        definition_service=lambda: SimpleNamespace(get_definition=lambda _: None),
        run_service=lambda: OrchestrationRunService(SidecarOrchestrationRunStore(
            2, client=lambda **_: authority.client())))
    from lib.orchestration.errors import RunServiceError
    try:
        foreign_result = foreign.resolve_human_decision(
            request, 'answer', owner_user_id=2, mode='input')
    except RunServiceError as exc:
        # Native credentials reject a forged owner before repository lookup.
        foreign_result = ('forbidden', exc.__cause__.__cause__.code)
    assert foreign_result is False or (
        isinstance(foreign_result, tuple)
        and foreign_result == ('forbidden', 'database_forbidden'))
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as workers:
        replies = list(workers.map(lambda _: starts.resolve_human_decision(
            request, 'answer', owner_user_id=1, mode='input'), range(4)))
    assert all(replies)
    assert calls == ['a', 'b']
    assert runs.get(run_id)['status'] == 'paused'
    assert starts.resolve_human_decision(request, 'answer', owner_user_id=1, mode='input')
    assert runtime.spawns == 2
    events, _ = history(runs, run_id)
    second = events[-1]['request_id']
    assert starts.resolve_human_decision(second, True, owner_user_id=1, mode='approve')
    assert runs.get(run_id)['status'] == 'done'
    assert calls == ['a', 'b', 'c']
    assert runtime.spawns == 3
    expiring = starts.start('durable', graph(
        {'id': 'deadline', 'type': 'control', 'kind': 'human', 'params': {'mode': 'approve'}},
        role('unreachable')), owner_user_id=1)
    assert runs.get(expiring)['status'] == 'paused'
    monkeypatch.setattr('lib.orchestration.continuation.time',
                        SimpleNamespace(time=lambda: time.time() + 3600))
    assert runs.get(expiring)['status'] == 'aborted'
    assert calls == ['a', 'b', 'c']


def test_goal_model_context_and_checklist_survive_authority_restart(application_storage):
    from copy import deepcopy
    from tests.storage.test_storage_authority_sql_contract import _import_conversation
    from lib.goal_runs.context_recovery import publish_worker_context
    from lib.goal_runs.worker_context import GoalWorkerContext
    from lib.tasks_pkg.model_replay import restore_prefix
    from lib.storage.service import storage_runtime
    from lib.storage.model_continuation import ModelContinuationStore
    authority = storage_runtime()
    conv = 'goal-recovery-' + uuid.uuid4().hex
    _import_conversation(application_storage, conv, messages=[
        {'role': 'user', 'content': 'objective'},
        {'role': 'assistant', 'content': 'completed worker'}])
    rows = application_storage.query('turn.list', {'conversation_id': conv, 'user_id': 1})
    messages = [{'role': 'user', 'content': 'objective'},
                {'role': 'assistant', 'content': '', 'tool_calls': [
                    {'id': 'completed-call', 'type': 'function',
                     'function': {'name': 'fixture_read', 'arguments': '{}'}}]},
                {'role': 'tool', 'tool_call_id': 'completed-call', 'content': 'committed evidence'},
                {'role': 'assistant', 'content': 'completed worker'}]
    task = {'id': 'fixture-task', '_userId': 1, 'convId': conv,
            '_attemptId': 'settled-fixture', '_turnId': rows[-1]['turnId'],
            '_turnVisibleRunTurnIds': [rows[-1]['turnId']],
            'config': {'autopilot': True, 'model': 'fixture-model'},
            '_goalWorkerCheckpoint': {'messages': deepcopy(messages),
                                      'task_state': {'_todos': [{'id': '1', 'content': 'Verify durable evidence', 'status': 'completed'}]}}}
    assert publish_worker_context(task)
    authority.stop()
    authority.start()
    cached = ModelContinuationStore().get(user_id=1, conversation_id=conv, lane_id='main')
    assert cached['messages'] == messages
    # The public reader validates the prefix before releasing either messages
    # or checklist state. An edit invalidates both, without mutating history.
    config = {'autopilot': True, 'model': 'fixture-model'}
    from lib.turn_lifecycle import create_turn_pair
    pair = create_turn_pair(conv, command_id='continue-' + conv,
        input_projection={'content': 'continue'}, config=config, user_id=1)
    pending = pair['turn']
    validated = ModelContinuationStore().read_context(user_id=1,
        conversation_id=conv, turn_id=pending['turnId'], replay=True)
    assert validated['continuation'] is not None
    restored, _ = restore_prefix(validated['turns'], config, conversation_id=conv, user_id=1,
                                 validated_context=validated)
    assert restored == messages
    context = GoalWorkerContext()
    context.prepare({'messages': restored, 'config': config})
    assert context.task_state['_todos'][0]['status'] == 'completed'
    changed = {'autopilot': True, 'model': 'fixture-model', 'grantedToolPermissions': ['different']}
    assert restore_prefix(validated['turns'], changed, conversation_id=conv, user_id=1,
                          validated_context=validated) == ([], 0)
    assert '_goalWorkerState' not in changed
    assert authority.client().query('turn.list', {'conversation_id': conv, 'user_id': 1})[:len(rows)] == rows


def test_large_compaction_archive_keeps_suspended_workflow_after_restart(application_storage, monkeypatch):
    """Archive >8 MiB crosses real transport; Continue reuses a bounded exact view."""
    from copy import deepcopy
    from lib.storage import service
    from lib.tasks_pkg import persistence_store
    from lib.tasks_pkg.compaction._resume_projection import (
        compaction_resume_witness, restore_compacted_resume)
    from lib.tasks_pkg.compaction._handoff import make_compaction_handoff
    from lib.tasks_pkg.compaction._receipt import build_compaction_receipt
    from tests.compaction.test_dispatch_context_recovery import _task
    import lib.agent_core.store
    conv = 'large-compaction-' + uuid.uuid4().hex
    application_storage.command('conversation.create', {'conv_id': conv, 'user_id': 1}, conv)
    store = persistence_store.DefaultConversationStore()
    monkeypatch.setattr(lib.agent_core.store, 'get_conversation_store', lambda: store)
    task = _task()
    task['convId'] = conv
    pre = [{'role': 'tool', 'content': '界' * (3 * 1024 * 1024)}]
    skill = 'Skill loaded: **workflow**\nid: workflow\n<skill_instructions>exact workflow</skill_instructions>'
    post = [{'role': 'user', 'content': 'Scoped work'},
            make_compaction_handoff('Accepted progress', phase='pre_turn'),
            {'role': 'assistant', 'content': skill}]
    task['messages'] = post
    receipt = build_compaction_receipt(trigger='window', status='completed',
        strategy='selective_summary', implementation='model_summary',
        summary_generated=True, summary_text='Accepted progress')
    receipt['resumeWitness'] = compaction_resume_witness(task)
    archive = store.archive_transcript(conv, pre, user_id=1, task_id='task', trigger='window')
    assert archive is not None, 'archive transport discarded a valid long conversation'
    store.update_archive_summary(archive, 'Accepted progress', 100, len(post),
        user_id=1, receipt=receipt, post_messages=post)
    runtime = service.storage_runtime()
    runtime.stop()
    runtime.start()
    fetched = store.get_compaction_archive(conv, archive, user_id=1,
        include_messages=True, include_post_messages=True)
    assert fetched['messages'] == pre
    restored = restore_compacted_resume(task, task['config'], [])
    assert restored == post
    assert any(message.get('content') == skill for message in restored)
