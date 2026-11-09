"""tests/test_user_steer_images.py — an image attached to a mid-turn human
steer is stored by its upload link and reaches the model as an image.

THE gap this pins (owner report 2026-11-08 + verified diagnosis)
---------------------------------------------------------------
The owner pasted a screenshot into a message sent while a Coordinator was
running. The message became a steer, and the Coordinator never saw the image.
Three seams each lost it:

  * PROJECTION (``steer_preview_images``) kept the composer's inline ``data:``
    preview and dropped the stored ``/api/images/<f>`` link.
  * NATIVE RECORD: TofuDB ``turn.steer.commit`` ignored ``images``. Only the
    SQL authority stored them, and only SQLite was tested.
  * MODEL DELIVERY was text-only: the round-boundary inbox carrier and a
    committed steer requeued as the next Turn both carried only the text.

The steer runs through the real command seam on both authorities
(``semantic_authority``: SQLite, and native TofuDB when ``TOFUDB_BIN`` names a
source-matched engine), for an executor in this process and in the other
server process.

Run with::

    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_user_steer_images.py -v
"""

from __future__ import annotations

import base64
import time
import unittest
import uuid

import pytest

from tests.support.semantic_authorities import semantic_authority as semantic_authority

pytestmark = pytest.mark.unit
pytest_plugins = ('tests.support.postgres_cluster',)

_LINK = '/api/images/1794133298800_6573ac8f.png'
# A real 1x1 PNG, so the inline fallback is a payload a model body accepts.
_PNG = ('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAF'
        'AAH/iZk9HQAAAABJRU5ErkJggg==')
_DATA_URL = f'data:image/png;base64,{_PNG}'
_STEER_TEXT = '有时候前端网页会突然这样。'


def _composer_image(**fields):
    """An image as the composer sends it after its upload (upload.js
    ``processImageFile``): inline payload, ``data:`` preview, stored link."""
    image = {'base64': _PNG, 'mediaType': 'image/png', 'preview': _DATA_URL,
             'sizeKB': 1, 'url': _LINK}
    image.update(fields)
    return image


def _failed_upload_image():
    """The same image when its upload failed: no stored link exists."""
    image = _composer_image()
    del image['url']
    return image


def _image_urls(content):
    return [block['image_url']['url'] for block in content
            if isinstance(block, dict) and block.get('type') == 'image_url']


# ═══════════════════ 1. The shared projection helper ═══════════════════

class SteerPreviewImagesTest(unittest.TestCase):

    def test_an_uploaded_image_is_stored_by_its_link_never_its_data_url(self):
        """The record keeps the durable upload reference. A reverse-proxy
        prefix on the client-stored URL is canonicalized away."""
        from lib.agent_inbox._steer_preview import steer_preview_images
        for image in (_composer_image(), _composer_image(url='/proxy/15000' + _LINK)):
            self.assertEqual(
                steer_preview_images({'_user_msg': {'images': [image]}}),
                [{'preview': _LINK, 'url': _LINK, 'sizeKB': 1}])

    def test_projects_bounded_display_dicts(self):
        from lib.agent_inbox._steer_preview import steer_preview_images
        item = {'_user_msg': {'role': 'user', 'content': 'see', 'images': [
            {'preview': '/api/images/a.png', 'caption': 'shot', 'sizeKB': 128},
            # Upload failed: the inline preview is the only copy of the image.
            {'preview': _DATA_URL, 'sizeKB': 4},
        ]}}
        out = steer_preview_images(item)
        self.assertEqual(out, [
            {'preview': '/api/images/a.png', 'url': '/api/images/a.png',
             'caption': 'shot', 'sizeKB': 128},
            {'preview': _DATA_URL, 'sizeKB': 4},
        ])

    def test_drops_entries_without_a_resolvable_preview(self):
        """No base64 payloads, no URL-less dicts: only display-ready previews
        are carried (the helper never smuggles binary into a sidecar)."""
        from lib.agent_inbox._steer_preview import steer_preview_images
        item = {'_user_msg': {'images': [
            {'base64': 'AAAA', 'mediaType': 'image/png'},   # no preview URL
            {'preview': '   '},                              # blank
            'not-a-dict',                                    # malformed
            {'preview': '/api/images/ok.png'},
        ]}}
        self.assertEqual(
            steer_preview_images(item),
            [{'preview': '/api/images/ok.png', 'url': '/api/images/ok.png'}])

    def test_no_images_returns_empty(self):
        from lib.agent_inbox._steer_preview import steer_preview_images
        self.assertEqual(steer_preview_images({'_user_msg': {'content': 'x'}}), [])
        self.assertEqual(steer_preview_images({'_user_msg': 'not-a-dict'}), [])
        self.assertEqual(steer_preview_images({}), [])

    def test_caps_the_image_count(self):
        from lib.agent_inbox._steer_preview import (
            MAX_PREVIEW_IMAGES, steer_preview_images,
        )
        item = {'_user_msg': {'images': [
            {'preview': f'/api/images/{i}.png'} for i in range(50)
        ]}}
        self.assertEqual(len(steer_preview_images(item)), MAX_PREVIEW_IMAGES)


