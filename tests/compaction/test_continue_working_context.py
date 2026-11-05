"""Durable compaction, failed Continue, and loaded workflow continuity."""
from copy import deepcopy

import pytest

from tests.compaction.test_dispatch_context_recovery import _task, _round, _archive_fixture
from tests.compaction.test_compaction_history_wire import tool_round, user
from tests.compaction.test_unified_continuity import execution

pytestmark = pytest.mark.unit


def test_startup_archive_restores_before_the_first_tool_batch(monkeypatch):
    from lib.tasks_pkg.compaction._resume_projection import restore_compacted_resume
    from lib.tasks_pkg.conv_message_builder._toolcalls import _reconstruct_tool_call_messages
    task = _task()
    receipt, post, _ = _archive_fixture(monkeypatch, task, [])
    assert receipt['resumeWitness']['toolRoundCount'] == 0
    skill = _round(1, 'Skill loaded: **workflow**\nid: workflow\n<skill_instructions>exact rules</skill_instructions>')
    skill['toolName'] = 'load_skill'
    restored = restore_compacted_resume(task, task['config'], [skill])
    assert restored == post + _reconstruct_tool_call_messages([skill])


@pytest.fixture
def compaction_case(monkeypatch):
    from lib.tasks_pkg.compaction._layer2 import _compact as l2
    import lib.tasks_pkg.manager as manager
    import lib.agent_core.store as storage
    task = _task()
    task['config']['model'] = 'gpt-6-astra'
    events, saved = [], []
    failure = {'stage': ''}

    class Store:
        def archive_transcript(self, *a, **k):
            return None if failure['stage'] == 'create' else 'archive'

        def prune_archives(self, *a, **k):
            pass

        def update_archive_summary(self, *a, **k):
            if failure['stage'] == 'protocol':
                from lib.storage.errors import StorageError
                raise StorageError('database_protocol_error', 'archive summary is invalid',
                                 operation_id='compaction_archive.update_summary')
            if failure['stage'] == 'cancel_post':
                task['aborted'] = True
                raise OSError('cancelled during durable write')
            if failure['stage'] == 'post':
                raise OSError('injected durable write failure')
            saved.append(deepcopy(k))

    monkeypatch.setattr(storage, 'get_conversation_store', lambda: Store())
    monkeypatch.setattr(manager, 'append_event', lambda task, event: events.append(event))
    monkeypatch.setattr(l2, '_get_context_limit', lambda task: 1_000_000)
    monkeypatch.setattr(l2, '_extract_recently_accessed_files', lambda *a: [])
    monkeypatch.setattr(l2, '_generate_query_aware_summary', lambda *a, **k:
        '### Objective\nContinue authorized work.\n### Pending / Next Steps\nComplete remaining work.')
    return task, events, saved, failure


@pytest.mark.parametrize('stage', ['create', 'post'])
def test_archive_failure_preserves_original_model_input_and_reports_failure(compaction_case, stage):
    from lib.tasks_pkg.compaction._layer2 import _compact as l2
    from lib.storage.errors import StorageError
    from lib.turn_activity_timeline import fold_activity_timeline
    task, events, saved, failure = compaction_case
    failure['stage'] = stage
    messages = [user('Work'), {'role': 'assistant', 'content': 'earlier evidence ' * 5000}, user('Continue')]
    before = deepcopy(messages)
    with pytest.raises(StorageError, match='original context retained'):
        l2.force_compact_if_needed(messages, task, force=True, preserve_budget_tokens=1)
    assert messages == before
    assert not saved
    timeline = None
    for seq, event in enumerate(events):
        event.update(seq=seq + 1, emittedAt=1000 + seq * 10)
        timeline = fold_activity_timeline(timeline, event, task)
    failed = [entry for entry in timeline['entries'] if entry['reasonCode'] == 'context_compaction']
    assert len(failed) == 1 and failed[0]['status'] == 'failed'
    assert failed[0]['severity'] == 'warning'
    if stage == 'create':
        assert not failed[0].get('archiveId')


