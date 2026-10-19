"""Submission evidence survives postcommit faults; live Stop fences new input.

The shared synthetic mailbox fixture uses portable record CAS, with no live
storage, worker or provider. Faults are injected at the before/after commit seams.
"""
from types import SimpleNamespace
import threading

import pytest

from lib.storage.errors import StorageError
from lib.swarm import message_service
from lib.swarm.run_messages import RunMessageError, RunMessageRepository
from tests.swarm import test_run_messages as mailbox_tests

context = mailbox_tests.context
args = mailbox_tests.args
pytestmark = pytest.mark.unit


def after_commit(context, monkeypatch, callback):
    original = context.client.command

    def command(*positional, **keyword):
        result = original(*positional, **keyword)
        callback()
        return result

    monkeypatch.setattr(context.client, 'command', command)


@pytest.mark.parametrize('fault', ['archived', 'readOnly', 'project', 'missing', 'read_error'])
def test_postcommit_scope_failure_returns_saved_receipt_without_closing(context, monkeypatch, fault):
    def revoke():
        def get(*positional, **keyword):
            if fault == 'read_error':
                raise RuntimeError('synthetic conversation read unavailable')
            if fault == 'missing':
                return None
            settings = {'projectPath': '/p'}
            if fault == 'project':
                settings['projectPath'] = '/other'
            else:
                settings[fault] = True
            return SimpleNamespace(metadata={'settings': settings})
        monkeypatch.setattr('lib.conversations.repository.ConversationRepository.get', get)

    after_commit(context, monkeypatch, revoke)
    sent = message_service.send(context.task, args(), context.session)
    value, revision = context.repo.load()
    assert sent['message_id'] == value['messages'][0]['id']
    assert sent['state'] == 'submitted' and sent['execution_started'] is False
    assert revision == 1 and value['closed'] is False
    assert value['messages'][0]['content'] == args()['content']
    # Returning saved evidence must not weaken fresh read ownership/scope.
    with pytest.raises(RuntimeError if fault == 'read_error' else RunMessageError):
        message_service.read(context.task, {
            'agent_id': 'child-a', 'swarm_run_id': 'launch',
            'message_id': sent['message_id'],
        })


def test_postcommit_mailbox_read_failure_needs_no_reconciliation_read(context, monkeypatch):
    original_query = context.client.query

    def fail_reads():
        def unavailable(*positional, **keyword):
            raise StorageError('database_unavailable', 'synthetic mailbox read unavailable')
        monkeypatch.setattr(context.client, 'query', unavailable)
        context.session._results_by_id['child-a'] = object()

    after_commit(context, monkeypatch, fail_reads)
    sent = message_service.send(context.task, args(), context.session)
    assert sent['state'] == 'submitted' and sent['execution_started'] is False
    monkeypatch.setattr(context.client, 'query', original_query)
    value, revision = context.repo.load()
    assert revision == 1 and value['closed'] is False
    assert value['messages'][0]['id'] == sent['message_id']


def install_stop(context, source):
    """Return a live stop action and the optional original-caller callback."""
    caller_stop = threading.Event()
    context.task['abort_event'] = threading.Event()
    context.child['abort_event'] = threading.Event()
    context.session._agents = {'child-a': SimpleNamespace(_child=context.child)}
    if source == 'caller_flag':
        return lambda: context.task.update(aborted=True), None
    if source == 'caller_event':
        return context.task['abort_event'].set, None
    if source == 'caller_check':
        return caller_stop.set, caller_stop.is_set
    if source == 'child_flag':
        return lambda: context.child.update(aborted=True), None
    if source == 'child_event':
        return context.child['abort_event'].set, None
    if source == 'session_check':
        context.session.abort_check = caller_stop.is_set
        return caller_stop.set, None
    if source == 'session_flag':
        return lambda: setattr(context.session, '_aborted', True), None
    raise AssertionError(source)


STOP_SOURCES = ['caller_flag', 'caller_event', 'caller_check', 'child_flag',
                'child_event', 'session_check', 'session_flag']


