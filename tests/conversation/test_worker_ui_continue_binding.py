"""A UI Continue on a Worker conversation resumes through its assignment (D3).

2026-11-06 11:39: a Continue pressed in the browser on a Worker conversation
started task e4fd0a5f in the API host as an ordinary chat attempt. It had
another task identity, no grant fence and no assignment association, so its
result never reached the Coordinator. ``control_conversation`` resumes the same
Turn through ``TrustedDispatch`` (lib/conversations/session_control.py). The
browser path now does the same, and refuses with an explanation when the grant
can no longer authorize a resume.
"""
import pytest

from lib.conversation_sync import command_service
from lib.project_dispatch.admission import KEY, WORKSPACE_TASK_KEY, TrustedDispatch
from lib.project_dispatch.repository import DispatchGrantRevoked
from lib.turn_lifecycle import LifecycleConflict

pytestmark = pytest.mark.unit

BINDING = {'assignmentId': '27ce29e9', 'owner': 1, 'tenant': '',
           'recipientId': 'worker-lane', 'projectPath': '/project'}
DISPATCH_PRINCIPAL = object()


class _Created(Exception):
    pass


@pytest.fixture
def service(monkeypatch):
    created = []

    def create_attempt(conversation_id, turn_id, **kwargs):
        created.append(kwargs)
        raise _Created

    monkeypatch.setattr(command_service, 'create_attempt', create_attempt)
    monkeypatch.setattr(command_service, 'get_turn', lambda *a, **kw: {
        'turnId': 'turn-1', 'conversationId': 'worker-lane', 'currentAttemptId': 'attempt-1',
        'projectionRevision': 3, 'projection': {}})
    monkeypatch.setattr('lib.conversation_sync.workspace.resolve_workspace_config',
                        lambda config, *a, **kw: dict(config))
    monkeypatch.setattr('lib.acceptance.continuity.review_source', lambda *a, **kw: None)
    monkeypatch.setattr(command_service, '_refuse_scheduled_execution_identity',
                        lambda config: None)
    commands = command_service.ConversationTurnCommandService(
        build_user_message=lambda *a: None, was_aborted_after=lambda *a: False,
        start_task=lambda *a, **kw: pytest.fail('no executor may start'))
    return commands, created


def _accepted(monkeypatch, config):
    monkeypatch.setattr(command_service, 'get_attempt', lambda attempt_id, **kw: {
        'attemptId': attempt_id, 'config': config})


def _ui_continue(commands):
    return commands.create_attempt('worker-lane', 'turn-1', 1, {
        'commandId': 'ui-continue', 'operation': 'continue',
        'expectedProjectionRevision': 3, 'config': {'model': 'browser-model'}},
        request_started_at=0.0)


def test_ui_continue_of_a_dispatched_turn_resumes_through_its_assignment(service, monkeypatch):
    commands, created = service
    _accepted(monkeypatch, {KEY: dict(BINDING), 'model': 'worker-model'})
    captured = []
    monkeypatch.setattr(TrustedDispatch, 'checked', lambda self: {'execution': None})
    monkeypatch.setattr(TrustedDispatch, 'principal', property(lambda self: DISPATCH_PRINCIPAL))

    def capture(self, config, owner, principal, recipient):
        captured.append((owner, principal, recipient))
        return {**config, KEY: dict(self.binding), WORKSPACE_TASK_KEY: 'project-worker:27ce29e9'}

    monkeypatch.setattr(TrustedDispatch, 'capture', capture)

    with pytest.raises(_Created):
        _ui_continue(commands)

    assert captured == [(1, DISPATCH_PRINCIPAL, 'worker-lane')]
    config = created[0]['config']
    assert config[KEY] == BINDING
    assert config[WORKSPACE_TASK_KEY] == 'project-worker:27ce29e9'


def test_ui_continue_refuses_when_the_assignment_can_no_longer_authorize_it(
        service, monkeypatch):
    commands, created = service
    _accepted(monkeypatch, {KEY: dict(BINDING)})

    def revoked(self):
        raise DispatchGrantRevoked('the Coordinator cancelled this assignment')

    monkeypatch.setattr(TrustedDispatch, 'checked', revoked)

    with pytest.raises(LifecycleConflict) as refused:
        _ui_continue(commands)

    assert refused.value.code == 'worker_binding_refused'
    assert 'cancelled this assignment' in refused.value.message
    assert created == []


def test_ui_continue_of_an_ordinary_turn_is_unchanged(service, monkeypatch):
    commands, created = service
    _accepted(monkeypatch, {'model': 'chat-model'})
    monkeypatch.setattr(TrustedDispatch, 'checked',
                        lambda self: pytest.fail('an ordinary Turn reads no dispatch'))

    with pytest.raises(_Created):
        _ui_continue(commands)

    assert KEY not in created[0]['config']


@pytest.mark.parametrize(('operation', 'response', 'message'), [
    ('regenerate', 'yes', 'humanResponse is only valid for answer_guidance'),
    ('answer_guidance', ' ', 'humanResponse required for answer_guidance'),
    ('answer_guidance', 'x' * 32769, 'humanResponse exceeds the 32768-character limit'),
], ids=['wrong-operation', 'blank-answer', 'oversized-answer'])
def test_invalid_response_is_rejected_before_loading_worker_binding(
        service, monkeypatch, operation, response, message):
    from lib.error_envelope import BadRequest

    commands, created = service
    monkeypatch.setattr(command_service, 'get_turn',
                        lambda *a, **kw: pytest.fail('Invalid request must not load a Turn'))

    with pytest.raises(BadRequest, match=message) as refused:
        commands.create_attempt('worker-lane', 'turn-1', 1, {
            'commandId': 'invalid-response', 'operation': operation,
            'expectedProjectionRevision': 3, 'config': {'model': 'browser-model'},
            'humanResponse': response}, request_started_at=0.0)

    assert refused.value.field == 'humanResponse'
    assert created == []