# ═══════════════════ 2. Delivered flush carries images ═══════════════════

class SteerFlushImagesTest(unittest.TestCase):

    def _task(self, conv):
        return {'id': f'task-{conv}', 'convId': conv, 'status': 'running',
                '_userId': 1, 'aborted': False,
                'config': {'model': 'm'}, 'toolRounds': []}

    def test_delivered_preview_carries_projected_images(self):
        """The post-LLM flush (the card the user actually reads) attaches the
        projected images to each delivered steer preview AND emits them on the
        USER_STEER_INJECT chip."""
        from lib.agent_core.events import EventType
        import lib.tasks_pkg.orchestrator._deferred_inbox_flush as flush_mod
        conv = f'steer-flush-{uuid.uuid4().hex[:10]}'
        task = self._task(conv)
        task['_steer_inject_pending'] = [{
            'value': 'look at this screenshot',
            'blockId': 'injection:user-steer:cmd1',
            'injectionId': 'cmd1',
            '_user_msg': {'role': 'user', 'content': 'look at this screenshot',
                          'images': [_composer_image(caption='UI')]},
        }]
        projected = [{'preview': _LINK, 'url': _LINK, 'caption': 'UI', 'sizeKB': 1}]
        emitted = []
        orig_append = flush_mod.append_event
        flush_mod.append_event = lambda t, ev: emitted.append(ev)
        try:
            flush_mod.flush_deferred_peer_and_steer(task, round_num=2,
                                                    tid='steerimg')
        finally:
            flush_mod.append_event = orig_append
        # Sidecar lane holds the delivered record with images.
        lane = task.get('_userSteerInjects')
        self.assertTrue(lane, 'delivered steer record accumulated')
        previews = lane[0]['previews']
        self.assertEqual(previews[0]['text'], 'look at this screenshot')
        self.assertEqual(previews[0]['images'], projected)
        # And the emitted USER_STEER_INJECT chip carries them too.
        steer_events = [e for e in emitted
                        if isinstance(e, dict)
                        and e.get('type') == EventType.USER_STEER_INJECT]
        self.assertTrue(steer_events, 'chip emitted')
        self.assertEqual(steer_events[0]['previews'][0]['images'], projected)

    def test_text_only_steer_has_no_images_key(self):
        """A plain-text steer stays text-only — no spurious empty images key."""
        import lib.tasks_pkg.orchestrator._deferred_inbox_flush as flush_mod
        conv = f'steer-flush-text-{uuid.uuid4().hex[:10]}'
        task = self._task(conv)
        task['_steer_inject_pending'] = [{
            'value': 'just words', 'blockId': 'injection:user-steer:cmd2',
            '_user_msg': {'role': 'user', 'content': 'just words'},
        }]
        flush_mod.flush_deferred_peer_and_steer(task, round_num=0,
                                                tid='steerimg')
        previews = task['_userSteerInjects'][0]['previews']
        self.assertNotIn('images', previews[0])


# ═══════════════════ 3. Record and model, on both authorities ═══════════════════

def _service():
    from lib.conversation_sync.command_service import ConversationTurnCommandService

    def never(*args, **kwargs):
        raise AssertionError('input for a busy lane starts no executor')
    return ConversationTurnCommandService(
        build_user_message=never, was_aborted_after=lambda *a, **k: False,
        start_task=never)