@pytest.mark.parametrize('source', STOP_SOURCES)
def test_live_stop_before_mailbox_commit_rejects_without_writing(context, monkeypatch, source):
    stop, abort_check = install_stop(context, source)
    original = RunMessageRepository.submit

    def submit(repository, **kwargs):
        # Initial routing has already passed; Stop arrives before mailbox CAS.
        stop()
        return original(repository, **kwargs)

    monkeypatch.setattr(RunMessageRepository, 'submit', submit)
    with pytest.raises(RunMessageError) as caught:
        message_service.send(context.task, args(), context.session, abort_check=abort_check)
    assert caught.value.submission_state == 'rejected'
    assert context.repo.load()[1] == 0


@pytest.mark.parametrize('source', STOP_SOURCES)
def test_live_stop_after_commit_returns_original_evidence_without_closing(context, monkeypatch, source):
    stop, abort_check = install_stop(context, source)
    after_commit(context, monkeypatch, stop)
    sent = message_service.send(context.task, args(), context.session, abort_check=abort_check)
    value, revision = context.repo.load()
    assert sent['state'] == 'submitted' and sent['execution_started'] is False
    assert (sent['agent_id'], sent['swarm_run_id']) == ('child-a', 'launch')
    assert value['messages'][0]['id'] == sent['message_id']
    assert revision == 1 and value['closed'] is False


def test_already_stopped_original_caller_rejects_even_with_task_copy(context):
    caller_stop = threading.Event()
    caller_stop.set()
    with pytest.raises(RunMessageError) as caught:
        message_service.send(dict(context.task), args(), context.session,
                             abort_check=caller_stop.is_set)
    assert caught.value.submission_state == 'rejected'
    assert context.repo.load()[1] == 0


def test_stop_during_target_validation_is_rechecked_before_commit(context, monkeypatch):
    caller_stop = threading.Event()
    original = message_service.validate_target
    validations = []

    def validate(binding):
        original(binding)
        validations.append(binding)
        if len(validations) == 2:
            caller_stop.set()

    monkeypatch.setattr(message_service, 'validate_target', validate)
    with pytest.raises(RunMessageError) as caught:
        message_service.send(dict(context.task), args(), context.session,
                             abort_check=caller_stop.is_set)
    assert caught.value.submission_state == 'rejected'
    assert context.repo.load()[1] == 0


def test_cas_retry_rechecks_stop_without_committing(context, monkeypatch):
    caller_stop = threading.Event()

    def conflict(*positional, **keyword):
        caller_stop.set()
        raise StorageError('database_conflict', 'synthetic CAS race')

    monkeypatch.setattr(context.client, 'command', conflict)
    with pytest.raises(RunMessageError) as caught:
        message_service.send(context.task, args(), context.session,
                             abort_check=caller_stop.is_set)
    assert caught.value.submission_state == 'rejected'
    assert context.repo.load()[1] == 0


def test_lost_commit_ack_then_stop_reconciles_saved_message(context, monkeypatch):
    caller_stop = threading.Event()
    original = context.client.command

    def lose_ack(*positional, **keyword):
        original(*positional, **keyword)
        caller_stop.set()
        raise StorageError('database_timeout', 'synthetic lost commit ACK', retryable=True)

    monkeypatch.setattr(context.client, 'command', lose_ack)
    sent = message_service.send(context.task, args(), context.session,
                                abort_check=caller_stop.is_set)
    value, revision = context.repo.load()
    assert revision == 1 and value['closed'] is False
    assert value['messages'][0]['id'] == sent['message_id']
    assert sent['state'] == 'submitted'


def test_postcommit_receipt_projection_failure_is_unknown_not_rejected(context, monkeypatch):
    original = RunMessageRepository.submit

    def submit(repository, **kwargs):
        saved = original(repository, **kwargs)
        # Fault in service receipt projection, after the actual durable commit.
        return {key: value for key, value in saved.items() if key != 'state'}

    monkeypatch.setattr(RunMessageRepository, 'submit', submit)
    with pytest.raises(RunMessageError) as caught:
        message_service.send(context.task, args(), context.session)
    assert caught.value.submission_state == 'unknown'
    assert caught.value.code == 'message_submission_unknown'
    value, revision = context.repo.load()
    assert revision == 1 and value['closed'] is False
    assert value['messages'][0]['state'] == 'submitted'
