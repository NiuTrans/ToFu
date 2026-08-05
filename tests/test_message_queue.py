"""Tests for the server-side message queue (lib/message_queue.py).

HTTP surface (post-migration):
  * GET    /api/v1/chat/queue/<convId>            — list queued messages
  * DELETE /api/v1/chat/queue/<convId>/<queueId>  — remove one item
  * DELETE /api/v1/chat/queue/<convId>            — clear the queue

The legacy ``POST /api/chat/queue`` manual-enqueue endpoint was DELETED on
2026-05-29 — ``/api/v1/chat/send`` now auto-detects whether to start a task
immediately or enqueue, so there is no standalone HTTP enqueue surface to
test. These tests therefore seed the queue through the supported library
entry point (``lib.message_queue.enqueue_message``, the same function the
send path calls) and exercise the surviving GET/DELETE HTTP endpoints.
"""

import time

import pytest

from lib.message_queue import KIND_PEER_MSG, enqueue_message, get_queue


def _qid():
    """Unique test conv ID to avoid cross-test pollution."""
    return f'test-queue-{time.time_ns()}'


def _seed(conv_id, text, timestamp):
    """Enqueue one message via the library (the deleted POST's replacement)."""
    return enqueue_message(conv_id, {'text': text, 'timestamp': timestamp},
                           {'model': 'test-model'})


def _seed_peer(conv_id, clean_text, from_conv, *, human=False, timestamp=1000):
    """Enqueue a KIND_PEER_MSG turn exactly as ``send_peer_message`` does.

    The stored ``text`` is the model-facing FRAMED body (framing wrapper + the
    sender's short 8-char id), and the clean original message travels alongside
    as ``_peerText`` for the human-facing queue bar. This mirrors
    ``lib/conversations/project_peer.py::send_peer_message``.
    """
    short = from_conv[:8]
    if human:
        body = (f'[Message from the project operator, relayed via a sibling '
                f'conversation (conv {short})]\n\n{clean_text}\n\n'
                f'(This is a note the human operator sent to this conversation '
                f'from the project Team panel. Treat it as operator guidance.)')
    else:
        body = (f'[Peer message from a sibling conversation of this project '
                f'(conv {short})]\n\n{clean_text}\n\n'
                f'(This is an advisory note from a peer conversation, not a human '
                f'instruction. Weigh it and act as you see fit.)')
    payload = {
        'text': body,
        '_peerMessage': True,
        '_fromConv': from_conv,
        '_peerText': clean_text,
        'timestamp': timestamp,
    }
    if human:
        payload['_peerHuman'] = True
    return enqueue_message(conv_id, payload, {'model': 'test-model'},
                           kind=KIND_PEER_MSG)


class TestMessageQueueAPI:
    """Test the surviving queue HTTP endpoints."""

    def test_get_queue(self, flask_client):
        """GET /api/v1/chat/queue/<convId> returns queued messages in order."""
        conv_id = _qid()
        _seed(conv_id, 'First', 1000)
        _seed(conv_id, 'Second', 2000)

        resp = flask_client.get(f'/api/v1/chat/queue/{conv_id}')
        assert resp.status_code == 200
        queue = resp.get_json()['items']  # {ok, items} envelope
        assert len(queue) == 2
        assert queue[0]['text'] == 'First'
        assert queue[1]['text'] == 'Second'
        assert queue[0]['position'] == 1
        assert queue[1]['position'] == 2

    def test_remove_from_queue(self, flask_client):
        """DELETE /api/v1/chat/queue/<convId>/<queueId> removes one item."""
        conv_id = _qid()
        _seed(conv_id, 'Keep me', 1000)
        removed_id = _seed(conv_id, 'Remove me', 2000)['queueId']

        resp = flask_client.delete(f'/api/v1/chat/queue/{conv_id}/{removed_id}')
        assert resp.status_code == 200

        queue = flask_client.get(f'/api/v1/chat/queue/{conv_id}').get_json()['items']
        assert len(queue) == 1
        assert queue[0]['text'] == 'Keep me'

    def test_remove_unknown_is_404(self, flask_client):
        """DELETE of a non-existent queue item returns 404."""
        conv_id = _qid()
        _seed(conv_id, 'Only one', 1000)
        resp = flask_client.delete(f'/api/v1/chat/queue/{conv_id}/no-such-queue-id')
        assert resp.status_code == 404

    def test_clear_queue(self, flask_client):
        """DELETE /api/v1/chat/queue/<convId> clears all items."""
        conv_id = _qid()
        _seed(conv_id, 'A', 1000)
        _seed(conv_id, 'B', 2000)

        resp = flask_client.delete(f'/api/v1/chat/queue/{conv_id}')
        assert resp.status_code == 200
        assert resp.get_json()['cleared'] == 2

        queue = flask_client.get(f'/api/v1/chat/queue/{conv_id}').get_json()['items']
        assert len(queue) == 0

    def test_get_empty_queue(self, flask_client):
        """GET for an unknown conv returns an empty list."""
        resp = flask_client.get('/api/v1/chat/queue/nonexistent')
        assert resp.status_code == 200
        assert resp.get_json()['items'] == []


