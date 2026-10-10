"""Conversation identity is server-authored, least-privilege and fail-closed."""
from __future__ import annotations

import copy
import json

import pytest

from lib.conversation_sync.execution_identity import (
    EXECUTION_IDENTITY_CONFIG_KEY as KEY,
    capture_execution_identity,
    restore_execution_identity,
)
from lib.identity import PrincipalContext

pytestmark = pytest.mark.unit


def _principal():
    return PrincipalContext.user(subject_id='synthetic-credential', owner_user_id=7,
                                 tenant_id='synthetic-tenant', scopes={'chat', 'admin'})


def _accepted():
    config = capture_execution_identity({'model': 'synthetic'}, 7, _principal())
    config.update(_turnOwnerUserId=7, _turnId='turn-test', _attemptId='attempt-test')
    return config


def test_identity_roundtrip_retains_subject_not_request_api_scopes():
    config = _accepted()
    principal, attended = restore_execution_identity(json.loads(json.dumps(config)))
    assert principal.kind == 'user'
    assert principal.subject_id == 'synthetic-credential'
    assert principal.owner_user_id == 7
    assert principal.tenant_id == 'synthetic-tenant'
    assert principal.scopes == frozenset()
    assert attended is True


def test_public_reserved_identity_fields_are_discarded_without_mutating_input():
    forged = {KEY: {'attended': False}, '_turnPrincipalTenantId': 'forged',
              '_turnPrincipalKind': 'system', '_turnPrincipalScopes': ['admin'],
              '_turnPrincipalSubjectId': 'forged', '_attended': False, '_principal': 'forged',
              'model': 'synthetic'}
    before = copy.deepcopy(forged)
    accepted = capture_execution_identity(forged, 7, _principal())
    accepted['_turnOwnerUserId'] = 7
    principal, attended = restore_execution_identity(accepted)
    assert principal.subject_id == 'synthetic-credential'
    assert principal.tenant_id == 'synthetic-tenant'
    assert principal.scopes == frozenset()
    assert attended is True
    assert '_principal' not in accepted
    assert '_attended' not in accepted
    assert forged == before


@pytest.mark.parametrize('principal', [
    PrincipalContext.system(subject_id='synthetic-coordinator', owner_user_id=7, scopes={'chat'}),
    PrincipalContext.user(subject_id='other-owner', owner_user_id=8),
    object(),
])
def test_untrusted_or_nonhuman_principal_cannot_accept_execution(principal):
    with pytest.raises(PermissionError):
        capture_execution_identity({}, 7, principal)


@pytest.mark.parametrize('envelope', [None, {}, [], 'user',
    {'version': True, 'principal': {}, 'attended': True}])
def test_present_malformed_envelope_never_uses_legacy_fallback(envelope):
    config = _accepted()
    config[KEY] = envelope
    with pytest.raises(PermissionError):
        restore_execution_identity(config)


@pytest.mark.parametrize('field,value', [
    ('kind', 'system'), ('owner_user_id', 8), ('owner_user_id', '7'),
    ('owner_user_id', True), ('tenant_id', 'different-tenant'),
    ('tenant_id', []), ('subject_id', ''), ('subject_id', 'x' * 257),
    ('scopes', ['admin']), ('scopes', 'chat'),
])
def test_present_corrupt_principal_is_denied(field, value):
    config = _accepted()
    config[KEY]['principal'][field] = value
    with pytest.raises(PermissionError):
        restore_execution_identity(config)


@pytest.mark.parametrize('field,value', [('version', 2), ('attended', False), ('extra', True)])
def test_envelope_version_attendance_and_unknown_fields_fail_closed(field, value):
    config = _accepted()
    config[KEY][field] = value
    with pytest.raises(PermissionError):
        restore_execution_identity(config)


def test_legacy_absence_requires_explicit_user_owner_and_rejects_nonhuman_markers():
    config = {'_turnOwnerUserId': 7, '_turnPrincipalTenantId': 'synthetic-tenant'}
    principal, attended = restore_execution_identity(config)
    assert principal.owner_user_id == 7 and attended is True
    for extra in ({'_turnPrincipalKind': 'system'}, {'_attended': False}, {'_principal': {}}):
        with pytest.raises(PermissionError):
            restore_execution_identity({**config, **extra})
    with pytest.raises(ValueError):
        restore_execution_identity({})


