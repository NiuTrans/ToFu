"""Cost and artifact-integrity contracts for whole-paper translation."""

from __future__ import annotations

import asyncio
import math

import pytest


pytestmark = pytest.mark.unit
TEST_OWNER_USER_ID = 1


@pytest.fixture(autouse=True)
def _cleanup_translate_runtime():
    yield
    import lib.paper.translate_runtime as runtime

    original_ttl = runtime._translate_runtime.ttl
    runtime._translate_runtime.ttl = -1
    try:
        runtime._cleanup_stale_translate_tasks()
    finally:
        runtime._translate_runtime.ttl = original_ttl


def test_representative_paper_uses_seven_large_bounded_chunks():
    from lib.paper.translate_engine import _split_paper_translation_chunks
    from lib.paper.translate_runtime import _TRANSLATE_CHUNK_SIZE

    sentence = (
        'Academic evidence, methods, limitations, and equations remain in '
        'their original order. '
    )
    source = ('BEGIN. ' + sentence * 700)[:52_868] + ' END.'
    assert len(source) == 52_873

    chunks = _split_paper_translation_chunks(source)

    assert len(chunks) == 7
    assert all(0 < len(chunk) <= _TRANSLATE_CHUNK_SIZE for chunk in chunks)
    assert chunks[0].startswith('BEGIN.')
    assert chunks[-1].endswith('END.')
    old_call_count = math.ceil(len(source) / 2_400)
    assert old_call_count == 23
    assert len(chunks) / old_call_count < 0.31


def test_worker_translates_large_chunks_and_persists_once(monkeypatch):
    import lib.paper.translate_engine as engine
    import lib.paper.translate_runtime as runtime

    translate_calls = []
    storage_commands = []

    class _Client:
        def command(self, operation, payload, command_id):
            storage_commands.append((operation, payload, command_id))
            return {'saved': True}

    def translate(chunk, _system_prompt, **kwargs):
        translate_calls.append((chunk, kwargs))
        index = len(translate_calls)
        return (
            f'translated-{index}。',
            {'_translate_trace': {'model': f'model-{index}'}},
        )

    monkeypatch.setattr(engine, '_translate_one_chunk', translate)
    monkeypatch.setattr(
        'lib.storage.get_storage_client', lambda *, write=False: _Client())
    task = runtime._new_translate_task(
        'paper-translation-budget',
        'paper-budget-hash',
        'zh',
        None,
        user_id=TEST_OWNER_USER_ID,
    )
    field_updates = []
    update_fields = runtime._translate_runtime.update_fields

    def record_update(*args, **kwargs):
        field_updates.append(dict(kwargs.get('fields') or {}))
        return update_fields(*args, **kwargs)

    monkeypatch.setattr(runtime._translate_runtime, 'update_fields', record_update)

    engine._run_translate_task(task, 'A' * 17_000)

    assert task['status'] == 'done'
    assert [len(call[0]) for call in translate_calls] == [8_000, 8_000, 1_000]
    assert all(call[1]['use_cache'] is True for call in translate_calls)
    assert all(call[1]['allow_mt'] is True for call in translate_calls)
    assert all(call[1]['strict_model'] is False for call in translate_calls)
    assert all(call[1]['accept_truncated'] is False for call in translate_calls)
    assert len(storage_commands) == 1
    operation, payload, command_id = storage_commands[0]
    assert operation == 'paper.translation.upsert'
    assert payload['text'] == 'translated-1。\n\ntranslated-2。\n\ntranslated-3。'
    assert payload['model'] == 'model-3'
    assert command_id.startswith('paper.translation.upsert:')
    full_text_updates = [row for row in field_updates if 'full_text' in row]
    assert full_text_updates == [{
        'full_text': payload['text'],
        'progress': {'done': 3, 'total': 3},
    }]
    assert task['result'] is None
    assert 'result' not in task['events'][-1]


