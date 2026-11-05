"""Conversation-adjacent HTTP routes answer 400 only for typed client faults.

Turn owner review, legacy human guidance and conversation search follow the
taxonomy that ``test_conversation_command_error_taxonomy.py`` pins for the
conversation-sync routes (docs/contracts/API_CONTRACT.md §2.3). A ``BadRequest``
keeps its 400, message and ``field``. A builtin ``ValueError``/``TypeError`` is a
server fault: a logged 500 whose body never carries the exception text. Storage
failures keep the storage taxonomy: its own 400 (``database_invalid_payload``)
is a typed rejection, while a protocol failure is a server fault.

Regression: each route answered every ``ValueError`` with 400 and its raw text,
and search also answered ``database_protocol_error`` with 400, so programming
errors and malformed storage responses read as the caller's mistake and left
no server log.
"""
from __future__ import annotations

import ast
import asyncio
import json
import logging
from pathlib import Path

import pytest
from quart import Quart, g

pytestmark = pytest.mark.unit

_OWNER = 1
_LEAK = 'internal invariant: stored record lost its owner'
_STORAGE_LEAK = 'private storage diagnostic'
_MAJORS = ('v1', 'v4')
_BUILTIN_FAULT_TYPES = (ValueError, TypeError)


def _raising(error):
    def raise_error(*_args, **_kwargs):
        raise error
    return raise_error


def _storage_error(code, message, **fields):
    from lib.storage.errors import StorageError

    return StorageError(code, message, **fields)


def _app(blueprint):
    """Serve ``blueprint`` on its legacy path and on its v4 projection."""
    from lib.api_keys import AuthContext
    from lib.api_v4 import attach_v4_envelope, copy_blueprint_to_v4
    from lib.http_transport.error_handlers import register_http_error_handlers
    from lib.identity import PrincipalContext

    app = Quart(__name__)

    @app.before_request
    def identity():
        g.auth_ctx = AuthContext(key_id='taxonomy-test', name='taxonomy-test',
                                 scopes=frozenset({'chat'}), owner_user_id=_OWNER)
        g.principal_context = PrincipalContext.user(
            subject_id='taxonomy-test', owner_user_id=_OWNER, scopes={'chat'})

    register_http_error_handlers(app)
    app.register_blueprint(blueprint)
    app.register_blueprint(attach_v4_envelope(copy_blueprint_to_v4(
        blueprint, name=f'{blueprint.name}_taxonomy_v4')))
    return app


def _send(app, method, path, **kwargs):
    async def run():
        response = await getattr(app.test_client(), method)(path, **kwargs)
        return response.status_code, await response.get_data(as_text=True)
    return asyncio.run(run())


def _assert_client_fault(major, status, text, message, *, field='', storage_code=''):
    assert status == 400
    body = json.loads(text)
    if major == 'v1':
        assert body['ok'] is False
        assert body['error'] == message
        assert body.get('field', '') == field
        assert body.get('storageCode', '') == storage_code
    else:
        assert (body['status'], body['code'], body['detail']) == (400, 'bad_request', message)


def _assert_logged_server_fault(status, text, caplog, leaked):
    assert status == 500
    assert leaked not in text
    assert leaked in caplog.text
    assert 'Traceback' in caplog.text


# ── Turn owner review ────────────────────────────────────────────────

@pytest.fixture
def review_app():
    from routes.api_v1.turn_review import api_v1_turn_review_bp

    return _app(api_v1_turn_review_bp)


def _arm_review(monkeypatch, error):
    import routes.api_v1.turn_review as turn_review

    monkeypatch.setattr(turn_review, 'update_review', _raising(error))


def _post_review(app, major):
    return _send(app, 'post', f'/api/{major}/conversations/conv-a/turns/turn-a/review',
                 json={'action': 'requirements'})


@pytest.mark.parametrize('major', _MAJORS)
def test_turn_review_client_fault_keeps_its_400_message_and_field(review_app, monkeypatch, major):
    from lib.error_envelope import BadRequest

    message = 'Output slots require unique IDs, unique artifacts and nonempty labels'
    _arm_review(monkeypatch, BadRequest(message, field='outputs'))

    status, text = _post_review(review_app, major)

    _assert_client_fault(major, status, text, message, field='outputs')


@pytest.mark.parametrize('major', _MAJORS)
@pytest.mark.parametrize('fault', _BUILTIN_FAULT_TYPES, ids=lambda fault: fault.__name__)
def test_turn_review_builtin_fault_is_a_logged_redacted_500(
        review_app, monkeypatch, caplog, major, fault):
    _arm_review(monkeypatch, fault(_LEAK))

    with caplog.at_level(logging.ERROR):
        status, text = _post_review(review_app, major)

    _assert_logged_server_fault(status, text, caplog, _LEAK)