@pytest.mark.parametrize('operation', ['create', 'resume', 'plan'])
def test_command_acceptance_stamps_authentication_not_config_actor_or_projection(monkeypatch, operation):
    import lib.conversation_sync.command_service as commands
    from lib.plan_contract import proposed_plan_document

    captured = []
    class CapturedAdmission(Exception):
        pass
    def accept(*args, **kwargs):
        captured.append(kwargs['config'])
        raise CapturedAdmission
    monkeypatch.setattr(commands, 'create_turn_pair', accept)
    monkeypatch.setattr(commands, 'create_attempt', accept)
    plan = proposed_plan_document(content='<proposed_plan>implement scoped change</proposed_plan>')
    source = {'turnId': 'turn-test', 'projectionRevision': 1, 'actor': 'planner',
              'status': 'completed', 'projection': {'proposedPlan': plan}}
    monkeypatch.setattr(commands, 'get_turn', lambda *a, **kw: source)
    service = commands.ConversationTurnCommandService(
        build_user_message=lambda message, *args: message,
        was_aborted_after=lambda *args: False,
        start_task=lambda *args, **kwargs: pytest.fail('No execution before durable admission'))
    body = {'commandId': 'identity-command', 'expectedProjectionRevision': 1,
            'operation': 'retry', 'planId': plan['planId'], 'contextMode': 'current',
            'inputActor': 'virtual_user', 'actor': 'assistant',
            'inputTurn': {'content': 'synthetic work', KEY: {'attended': False}},
            'config': {KEY: {'attended': False}, '_turnPrincipalTenantId': 'forged',
                       '_attended': False, '_principal': 'forged', 'model': 'synthetic'}}
    with pytest.raises(CapturedAdmission):
        if operation == 'create':
            service.create_turn('conv-test', 7, body, request_started_at=1, principal=_principal())
        elif operation == 'resume':
            service.create_attempt('conv-test', 'turn-test', 7, body,
                                   request_started_at=1, principal=_principal())
        else:
            service.execute_plan('conv-test', 'turn-test', 7, body,
                                 request_started_at=1, principal=_principal())
    config = captured[0]
    config['_turnOwnerUserId'] = 7
    principal, attended = restore_execution_identity(config)
    assert principal.subject_id == 'synthetic-credential'
    assert principal.tenant_id == 'synthetic-tenant'
    assert principal.scopes == frozenset() and attended is True


def test_executor_rejects_corrupt_identity_before_context_registration_or_provider(monkeypatch):
    import lib.conversation_sync.task_start as runtime
    config = _accepted()
    config[KEY]['principal']['kind'] = 'system'
    monkeypatch.setattr('lib.turn_lifecycle.build_api_messages',
                        lambda *a, **kw: pytest.fail('Corrupt identity reached context'))
    monkeypatch.setattr(runtime, 'create_task',
                        lambda *a, **kw: pytest.fail('Corrupt identity reached registration'))
    with pytest.raises(PermissionError):
        runtime.start_conversation_attempt_executor('conv-test', config)


def test_executor_registration_uses_durable_subject_tenant_and_attendance(monkeypatch):
    import lib.conversation_sync.task_start as runtime
    config = json.loads(json.dumps(_accepted()))
    captured = []
    task = {'id': 'task-test', 'convId': 'conv-test', 'config': config}
    monkeypatch.setattr(runtime, '_throttled_cleanup_old_tasks', lambda: None)
    monkeypatch.setattr('lib.turn_lifecycle.build_api_messages',
                        lambda *a, **kw: [{'role': 'user', 'content': 'synthetic'}])
    def register(*args, principal, **kwargs):
        captured.append(principal)
        return task
    monkeypatch.setattr(runtime, 'create_task', register)
    monkeypatch.setattr('lib.tasks_pkg.manager.discard_task', lambda *a, **kw: None)
    def stop_before_bind(*args):
        raise RuntimeError('stop-before-billable')
    monkeypatch.setattr(runtime, '_prepare_unbound_task_dispatch', stop_before_bind)
    with pytest.raises(RuntimeError, match='stop-before-billable'):
        runtime.start_conversation_attempt_executor('conv-test', config)
    assert captured[0].subject_id == 'synthetic-credential'
    assert captured[0].tenant_id == 'synthetic-tenant'
    assert captured[0].scopes == frozenset()
    assert task['_attended'] is True


