"""An interrupted Turn keeps its exact working view, so Continue is seamless.

Every interruption (Stop, process restart, provider failure) used to rebuild the
model view from transcripts on Continue, re-acquiring and re-noting all turn-start
context. The suspended working view is now checkpointed for every root Turn that
does not complete, rewound to its last protocol-valid boundary, and restored on
Continue before context is composed.
"""
from copy import deepcopy

import pytest

from tests.compaction.test_dispatch_context_recovery import _round, _task

pytestmark = pytest.mark.unit


class _Archives:
    """In-memory archive owner with the conversation store's archive surface."""

    def __init__(self):
        self.rows = {}
        self.creates = []
        self.updates = []

    def archive_transcript(self, conv_id, messages, *, user_id, trigger='force',
                           task_id='', receipt=None, **kwargs):
        archive = f'archive-{len(self.rows) + 1}'
        self.creates.append({'messages': deepcopy(messages), 'trigger': trigger})
        self.rows[archive] = {'receipt': deepcopy(receipt or {}), 'post': None}
        return archive

    def update_archive_summary(self, archive_id, summary, tokens_after, msgs_after, *,
                               user_id, receipt=None, post_messages=None, **kwargs):
        row = self.rows[archive_id]
        if receipt is not None:
            row['receipt'] = deepcopy(receipt)
        if post_messages is not None:
            row['post'] = deepcopy(post_messages)
        self.updates.append(archive_id)
        return {'updated': True}

    def prune_archives(self, *args, **kwargs):
        return 0

    def list_compaction_archives(self, conversation, *, user_id, limit):
        return [{'id': archive, 'resultStatus': row['receipt'].get('status', 'legacy')}
                for archive, row in reversed(self.rows.items())][:limit]

    def get_compaction_archive(self, conversation, archive, *, user_id,
                               include_messages, include_post_messages, **kwargs):
        row = self.rows[archive]
        return {'archive': {'receipt': deepcopy(row['receipt'])},
                'postMessages': deepcopy(row['post']) if include_post_messages else None}


@pytest.fixture
def archives(monkeypatch):
    import lib.agent_core.store
    store = _Archives()
    monkeypatch.setattr(lib.agent_core.store, 'get_conversation_store', lambda: store)
    return store


def _carrier(text):
    return {'role': 'user', 'content': [{'type': 'text', 'text': text}],
            '_isMeta': True, '_contextComposer': True,
            '_contextBlocks': [{'id': 'relevant_memories', 'hash': 'h', 'stability': 'turn',
                                'lifecycle': 'task'}]}


def _interrupted(task, rounds, view):
    from lib.tasks_pkg.conv_message_builder._toolcalls import _reconstruct_tool_call_messages
    task['toolRounds'] = rounds
    task['messages'] = view if view is not None else (
        [{'role': 'user', 'content': 'work'}, _carrier('memories selected for this turn')]
        + _reconstruct_tool_call_messages(rounds))
    task['_messages_include_current_run'] = True
    task.update(status='done', aborted=True, finishReason='aborted')
    return task


def test_interrupted_turn_without_compaction_saves_its_exact_working_view(archives):
    from lib.tasks_pkg.compaction._resume_projection import preserve_suspended_working_view
    rounds = [_round(i) for i in range(3)]
    task = _interrupted(_task(), rounds, None)
    view = deepcopy(task['messages'])
    preserve_suspended_working_view(task)
    preserve_suspended_working_view(task)
    assert len(archives.creates) == 1 and archives.updates == ['archive-1']
    # The checkpoint is not a compaction: it keeps no second source transcript.
    assert archives.creates[0] == {'messages': [], 'trigger': 'checkpoint'}
    row = archives.rows['archive-1']
    assert row['post'] == view
    receipt = row['receipt']
    assert receipt['implementation'] == 'suspended_working_context'
    assert receipt['status'] == 'completed' and 'summary' not in receipt
    assert receipt['resumeWitness']['turnId'] == 'turn'
    assert receipt['resumeWitness']['toolRoundCount'] == 3
    assert task['_suspendedCompactionArchiveId'] == 'archive-1'


@pytest.mark.parametrize('terminal', [
    {'status': 'done', 'finishReason': 'stop', 'content': 'final answer'},
])
def test_completed_turn_saves_no_checkpoint(archives, terminal):
    from lib.tasks_pkg.compaction._resume_projection import preserve_suspended_working_view
    task = _interrupted(_task(), [_round(0)], None)
    task.pop('aborted')
    task.update(terminal)
    preserve_suspended_working_view(task)
    assert not archives.creates and not archives.updates


