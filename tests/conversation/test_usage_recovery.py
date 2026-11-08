"""Unknown usage authorizes only an explicitly confirmed, fresh bounded Turn.

Exercise the real Sidecar and Turn/Attempt event fold. Physical execution outcomes are controlled; durable admission stays real: recovery must not settle old usage, replay a checkpoint, bypass the
owner/revision/lane fences, or launch twice after a lost acknowledgement.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import math
import os
from pathlib import Path
import shutil
import tempfile
import time
import uuid

import pytest

from tests.support.conversation_workers import worker_attempts as worker_attempts


pytestmark = [pytest.mark.api, pytest.mark.auth_mode('open')]
pytest_plugins = ('tests._chat_sidecar',)

PAID_USAGE = {'prompt_tokens': 80, 'completion_tokens': 20, 'total_tokens': 100}
SAVED_OUTPUT = '已保存的部分结果；尚未完成的工作需要新的明确授权。'
UNKNOWN_USAGE = 'execution_token_usage_unknown'


@dataclass
class RecoverySource:
    client: object
    conversation_id: str
    owner_id: int
    turn_id: str
    attempt_id: str

    owner_preview: dict | None = None
    @property
    def url(self):
        return (f'/api/v4/conversations/{self.conversation_id}/turns/'
                f'{self.turn_id}/usage-recovery')

    def snapshot(self):
        return self.client.query('turn.sync.snapshot', {
            'conversation_id': self.conversation_id, 'user_id': self.owner_id,
        })

    def turn(self):
        return self.client.query('turn.get', {
            'conversation_id': self.conversation_id, 'turn_id': self.turn_id,
            'user_id': self.owner_id,
        })

    def attempt(self):
        return self.client.query('turn.attempt.get', {
            'attempt_id': self.attempt_id, 'user_id': self.owner_id,
            'include_execution_config': True,
        })


@pytest.fixture(scope='module')
def _chat_sidecar_runtime():
    """Override the SQL-only helper for explicit, genuinely native test runs.

    Both authorities are disposable. Selecting tofudb here exercises native
    persistence rather than just booting a native app before replacing it with
    the shared helper's hard-coded SQLite authority.
    """
    from lib.storage import StorageRuntime, StorageSupervisor
    from lib.storage.service import install_runtime_for_test

    backend = os.environ.get('TOFU_PERSONAL_STORAGE_BACKEND', 'sqlite')
    assert backend in {'sqlite', 'tofudb'}, f'Unsupported test backend: {backend}'
    root = Path(tempfile.mkdtemp(prefix='usage-recovery-sidecar-'))
    runtime = StorageRuntime(StorageSupervisor(
        project_root=root, backend=backend, startup_timeout=60,
    ), auto_restart=False)
    install_runtime_for_test(runtime)
    try:
        runtime.start()
        yield runtime
    finally:
        install_runtime_for_test(None)
        try:
            runtime.stop()
        finally:
            shutil.rmtree(root, ignore_errors=True)


@pytest.fixture()
def recovery_source(chat_sidecar, flask_client, monkeypatch):
    """Create owned records through the real lifecycle, never fake projections."""
    from lib.error_envelope import make_envelope
    from lib.storage import get_storage_client
    from lib.turn_lifecycle import (
        bind_task, claim_attempt_start, create_turn_pair, mark_task_started,
        record_task_event,
    )

    default_client = get_storage_client(write=True)
    conversations = []
    foreign_authorities = []

    def create_record(*, owner_id=1, config=None, tool_rounds=None,
                      usage_complete=False, error_kind=UNKNOWN_USAGE,
                      turn_status='failed', with_error=True, api_rounds=None,
                      source_input='Finish the existing task.',
                      saved_output=SAVED_OUTPUT, conversation_id=None,
                      task_id=None, client=default_client):
        conversation_id = conversation_id or f'usage-recovery-{uuid.uuid4().hex}'
        client.command('conversation.create', {
            'conv_id': conversation_id, 'user_id': owner_id,
            'title': 'usage recovery', 'created_at': 1, 'updated_at': 1,
            'settings': {},
        }, f'create-{conversation_id}')
        conversations.append((client, conversation_id, owner_id))
        saved_config = {
            'model': 'gpt-4o-mini', 'maxTokens': 2048, 'temperature': 0.2,
            # None/negative represent an old unbounded execution. A new
            # recovery must replace them, not renew the old authority.
            'executionLimits': {'max_tokens': None, 'max_tool_calls': -1,
                                'deadline': None},
            'autopilot': True, 'activeFlow': 'old-flow', 'flowId': 'old-flow',
            'flowDefinition': {'id': 'old-flow', 'nodes': []},
            '_goalRunId': 'old-goal', '_goalObjective': 'old objective',
            '_resumeSource': {'turnId': 'old-resume-source'},
            'checkpointUsage': deepcopy(PAID_USAGE),
            'checkpointUsageComplete': False, 'checkpointToolRounds': [],
            'checkpointSegments': [], 'resumePrefill': saved_output,
            'contentPrefix': saved_output,
        }
        saved_config.update(deepcopy(config or {}))
        created = create_turn_pair(
            conversation_id, command_id=f'source-{conversation_id}',
            input_projection={'content': source_input},
            config=saved_config, user_id=owner_id,
        )
        turn_id = created['turn']['turnId']
        attempt_id = created['attempt']['attemptId']
        task_id = task_id or f'source-task-{uuid.uuid4().hex}'
        assert claim_attempt_start(attempt_id, user_id=owner_id)
        bind_task(attempt_id, task_id, user_id=owner_id)
        mark_task_started(attempt_id, task_id, user_id=owner_id)
        task = {
            'id': task_id, 'convId': conversation_id, '_turnId': turn_id,
            '_attemptId': attempt_id, '_userId': owner_id,
            'status': 'running', 'content': saved_output,
            'thinking': 'Existing reasoning is retained.', 'segments': [],
            'config': saved_config, 'model': saved_config.get('model', ''),
            'toolRounds': deepcopy(tool_rounds if tool_rounds is not None else [{
                'toolCallId': 'saved-read', 'toolName': 'read_files',
                'toolArgs': {'path': 'saved.txt'}, 'toolContent': 'saved evidence',
                'status': 'done', 'roundNum': 1, 'llmRound': 0,
            }]),
            'usage': deepcopy(PAID_USAGE),
            'apiRounds': deepcopy(api_rounds or []),
            '_executionActualsComplete': usage_complete,
        }
        assert record_task_event(task, {'type': 'delta', 'content': saved_output})
        error = make_envelope(error_kind, model='gpt-4o-mini',
                              detail='The preceding request has no confirmed usage.',
                              context='usage-recovery-test', source='test')
        assert turn_status in {'failed', 'interrupted'}
        terminal_error = {'error': error} if with_error else {}
        if turn_status == 'interrupted':
            task.update(status='aborted', finishReason='aborted', aborted=True,
                        _abort_reason='server_shutdown', **terminal_error)
            event = {'type': 'aborted', **terminal_error}
        else:
            task.update(status='error', finishReason='error', **terminal_error)
            event = {'type': 'done', **terminal_error}
        assert record_task_event(task, event)
        source = RecoverySource(client, conversation_id, owner_id, turn_id, attempt_id)
        assert source.turn()['status'] == turn_status
        assert source.attempt()['status'] == turn_status
        assert source.turn()['projection']['usage'] == PAID_USAGE
        assert source.turn()['projection']['usageComplete'] is usage_complete
        from lib.conversation_sync.runtime import conversation_turn_commands
        source.owner_preview = conversation_turn_commands.usage_recovery(
            conversation_id, turn_id, owner_id).value
        return source

    def create(*, owner_id=1, **options):
        if os.environ.get('TOFU_PERSONAL_STORAGE_BACKEND') != 'tofudb' or owner_id == 1:
            return create_record(owner_id=owner_id, **options)
        # Personal native authorities are physically owner-scoped: foreign
        # durable data belongs in its own authority, never a forged owner row
        # in the current user's database. Restore the app's own authority
        # before HTTP, retaining this separate client's read-only evidence.
        from lib.storage import StorageRuntime, StorageSupervisor
        from lib.storage.service import install_runtime_for_test
        root = Path(tempfile.mkdtemp(prefix='usage-recovery-foreign-'))
        runtime = StorageRuntime(StorageSupervisor(
            project_root=root, backend='tofudb', startup_timeout=60,
        ), auto_restart=False)
        foreign_authorities.append((runtime, root))
        try:
            with monkeypatch.context() as scope:
                scope.setenv('TOFUDB_OWNER_ID', str(owner_id))
                install_runtime_for_test(runtime)
                runtime.start()
                source = create_record(owner_id=owner_id,
                    client=get_storage_client(write=True), **options)
        finally:
            # install_runtime_for_test deliberately stops the previous owner.
            # Reopen our isolated app authority before the request, then open
            # the foreign authority independently rather than swapping globals
            # during HTTP or retaining a client that shutdown already closed.
            install_runtime_for_test(chat_sidecar)
            chat_sidecar.start()
        with monkeypatch.context() as scope:
            scope.setenv('TOFUDB_OWNER_ID', str(owner_id))
            runtime.start()
        source.client = runtime.client(write=True)
        conversations[-1] = (source.client, source.conversation_id, owner_id)
        return source

    try:
        yield create
    finally:
        from lib.runtime.runtime_state_store import reset_for_test
        try:
            for client, conversation_id, owner_id in conversations:
                client.command('conversation.purge', {
                    'conv_id': conversation_id, 'user_id': owner_id,
                }, f'purge-{conversation_id}')
        finally:
            for runtime, root in foreign_authorities:
                try:
                    runtime.stop()
                finally:
                    shutil.rmtree(root, ignore_errors=True)
            reset_for_test()


@pytest.fixture()
def worker_admissions(monkeypatch, worker_attempts):
    """Observe real worker admission without entering a model or tool."""
    from tests.support.conversation_workers import observe_admissions
    return observe_admissions(monkeypatch)


def _preview(flask_client, source):
    response = flask_client.get(source.url)
    assert response.status_code == 200, response.get_json()

    assert response.headers['Cache-Control'] == 'private, no-store'
    return response.get_json()


def _confirmation(preview, command_id=None):
    return {
        'commandId': command_id or f'recover-{uuid.uuid4().hex}',
        'expectedProjectionRevision': preview['sourceProjectionRevision'],
        'confirmed': True, 'budget': deepcopy(preview['budget']),
    }


def _assert_conflict(response, kind):
    assert response.status_code == 409, response.get_json()
    assert response.get_json()['error']['kind'] == kind


def _finish_success(worker_attempts, source, accepted, task_id):
    attempt_id = accepted['attempt']['attemptId']
    task = {
        'id': task_id, 'convId': source.conversation_id, '_userId': source.owner_id,
        '_turnId': accepted['turn']['turnId'], '_attemptId': attempt_id,
        'status': 'done', 'finishReason': 'stop', 'content': 'Fresh work completed.',
        'thinking': '', 'toolRounds': [], 'segments': [],
        'config': {'model': 'gpt-4o-mini'},
    }
    assert worker_attempts.publish(task, {'type': 'done', 'finishReason': 'stop'})


def test_get_is_read_only_and_proposes_a_finite_new_budget(
    flask_client, recovery_source, worker_admissions,
):
    source = recovery_source()
    before = deepcopy(source.snapshot())
    before_attempt = deepcopy(source.attempt())

    preview = _preview(flask_client, source)
    assert preview['ok'] is True
    assert preview['conversationId'] == source.conversation_id
    assert preview['sourceTurnId'] == source.turn_id
    assert preview['sourceProjectionRevision'] == source.turn()['projectionRevision']
    assert (preview['state'], preview['reason']) == ('ready', 'ready')
    assert set(preview['budget']) == {'maxTokens', 'maxToolCalls', 'durationSeconds'}
    for name, value in preview['budget'].items():
        assert type(value) is int and math.isfinite(value)
        assert value >= (0 if name == 'maxToolCalls' else 1)
    assert _preview(flask_client, source) == preview
    assert source.snapshot() == before
    assert source.attempt() == before_attempt
    assert worker_admissions == []


@pytest.mark.parametrize('source_options', [
    {'error_kind': 'bad_request'},
    {'error_kind': 'generic'},
    {'turn_status': 'interrupted', 'with_error': False},
], ids=['request-rejection', 'generic-failure', 'restart-without-error'])
def test_any_settled_unconfirmed_usage_has_a_forward_recovery_not_a_doomed_resume(
    flask_client, recovery_source, worker_admissions, source_options,
):
    source = recovery_source(**source_options)
    before = deepcopy(source.turn())
    preview = _preview(flask_client, source)
    assert (preview['state'], preview['reason']) == ('ready', 'ready')
    response = flask_client.post(source.url, json=_confirmation(preview))
    assert response.status_code == 200, response.get_json()
    assert response.get_json()['attempt']['operation'] == 'generate'
    assert len(worker_admissions) == 1
    assert source.turn() == before


def test_proposed_budget_fits_saved_large_input_and_a_real_model_admission(
    flask_client, recovery_source, worker_admissions,
):
    from lib.agent_core.execution_budget import model_budget_admission

    prompt_tokens, output_limit = 226646, 32768
    source = recovery_source(config={'maxTokens': output_limit}, api_rounds=[{
        'round': 1, 'model': 'gpt-4o-mini',
        'usage': {'prompt_tokens': prompt_tokens, 'completion_tokens': 1500},
    }])
    before = deepcopy(source.turn())
    preview = _preview(flask_client, source)
    assert preview['state'] == 'ready'
    assert preview['budget']['maxTokens'] > prompt_tokens + output_limit
    response = flask_client.post(source.url, json=_confirmation(preview))
    assert response.status_code == 200, response.get_json()
    config = worker_admissions[0]['config']
    owner = {'id': 'fresh-recovery', 'config': config, 'usage': {}, 'toolRounds': []}
    lease = model_budget_admission(owner)(
        input_tokens=prompt_tokens, max_output_tokens=output_limit)
    assert lease is not None
    lease.settle({'prompt_tokens': prompt_tokens, 'completion_tokens': 1})
    assert owner.get('_executionActualsComplete') is not False
    assert source.turn() == before


def test_large_saved_context_is_counted_locally_even_without_a_provider_receipt(
    flask_client, recovery_source, worker_admissions,
):
    from lib.token_counter import count_tokens

    original_task = 'Preserve the entire original user task. ' * 19000
    source = recovery_source(source_input=original_task)
    estimate = count_tokens([{'role': 'user', 'content': original_task}],
                            model='gpt-4o-mini', mode='tiktoken')['tokens']
    assert estimate > 100000
    preview = _preview(flask_client, source)
    assert preview['state'] == 'ready'
    assert preview['budget']['maxTokens'] > estimate + 2048
    assert worker_admissions == []


def test_a_saved_limit_too_small_for_one_request_is_blocked_before_authorization(
    flask_client, recovery_source, worker_admissions,
):
    source = recovery_source(config={
        'maxTokens': 2048, 'executionLimits': {'max_tokens': 1000},
    }, api_rounds=[{
        'round': 1, 'model': 'gpt-4o-mini',
        'usage': {'prompt_tokens': 5000, 'completion_tokens': 10},
    }])
    before = deepcopy(source.snapshot())
    preview = _preview(flask_client, source)
    assert (preview['state'], preview['reason']) == ('blocked', 'budget_insufficient')
    response = flask_client.post(source.url, json=_confirmation(preview))
    _assert_conflict(response, 'usage_recovery_blocked')
    assert source.snapshot() == before and worker_admissions == []


@pytest.mark.parametrize(('operation', 'content'), [
    ('continue', ''), ('checkpoint_resume', SAVED_OUTPUT),
])
def test_unconfirmed_settlement_still_accepts_an_ordinary_resume(
    flask_client, recovery_source, worker_admissions, operation, content,
):
    """A missing receipt must not cost the reader their unfinished task.

    The conversation turn carries ``_unknownUsagePolicy=continue``, so the
    restored checkpoint's incomplete accounting is recorded as a lower bound
    and the plain resume still dispatches. A saved prose tail uses the
    checkpoint anchor; only a prose-free tail can use ``continue``.
    """
    source = recovery_source(error_kind='bad_request', saved_output=content)
    offered = [option['operation']
               for option in source.turn()['settlement']['resumeOptions']]
    assert operation in offered
    response = flask_client.post(
        f'/api/v4/conversations/{source.conversation_id}/turns/{source.turn_id}/attempts',
        json={'commandId': f'resume-{uuid.uuid4().hex}', 'operation': operation,
              'expectedProjectionRevision': source.turn()['projectionRevision'],
              'config': {'model': 'gpt-4o-mini'}})
    assert response.status_code == 200, response.get_json()
    assert response.get_json()['attempt']['operation'] == operation
    assert len(worker_admissions) == 1
    assert worker_admissions[0]['config']['_unknownUsagePolicy'] == 'continue'


def test_recovery_carries_original_objective_as_task_text_not_old_automation_authority(
    flask_client, recovery_source, worker_admissions,
):
    objective = 'Complete the reviewed source change, validate it and preserve existing user edits.'
    source = recovery_source(config={'_goalObjective': objective})
    response = flask_client.post(source.url, json=_confirmation(_preview(flask_client, source)))
    assert response.status_code == 200, response.get_json()
    accepted = response.get_json()
    assert objective in accepted['submittedTurn']['projection']['content']
    config = worker_admissions[0]['config']
    assert '_goalObjective' not in config and '_goalRunId' not in config
    assert config['autopilot'] is False


def test_recovery_model_context_keeps_original_task_and_tool_results_not_rejected_ciphertext(
    flask_client, recovery_source, worker_admissions,
):
    from lib.turn_lifecycle import build_api_messages

    source = recovery_source(tool_rounds=[{
        'toolName': 'read_files', 'toolCallId': 'saved-read',
        'toolArgs': {'path': 'saved.txt'}, 'toolContent': 'Exact saved tool evidence.',
        'status': 'done', 'roundNum': 1, 'llmRound': 0,
        '_responsesItems': [
            {'type': 'reasoning', 'id': 'rejected-reasoning', 'encrypted_content': 'bad-ciphertext'},
            {'type': 'compaction', 'id': 'rejected-compaction', 'encrypted_content': 'bad-compaction'},
        ],
    }])
    before = deepcopy(source.turn())
    response = flask_client.post(source.url, json=_confirmation(_preview(flask_client, source)))
    assert response.status_code == 200, response.get_json()
    target = response.get_json()['turn']['turnId']
    config = deepcopy(worker_admissions[0]['config'])
    messages = build_api_messages(source.conversation_id, target, config, user_id=1)
    assert messages is not None
    assert any(message.get('content') == 'Finish the existing task.' for message in messages)
    assert any(message.get('content') == 'Exact saved tool evidence.' for message in messages)
    assert not any(item.get('type') in {'reasoning', 'compaction'}
                   for message in messages for item in message.get('_responses_items', []))
    assert config['_modelReplayStatus'] == 'replay_disabled'
    assert source.turn() == before


def test_preview_does_not_increase_smaller_saved_limits(
    flask_client, recovery_source, worker_admissions,
):
    source = recovery_source(config={'executionLimits': {
        'max_tokens': 3210, 'max_tool_calls': 4, 'deadline': time.time() + 60,
    }})
    preview = _preview(flask_client, source)
    assert preview['state'] == 'ready'
    assert preview['budget']['maxTokens'] == 3210
    assert preview['budget']['maxToolCalls'] == 4
    assert 1 <= preview['budget']['durationSeconds'] <= 61
    assert worker_admissions == []


@pytest.mark.parametrize('confirmed', [None, False], ids=['missing', 'false'])
def test_post_requires_explicit_confirmation(
    flask_client, recovery_source, worker_admissions, confirmed,
):
    source = recovery_source()
    body = _confirmation(_preview(flask_client, source))
    if confirmed is None:
        body.pop('confirmed')
    else:
        body['confirmed'] = confirmed
    before = deepcopy(source.snapshot())
    response = flask_client.post(source.url, json=body)
    assert response.status_code == 400, response.get_json()
    assert source.snapshot() == before
    assert worker_admissions == []


@pytest.mark.parametrize('field', ['maxTokens', 'maxToolCalls', 'durationSeconds'])
def test_tampered_budget_is_rejected_without_creating_a_turn(
    flask_client, recovery_source, worker_admissions, field,
):
    source = recovery_source()
    body = _confirmation(_preview(flask_client, source))
    body['budget'][field] += 1
    before = deepcopy(source.snapshot())
    response = flask_client.post(source.url, json=body)
    _assert_conflict(response, 'usage_recovery_budget_changed')
    assert source.snapshot() == before
    assert worker_admissions == []


@pytest.mark.parametrize('field', ['maxTokens', 'maxToolCalls', 'durationSeconds'])
def test_boolean_budget_is_not_an_integer_authorization(
    flask_client, recovery_source, worker_admissions, field,
):
    source = recovery_source()
    body = _confirmation(_preview(flask_client, source))
    body['budget'][field] = True
    before = deepcopy(source.snapshot())
    response = flask_client.post(source.url, json=body)
    assert response.status_code == 400, response.get_json()
    assert source.snapshot() == before
    assert worker_admissions == []


def test_post_does_not_accept_caller_config_or_checkpoint_authority(
    flask_client, recovery_source, worker_admissions,
):
    source = recovery_source()
    body = _confirmation(_preview(flask_client, source))
    body['config'] = {'autopilot': True, 'executionLimits': {},
                      'checkpointUsageComplete': True, '_resumeSource': source.turn_id}
    before = deepcopy(source.snapshot())
    response = flask_client.post(source.url, json=body)
    assert response.status_code == 400, response.get_json()
    assert source.snapshot() == before
    assert worker_admissions == []


def test_confirmed_recovery_creates_fresh_bounded_turn_and_preserves_unknown_source(
    flask_client, recovery_source, worker_admissions,
):
    from lib.agent_core.execution_clock import ExecutionClock
    from lib.turn_lifecycle import get_attempt, get_turn

    safety_preferences = {'autoApply': False, 'readOnlyPaths': ['/reference-only'],
        'browserEnabled': True, 'browserClientId': 'original-device',
        'codeExecEnabled': False, 'humanGuidanceEnabled': True,
        'fetchEnabled': False, 'mcpToolExposure': 'delegate'}
    source = recovery_source(config=safety_preferences)
    preview = _preview(flask_client, source)
    source_before = deepcopy(source.turn())
    attempt_before = deepcopy(source.attempt())
    body = _confirmation(preview)
    started_at = time.time()
    response = flask_client.post(source.url, json=body)
    finished_at = time.time()
    assert response.status_code == 200, response.get_json()
    accepted = response.get_json()
    assert len(worker_admissions) == 1
    start = worker_admissions[0]
    assert start['conversationId'] == source.conversation_id
    frozen_attempt = source.client.query('turn.attempt.get', {
        'attempt_id': accepted['attempt']['attemptId'], 'user_id': source.owner_id,
        'include_execution_config': True})
    watermark = frozen_attempt['config']['_dispatchRequestStartedAtMs']
    assert int(started_at * 1000) <= watermark <= int(finished_at * 1000)
    new_turn = accepted['turn']
    new_attempt = accepted['attempt']
    assert new_turn['turnId'] != source.turn_id
    assert new_attempt['attemptId'] != source.attempt_id
    assert new_turn['actor'] == 'assistant' and new_turn['kind'] == 'reply'
    assert new_turn['laneId'] == source_before['laneId']
    assert new_attempt['operation'] == 'generate'
    assert new_attempt['taskId'] == start['taskId']
    assert new_attempt['turnId'] == new_turn['turnId']
    submitted = accepted['submittedTurn']
    assert submitted['actor'] == 'human'
    assert submitted['parentTurnId'] == source.turn_id
    assert new_turn['parentTurnId'] == submitted['turnId']
    assert submitted['projection']['content']
    assert submitted['projection']['content'] != SAVED_OUTPUT
    assert submitted['projection']['usageRecovery'] == {
        'sourceTurnId': source.turn_id,
        'sourceProjectionRevision': source_before['projectionRevision'],
        'budget': preview['budget'],
    }

    stored_config = get_attempt(new_attempt['attemptId'], user_id=1,
                                include_execution_config=True)['config']
    for config in (stored_config, start['config']):
        assert config['model'] == attempt_before['config']['model']
        assert config['maxTokens'] == attempt_before['config']['maxTokens']

        for key, expected in safety_preferences.items():
            assert config[key] == expected
        assert config['autopilot'] is False
        assert not config.get('activeFlow') and not config.get('planMode')
        assert not any(key.startswith('checkpoint') for key in config)
        for forbidden in ('_resumeSource', 'resumePrefill', 'contentPrefix',
                          'flowId', 'flowDefinition', '_goalRunId', '_goalObjective'):
            assert forbidden not in config
        limits = config['executionLimits']
        assert limits['max_tokens'] == preview['budget']['maxTokens']
        assert limits['max_tool_calls'] == preview['budget']['maxToolCalls']
        assert math.isfinite(limits['deadline'])
        assert (started_at + preview['budget']['durationSeconds']
                <= limits['deadline'] <= finished_at + preview['budget']['durationSeconds'])
    assert start['config']['_turnOperation'] == 'generate'
    fresh_projection = get_turn(source.conversation_id, new_turn['turnId'],
                                user_id=1)['projection']
    assert fresh_projection.get('content', '') == ''
    assert not fresh_projection.get('usage')
    assert fresh_projection.get('usageComplete') is not False
    assert source.turn() == source_before
    assert source.attempt() == attempt_before
    assert source.turn()['projection']['usageComplete'] is False
    assert source.turn()['projection']['usage'] == PAID_USAGE
    assert source.turn()['projection']['content'] == SAVED_OUTPUT
    assert len(source.snapshot()['turns']) == 4


def test_same_command_starts_once_and_a_different_command_cannot_reuse_old_tail(
    worker_attempts,
    flask_client, recovery_source, worker_admissions,
):
    source = recovery_source()
    body = _confirmation(_preview(flask_client, source))
    first = flask_client.post(source.url, json=body)
    assert first.status_code == 200, first.get_json()
    accepted = first.get_json()
    before_replay = deepcopy(source.snapshot())
    replay = flask_client.post(source.url, json=body)
    assert replay.status_code == 200, replay.get_json()
    assert replay.get_json()['idempotentReplay'] is True
    assert replay.get_json()['attempt']['attemptId'] == accepted['attempt']['attemptId']
    assert replay.get_json()['turn']['turnId'] == accepted['turn']['turnId']
    assert len(worker_admissions) == 1
    assert source.snapshot() == before_replay

    # Settle the successor so rejection proves stale-tail CAS, not just busy.
    _finish_success(worker_attempts, source, accepted, worker_admissions[0]['taskId'])
    before_new_command = deepcopy(source.snapshot())
    different = {**body, 'commandId': f'different-{uuid.uuid4().hex}'}
    rejected = flask_client.post(source.url, json=different)
    _assert_conflict(rejected, 'lane_advanced')
    assert source.snapshot() == before_new_command
    settled_replay = flask_client.post(source.url, json=body)
    assert settled_replay.status_code == 200, settled_replay.get_json()
    assert settled_replay.get_json()['attempt']['attemptId'] == accepted['attempt']['attemptId']
    assert len(worker_admissions) == 1


def test_busy_lane_is_rejected_instead_of_queuing_another_recovery(
    flask_client, recovery_source, worker_admissions,
):
    from lib.turn_lifecycle import create_turn_pair
    source = recovery_source()
    body = _confirmation(_preview(flask_client, source))
    create_turn_pair(source.conversation_id, command_id=f'other-{uuid.uuid4().hex}',
                     input_projection={'content': 'A newer human intent.'},
                     config={'model': 'gpt-4o-mini'}, user_id=1)
    before = deepcopy(source.snapshot())
    response = flask_client.post(source.url, json=body)
    _assert_conflict(response, 'lane_busy')
    assert source.snapshot() == before
    assert source.snapshot()['queueItems'] == []
    assert worker_admissions == []


@pytest.mark.parametrize('method', ['get', 'post'])
def test_foreign_owner_cannot_inspect_or_recover_a_turn(
    flask_client, recovery_source, worker_admissions, method,
):
    source = recovery_source(owner_id=2)
    before = deepcopy(source.snapshot())
    if method == 'get':
        response = flask_client.get(source.url)
    else:
        # A known id/revision/budget is still not ownership authority.
        response = flask_client.post(source.url, json=_confirmation(source.owner_preview))
    assert response.status_code == 404, response.get_json()
    assert source.snapshot() == before
    assert worker_admissions == []


def test_revision_changed_after_preview_rejects_post_without_starting(
    flask_client, recovery_source, worker_admissions,
):
    from lib.turn_lifecycle import update_turn_projection
    source = recovery_source()
    body = _confirmation(_preview(flask_client, source))
    current = source.turn()
    update_turn_projection(source.conversation_id, source.turn_id,
        projection={**current['projection'], 'content': SAVED_OUTPUT + ' Updated.'},
        expected_projection_revision=current['projectionRevision'], user_id=1)
    before = deepcopy(source.snapshot())
    response = flask_client.post(source.url, json=body)
    _assert_conflict(response, 'projection_conflict')
    assert source.snapshot() == before
    assert worker_admissions == []


def test_source_revision_cas_also_applies_after_recovery_has_been_prepared(
    flask_client, recovery_source, worker_admissions,
):
    """The storage acceptance fence, not only HTTP preflight, owns this race."""
    from lib.conversation_sync.runtime import conversation_turn_commands
    from lib.conversation_sync.usage_recovery import confirmed_recovery, prepare_usage_recovery
    from lib.turn_lifecycle import LifecycleConflict, update_turn_projection

    source = recovery_source()
    plan = prepare_usage_recovery(source.conversation_id, source.turn_id, 1)
    now = time.time()
    config, projection = confirmed_recovery(plan, _confirmation(plan.preview), now=now)
    update_turn_projection(source.conversation_id, source.turn_id,
        projection={**source.turn()['projection'], 'content': SAVED_OUTPUT + ' Raced.'},
        expected_projection_revision=plan.source['projectionRevision'], user_id=1)
    before = deepcopy(source.snapshot())
    with pytest.raises(LifecycleConflict) as conflict:
        conversation_turn_commands.create_turn(source.conversation_id, 1, {
            'commandId': f'raced-{uuid.uuid4().hex}', 'config': config,
            'inputTurn': projection, 'laneId': plan.source['laneId'],
            'parentTurnId': source.turn_id, 'requireParentIsLaneTail': True,
        }, request_started_at=now,
            trusted_usage_recovery_revision=plan.source['projectionRevision'])
    assert conflict.value.code == 'lane_advanced'
    assert source.snapshot() == before
    assert worker_admissions == []


def _unknown_effect(*, status='aborted'):
    row = {
        'toolCallId': 'uncertain-command', 'toolName': 'run_command',
        'toolArgs': {'command': 'external-operation'},
        'executionEffect': {'operationId': 'a' * 32, 'state': 'unknown',
                            'recovery': 'reconcile', 'scope': 'attempt'},
    }
    if status is not None:
        row['status'] = status
    return row


UNCONFIRMED_ROWS = [
    pytest.param([_unknown_effect()], id='unknown-effect'),
    pytest.param([_unknown_effect(status=None)], id='unknown-effect-missing-status'),
    pytest.param([{'toolName': 'run_command', 'toolCallId': 'legacy-running',
                   'status': 'running'}], id='legacy-running'),
    pytest.param([{'toolName': 'run_command', 'toolCallId': 'legacy-missing'}],
                 id='legacy-missing-status'),

    pytest.param([{'toolName': 'write_file', 'toolCallId': 'legacy-error',
                   'status': 'error'}], id='legacy-error-is-not-an-effect-verdict'),
    pytest.param([{'toolName': 'run_command', 'toolCallId': 'background',
                   'status': 'done', 'exitCode': 'background'}], id='background'),

    pytest.param([{'_programSynthetic': True, '_programCallId': 'program-running',
                   'status': 'searching', 'programStatus': 'running'}],
                 id='running-program'),
    pytest.param([{'toolName': 'execute_program', 'toolCallId': 'program',
                   'status': 'done', 'childCalls': [{
                       'name': 'run_command', 'id': 'aborted-child', 'status': 'aborted',
                   }]}], id='actual-child-aborted'),
    pytest.param([{'toolName': 'execute_program', 'toolCallId': 'program',
                   'status': 'done', 'childCalls': [{
                       'name': 'run_command', 'id': 'unconfirmed-child',
                   }]}], id='actual-child-missing-status'),
    pytest.param([{'toolName': 'spawn_agents', 'toolCallId': 'launch-receipt',
                   'status': 'done', 'executionEffect': {
                       'operationId': 'b' * 32, 'state': 'returned',
                       'recovery': 'reconcile', 'scope': 'attempt',
                   }}], id='spawn-receipt-not-child-effect-inventory'),
    pytest.param([{'toolName': 'spawn_agents', 'toolCallId': 'settled-panel',
                   'status': 'done', '_swarmSnapshot': {'agents': [
                       {'agentId': 'finished-child', 'status': 'completed'},
                   ]}}], id='settled-panel-not-child-effect-inventory'),
    *[pytest.param([{'toolName': 'execute_program', 'toolCallId': 'program',
                     'status': 'done', key: [_unknown_effect()]}], id=f'nested-{key}')
      for key in ('toolRounds', 'tool_calls', 'calls', 'results', 'steps',
                  'children', 'programRuns', '_programRuns', 'toolResults',
                  'childCalls', 'childResults')],
    pytest.param([{'toolName': 'execute_program', 'toolCallId': 'program',
                   'status': 'done', 'childCalls': [_unknown_effect(status=None)]}],
                 id='nested-missing-status'),
    pytest.param([{'toolName': 'execute_program', 'toolCallId': 'program',
                   'status': 'done', 'childResults': [{
                       'toolName': 'run_command', 'toolCallId': 'child-background',
                       'status': 'done', 'exitCode': 'background',
                   }]}], id='nested-background'),
]


@pytest.mark.parametrize('tool_rounds', UNCONFIRMED_ROWS)
def test_unconfirmed_tool_effect_or_background_work_blocks_recovery(
    flask_client, recovery_source, worker_admissions, tool_rounds,
):
    source = recovery_source(tool_rounds=tool_rounds)
    before = deepcopy(source.snapshot())
    preview = _preview(flask_client, source)
    assert (preview['state'], preview['reason']) == ('blocked', 'operation_unconfirmed')
    response = flask_client.post(source.url, json=_confirmation(preview))
    _assert_conflict(response, 'usage_recovery_blocked')
    assert source.snapshot() == before
    assert worker_admissions == []


@pytest.mark.parametrize('source_options', [
    {'usage_complete': True},
    {'usage_complete': True, 'error_kind': 'generic'},
], ids=['complete-usage', 'different-failure-with-complete-usage'])
def test_non_applicable_source_never_authorizes_new_work(
    flask_client, recovery_source, worker_admissions, source_options,
):
    source = recovery_source(**source_options)
    before = deepcopy(source.snapshot())
    preview = _preview(flask_client, source)
    assert (preview['state'], preview['reason']) == ('blocked', 'not_applicable')
    response = flask_client.post(source.url, json=_confirmation(preview))
    _assert_conflict(response, 'usage_recovery_blocked')
    assert source.snapshot() == before
    assert worker_admissions == []


@pytest.mark.parametrize('tool_rounds', [
    [{'toolName': 'read_files', 'toolCallId': 'safe-read', 'status': 'aborted',
      'executionEffect': {'operationId': 'c' * 32, 'state': 'unknown',
                          'recovery': 'read_only', 'scope': 'attempt'}}],
    [{'toolName': 'spawn_agents', 'toolCallId': 'unsent-launch', 'status': 'aborted',
      'executionEffect': {'operationId': 'd' * 32, 'state': 'not_started',
                          'recovery': 'reconcile', 'scope': 'attempt'}}],
    [{'toolName': 'read_files', 'toolCallId': 'completed-read', 'status': 'done',
      'results': [{'toolName': 'read_files', 'badge': 'complete',
                   'summary': 'Display metadata is not another pending tool.'}]}],
    [{'toolName': 'execute_program', 'toolCallId': 'completed-program', 'status': 'done',
      'childCalls': [{'name': 'read_files', 'id': 'completed-child',
                      'status': 'completed'}]}],
], ids=['read-only', 'proven-not-started', 'display-metadata', 'completed-program-child'])
def test_safe_receipts_and_display_metadata_do_not_block_preview(
    flask_client, recovery_source, worker_admissions, tool_rounds,
):
    source = recovery_source(tool_rounds=tool_rounds)
    before = deepcopy(source.snapshot())
    preview = _preview(flask_client, source)
    assert (preview['state'], preview['reason']) == ('ready', 'ready')
    assert source.snapshot() == before
    assert worker_admissions == []


@pytest.mark.parametrize('config', [
    {'model': ''},
    {'executionLimits': {'max_tokens': 0, 'max_tool_calls': 0}},
], ids=['missing-model', 'zero-token-cap'])
def test_unavailable_source_configuration_cannot_authorize_work(
    flask_client, recovery_source, worker_admissions, config,
):
    source = recovery_source(config=config)
    before = deepcopy(source.snapshot())
    preview = _preview(flask_client, source)
    assert (preview['state'], preview['reason']) == ('blocked', 'configuration_unavailable')
    response = flask_client.post(source.url, json=_confirmation(preview))
    _assert_conflict(response, 'usage_recovery_blocked')
    assert source.snapshot() == before
    assert worker_admissions == []


@pytest.mark.parametrize('publication', [
    'unpublished', 'published', 'conflict', 'missing-stage', 'duplicate',
])
def test_unknown_write_is_checked_against_real_file_publication_evidence(
    flask_client, recovery_source, worker_admissions, tmp_path, monkeypatch, publication,
):
    from lib.project_mod import modifications
    from lib.project_mod.effect_reconciliation import inspect_prepared_write, reference_for
    from lib.project_mod.session_lock import locked_session
    from lib.project_mod.write_intents import locked_intents
    from tests.project_mod.test_project_write_intents import stage

    # Only isolate the journal directory. The intent writer, classifier,
    # owner-scoped publication reader and recovery policy all remain real.
    monkeypatch.setattr(modifications, 'SESSIONS_DIR', str(tmp_path / 'sessions'))
    base, target, staged, request = stage(tmp_path)
    conversation_id = f'write-recovery-{uuid.uuid4().hex}'
    task_id = f'write-task-{uuid.uuid4().hex}'
    request.update(conv_id=conversation_id, task_id=task_id)
    session = Path(modifications._get_session_dir(str(base)))
    with locked_session(str(session)), locked_intents(str(session)) as store:
        record = store.prepare(**request)
    if publication in {'published', 'conflict'}:
        os.replace(staged, target)
    if publication == 'conflict':
        target.write_bytes(b'later user edit; never overwrite')
    elif publication == 'missing-stage':
        staged.unlink()
    row = {
        'toolCallId': 'uncertain-write', 'toolName': 'write_file', 'taskId': task_id,
        'status': 'error', 'executionEffect': {
            'operationId': 'e' * 32, 'state': 'unknown', 'scope': 'attempt',
            'recovery': 'reconcile', 'reconciliation': reference_for(record),
        },
    }
    rows = [row, deepcopy(row)] if publication == 'duplicate' else [row]
    source = recovery_source(conversation_id=conversation_id, task_id=task_id,
                             tool_rounds=rows)
    before_source = deepcopy(source.turn())
    before_attempt = deepcopy(source.attempt())
    before_snapshot = deepcopy(source.snapshot())
    before_target = (target.read_bytes(), target.stat().st_ino, target.stat().st_mtime_ns)
    before_stage = staged.read_bytes() if staged.exists() else None

    def journal_bytes():
        return {path.relative_to(session): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in session.rglob('*') if path.is_file()}

    before_journal = journal_bytes()
    outcome, _ = inspect_prepared_write(reference_for(record),
                                        conversation_id=conversation_id)
    assert outcome == {
        'unpublished': 'file_not_published', 'published': 'file_published',
        'conflict': 'unknown', 'missing-stage': 'unknown', 'duplicate': 'file_not_published',
    }[publication]
    preview = _preview(flask_client, source)
    ready = publication in {'unpublished', 'published'}
    assert (preview['state'], preview['reason']) == (
        ('ready', 'ready') if ready else ('blocked', 'operation_unconfirmed'))
    assert source.snapshot() == before_snapshot
    assert worker_admissions == []
    response = flask_client.post(source.url, json=_confirmation(preview))
    if ready:
        assert response.status_code == 200, response.get_json()
        assert len(worker_admissions) == 1
    else:
        _assert_conflict(response, 'usage_recovery_blocked')
        assert source.snapshot() == before_snapshot
        assert worker_admissions == []
    assert source.turn() == before_source
    assert source.attempt() == before_attempt
    assert journal_bytes() == before_journal
    assert (target.read_bytes(), target.stat().st_ino, target.stat().st_mtime_ns) == before_target
    assert (staged.read_bytes() if staged.exists() else None) == before_stage
    with locked_intents(str(session)) as store:
        assert store.read() == [record]


@pytest.mark.parametrize('mode', [
    {},
    {'autopilot': True},
    {'autopilotEnabled': True},
    {'activeFlow': 'goal'},
    {'_unknownUsagePolicy': 'fence'},
], ids=['chat', 'goal-mode', 'goal-mode-setting', 'flow', 'forged-fence'])
def test_every_conversation_resume_continues_without_a_receipt(
    flask_client, recovery_source, worker_admissions, mode,
):
    """Goal Mode and Flow resumes keep going on a missing receipt, like chat.

    ``recovery_source`` saves a Goal Mode run's automation config. Whatever
    mode the resume selects, and whatever policy a request claims, the
    conversation turn dispatches with the ``continue`` policy; an explicit
    finite cap remains the only stop.
    """
    source = recovery_source(error_kind='bad_request')
    # A saved prose tail resumes from the tool checkpoint; only a prose-free
    # tail uses the bare ``continue`` operation. Both carry the same
    # ``_unknownUsagePolicy=continue`` dispatch this test asserts, so resume
    # through whichever operation the settlement offers.
    offered = [option['operation']
               for option in source.turn()['settlement']['resumeOptions']]
    operation = 'continue' if 'continue' in offered else 'checkpoint_resume'
    response = flask_client.post(
        f'/api/v4/conversations/{source.conversation_id}/turns/{source.turn_id}/attempts',
        json={'commandId': f'resume-{uuid.uuid4().hex}', 'operation': operation,
              'expectedProjectionRevision': source.turn()['projectionRevision'],
              'config': {'model': 'gpt-4o-mini', **mode}})
    assert response.status_code == 200, response.get_json()
    assert len(worker_admissions) == 1
    assert worker_admissions[0]['config']['_unknownUsagePolicy'] == 'continue'


def test_a_new_goal_mode_turn_continues_without_a_receipt(
    flask_client, recovery_source, worker_admissions,
):
    """A fresh Goal Mode run shares ordinary chat's unknown-usage policy."""
    source = recovery_source(error_kind='bad_request')
    response = flask_client.post(
        f'/api/v4/conversations/{source.conversation_id}/turns',
        json={'commandId': f'goal-{uuid.uuid4().hex}',
              'inputTurn': {'content': 'Keep hardening the storage layer.'},
              'config': {'model': 'gpt-4o-mini', 'autopilot': True}})
    assert response.status_code == 200, response.get_json()
    assert len(worker_admissions) == 1
    config = worker_admissions[0]['config']
    assert config['autopilot'] is True
    assert config['_unknownUsagePolicy'] == 'continue'