def test_archive_protocol_failure_keeps_its_nonretryable_typed_cause(compaction_case):
    from lib.tasks_pkg.compaction._layer2 import _compact as l2
    from lib.storage.errors import StorageError
    task, _, _, failure = compaction_case
    failure['stage'] = 'protocol'
    messages = [user('Work'), {'role': 'assistant', 'content': 'earlier evidence ' * 5000},
                user('Continue')]
    before = deepcopy(messages)
    with pytest.raises(StorageError) as caught:
        l2.force_compact_if_needed(messages, task, force=True, preserve_budget_tokens=1)
    assert caught.value.code == 'database_protocol_error'
    assert caught.value.retryable is False
    assert caught.value.operation_id == 'compaction_archive.update_summary'
    assert caught.value.message == 'archive summary is invalid'
    assert messages == before


@pytest.mark.parametrize('backend', ['sqlite', 'tofudb'])
def test_large_continuity_archive_commits_narrative_and_restores_full_post(
        monkeypatch, tmp_path, backend):
    from pathlib import Path
    import uuid
    import lib.agent_core.store
    from lib.storage import StorageSupervisor
    from lib.storage.errors import StorageError
    from lib.tasks_pkg import persistence_store
    from lib.tasks_pkg.compaction._continuity import read_continuity
    from lib.tasks_pkg.compaction._handoff import make_compaction_handoff
    from lib.tasks_pkg.compaction._layer2._compact import _persist_compaction_candidate
    from lib.tasks_pkg.compaction._layer2._summary_acceptance import accept_summary
    from lib.tasks_pkg.compaction._resume_projection import restore_compacted_resume

    if backend == 'tofudb':
        from tests.support.tofudb_binary import ensure_engine_binary
        engine = ensure_engine_binary(Path(__file__).resolve().parents[2])
        if engine is None:
            pytest.skip('explicit native test engine unavailable')
        monkeypatch.setenv('TOFUDB_BIN', str(engine))
    supervisor = StorageSupervisor(project_root=tmp_path, backend=backend, startup_timeout=60)
    supervisor.start()
    try:
        monkeypatch.setattr(persistence_store, '_storage_client',
                            lambda **kwargs: supervisor.client)
        store = persistence_store.DefaultConversationStore()
        monkeypatch.setattr(lib.agent_core.store, 'get_conversation_store', lambda: store)
        supervisor.client.command('conversation.create',
            {'conv_id': 'conversation', 'user_id': 1}, uuid.uuid4().hex)
        source = []
        for index in range(324):
            source += execution(f'test-{index}', f'pytest tests/example.py::{index}',
                                f'1 failed; invocation {index} ' + 'evidence ' * 80)
        accepted = accept_summary(
            'Model narrative', source, {'convId': 'conversation'}, fallback=False,
            recent_files_block='', turn_diff_block='', turn_diff_included=False,
            result_meta={}, log_prefix='', recovery_max_chars=22_000)
        assert len(accepted.text) > 200_000
        assert len(read_continuity([
            make_compaction_handoff(
                accepted.text, phase='mid_turn',
                continuity_digest=accepted.continuity_digest)]
        )['observations']) == 324
        assert len(accepted.archive_text) <= 200_000
        messages = [user('preserve objective'), make_compaction_handoff(
            accepted.text, phase='mid_turn', continuity_digest=accepted.continuity_digest)]
        task = _task()
        task['messages'] = messages
        archive = store.archive_transcript(
            'conversation', source, user_id=1, task_id='task', trigger='window')
        assert archive is not None
        with pytest.raises(StorageError) as rejected:
            store.update_archive_summary(
                archive, accepted.text, 100, len(messages), user_id=1,
                receipt={}, post_messages=messages)
        assert rejected.value.code == 'database_protocol_error'
        assert rejected.value.retryable is False

        meta = {'archive_id': archive, 'trigger': 'window', 'round_num': 672,
                'summary_text': accepted.text,
                'archive_summary_text': accepted.archive_text,
                'continuityDigest': accepted.continuity_digest,
                'handoff_phase': 'mid_turn'}
        receipt = _persist_compaction_candidate(messages, task, meta)
        assert receipt['summary']['chars'] == len(accepted.text)
        saved = store.get_compaction_archive(
            'conversation', archive, user_id=1, include_messages=False,
            include_post_messages=True)
        assert saved['archive']['summary'] == accepted.archive_text
        assert saved['postMessages'] == messages
        assert len(read_continuity(saved['postMessages'], task)['observations']) == 324
        supervisor.stop()
        supervisor.start()
        assert restore_compacted_resume(task, task['config'], []) == messages
    finally:
        supervisor.stop()