@pytest.fixture(params=['this process', 'other process'])
def live_reply(request, semantic_authority, monkeypatch):
    """A running reply whose executor lives in this or the other server process.

    Yields the owner, the conversation and the executor's task view.
    """
    from lib import agent_inbox
    from lib.turn_lifecycle import bind_task, create_turn_pair
    from tests.support.chat_tasks import chat_task_registry, register_local_executor
    client, owner, _engine = semantic_authority
    monkeypatch.setattr('lib.storage.get_storage_client', client)
    conversation = f'steer-images-{uuid.uuid4().hex[:10]}'
    pair = create_turn_pair(conversation, command_id='human-request',
                            input_projection={'content': 'Check the frontend'},
                            config={}, user_id=owner,
                            conversation_defaults={'allowCreate': True})
    task_id = f'task-{conversation}'
    attempt_id, turn_id = pair['attempt']['attemptId'], pair['turn']['turnId']
    bind_task(attempt_id, task_id, user_id=owner)
    if request.param == 'this process':
        register_local_executor(task_id, conv_id=conversation, attempt_id=attempt_id,
                                turn_id=turn_id, user_id=owner)
    executor = {'id': task_id, 'convId': conversation, '_userId': owner,
                '_turnId': turn_id, '_attemptId': attempt_id, 'status': 'running',
                'aborted': False, 'config': {'model': 'gpt-4o'}, 'toolRounds': []}
    try:
        yield owner, conversation, executor
    finally:
        chat_task_registry.pop(task_id, None)
        agent_inbox.reset_for_test(conversation)


def _steer(owner, conversation, command_id, input_turn):
    result = _service().create_turn(conversation, owner, {
        'commandId': command_id, 'inputTurn': input_turn, 'config': {},
        'injectMode': 'steer'}, request_started_at=time.time()).value
    assert result.get('steered') is True and not result.get('steerFallback'), result
    return result


def _record(owner, conversation, executor, command_id):
    from lib.turn_lifecycle import get_turn
    projection = get_turn(conversation, executor['_turnId'], user_id=owner)['projection']
    [record] = [record for record in projection.get('_userSteerInjects') or []
                if record.get('commandId') == command_id]
    return record


def _round_boundary(executor):
    """The executor's next round boundary; returns the one coalesced carrier."""
    from lib.tasks_pkg.orchestrator._swarm_inbox import drain_and_inject_inbox
    messages = [{'role': 'user', 'content': 'Check the frontend'}]
    drain_and_inject_inbox(task=executor, messages=messages, round_num=1,
                           tid='steer-images')
    assert len(messages) == 2, messages
    return messages[-1]


@pytest.mark.parametrize('semantic_authority', ['sqlite', 'native'], indirect=True)
def test_a_steered_screenshot_is_stored_by_link_and_reaches_the_model(
        live_reply, tmp_path, monkeypatch):
    import lib.runtime.runtime_paths as runtime_paths
    from lib.llm.body import build_body
    owner, conversation, executor = live_reply
    _steer(owner, conversation, 'steer-shot',
           {'content': _STEER_TEXT, 'images': [_composer_image()]})

    record = _record(owner, conversation, executor, 'steer-shot')
    assert record['deliveryState'] == 'pending'
    assert record['previews'] == [{
        'text': _STEER_TEXT,
        'images': [{'preview': _LINK, 'url': _LINK, 'sizeKB': 1}],
    }]

    carrier = _round_boundary(executor)
    assert carrier['_containsHumanSteer'] is True
    content = carrier['content']
    assert isinstance(content, list), content
    # The model receives the image itself (resolved from the stored upload at
    # dispatch) plus the reference it can pass to inspect_image.
    assert _image_urls(content) == [_LINK]
    texts = [block['text'] for block in content if block.get('type') == 'text']
    assert any(f'[image ref: {_LINK}' in text for text in texts), texts
    assert texts[-1] == _STEER_TEXT
    assert _STEER_TEXT not in texts[:-1]

    # The provider request carries the uploaded bytes themselves.
    uploads = tmp_path / 'uploads'
    (uploads / 'images').mkdir(parents=True)
    (uploads / 'images' / _LINK.rsplit('/', 1)[1]).write_bytes(base64.b64decode(_PNG))
    monkeypatch.setattr(runtime_paths, 'uploads_root', lambda: str(uploads))
    body = build_body('gpt-4o', [carrier], image_user_id=owner)
    assert _image_urls(body['messages'][-1]['content']) == [_DATA_URL]


@pytest.mark.parametrize('semantic_authority', ['sqlite', 'native'], indirect=True)
def test_a_text_only_steer_is_unchanged(live_reply):
    owner, conversation, executor = live_reply
    _steer(owner, conversation, 'steer-text', {'content': 'just words'})
    assert _record(owner, conversation, executor, 'steer-text')['previews'] == [
        {'text': 'just words'}]
    carrier = _round_boundary(executor)
    assert carrier['content'] == 'just words'
    assert carrier['_containsHumanSteer'] is True