@pytest.mark.parametrize('major', _MAJORS)
def test_turn_review_storage_protocol_failure_is_a_server_fault(
        review_app, monkeypatch, caplog, major):
    _arm_review(monkeypatch, _storage_error('database_protocol_error', _STORAGE_LEAK))

    with caplog.at_level(logging.ERROR):
        status, text = _post_review(review_app, major)

    assert status == 500
    assert _STORAGE_LEAK in caplog.text
    if major == 'v4':
        assert _STORAGE_LEAK not in text


# ── Legacy human guidance ────────────────────────────────────────────

@pytest.fixture
def human_app():
    import routes.chat_human_io  # noqa: F401  (attaches the route to its blueprint)
    from routes.api_v1.chat import api_v1_chat_bp

    return _app(api_v1_chat_bp)


def _arm_guidance(monkeypatch, error):
    import lib.tasks_pkg.human_guidance as human_guidance

    monkeypatch.setattr(human_guidance, 'resolve_human_guidance', _raising(error))


def _answer(app, major):
    return _send(app, 'post', f'/api/{major}/chat/human-response',
                 json={'guidanceId': 'hg_taxonomy', 'response': 'Server'})


@pytest.mark.parametrize('major', _MAJORS)
def test_human_guidance_client_fault_keeps_its_400_message_and_field(human_app, monkeypatch, major):
    from lib.error_envelope import BadRequest

    _arm_guidance(monkeypatch, BadRequest('human guidance response is required', field='response'))

    status, text = _answer(human_app, major)

    _assert_client_fault(major, status, text, 'human guidance response is required',
                         field='response')


@pytest.mark.parametrize('major', _MAJORS)
def test_human_guidance_storage_rejection_is_a_typed_400(human_app, monkeypatch, caplog, major):
    message = 'Human guidance response is not a declared choice'
    _arm_guidance(monkeypatch, _storage_error(
        'database_invalid_payload', message, operation_id='storage-op'))

    with caplog.at_level(logging.WARNING):
        status, text = _answer(human_app, major)

    _assert_client_fault(major, status, text, message, storage_code='database_invalid_payload')
    if major == 'v1':
        assert json.loads(text)['operationId'] == 'storage-op'
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]


@pytest.mark.parametrize('major', _MAJORS)
@pytest.mark.parametrize('fault', _BUILTIN_FAULT_TYPES, ids=lambda fault: fault.__name__)
def test_human_guidance_builtin_fault_is_a_logged_redacted_500(
        human_app, monkeypatch, caplog, major, fault):
    _arm_guidance(monkeypatch, fault(_LEAK))

    with caplog.at_level(logging.ERROR):
        status, text = _answer(human_app, major)

    _assert_logged_server_fault(status, text, caplog, _LEAK)


@pytest.mark.parametrize('major', _MAJORS)
def test_human_guidance_storage_protocol_failure_is_a_server_fault(
        human_app, monkeypatch, caplog, major):
    _arm_guidance(monkeypatch, _storage_error('database_protocol_error', _STORAGE_LEAK))

    with caplog.at_level(logging.ERROR):
        status, text = _answer(human_app, major)

    assert status == 500
    assert _STORAGE_LEAK in caplog.text
    if major == 'v4':
        assert _STORAGE_LEAK not in text


@pytest.mark.parametrize('case', ['not_text', 'blank', 'too_long'])
def test_human_guidance_rejects_the_callers_response_as_a_typed_fault(case):
    from lib.error_envelope import BadRequest
    from lib.human_gate_contract import MAX_HUMAN_GATE_RESPONSE_LENGTH
    from lib.tasks_pkg.human_guidance import resolve_human_guidance

    response = {'not_text': 7, 'blank': '  ',
                'too_long': 'x' * (MAX_HUMAN_GATE_RESPONSE_LENGTH + 1)}[case]
    with pytest.raises(BadRequest) as caught:
        resolve_human_guidance('hg_taxonomy', response, owner_user_id=_OWNER)
    assert caught.value.field == 'response'


# ── Conversation search ──────────────────────────────────────────────

class _UnreachableSearchStorage:
    def query(self, *_args):
        pytest.fail('a rejected search must not reach storage')


class _SearchStorage:
    def __init__(self, outcome):
        self.outcome = outcome

    def query(self, *_args):
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


@pytest.fixture
def search_app():
    import routes.conversations_search  # noqa: F401  (attaches the route to its blueprint)
    from routes.api_v1.conversations import api_v1_conversations_bp

    return _app(api_v1_conversations_bp)


def _search_storage(monkeypatch, client):
    import lib.storage

    monkeypatch.setattr(lib.storage, 'get_storage_client', lambda *_a, **_kw: client)


def _search(app, major, **query):
    return _send(app, 'get', f'/api/{major}/conversations/search', query_string=query)


@pytest.mark.parametrize('major', _MAJORS)
@pytest.mark.parametrize(('query', 'message', 'field'), [
    ({'q': 'x' * 513}, 'Search query is too long', 'q'),
    ({'q': ' '.join(['term'] * 33)}, 'Search query is too long', 'q'),
    ({'q': 'nul\x00query'}, 'Search query contains NUL', 'q'),
    ({'q': 'ab', 'cursor': 'c' * 8193}, 'Invalid search cursor', 'cursor'),
], ids=['characters', 'terms', 'nul', 'cursor'])
def test_search_client_fault_keeps_its_400_message_and_field(
        search_app, monkeypatch, major, query, message, field):
    _search_storage(monkeypatch, _UnreachableSearchStorage())

    status, text = _search(search_app, major, **query)

    _assert_client_fault(major, status, text, message, field=field)