def test_loaded_skills_survive_l1_l2_and_durable_resume(compaction_case, monkeypatch):
    from lib.tasks_pkg.compaction._layer1 import micro_compact
    from lib.tasks_pkg.compaction._layer2 import _compact as l2
    from lib.tasks_pkg.compaction._resume_projection import restore_compacted_resume
    task, events, saved, failure = compaction_case
    skill = _round(0, 'Skill loaded: **workflow**\nid: workflow\ncontent_hash: abc\n<skill_instructions>' + 'Exact workflow rule.\n' * 1000 + '</skill_instructions>')
    skill['toolName'] = 'load_skill'
    from lib.tasks_pkg.conv_message_builder._toolcalls import _reconstruct_tool_call_messages
    task['toolRounds'] = [skill] + [_round(i) for i in range(1, 20)]
    messages = [user('Scoped work')] + _reconstruct_tool_call_messages(task['toolRounds'])
    messages.insert(1, {'role': 'assistant', 'content': 'earlier evidence ' * 5000})
    task['messages'] = messages
    micro_compact(messages, task=task)
    assert any(message.get('content') == skill['toolContent'] for message in messages)
    assert l2.force_compact_if_needed(messages, task, force=True, preserve_budget_tokens=1)
    assert any(message.get('content') == skill['toolContent'] for message in messages)
    assert saved and saved[-1]['post_messages'] == messages
    receipt = saved[-1]['receipt']

    class ReplayStore:
        def list_compaction_archives(self, *a, **k):
            return [{'id': 'archive', 'resultStatus': 'completed'}]

        def get_compaction_archive(self, *a, **k):
            return {'archive': {'receipt': receipt}, 'postMessages': deepcopy(messages) if k['include_post_messages'] else None}

    import lib.agent_core.store
    monkeypatch.setattr(lib.agent_core.store, 'get_conversation_store', lambda: ReplayStore())
    resumed = restore_compacted_resume(task, task['config'], task['toolRounds'])
    assert resumed == messages