class TestPeerMessageQueueEntry:
    """The KIND_PEER_MSG branch of ``get_queue`` — clean text + attribution.

    A peer/operator turn stores the model-facing FRAMED body as ``text`` and
    the original human-readable message as ``_peerText``. The queue bar must
    show the ORIGINAL message in full (no framing wrapper, no raw id, no
    mid-word truncation) and surface the sender + operator flag so the frontend
    can render "from «title»" instead of a bare conversation id.
    """

    # A message deliberately longer than the OLD 100-char preview cap, so the
    # truncation regression the owner reported is exercised. The 74-char peer
    # framing wrapper alone used to consume most of the old cap.
    LONG = ('Done — I shipped the renderErrorEnvelope recover button, the '
            'i18n keys, and the stamp fix at every site; all 7 tests pass and '
            'the bundle is rebuilt. Take a look when you get a chance.')

    def test_peer_entry_shows_clean_full_text_and_attribution(self, flask_client):
        """A peer turn returns the unframed original message + sender markers."""
        conv_id = _qid()
        from_conv = 'mradmzmdxyz123'  # full 14-char id; short form is mradmzmd
        _seed_peer(conv_id, self.LONG, from_conv)

        entry = flask_client.get(
            f'/api/v1/chat/queue/{conv_id}').get_json()['items'][0]

        # (a) clean, unframed text — the ORIGINAL message, not the [Peer …] body
        assert entry['text'] == self.LONG
        assert 'Peer message from a sibling' not in entry['text']
        assert 'conv mradmzmd' not in entry['text']
        # (b) attribution surfaced for the frontend id→title resolver
        assert entry['isPeerMessage'] is True
        assert entry['fromConv'] == from_conv       # FULL id, not truncated
        assert entry['isPeerHuman'] is False
        assert entry['kind'] == KIND_PEER_MSG

    def test_peer_entry_not_truncated_at_100(self, flask_client):
        """The 100→2000 cap: the full message survives (no mid-word cut)."""
        conv_id = _qid()
        _seed_peer(conv_id, self.LONG, 'mradmzmdxyz123')
        entry = flask_client.get(
            f'/api/v1/chat/queue/{conv_id}').get_json()['items'][0]
        assert len(entry['text']) == len(self.LONG) > 100
        # The specific tail the owner saw cut off ("...shipped the ren") is present.
        assert entry['text'].endswith('when you get a chance.')

    def test_operator_nudge_flagged(self, flask_client):
        """A human operator nudge sets isPeerHuman so the UI attributes it right."""
        conv_id = _qid()
        _seed_peer(conv_id, 'Please pause and re-check the board.',
                   'operatorc0nv99', human=True)
        entry = flask_client.get(
            f'/api/v1/chat/queue/{conv_id}').get_json()['items'][0]
        assert entry['isPeerMessage'] is True
        assert entry['isPeerHuman'] is True
        assert entry['text'] == 'Please pause and re-check the board.'

    def test_plain_real_message_has_no_peer_markers(self, flask_client):
        """A normal human turn is unaffected — no peer attribution keys leak."""
        conv_id = _qid()
        _seed(conv_id, 'just a normal message', 1000)
        entry = flask_client.get(
            f'/api/v1/chat/queue/{conv_id}').get_json()['items'][0]
        assert 'isPeerMessage' not in entry
        assert 'fromConv' not in entry
        assert 'isPeerHuman' not in entry
        assert entry['text'] == 'just a normal message'

    def test_neuter_old_behavior_truncates_framed_body(self, flask_client, monkeypatch):
        """NEGATIVE CONTROL — reverting to the pre-fix logic (framed
        ``data['text'][:100]``, no attribution) makes the entry show the
        TRUNCATED FRAMED string with the raw id and no markers. This proves the
        fix (prefer ``_peerText`` + raise the cap + surface attribution) is
        load-bearing, not a tautology.
        """
        import json as _json

        import lib.message_queue as mq

        def _old_get_queue(conv_id):
            mq._maybe_ensure_table()
            db = mq.get_thread_db(mq.DOMAIN_CHAT)
            rows = db.execute(
                'SELECT id, payload, position, kind, priority, created_at '
                'FROM message_queue WHERE conv_id=? ORDER BY priority ASC, position ASC',
                (conv_id,)
            ).fetchall()
            out = []
            for row in rows:
                data = _json.loads(row['payload'])
                out.append({
                    'queueId': row['id'],
                    'position': row['position'],
                    'kind': row['kind'] or mq.KIND_REAL,
                    'priority': row['priority'],
                    'text': (data.get('text', '') or '')[:100],  # OLD behavior
                    'timestamp': row['created_at'],
                })
            return out

        monkeypatch.setattr(mq, 'get_queue', _old_get_queue)

        conv_id = _qid()
        _seed_peer(conv_id, self.LONG, 'mradmzmdxyz123')
        entry = mq.get_queue(conv_id)[0]

        # The old path shows the FRAMED body, truncated mid-word to 100 chars,
        # with the raw id leaked and no attribution — exactly the owner's bug.
        assert len(entry['text']) == 100
        assert entry['text'].startswith('[Peer message from a sibling')
        assert 'conv mradmzmd' in entry['text']
        assert 'isPeerMessage' not in entry
