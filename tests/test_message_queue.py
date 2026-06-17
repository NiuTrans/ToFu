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

from lib.message_queue import enqueue_message


def _qid():
    """Unique test conv ID to avoid cross-test pollution."""
    return f'test-queue-{time.time_ns()}'


def _seed(conv_id, text, timestamp):
    """Enqueue one message via the library (the deleted POST's replacement)."""
    return enqueue_message(conv_id, {'text': text, 'timestamp': timestamp},
                           {'model': 'test-model'})


class TestMessageQueueAPI:
    """Test the surviving queue HTTP endpoints."""

    def test_get_queue(self, flask_client):
        """GET /api/v1/chat/queue/<convId> returns queued messages in order."""
        conv_id = _qid()
        _seed(conv_id, 'First', 1000)
        _seed(conv_id, 'Second', 2000)

        resp = flask_client.get(f'/api/v1/chat/queue/{conv_id}')
        assert resp.status_code == 200
        queue = resp.get_json()
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

        queue = flask_client.get(f'/api/v1/chat/queue/{conv_id}').get_json()
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

        queue = flask_client.get(f'/api/v1/chat/queue/{conv_id}').get_json()
        assert len(queue) == 0

    def test_get_empty_queue(self, flask_client):
        """GET for an unknown conv returns an empty list."""
        resp = flask_client.get('/api/v1/chat/queue/nonexistent')
        assert resp.status_code == 200
        assert resp.get_json() == []