@pytest.mark.parametrize('field', ['_conversationExecutionAdmission', '_principalContext',
    '_turnOwnerUserId', '_turnId', '_attemptId', '_userId', '_attended', '_unattended'])
def test_private_fields_cannot_enter_public_settings_or_projection(field):
    from lib.conversation_sync.command_service import _without_client_plan_authority
    from lib.conversation_sync.settings_policy import public_conversation_defaults
    forged = {'content': 'retained text', field: {'forged': True}}
    original = copy.deepcopy(forged)
    assert field not in _without_client_plan_authority(forged)
    defaults = public_conversation_defaults({'settings': forged})
    assert defaults['settings'] == {'content': 'retained text'}
    assert forged == original


@pytest.mark.parametrize('operation', ['create', 'resume', 'plan'])
def test_system_principal_is_rejected_at_each_executable_command(monkeypatch, operation):
    import lib.conversation_sync.command_service as commands
    from lib.plan_contract import proposed_plan_document
    plan = proposed_plan_document(content='<proposed_plan>implement scoped change</proposed_plan>')
    monkeypatch.setattr(commands, 'get_turn', lambda *a, **kw: {
        'turnId': 'turn-test', 'projectionRevision': 1, 'actor': 'planner',
        'status': 'completed', 'projection': {'proposedPlan': plan}})
    def no_accept(*a, **kw):
        pytest.fail('System dispatch reached durable execution acceptance')
    monkeypatch.setattr(commands, 'create_turn_pair', no_accept)
    monkeypatch.setattr(commands, 'create_attempt', no_accept)
    service = commands.ConversationTurnCommandService(
        build_user_message=lambda message, *args: message,
        was_aborted_after=lambda *args: False, start_task=no_accept)
    principal = PrincipalContext.system(subject_id='synthetic-coordinator',
                                        owner_user_id=7, scopes={'chat'})
    body = {'commandId': 'system-command', 'expectedProjectionRevision': 1,
            'operation': 'retry', 'planId': plan['planId'], 'contextMode': 'current',
            'inputActor': 'human', 'inputTurn': 'synthetic work'}
    with pytest.raises(PermissionError, match='trusted grant admission'):
        if operation == 'create':
            service.create_turn('conv-test', 7, body, request_started_at=1, principal=principal)
        elif operation == 'resume':
            service.create_attempt('conv-test', 'turn-test', 7, body,
                                   request_started_at=1, principal=principal)
        else:
            service.execute_plan('conv-test', 'turn-test', 7, body,
                                 request_started_at=1, principal=principal)


def test_replayed_dispatch_uses_owned_accepted_identity_not_request_identity(monkeypatch):
    import lib.conversation_sync.command_service as commands
    accepted = _accepted()
    request = capture_execution_identity({}, 7,
        PrincipalContext.user(subject_id='different-request', owner_user_id=7))
    monkeypatch.setattr(commands, 'claim_attempt_start', lambda *a, **kw: True)
    def read_attempt(attempt_id, *, user_id, include_execution_config=False):
        assert attempt_id == 'attempt-test' and user_id == 7
        assert include_execution_config is True
        return {'config': json.loads(json.dumps(accepted))}
    monkeypatch.setattr(commands, 'get_attempt', read_attempt)
    monkeypatch.setattr(commands, 'bind_task', lambda *a, **kw: {'taskId': 'task-test'})
    captured = []
    def start(conv_id, config, data, abort_after_ts, register, **kwargs):
        captured.append(config)
        register('task-test')
        return 'task-test', None
    service = commands.ConversationTurnCommandService(
        build_user_message=lambda message, *args: message,
        was_aborted_after=lambda *args: False, start_task=start)
    result = {'turn': {'turnId': 'turn-test', 'conversationId': 'conv-test',
                       'actor': 'assistant', 'kind': 'reply', 'projection': {}},
              'attempt': {'attemptId': 'attempt-test', 'operation': 'create'}}
    service._start_accepted_attempt_serialized(result, 7, request, {}, abort_after_ts=1)
    principal, attended = restore_execution_identity(captured[0])
    assert principal.subject_id == 'synthetic-credential'
    assert principal.tenant_id == 'synthetic-tenant' and attended is True


