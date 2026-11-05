"""Running-turn translation previews can be re-read by their owner.

Incremental previews are push-only while their Turn runs: settled-Turn
authority refuses translation overlays until settlement, and each block's
final preview is published once. A browser that reloads, reconnects or was
showing another conversation re-reads the completed task-local cache through
this owner-scoped read. The read never touches Turn state.
"""

from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def running_tasks(monkeypatch):
    """Register executor tasks in the real chat registry; discard them after."""
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    from lib.translate import incremental

    created = []

    def register(task_id, *, user_id=9, conversation='conv-a', running=True):
        task = chat_task_runtime.create(user_id=user_id, task_id=task_id)
        task.update(convId=conversation, _turnId=f'turn-{task_id}',
                    config={'autoTranslate': True, 'uiLang': 'zh'})
        if running:
            chat_task_runtime.mark_running(task_id)
        created.append(task)
        return task

    yield register
    for task in created:
        incremental.cancel_incremental(task)
        chat_task_runtime.discard(task['id'])


def _translate_blocks(monkeypatch, task, blocks):
    from lib.translate import incremental

    accumulator = incremental._Accumulator(task)
    monkeypatch.setattr(accumulator, '_push', lambda _payload, **_options: None)
    monkeypatch.setattr(
        accumulator, '_translate',
        lambda text, **_kwargs: (f'译:{text}', 'translator'))
    for key, text in blocks.items():
        accumulator._process_preview_segment(key, text)
    return accumulator


def test_owner_reads_completed_previews_of_running_turns(
        monkeypatch, running_tasks):
    from lib.translate import incremental

    task = running_tasks('rehydrate-own')
    _translate_blocks(monkeypatch, task, {
        'text:llm-1': 'Checking the logs',
        'thinking:llm-1': 'The worker retired early',
    })

    assert incremental.preview_translations_for_conversation(
        'conv-a', user_id=9) == [{
            'turnId': 'turn-rehydrate-own',
            'partialByRound': {
                'text:llm-1': '译:Checking the logs',
                'thinking:llm-1': '译:The worker retired early',
            },
        }]
    assert incremental.preview_translations_for_conversation(
        'conv-a', user_id=10) == []
    assert incremental.preview_translations_for_conversation(
        'conv-b', user_id=9) == []


def test_only_live_uncancelled_turns_with_previews_are_returned(
        monkeypatch, running_tasks):
    from lib.translate import incremental

    pending = running_tasks('rehydrate-pending', running=False)
    aborted = running_tasks('rehydrate-aborted')
    aborted['aborted'] = True
    cancelled = running_tasks('rehydrate-cancelled')
    running_tasks('rehydrate-empty')
    foreign = running_tasks('rehydrate-foreign', user_id=10)
    for task in (pending, aborted, cancelled, foreign):
        _translate_blocks(monkeypatch, task, {'text:llm-1': 'Checking'})
    incremental.cancel_incremental(cancelled)

    assert incremental.preview_translations_for_conversation(
        'conv-a', user_id=9) == [{
            'turnId': 'turn-rehydrate-pending',
            'partialByRound': {'text:llm-1': '译:Checking'},
        }]


def test_settled_task_previews_are_not_rehydrated(monkeypatch, running_tasks):
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    from lib.translate import incremental

    task = running_tasks('rehydrate-settled')
    _translate_blocks(monkeypatch, task, {'text:llm-1': 'Checking'})
    chat_task_runtime.finish(task['id'], result='done')

    assert incremental.preview_translations_for_conversation(
        'conv-a', user_id=9) == []


def test_preview_route_returns_the_callers_running_previews(
        monkeypatch, running_tasks):
    from quart import Quart, g

    from lib.api_keys import local_admin_context
    from lib.identity import PERSONAL_USER_ID
    from routes.api_v1 import translate as translate_route

    task = running_tasks('rehydrate-route', user_id=PERSONAL_USER_ID)
    _translate_blocks(monkeypatch, task, {'text:llm-2': 'Rebuilding'})
    app = Quart(__name__)

    @app.before_request
    def _bind_test_owner():
        g.auth_ctx = local_admin_context()

    app.register_blueprint(translate_route.api_v1_translate_bp)

    async def read(conversation):
        async with app.test_client() as client:
            response = await client.get(
                f'/api/v1/translate/previews/{conversation}')
            return response.status_code, await response.get_json()

    status, body = asyncio.run(read('conv-a'))
    assert status == 200
    assert body['ok'] is True
    assert body['previews'] == [{
        'turnId': 'turn-rehydrate-route',
        'partialByRound': {'text:llm-2': '译:Rebuilding'},
    }]
    status, body = asyncio.run(read('conv-unknown'))
    assert status == 200
    assert body['previews'] == []