@pytest.mark.parametrize('semantic_authority', ['sqlite', 'native'], indirect=True)
def test_an_image_sent_without_words_waits_as_the_next_turn(live_reply):
    """Both authorities keep a steer by its text. A screenshot sent alone into
    a running reply degrades to the durable queue, images intact, instead of
    failing the send."""
    from lib.turn_lifecycle import get_turn
    owner, conversation, executor = live_reply
    result = _service().create_turn(conversation, owner, {
        'commandId': 'steer-image-only',
        'inputTurn': {'content': '', 'images': [_composer_image()]},
        'config': {}, 'injectMode': 'steer'}, request_started_at=time.time()).value
    assert result.get('queued') is True and result.get('steerFallback') is True, result
    queued = get_turn(conversation, result['submittedTurn']['turnId'], user_id=owner)
    assert [image['url'] for image in queued['projection']['images']] == [_LINK]
    assert not _record_ids(owner, conversation, executor)


def _record_ids(owner, conversation, executor):
    from lib.turn_lifecycle import get_turn
    projection = get_turn(conversation, executor['_turnId'], user_id=owner)['projection']
    return [record.get('commandId') for record in projection.get('_userSteerInjects') or []]


@pytest.mark.parametrize('semantic_authority', ['sqlite', 'native'], indirect=True)
@pytest.mark.parametrize('live_reply', ['other process'], indirect=True)
def test_a_committed_steer_requeued_as_the_next_turn_keeps_its_images(live_reply, monkeypatch):
    """A steer that missed its executor's last boundary becomes the next Turn;
    that Turn's message carries the images in the shape the message builder
    renders (a stored link, or the inline payload of a failed upload)."""
    from lib.scheduler import conversation_dispatch
    from lib.tasks_pkg.committed_steers import committed_steers, requeue_committed_steer
    owner, conversation, executor = live_reply
    _steer(owner, conversation, 'steer-late',
           {'content': _STEER_TEXT, 'images': [_composer_image(), _failed_upload_image()]})
    dispatched = []

    class Accepted:
        accepted, disposition, queue_id = True, 'queued', 'queue-late'

    def dispatch(conversation_id, message, config, **kwargs):
        dispatched.append(message)
        return Accepted()

    monkeypatch.setattr(conversation_dispatch, 'dispatch_scheduled_turn', dispatch)
    [item] = committed_steers(executor)
    assert requeue_committed_steer(executor, item)['deliveryState'] == 'queued'
    [message] = dispatched
    assert message['content'] == _STEER_TEXT
    assert message['images'] == [
        {'preview': _LINK, 'url': _LINK, 'sizeKB': 1},
        {'preview': _DATA_URL, 'sizeKB': 1, 'base64': _PNG, 'mediaType': 'image/png'},
    ]


def test_an_image_whose_upload_failed_reaches_the_model_inline():
    """Without a stored link the inline payload is the only copy; the model
    still receives it rather than the text alone."""
    from lib import agent_inbox
    from lib.tasks_pkg.orchestrator._swarm_inbox import drain_and_inject_inbox
    conversation = f'steer-inline-{uuid.uuid4().hex[:10]}'
    agent_inbox.enqueue(conversation, _STEER_TEXT, priority='next', mode='user-steer', extra={
        'injectionId': 'cmd-inline', 'blockId': 'injection:user-steer:cmd-inline',
        '_user_msg': {'role': 'user', 'content': _STEER_TEXT,
                      'images': [_failed_upload_image()]}})
    task = {'id': 'steer-inline', 'convId': conversation, '_userId': 1,
            'config': {}, 'aborted': False, 'toolRounds': []}
    messages = [{'role': 'user', 'content': 'first'}]
    try:
        drain_and_inject_inbox(task=task, messages=messages, round_num=1, tid='inline')
    finally:
        agent_inbox.reset_for_test(conversation)
    content = messages[-1]['content']
    assert isinstance(content, list), content
    assert _image_urls(content) == [_DATA_URL]
    assert content[-1] == {'type': 'text', 'text': _STEER_TEXT}


if __name__ == '__main__':
    from tests._standalone_guard import guard_standalone_storage
    guard_standalone_storage('test_user_steer_images.__main__')
    unittest.main()
