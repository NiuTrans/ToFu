"""tests/test_user_steer_images.py — a mid-turn human steer's image attachments
survive into BOTH injection records (never silently dropped).

THE gap this pins (owner report + verified diagnosis)
----------------------------------------------------
The composer carries a steer's images on the inbox item's ``_user_msg``, and
the model DOES receive them — but the injection chip / sidecar kept only the
text, so the operator's images silently vanished from the rendered
"你中途插入了 N 条消息" card. Two producers each dropped them:

  * PENDING record (``_turns_write._turn_steer_commit``) — persisted the moment
    the steer commits; rendered immediately as deliveryState:'pending'.
  * DELIVERED flush (``_deferred_inbox_flush``) — emitted after the LLM call
    confirms consumption; this is the card the user actually reads.

Both now project ``_user_msg.images`` through the shared
``steer_preview_images`` helper into bounded ``{preview, caption, sizeKB}``
dicts. These tests pin the projection at all three seams.

Run with::

    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_user_steer_images.py -v
"""

from __future__ import annotations

import unittest
import uuid

import pytest

pytestmark = pytest.mark.unit
pytest_plugins = ('tests._chat_sidecar',)

TEST_OWNER_USER_ID = 1


def _new_conversation_id(prefix='steer-img'):
    return f'{prefix}-{uuid.uuid4().hex[:10]}'


def _open_steer_window(conversation_id):
    """Create a live turn + first attempt and bind a running task so the
    turn.steer.commit window (live attempt, task-bound, current) is open."""
    from lib.turn_lifecycle import bind_task, create_turn_pair
    created = create_turn_pair(
        conversation_id,
        command_id=f'create-{conversation_id}',
        input_projection={'content': 'hello'},
        config={'model': 'gpt-4o'},
        user_id=TEST_OWNER_USER_ID,
        conversation_defaults={
            'allowCreate': True,
            'title': 'Steer images',
            'settings': {'model': 'gpt-4o'},
        },
    )
    attempt_id = created['attempt']['attemptId']
    turn_id = created['turn']['turnId']
    bind_task(attempt_id, f'task-{conversation_id}', user_id=TEST_OWNER_USER_ID)
    return attempt_id, turn_id


# ═══════════════════ 1. The shared projection helper ═══════════════════

class SteerPreviewImagesTest(unittest.TestCase):

    def test_projects_bounded_display_dicts(self):
        from lib.agent_inbox._steer_preview import steer_preview_images
        item = {'_user_msg': {'role': 'user', 'content': 'see', 'images': [
            {'preview': '/api/images/a.png', 'caption': 'shot', 'sizeKB': 128},
            {'preview': 'data:image/png;base64,iVBORw0KGgo=', 'sizeKB': 4},
        ]}}
        out = steer_preview_images(item)
        self.assertEqual(out, [
            {'preview': '/api/images/a.png', 'caption': 'shot', 'sizeKB': 128},
            {'preview': 'data:image/png;base64,iVBORw0KGgo=', 'sizeKB': 4},
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
            steer_preview_images(item), [{'preview': '/api/images/ok.png'}])

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
                '_userId': TEST_OWNER_USER_ID, 'aborted': False,
                'config': {'model': 'm'}, 'toolRounds': []}

    def test_delivered_preview_carries_projected_images(self):
        """The post-LLM flush (the card the user actually reads) attaches the
        projected images to each delivered steer preview AND emits them on the
        USER_STEER_INJECT chip."""
        from lib.agent_core.events import EventType
        import lib.tasks_pkg.orchestrator._deferred_inbox_flush as flush_mod
        conv = _new_conversation_id('steer-flush')
        task = self._task(conv)
        task['_steer_inject_pending'] = [{
            'value': 'look at this screenshot',
            'blockId': 'injection:user-steer:cmd1',
            'injectionId': 'cmd1',
            '_user_msg': {'role': 'user', 'content': 'look at this screenshot',
                          'images': [
                              {'preview': '/api/images/shot.png',
                               'caption': 'UI', 'sizeKB': 96},
                          ]},
        }]
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
        self.assertEqual(previews[0]['images'],
                         [{'preview': '/api/images/shot.png',
                           'caption': 'UI', 'sizeKB': 96}])
        # And the emitted USER_STEER_INJECT chip carries them too.
        steer_events = [e for e in emitted
                        if isinstance(e, dict)
                        and e.get('type') == EventType.USER_STEER_INJECT]
        self.assertTrue(steer_events, 'chip emitted')
        self.assertEqual(steer_events[0]['previews'][0]['images'],
                         [{'preview': '/api/images/shot.png',
                           'caption': 'UI', 'sizeKB': 96}])

    def test_text_only_steer_has_no_images_key(self):
        """A plain-text steer stays text-only — no spurious empty images key."""
        import lib.tasks_pkg.orchestrator._deferred_inbox_flush as flush_mod
        conv = _new_conversation_id('steer-flush-text')
        task = self._task(conv)
        task['_steer_inject_pending'] = [{
            'value': 'just words', 'blockId': 'injection:user-steer:cmd2',
            '_user_msg': {'role': 'user', 'content': 'just words'},
        }]
        flush_mod.flush_deferred_peer_and_steer(task, round_num=0,
                                                tid='steerimg')
        previews = task['_userSteerInjects'][0]['previews']
        self.assertNotIn('images', previews[0])


# ═══════════════════ 3. Pending durable record carries images ═══════════════════

@pytest.mark.usefixtures('chat_sidecar')
class SteerCommitImagesTest(unittest.TestCase):

    def test_pending_record_persists_images(self):
        """The moment-of-commit pending record (rendered immediately as
        deliveryState:'pending') also persists the images, so the chip shows
        thumbnails even before the model consumes the steer."""
        from lib.turn_lifecycle import commit_user_steer, get_turn
        conv = _new_conversation_id()
        attempt_id, turn_id = _open_steer_window(conv)
        images = [{'preview': '/api/images/p.png', 'caption': 'p',
                   'sizeKB': 12}]
        commit_user_steer(
            conv, attempt_id, command_id='cmd-img-1', text='pending words',
            user_id=TEST_OWNER_USER_ID, images=images,
        )
        projection = get_turn(conv, turn_id, user_id=TEST_OWNER_USER_ID)[
            'projection']
        records = projection.get('_userSteerInjects') or []
        pending = [r for r in records if r.get('deliveryState') == 'pending']
        self.assertTrue(pending, 'a pending steer record was persisted')
        self.assertEqual(pending[0]['previews'][0]['images'], images)

    def test_pending_record_without_images_omits_key(self):
        from lib.turn_lifecycle import commit_user_steer, get_turn
        conv = _new_conversation_id()
        attempt_id, turn_id = _open_steer_window(conv)
        commit_user_steer(
            conv, attempt_id, command_id='cmd-img-2', text='plain',
            user_id=TEST_OWNER_USER_ID,
        )
        projection = get_turn(conv, turn_id, user_id=TEST_OWNER_USER_ID)[
            'projection']
        records = projection.get('_userSteerInjects') or []
        self.assertTrue(records)
        self.assertNotIn('images', records[-1]['previews'][0])


if __name__ == '__main__':
    from tests._standalone_guard import guard_standalone_storage
    guard_standalone_storage('test_user_steer_images.__main__')
    unittest.main()