def test_output_budget_failure_never_publishes_or_builds_prefixes(monkeypatch):
    import lib.paper.translate_engine as engine
    import lib.paper.translate_runtime as runtime

    storage_commands = []

    class _Client:
        def command(self, operation, payload, command_id):
            storage_commands.append((operation, payload, command_id))
            return {'saved': True}

    monkeypatch.setattr(engine, 'PAPER_TRANSLATION_MAX_OUTPUT_CHARS', 20)
    monkeypatch.setattr(engine, 'PAPER_TRANSLATION_MAX_OUTPUT_BYTES', 100)
    monkeypatch.setattr(
        engine,
        '_translate_one_chunk',
        lambda *_args, **_kwargs: ('x' * 12, {}),
    )
    monkeypatch.setattr(
        'lib.storage.get_storage_client', lambda *, write=False: _Client())
    task = runtime._new_translate_task(
        'paper-translation-output-budget',
        'paper-output-budget-hash',
        'zh',
        None,
        user_id=TEST_OWNER_USER_ID,
    )

    engine._run_translate_task(task, 'A' * 8_001)

    assert task['status'] == 'error'
    assert '20 characters' in str(task['error'])
    assert task['full_text'] == ''
    assert storage_commands == []
    assert [event['type'] for event in task['events']].count('chunk') == 1


def test_translation_storage_enforces_utf8_budget_and_bounds_legacy_reads(
    monkeypatch,
):
    from lib.storage.errors import StorageError
    import lib.storage_sidecar.operations_pkg._papers as operations

    class _Session:
        def __init__(self):
            self.executed = False
            self.query = None
            self.args = None

        def lock_key(self, *_args):
            return None

        def execute(self, *_args):
            self.executed = True

        def fetch_one(self, query, args):
            self.query = query
            self.args = args
            return {'bounded_text': '\u8bd1\u8bd1', 'model': 'old', 'created_at': 1}

    monkeypatch.setattr(operations, 'PAPER_TRANSLATION_MAX_OUTPUT_CHARS', 10)
    monkeypatch.setattr(operations, 'PAPER_TRANSLATION_MAX_OUTPUT_BYTES', 5)
    session = _Session()
    payload = {
        'user_id': TEST_OWNER_USER_ID,
        'paper_hash': 'paper-storage-budget',
        'lang': 'zh',
        'text': '\u8bd1\u8bd1',
        'model': 'm',
        'created_at': 1,
    }

    with pytest.raises(StorageError, match='5 UTF-8 bytes'):
        operations._paper_translation_upsert(session, payload)
    assert session.executed is False

    assert operations._paper_translation_get(session, payload) is None
    assert 'substr(text, 1, ?)' in session.query
    assert session.args[0] == 11


def test_chunk_failure_never_publishes_placeholder_artifact(monkeypatch):
    from lib.translate.errors import TranslationContentRefused
    import lib.paper.translate_engine as engine
    import lib.paper.translate_runtime as runtime

    storage_commands = []

    class _Client:
        def command(self, operation, payload, command_id):
            storage_commands.append((operation, payload, command_id))
            return {'saved': True}

    def refuse(*_args, **_kwargs):
        raise TranslationContentRefused(
            'truncated', 'provider ended before the paper slice completed',
            attempts=5, content_fails=5,
        )

    monkeypatch.setattr(engine, '_translate_one_chunk', refuse)
    monkeypatch.setattr(
        'lib.storage.get_storage_client', lambda *, write=False: _Client())
    task = runtime._new_translate_task(
        'paper-translation-refused',
        'paper-refused-hash',
        'zh',
        None,
        user_id=TEST_OWNER_USER_ID,
    )

    engine._run_translate_task(task, 'A source paragraph that must translate.')

    assert task['status'] == 'error'
    assert task['error']['kind'] == 'content_refused'
    assert task['full_text'] == ''
    assert '[Translation error for this section:' not in task['full_text']
    assert storage_commands == []


@pytest.mark.parametrize('storage_result', [OSError('disk unavailable'), {'saved': False}])
def test_persistence_failure_cannot_mark_translation_done(
    monkeypatch, storage_result,
):
    import lib.paper.translate_engine as engine
    import lib.paper.translate_runtime as runtime

    class _Client:
        def command(self, _operation, _payload, _command_id):
            if isinstance(storage_result, BaseException):
                raise storage_result
            return storage_result

    monkeypatch.setattr(
        engine,
        '_translate_one_chunk',
        lambda *_args, **_kwargs: (
            'validated translation。',
            {'_translate_trace': {'model': 'translator'}},
        ),
    )
    monkeypatch.setattr(
        'lib.storage.get_storage_client', lambda *, write=False: _Client())
    task = runtime._new_translate_task(
        f'paper-translation-persist-{type(storage_result).__name__}',
        'paper-persist-hash',
        'zh',
        None,
        user_id=TEST_OWNER_USER_ID,
    )

    engine._run_translate_task(task, 'One source paragraph.')

    assert task['status'] == 'error'
    assert task['full_text'] == 'validated translation。'
    assert task['result'] is None


