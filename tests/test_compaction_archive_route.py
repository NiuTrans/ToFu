"""Summary-first and downloadable compaction archive route contracts."""

from __future__ import annotations

import asyncio
import json

import pytest

pytestmark = pytest.mark.unit


def _make_app():
    from quart import Quart
    if 'PROVIDE_AUTOMATIC_OPTIONS' not in Quart.default_config:
        Quart.default_config = {
            **Quart.default_config,
            'PROVIDE_AUTOMATIC_OPTIONS': True,
        }
    return Quart(__name__)


class _ArchiveStore:
    def __init__(self):
        self.include_messages: list[bool] = []

    def get_compaction_archive(
        self, conv_id, archive_id, *, user_id, include_messages=True,
    ):
        self.include_messages.append(include_messages)
        archive = {
            'id': archive_id,
            'convId': conv_id,
            'summary': 'small receipt',
            'messagesCount': 1,
            'payloadSize': 1024,
            'createdAt': 1_700_000_000_000,
            'tokenCountKind': 'estimated',
            'receipt': {
                'schemaVersion': 'tofu.compaction-receipt/v1',
                'status': 'completed',
                'strategy': 'selective_summary',
            },
        }
        document = {'archive': archive}
        if include_messages:
            document['messages'] = [{'role': 'user', 'content': 'raw'}]
        return document


async def _body(response_or_pair):
    if isinstance(response_or_pair, tuple):
        response, status = response_or_pair
    else:
        response, status = response_or_pair, response_or_pair.status_code
    raw = await response.get_data(as_text=True)
    return status, json.loads(raw), response


def test_summary_projection_does_not_request_messages(monkeypatch):
    async def run():
        import routes.conversations_compaction as route
        from quart import g
        from lib.api_keys import local_admin_context

        store = _ArchiveStore()
        monkeypatch.setattr(route, 'get_conversation_store', lambda: store)
        app = _make_app()
        async with app.test_request_context(
                '/api/v1/conversations/c/compactions/a?includeMessages=false'):
            g.auth_ctx = local_admin_context()
            status, body, _response = await _body(
                await route.get_compaction('c', 'a'))
        assert status == 200
        assert body['archive']['summary'] == 'small receipt'
        assert body['archive']['receipt']['strategy'] == 'selective_summary'
        assert 'messages' not in body
        assert store.include_messages == [False]

    asyncio.run(run())


def test_default_projection_remains_full_for_compatibility(monkeypatch):
    async def run():
        import routes.conversations_compaction as route
        from quart import g
        from lib.api_keys import local_admin_context

        store = _ArchiveStore()
        monkeypatch.setattr(route, 'get_conversation_store', lambda: store)
        app = _make_app()
        async with app.test_request_context(
                '/api/v1/conversations/c/compactions/a'):
            g.auth_ctx = local_admin_context()
            status, body, _response = await _body(
                await route.get_compaction('c', 'a'))
        assert status == 200
        assert body['messages'][0]['content'] == 'raw'
        assert store.include_messages == [True]

    asyncio.run(run())


def test_download_is_raw_attachment_without_api_envelope(monkeypatch):
    async def run():
        import routes.conversations_compaction as route
        from quart import g
        from lib.api_keys import local_admin_context

        store = _ArchiveStore()
        monkeypatch.setattr(route, 'get_conversation_store', lambda: store)
        app = _make_app()
        async with app.test_request_context(
                '/api/v1/conversations/c/compactions/a?download=true'):
            g.auth_ctx = local_admin_context()
            status, body, response = await _body(
                await route.get_compaction('c', 'a'))
        assert status == 200
        assert body['messages'][0]['content'] == 'raw'
        assert 'ok' not in body
        assert response.headers['Content-Disposition'].startswith('attachment;')
        assert store.include_messages == [True]

    asyncio.run(run())