def test_input_only_messages_are_not_a_working_view(archives):
    from lib.tasks_pkg.compaction._resume_projection import preserve_suspended_working_view
    task = _interrupted(_task(), [_round(0)], None)
    task['_messages_include_current_run'] = False
    preserve_suspended_working_view(task)
    assert not archives.creates


def test_view_missing_a_witnessed_round_is_never_checkpointed(archives):
    """An auto-retry restores the pristine input; it is not this Turn's view."""
    from lib.tasks_pkg.compaction._resume_projection import preserve_suspended_working_view
    task = _interrupted(_task(), [_round(0), _round(1)],
                        [{'role': 'user', 'content': 'work'}])
    preserve_suspended_working_view(task)
    assert not archives.creates and not task.get('_suspendedCompactionSaved')


def test_unfinished_tool_batch_rewinds_to_the_last_protocol_valid_boundary(archives):
    from lib.tasks_pkg.compaction._resume_projection import preserve_suspended_working_view
    from lib.tasks_pkg.conv_message_builder._toolcalls import _reconstruct_tool_call_messages
    settled = [_round(0), _round(1)]
    first_open, second_open = _round(2), _round(3)
    second_open['llmRound'] = 2
    boundary = ([{'role': 'user', 'content': 'work'}, _carrier('memories')]
                + _reconstruct_tool_call_messages(settled))
    # One call of the in-flight batch returned before the interruption.
    open_batch = _reconstruct_tool_call_messages([first_open, second_open])
    view = boundary + open_batch[:2]
    rounds = settled + [first_open, {**second_open, 'toolContent': None, 'status': 'running'}]
    task = _interrupted(_task(), rounds, view)
    preserve_suspended_working_view(task)
    row = archives.rows['archive-1']
    assert row['post'] == boundary
    assert row['receipt']['resumeWitness']['toolRoundCount'] == 2


def test_continue_restores_the_checkpoint_plus_rounds_settled_after_it(archives):
    from lib.tasks_pkg.compaction._resume_projection import (
        preserve_suspended_working_view, restore_compacted_resume, resume_background_digest,
        resume_execution_digest)
    from lib.tasks_pkg.conv_message_builder._toolcalls import _reconstruct_tool_call_messages
    settled = [_round(0), _round(1)]
    in_flight = _round(2)
    view = ([{'role': 'user', 'content': 'work'}, _carrier('memories')]
            + _reconstruct_tool_call_messages(settled)
            + _reconstruct_tool_call_messages([in_flight])[:1])
    task = _interrupted(_task(), settled + [{**in_flight, 'toolContent': None,
                                              'status': 'running'}], view)
    preserve_suspended_working_view(task)
    saved = archives.rows['archive-1']['post']
    # Restart recovery closed the in-flight call with its effect receipt.
    recovered = {**in_flight, 'toolContent': 'interrupted: outcome unknown'}
    config = deepcopy(task['config'])
    config['_resumeBackgroundDigest'] = resume_background_digest([])
    config['_resumeExecutionDigest'] = resume_execution_digest('', '')
    resumed = _task()
    resumed['_attemptId'] = 'continue-attempt'
    restored = restore_compacted_resume(resumed, config, settled + [recovered],
                                        fresh_messages=[])
    assert restored == saved + _reconstruct_tool_call_messages([recovered])
    assert resumed['_compactionResumeOutcome'] == {'reason': 'restored',
                                                   'archiveId': 'archive-1'}
    assert resumed['_resumedCheckpointArchiveId'] == 'archive-1'


def _live_checkpoint(archives, turn):
    archive = f'archive-{len(archives.rows) + 1}'
    archives.rows[archive] = {'receipt': {
        'status': 'completed', 'implementation': 'suspended_working_context',
        'resumeWitness': {'version': 1, 'turnId': turn}}, 'post': ['old view']}
    return archive