def test_suspension_keeps_exact_working_context_after_background_delivery(compaction_case, monkeypatch):
    from lib.tasks_pkg.compaction._layer2 import _compact as l2
    from lib.tasks_pkg.compaction._resume_projection import (
        preserve_suspended_compaction, restore_compacted_resume, resume_background_digest, resume_execution_digest)
    from lib.tasks_pkg.compaction._handoff import is_compaction_handoff
    task, events, saved, failure = compaction_case
    messages = [user('Work'), {'role': 'assistant', 'content': 'earlier evidence ' * 5000}, user('Continue')]
    task['messages'] = messages
    assert l2.force_compact_if_needed(messages, task, force=True, preserve_budget_tokens=1)
    assert any(is_compaction_handoff(message) for message in messages)
    # The late receipt changes background truth after the original compaction.
    background = {'blockId': 'late-background', 'round': 1, 'previews': [
        {'text': 'exact background result', 'textTruncated': False}]}
    task['_bgCommandInjects'] = [background]
    messages.append({'role': 'user', 'content': 'exact background result', '_isMeta': True})
    task['status'] = 'error'
    task['aborted'] = True
    preserve_suspended_compaction(task)
    preserve_suspended_compaction(task)
    checkpoint = saved[-1]
    assert checkpoint['post_messages'] == messages
    assert checkpoint['receipt']['implementation'] == 'suspended_working_context'
    assert len(saved) == 2  # one compaction commit and one suspension, no repeated writes
    assert task['aborted'] is True
    config = deepcopy(task['config'])
    config['_resumeBackgroundDigest'] = resume_background_digest([background])
    config['_resumeExecutionDigest'] = resume_execution_digest(task['content'], task['thinking'])

    class Store:
        def list_compaction_archives(self, *a, **k):
            return [{'id': 'checkpoint', 'resultStatus': 'completed'}]
        def get_compaction_archive(self, *a, **k):
            return {'archive': {'receipt': checkpoint['receipt']},
                    'postMessages': deepcopy(messages) if k['include_post_messages'] else None}

    import lib.agent_core.store
    monkeypatch.setattr(lib.agent_core.store, 'get_conversation_store', lambda: Store())
    resumed_task = _task()
    # Even zero-tool suspension can precede the first successful provider round.
    resumed = restore_compacted_resume(resumed_task, config, [], fresh_messages=[])
    assert resumed == messages
    config['_resumeExecutionDigest'] = resume_execution_digest('edited after suspension', '')
    assert restore_compacted_resume(resumed_task, config, [], fresh_messages=[]) is None


def test_unfinished_tool_batch_cannot_publish_suspended_context(compaction_case):
    from lib.tasks_pkg.compaction._resume_projection import preserve_suspended_compaction
    from lib.tasks_pkg.compaction._handoff import make_compaction_handoff
    task, events, saved, failure = compaction_case
    task.update(status='error', _acceptedCompactionReceipt={'summary': {'accepted': True}})
    task['messages'] = [user('Scoped work'), make_compaction_handoff('Accepted progress', phase='pre_turn'),
        {'role':'assistant', 'content':'', 'tool_calls':[
            {'id':'unsettled', 'function':{'name':'write_file', 'arguments':'{}'}}]}]
    preserve_suspended_compaction(task)
    assert not saved and not task.get('_suspendedCompactionSaved')


def test_owner_stop_during_failed_snapshot_write_keeps_original_input(compaction_case):
    from lib.tasks_pkg.compaction._layer2 import _compact as l2
    task, events, saved, failure = compaction_case
    failure['stage'] = 'cancel_post'
    messages = [user('Work'), {'role':'assistant', 'content':'old evidence ' * 5000}, user('Continue')]
    before = deepcopy(messages)
    assert not l2.force_compact_if_needed(messages, task, force=True, preserve_budget_tokens=1)
    assert task['aborted'] and messages == before
    assert not task.get('_acceptedCompactionReceipt')


_STEER = 'Use the staging database instead of production.'


def _drain_inbox_carrier(task, messages, monkeypatch, events, mode):
    """Append one inbox carrier through the real round drain (not yet consumed)."""
    from lib import agent_inbox
    from lib.tasks_pkg.orchestrator._swarm_inbox import drain_and_inject_inbox
    monkeypatch.setattr('lib.tasks_pkg.orchestrator._deferred_inbox_flush.append_event',
                        lambda _task, event: events.append(event))
    agent_inbox.reset_for_test()
    extra = ({'injectionId': 'cmd', 'blockId': 'injection:user-steer:cmd'}
             if mode == 'user-steer' else {'fromConv': 'peer-conversation', 'queueId': 'q1'})
    agent_inbox.enqueue(task['convId'], _STEER, priority='next', mode=mode, extra=extra)
    drain_and_inject_inbox(task=task, messages=messages, round_num=1, tid='inbox')
    assert _STEER in messages[-1]['content']


