"""Authenticated HTTP ownership and input bounds for blocking human gates."""

from __future__ import annotations

import threading
import time

import pytest
from quart import Quart, g, request

from lib.api_keys import AuthContext
from lib.human_gate_contract import (
    MAX_HUMAN_GATE_REQUEST_ID_LENGTH,
    MAX_HUMAN_GATE_RESPONSE_LENGTH,
)
from lib.tasks_pkg import approval, human_guidance, stdin_handler
from lib.identity import PrincipalContext


pytestmark = pytest.mark.unit
OWNER = 9101
OTHER_OWNER = 9102


def _wait_until(predicate, *, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError('human gate did not become pending')


@pytest.fixture
def owner_http_app():
    # Import the route package so the focused chat controls are attached to
    # their retained blueprint before it is registered on this test app.
    import server  # noqa: F401
    import routes  # noqa: F401
    from lib.http_error_handlers import register_http_error_handlers
    from routes.api_v1.chat import api_v1_chat_bp
    from routes.api_v1.project import api_v1_project_bp

    app = Quart(__name__)
    app.config['TESTING'] = True
    @app.before_request
    def _identity() -> None:
        owner_user_id = int(request.headers['X-Test-Owner'])
        g.auth_ctx = AuthContext(
            key_id=f'human-gate-test-{owner_user_id}',
            name='human-gate-test',
            scopes=frozenset({'admin'}),
            owner_user_id=owner_user_id,
        )
        g.principal_context = PrincipalContext.user(
            subject_id=f'human-gate-test:{owner_user_id}',
            owner_user_id=owner_user_id,
            scopes={'admin'},
        )

    register_http_error_handlers(app)
    app.register_blueprint(api_v1_chat_bp)
    app.register_blueprint(api_v1_project_bp)
    return app


@pytest.fixture
def owner_headers():
    return (
        {'X-Test-Owner': str(OWNER)},
        {'X-Test-Owner': str(OTHER_OWNER)},
    )


@pytest.mark.asyncio
async def test_http_resolvers_cannot_cross_owner_boundary(
    owner_http_app,
    owner_headers,
):
    owner_header, other_header = owner_headers
    client = owner_http_app.test_client()
    stdin_id = 'stdin-http-owner'
    guidance_id = 'guidance-http-owner'
    approval_id = 'approval-http-owner'
    task = {'id': 'task-http-owner', '_userId': OWNER, 'aborted': False}
    results = {}
    waiters = [
        threading.Thread(target=lambda: results.setdefault(
            'stdin', stdin_handler.request_stdin(
                stdin_id, owner_user_id=OWNER))),
        threading.Thread(target=lambda: results.setdefault(
            'guidance', human_guidance.request_human_guidance(
                guidance_id, task=task))),
        threading.Thread(target=lambda: results.setdefault(
            'approval', approval.request_write_approval(
                approval_id, timeout=2, owner_user_id=OWNER))),
    ]
    for waiter in waiters:
        waiter.start()
    _wait_until(lambda: all((
        stdin_handler.is_stdin_pending(stdin_id, owner_user_id=OWNER),
        human_guidance.is_human_guidance_pending(
            guidance_id, owner_user_id=OWNER),
        approval.is_write_approval_pending(
            approval_id, owner_user_id=OWNER),
    )))

    requests = [
        ('/api/v1/chat/stdin-response',
         {'stdinId': stdin_id, 'input': 'stdin answer'}),
        ('/api/v1/chat/human-response',
         {'guidanceId': guidance_id, 'response': 'guidance answer'}),
        ('/api/v1/project/write-approval',
         {'approvalId': approval_id, 'approved': True}),
    ]
    try:
        for path, body in requests:
            foreign = await client.post(
                path, json=body, headers=other_header)
            assert foreign.status_code == 404
        for path, body in requests:
            owned = await client.post(path, json=body, headers=owner_header)
            assert owned.status_code == 200
        for waiter in waiters:
            waiter.join(timeout=1)
        assert not any(waiter.is_alive() for waiter in waiters)
        assert results == {
            'stdin': 'stdin answer',
            'guidance': 'guidance answer',
            'approval': True,
        }
    finally:
        stdin_handler.resolve_stdin(
            stdin_id, None, owner_user_id=OWNER)
        human_guidance.cancel_human_guidance(
            guidance_id, owner_user_id=OWNER)
        approval.resolve_write_approval(
            approval_id, False, owner_user_id=OWNER)
        for waiter in waiters:
            waiter.join(timeout=1)


@pytest.mark.asyncio
@pytest.mark.parametrize(('path', 'body'), [
    ('/api/v1/chat/stdin-response', {
        'stdinId': 'stdin-invalid', 'input': {'not': 'text'},
    }),
    ('/api/v1/chat/human-response', {
        'guidanceId': 'guidance-invalid',
        'response': 'x' * (MAX_HUMAN_GATE_RESPONSE_LENGTH + 1),
    }),
    ('/api/v1/project/write-approval', {
        'approvalId': 'x' * (MAX_HUMAN_GATE_REQUEST_ID_LENGTH + 1),
        'approved': True,
    }),
    ('/api/v1/project/write-approval', {
        'approvalId': 'approval-invalid', 'approved': {'yes': True},
    }),
])
async def test_human_gate_http_rejects_untyped_or_oversized_fields(
    owner_http_app,
    owner_headers,
    path,
    body,
):
    owner_header, _other_header = owner_headers
    response = await owner_http_app.test_client().post(
        path, json=body, headers=owner_header)
    assert response.status_code == 400