@pytest.mark.parametrize('stored_marker', [
    {KEY: None}, {'_turnPrincipalKind': 'system'}, {'_attended': False},
    {'_unattended': True}, {'_principalContext': {'kind': 'system'}},
    {'_conversationExecutionAdmission': {'accepted': True}},
])
def test_replay_rejects_durable_nonhuman_or_corrupt_identity_before_executor(monkeypatch, stored_marker):
    import lib.conversation_sync.command_service as commands
    monkeypatch.setattr(commands, 'claim_attempt_start', lambda *a, **kw: True)
    monkeypatch.setattr(commands, 'get_attempt', lambda *a, **kw: {'config': stored_marker})

    monkeypatch.setattr(commands, 'get_turn', lambda *a, **kw: {'turnId': 'turn-test'})
    failures = []
    monkeypatch.setattr(commands, 'fail_start', lambda *a, **kw: failures.append(kw))
    def no_worker(*a, **kw):
        pytest.fail('Stored nonhuman evidence was lost before executor admission')
    monkeypatch.setattr(commands, 'bind_task', no_worker)
    service = commands.ConversationTurnCommandService(
        build_user_message=lambda message, *args: message,
        was_aborted_after=lambda *args: False, start_task=no_worker)
    result = {'turn': {'turnId': 'turn-test', 'conversationId': 'conv-test',
                       'actor': 'assistant', 'kind': 'reply', 'projection': {}},
              'attempt': {'attemptId': 'attempt-test', 'operation': 'create'}}
    request = capture_execution_identity({}, 7, _principal())
    with pytest.raises(commands.AttemptStartFailure):
        service._start_accepted_attempt_serialized(result, 7, request, {}, abort_after_ts=1)
    assert len(failures) == 1


def test_every_contract_reserved_field_is_private_in_settings_and_projection():
    from lib.conversation_sync.execution_identity import RESERVED_EXECUTION_FIELDS
    from lib.conversation_sync.command_service import _without_client_plan_authority
    from lib.conversation_sync.settings_policy import public_conversation_defaults
    forged = {field: {'untrusted': True} for field in RESERVED_EXECUTION_FIELDS}
    forged['content'] = 'retained text'
    assert _without_client_plan_authority(forged) == {'content': 'retained text'}
    assert public_conversation_defaults({'settings': forged}) == {
        'settings': {'content': 'retained text'}}


@pytest.mark.parametrize('view_name,method,args', [
    ('conversation_turn_create_v3', 'create_turn', ('conv-test',)),
    ('conversation_attempt_create_v3', 'create_attempt', ('conv-test', 'turn-test')),
    ('conversation_plan_execute_v3', 'execute_plan', ('conv-test', 'turn-test')),
])
def test_executable_http_adapter_passes_authenticated_principal(monkeypatch, view_name, method, args):
    import inspect
    import routes.conversation_sync_v3 as routes
    from lib.conversation_sync.command_service import CommandOutcome
    principal = _principal()
    monkeypatch.setattr(routes, '_request_principal', lambda: principal)
    monkeypatch.setattr(routes, '_request_user_id', lambda: principal.require_owner())
    monkeypatch.setattr(routes, 'request_execution_clock', lambda: None)
    monkeypatch.setattr(routes, '_validate_body', lambda *a: None)
    monkeypatch.setattr(routes, 'parse_body', lambda: {'commandId': 'synthetic-adapter'})
    monkeypatch.setattr(routes, '_command_response', lambda operation, command: command())
    captured = []
    def command(*passed, **kwargs):
        assert passed[-2] == principal.require_owner()
        config = capture_execution_identity({}, passed[-2], kwargs['principal'])
        config['_turnOwnerUserId'] = principal.require_owner()
        captured.append(restore_execution_identity(config)[0])
        return CommandOutcome({'ok': True})
    monkeypatch.setattr(routes.conversation_turn_commands, method, command)
    inspect.unwrap(getattr(routes, view_name))(*args)
    assert captured[0].subject_id == principal.subject_id
    assert captured[0].tenant_id == principal.tenant_id
    assert captured[0].scopes == frozenset()