def test_each_suspension_keeps_one_live_newest_checkpoint(archives):
    from lib.tasks_pkg.compaction._resume_projection import preserve_suspended_working_view
    abandoned = _live_checkpoint(archives, 'older-turn')  # never continued
    restored = _live_checkpoint(archives, 'turn')
    archives.rows['archive-3'] = {'receipt': {'status': 'completed', 'summary': {
        'accepted': True}}, 'post': ['compacted']}  # a real compaction stays
    task = _interrupted(_task(), [_round(0)], None)
    task['_resumedCheckpointArchiveId'] = restored
    preserve_suspended_working_view(task)
    # A fresh newest archive: restore scans only the newest candidates.
    assert task['_suspendedCompactionArchiveId'] == 'archive-4'
    assert archives.rows['archive-4']['post'] == task['messages']
    for archive in (abandoned, restored):
        row = archives.rows[archive]
        assert row['post'] == [] and row['receipt']['status'] == 'retired'
    assert archives.rows['archive-3']['post'] == ['compacted']


def test_unwritten_checkpoint_is_never_a_restore_candidate(archives, monkeypatch):
    from lib.tasks_pkg.compaction._resume_projection import (
        preserve_suspended_working_view, restore_compacted_resume)
    task = _interrupted(_task(), [_round(0)], None)

    def fail(*args, **kwargs):
        raise OSError('summary write lost')
    monkeypatch.setattr(archives, 'update_archive_summary', fail)
    with pytest.raises(OSError):
        preserve_suspended_working_view(task)
    assert archives.rows['archive-1']['receipt']['status'] == 'pending'
    assert restore_compacted_resume(_task(), task['config'], [_round(0)],
                                    fresh_messages=[]) is None


def test_completion_retires_the_checkpoint_it_restored(archives):
    from lib.tasks_pkg.compaction._resume_projection import retire_suspended_working_view
    restored = _live_checkpoint(archives, 'turn')
    completed = _task()
    completed.update(status='done', finishReason='stop', content='final answer',
                     _resumedCheckpointArchiveId=restored)
    retire_suspended_working_view(completed)
    row = archives.rows[restored]
    assert row['post'] == [] and 'resumeWitness' not in row['receipt']
    assert row['receipt']['status'] == 'retired'


@pytest.mark.parametrize('backend', ['sqlite', 'tofudb'])
def test_checkpoint_lifecycle_on_real_storage_survives_restart(monkeypatch, tmp_path, backend):
    from pathlib import Path
    import uuid
    import lib.agent_core.store
    from lib.storage import StorageSupervisor
    from lib.tasks_pkg import persistence_store
    from lib.tasks_pkg.compaction._resume_projection import (
        preserve_suspended_working_view, restore_compacted_resume, resume_background_digest,
        resume_execution_digest, retire_suspended_working_view)
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
        rounds = [_round(i) for i in range(2)]
        task = _interrupted(_task(), rounds, None)
        view = deepcopy(task['messages'])
        preserve_suspended_working_view(task)
        archive = task['_suspendedCompactionArchiveId']
        supervisor.stop()
        supervisor.start()
        config = deepcopy(task['config']) | {
            '_resumeBackgroundDigest': resume_background_digest([]),
            '_resumeExecutionDigest': resume_execution_digest('', '')}
        resumed = _task()
        resumed['_attemptId'] = 'continue-attempt'
        assert restore_compacted_resume(resumed, config, rounds, fresh_messages=[]) == view
        resumed.update(status='done', finishReason='stop', content='final answer')
        retire_suspended_working_view(resumed)
        assert restore_compacted_resume(_task(), config, rounds, fresh_messages=[]) is None
        saved = store.get_compaction_archive('conversation', archive, user_id=1,
                                             include_messages=True, include_post_messages=True)
        assert saved['messages'] == [] and saved['postMessages'] == []
        assert saved['archive']['receipt']['status'] == 'retired'
    finally:
        supervisor.stop()


def test_compaction_receipts_still_require_an_accepted_summary(archives):
    from lib.tasks_pkg.compaction._resume_projection import (
        compaction_resume_witness, restore_compacted_resume)
    task = _task()
    task['toolRounds'] = [_round(0)]
    task['messages'] = [{'role': 'user', 'content': 'work'}]
    witness = compaction_resume_witness(task)
    archives.rows['archive-1'] = {
        'receipt': {'status': 'completed', 'implementation': 'llm_summary',
                    'summary': {'accepted': False, 'complete': True},
                    'resumeWitness': witness},
        'post': [{'role': 'user', 'content': 'work'}]}
    assert restore_compacted_resume(_task(), task['config'], [_round(0)],
                                    fresh_messages=[]) is None