def test_route_rejects_oversized_paper_before_starting_work():
    from quart import Quart, g

    from lib.api_keys import local_admin_context
    from lib.paper.translate_runtime import _TRANSLATE_MAX_SOURCE_CHARS
    from routes.paper_pkg._qa_translate import api_v1_paper_bp

    app = Quart(__name__)

    @app.before_request
    def _bind_test_owner():
        g.auth_ctx = local_admin_context()

    app.register_blueprint(api_v1_paper_bp)

    async def request_oversized_paper():
        async with app.test_client() as client:
            response = await client.post(
                '/api/v1/paper/translate/start',
                json={
                    'paper_text': 'x' * (_TRANSLATE_MAX_SOURCE_CHARS + 1),
                    'lang': 'zh',
                },
            )
            return response.status_code, await response.get_json()

    status, body = asyncio.run(request_oversized_paper())
    assert status == 413
    assert body['ok'] is False
    assert str(_TRANSLATE_MAX_SOURCE_CHARS) in str(body['error'])


def test_terminal_poll_sends_one_artifact_and_keeps_caught_up_snapshot():
    from quart import Quart, g

    from lib.api_keys import local_admin_context
    import lib.paper.translate_runtime as runtime
    from routes.paper_pkg._qa_translate import api_v1_paper_bp

    app = Quart(__name__)

    @app.before_request
    def _bind_test_owner():
        g.auth_ctx = local_admin_context()

    app.register_blueprint(api_v1_paper_bp)
    task_id = 'paper-translation-terminal-wire'
    artifact = '译文' * 64
    runtime._new_translate_task(
        task_id,
        'paper-terminal-wire-hash',
        'zh',
        None,
        user_id=TEST_OWNER_USER_ID,
    )
    runtime._translate_runtime.mark_running(task_id)
    runtime._translate_runtime.update_fields(
        task_id, fields={'full_text': artifact})
    runtime._translate_runtime.finish(
        task_id, terminal_event_fields={'text': artifact})

    async def poll(cursor):
        async with app.test_client() as client:
            response = await client.get(
                f'/api/v1/paper/translate/poll?task_id={task_id}&cursor={cursor}')
            return response.status_code, await response.get_json()

    status, terminal_page = asyncio.run(poll(0))
    assert status == 200
    assert terminal_page['events'][0]['text'] == artifact
    assert 'text' not in terminal_page

    status, caught_up_page = asyncio.run(
        poll(terminal_page['next_cursor']))
    assert status == 200
    assert caught_up_page['events'] == []
    assert caught_up_page['text'] == artifact


def test_cancel_fenced_translation_is_not_rejoined(monkeypatch):
    from quart import Quart, g

    from lib.api_keys import local_admin_context
    import lib.paper.translate_runtime as runtime
    import routes.paper_pkg._qa_translate as routes

    app = Quart(__name__)

    @app.before_request
    def _bind_test_owner():
        g.auth_ctx = local_admin_context()

    app.register_blueprint(routes.api_v1_paper_bp)
    old_task_id = 'paper-translation-cancelled-carrier'
    old_task = runtime._new_translate_task(
        old_task_id,
        'paper-cancelled-carrier-hash',
        'zh',
        None,
        user_id=TEST_OWNER_USER_ID,
    )
    runtime._translate_runtime.mark_running(old_task_id)
    old_task['abort_event'].set()
    monkeypatch.setattr(
        routes.PaperArtifactRepository,
        'get_translation',
        lambda *_args, **_kwargs: None,
    )
    submitted = []
    monkeypatch.setattr(
        routes,
        'submit_translation_task',
        lambda _runtime, task_id, *_args, **_kwargs: submitted.append(task_id) or True,
    )

    async def requests():
        async with app.test_client() as client:
            lookup = await client.post(
                '/api/v1/paper/translate/lookup',
                json={
                    'paper_hash': 'paper-cancelled-carrier-hash',
                    'lang': 'zh',
                },
            )
            started = await client.post(
                '/api/v1/paper/translate/start',
                json={
                    'paper_text': 'fresh source',
                    'paper_hash': 'paper-cancelled-carrier-hash',
                    'lang': 'zh',
                },
            )
            return await lookup.get_json(), await started.get_json()

    lookup, started = asyncio.run(requests())
    assert lookup == {'ok': False}
    assert started['ok'] is True
    assert started['existed'] is False
    assert started['task_id'] != old_task_id
    assert submitted == [started['task_id']]
