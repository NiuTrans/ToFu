"""Integration tests for the native-async conversation handlers.

Stage-4 of the native-async migration converted ``get_conv`` and ``list_convs``
in routes/conversations.py from sync ``def`` (thread-pool) to ``async def`` that
uses the await-able DB facade (``async_fetchone`` / ``async_fetchall``). These
tests drive the REAL Quart app over HTTP (via the conftest ``flask_client`` sync
adapter) so we verify the converted handlers actually return JSON — not a leaked
coroutine object — and that the meta/prefetch branches still work.

Run:  pytest tests/test_conversations_async.py -m api
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.mark.api
class TestAsyncConversationHandlersAreCoroutines:
    def test_handlers_are_coroutine_views(self, flask_app):
        """The converted view functions must be coroutine functions, else
        Quart would run them in the thread pool and serialize the coroutine
        OBJECT as the response (the dual-mode-decorator trap)."""
        get_conv = flask_app.view_functions['api_v1_conversations.get_conv']
        list_convs = flask_app.view_functions['api_v1_conversations.list_convs']
        assert asyncio.iscoroutinefunction(get_conv)
        assert asyncio.iscoroutinefunction(list_convs)


@pytest.mark.api
class TestAsyncConversationCrud:
    @pytest.fixture()
    def a_conv(self, flask_client):
        now = int(time.time() * 1000)
        conv_id = f'async-conv-{now}'
        resp = flask_client.put(f'/api/v1/conversations/{conv_id}', json={
            'title': 'Async Handler Test',
            'messages': [
                {'role': 'user', 'content': 'hello async', 'timestamp': now},
                {'role': 'assistant', 'content': 'hi from async', 'timestamp': now + 1},
            ],
            'createdAt': now, 'updatedAt': now,
        })
        assert resp.status_code == 200, resp.data
        yield conv_id
        flask_client.delete(f'/api/v1/conversations/{conv_id}')

    def test_get_conv_returns_full_conversation(self, flask_client, a_conv):
        resp = flask_client.get(f'/api/v1/conversations/{a_conv}')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['id'] == a_conv
        assert data['title'] == 'Async Handler Test'
        assert len(data['messages']) == 2
        assert data['messages'][0]['content'] == 'hello async'

    def test_get_conv_404_for_missing(self, flask_client):
        resp = flask_client.get('/api/v1/conversations/does-not-exist-xyz')
        assert resp.status_code == 404

    def test_list_convs_default_includes_conv(self, flask_client, a_conv):
        resp = flask_client.get('/api/v1/conversations')
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, list)
        assert a_conv in [c['id'] for c in data]

    def test_list_convs_default_is_metadata_only(self, flask_client, a_conv):
        """The default list must NOT ship message BODIES (over-fetch fix) — it
        returns msgCount instead. A headless caller opts into bodies via
        ?full=1."""
        resp = flask_client.get('/api/v1/conversations')
        assert resp.status_code == 200
        row = next(c for c in resp.get_json() if c['id'] == a_conv)
        assert 'messages' not in row, (
            'default list leaked message bodies — should be metadata-only')
        assert row.get('msgCount') == 2, f'msgCount wrong: {row.get("msgCount")}'

    def test_list_convs_full_includes_bodies(self, flask_client, a_conv):
        """?full=1 restores the legacy shape WITH message bodies."""
        resp = flask_client.get('/api/v1/conversations?full=1')
        assert resp.status_code == 200
        row = next(c for c in resp.get_json() if c['id'] == a_conv)
        assert isinstance(row.get('messages'), list)
        assert len(row['messages']) == 2
        assert row['messages'][0]['content'] == 'hello async'

    def test_list_convs_meta_only(self, flask_client, a_conv):
        resp = flask_client.get('/api/v1/conversations?meta=1')
        assert resp.status_code == 200
        # meta payload is a JSON object/array served from the meta cache;
        # just assert it parses and the ETag header is present.
        assert resp.get_json() is not None
        assert 'ETag' in resp.headers

    def test_list_convs_meta_prefetch(self, flask_client, a_conv):
        resp = flask_client.get(f'/api/v1/conversations?meta=1&prefetch={a_conv}')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'conversations' in data
        assert 'prefetched' in data
        assert data['prefetched'] is not None
        assert data['prefetched']['id'] == a_conv
        assert len(data['prefetched']['messages']) == 2