def test_consumed_steer_keeps_the_compaction_resumable(compaction_case, monkeypatch):
    from lib import agent_inbox
    from lib.tasks_pkg.compaction._layer2 import _compact as l2
    from lib.tasks_pkg.compaction._resume_projection import restore_compacted_resume
    from lib.tasks_pkg.model_replay import UNVERIFIED_INBOX_DELIVERY
    from lib.tasks_pkg.orchestrator._deferred_inbox_flush import flush_deferred_peer_and_steer
    task, events, saved, failure = compaction_case
    messages = [user('Work'), {'role': 'assistant', 'content': 'earlier evidence ' * 5000}, user('Continue')]
    task['messages'] = messages
    try:
        _drain_inbox_carrier(task, messages, monkeypatch, events, 'user-steer')
        assert task[UNVERIFIED_INBOX_DELIVERY] is True
        # The provider returned, so the steer was consumed; the Turn keeps only
        # a display record and a canonical rebuild could not restore it.
        flush_deferred_peer_and_steer(task, round_num=1, tid='inbox')
    finally:
        agent_inbox.reset_for_test()
    assert task['_userSteerInjects'][0]['deliveryState'] == 'delivered'
    assert l2.force_compact_if_needed(messages, task, force=True, preserve_budget_tokens=1)
    receipt, post = saved[-1]['receipt'], saved[-1]['post_messages']
    assert receipt['resumeWitness']['turnId'] == 'turn'
    assert sum(_STEER in str(message.get('content')) for message in post) == 1

    class Store:
        def list_compaction_archives(self, *a, **k):
            return [{'id': 'archive', 'resultStatus': 'completed'}]

        def get_compaction_archive(self, *a, **k):
            return {'archive': {'receipt': deepcopy(receipt)},
                    'postMessages': deepcopy(post) if k['include_post_messages'] else None}

    import lib.agent_core.store
    monkeypatch.setattr(lib.agent_core.store, 'get_conversation_store', lambda: Store())
    resumed_task = _task()
    resumed_task['_attemptId'] = 'continue-attempt'
    assert restore_compacted_resume(resumed_task, task['config'], [], fresh_messages=[]) == post
    assert resumed_task['_compactionResumeOutcome'] == {'reason': 'restored', 'archiveId': 'archive'}


@pytest.mark.parametrize('mode', ['user-steer', 'peer-msg'])
def test_unconsumed_inbox_carrier_never_becomes_a_resume_point(compaction_case, monkeypatch, mode):
    # Unconsumed carriers are salvaged to the queue (steer) or redelivered from
    # their durable row (peer); a snapshot holding one would deliver it twice.
    from lib import agent_inbox
    from lib.tasks_pkg.compaction._layer2 import _compact as l2
    from lib.tasks_pkg.compaction._resume_projection import preserve_suspended_compaction
    from lib.tasks_pkg.model_replay import UNVERIFIED_INBOX_DELIVERY
    task, events, saved, failure = compaction_case
    messages = [user('Work'), {'role': 'assistant', 'content': 'earlier evidence ' * 5000}, user('Continue')]
    task['messages'] = messages
    try:
        _drain_inbox_carrier(task, messages, monkeypatch, events, mode)
    finally:
        agent_inbox.reset_for_test()
    assert task[UNVERIFIED_INBOX_DELIVERY] is True and not task.get('_userSteerInjects')
    # Dispatch admission compacts after the round drain, before the provider returns.
    assert l2.force_compact_if_needed(messages, task, force=True, preserve_budget_tokens=1)
    assert 'resumeWitness' not in saved[-1]['receipt']
    task.update(status='aborted', aborted=True)
    writes = len(saved)
    preserve_suspended_compaction(task)
    assert len(saved) == writes and not task.get('_suspendedCompactionSaved')