@pytest.mark.parametrize('major', _MAJORS)
@pytest.mark.parametrize('fault', _BUILTIN_FAULT_TYPES, ids=lambda fault: fault.__name__)
def test_search_builtin_fault_is_a_logged_redacted_500(
        search_app, monkeypatch, caplog, major, fault):
    import routes.conversations_search as conversations_search

    _search_storage(monkeypatch, _UnreachableSearchStorage())
    monkeypatch.setattr(conversations_search, 'search_conversation_page', _raising(fault(_LEAK)))

    with caplog.at_level(logging.ERROR):
        status, text = _search(search_app, major, q='python')

    _assert_logged_server_fault(status, text, caplog, _LEAK)


@pytest.mark.parametrize('major', _MAJORS)
@pytest.mark.parametrize('outcome', [
    'rejected_cursor',
    'malformed_page',
])
def test_search_storage_protocol_failure_is_a_server_fault(
        search_app, monkeypatch, caplog, major, outcome):
    """A protocol failure is never the caller's fault, whatever provoked it."""
    storage_outcome = {
        'rejected_cursor': _storage_error(
            'database_protocol_error', 'Invalid conversation search cursor'),
        'malformed_page': [],
    }[outcome]
    _search_storage(monkeypatch, _SearchStorage(storage_outcome))

    with caplog.at_level(logging.ERROR):
        status, text = _search(search_app, major, q='python', cursor='forged')

    assert status == 500
    body = json.loads(text)
    if major == 'v1':
        assert body['storageCode'] == 'database_protocol_error'
    else:
        assert (body['code'], body['detail']) == ('internal_error', 'database_protocol_error')
    assert 'conversation.search' in caplog.text
    assert 'Traceback' in caplog.text


@pytest.mark.parametrize('major', _MAJORS)
def test_search_storage_rejection_is_a_typed_400(search_app, monkeypatch, caplog, major):
    _search_storage(monkeypatch, _SearchStorage(_storage_error(
        'database_invalid_payload', 'Invalid diagnostic cursor', operation_id='storage-op')))

    with caplog.at_level(logging.WARNING):
        status, text = _search(search_app, major, q='python')

    _assert_client_fault(major, status, text, 'Invalid diagnostic cursor',
                         storage_code='database_invalid_payload')
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]


@pytest.mark.parametrize(('query', 'cursor', 'field'), [
    (None, None, 'q'),
    ('x' * 513, None, 'q'),
    (' '.join(['term'] * 33), None, 'q'),
    ('nul\x00query', None, 'q'),
    ('ab', 'c' * 8193, 'cursor'),
    ('ab', 7, 'cursor'),
], ids=['query-type', 'characters', 'terms', 'nul', 'cursor-length', 'cursor-type'])
def test_search_rejects_the_callers_query_and_cursor_as_typed_faults(query, cursor, field):
    from lib.conversations.search import search_conversation_page
    from lib.error_envelope import BadRequest

    with pytest.raises(BadRequest) as caught:
        search_conversation_page(_UnreachableSearchStorage(), user_id=_OWNER,
                                 query=query, cursor=cursor)
    assert caught.value.field == field


# ── Source guard ─────────────────────────────────────────────────────

_ROUTE_SOURCES = (
    'routes/api_v1/turn_review.py',
    'routes/chat_human_io.py',
    'routes/conversations_search.py',
)
_BUILTIN_FAULTS = frozenset({
    'ArithmeticError', 'AttributeError', 'BaseException', 'Exception',
    'IndexError', 'KeyError', 'LookupError', 'OverflowError', 'RuntimeError',
    'TypeError', 'ValueError',
})
_CLIENT_RESPONSES = frozenset({'api_bad_request', 'api_conflict', 'api_not_found'})


def _caught_names(handler: ast.ExceptHandler) -> set[str]:
    if handler.type is None:
        return {'BaseException'}
    nodes = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return {node.id for node in nodes if isinstance(node, ast.Name)}


def _called_names(statements) -> set[str]:
    return {node.func.id for statement in statements for node in ast.walk(statement)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}


@pytest.mark.parametrize('relative', _ROUTE_SOURCES)
def test_no_builtin_exception_is_answered_as_a_client_fault(relative):
    """A builtin exception may be translated or logged, never answered as 4xx."""
    source = (Path(__file__).resolve().parents[2] / relative).read_text(encoding='utf-8')
    offenders = [
        f'line {node.lineno}: {sorted(_called_names(node.body) & _CLIENT_RESPONSES)}'
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ExceptHandler) and _caught_names(node) & _BUILTIN_FAULTS
        and _called_names(node.body) & _CLIENT_RESPONSES
    ]
    assert offenders == []
