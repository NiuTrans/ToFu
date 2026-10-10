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
    def __init__(self, post_messages=None):
        self.include_messages: list[bool] = []
        self.include_post_messages: list[bool] = []
        self._post_messages = post_messages
        self.owners: list[int] = []

    def get_compaction_archive(
        self, conv_id, archive_id, *, user_id, include_messages=True,
        include_post_messages=False,
    ):
        self.include_messages.append(include_messages)
        self.owners.append(user_id)
        self.include_post_messages.append(include_post_messages)
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
        if include_post_messages:
            document['postMessages'] = self._post_messages
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
                '/api/v4/conversations/c/compactions/a?includeMessages=false'):
            g.auth_ctx = local_admin_context()
            status, body, _response = await _body(
                await route.get_compaction('c', 'a'))
        assert status == 200
        assert body['archive']['summary'] == 'small receipt'
        assert body['archive']['receipt']['strategy'] == 'selective_summary'
        assert 'messages' not in body
        assert store.include_messages == [False]
        assert store.include_post_messages == [False]

    asyncio.run(run())


def test_post_view_returns_post_messages_without_pre_transcript(monkeypatch):
    async def run():
        import routes.conversations_compaction as route
        from quart import g
        from lib.api_keys import local_admin_context

        store = _ArchiveStore(
            post_messages=[{'role': 'system', 'content': 'summary'}])
        monkeypatch.setattr(route, 'get_conversation_store', lambda: store)
        app = _make_app()
        async with app.test_request_context(
                '/api/v4/conversations/c/compactions/a?view=post'):
            g.auth_ctx = local_admin_context()
            status, body, _response = await _body(
                await route.get_compaction('c', 'a'))
        assert status == 200
        assert body['view'] == 'post'
        assert body['postMessages'] == [
            {'role': 'system', 'content': 'summary'}]
        assert body['postSnapshot'] == {
            'stage': 'compaction_result',
            'status': 'captured',
            'includesRuntimeRecomposition': False,
            'includesProviderAdaptation': False,
        }
        assert 'messages' not in body
        assert store.include_messages == [False]
        assert store.include_post_messages == [True]

    asyncio.run(run())


def test_post_view_marks_legacy_archive_with_null_post_messages(monkeypatch):
    async def run():
        import routes.conversations_compaction as route
        from quart import g
        from lib.api_keys import local_admin_context

        store = _ArchiveStore(post_messages=None)
        monkeypatch.setattr(route, 'get_conversation_store', lambda: store)
        app = _make_app()
        async with app.test_request_context(
                '/api/v4/conversations/c/compactions/a?view=post'):
            g.auth_ctx = local_admin_context()
            status, body, _response = await _body(
                await route.get_compaction('c', 'a'))
        assert status == 200
        assert body['view'] == 'post'
        assert body['postMessages'] is None
        assert body['postSnapshot']['status'] == 'unavailable'
        assert 'messages' not in body

    asyncio.run(run())


def test_invalid_view_is_rejected(monkeypatch):
    async def run():
        import routes.conversations_compaction as route
        from quart import g
        from lib.api_keys import local_admin_context

        store = _ArchiveStore()
        monkeypatch.setattr(route, 'get_conversation_store', lambda: store)
        app = _make_app()
        async with app.test_request_context(
                '/api/v4/conversations/c/compactions/a?view=sideways'):
            g.auth_ctx = local_admin_context()
            status, body, _response = await _body(
                await route.get_compaction('c', 'a'))
        assert status == 400
        assert 'invalid_view' in json.dumps(body)
        assert store.include_messages == []

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
                '/api/v4/conversations/c/compactions/a'):
            g.auth_ctx = local_admin_context()
            status, body, _response = await _body(
                await route.get_compaction('c', 'a'))
        assert status == 200
        assert body['messages'][0]['content'] == 'raw'
        assert store.include_messages == [True]
        assert store.include_post_messages == [False]

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
                '/api/v4/conversations/c/compactions/a?download=true'):
            g.auth_ctx = local_admin_context()
            status, body, response = await _body(
                await route.get_compaction('c', 'a'))
        assert status == 200
        assert body['messages'][0]['content'] == 'raw'
        assert 'ok' not in body
        assert response.headers['Content-Disposition'].startswith('attachment;')
        assert store.include_messages == [True]

    asyncio.run(run())


@pytest.mark.parametrize('messages', [None, [], [
    {'role': 'system', 'content': 'unchanged policy'},
    {'role': 'tool', 'tool_call_id': 'compact', 'content': '恢复摘要'},
]])
def test_post_download_matches_json_projection_without_loading_pre(monkeypatch, messages):
    async def run():
        import routes.conversations_compaction as route
        from quart import g
        from lib.api_keys import local_admin_context

        store = _ArchiveStore(post_messages=messages)
        monkeypatch.setattr(route, 'get_conversation_store', lambda: store)
        app = _make_app()
        results = []
        for suffix in ('', '&download=true'):
            async with app.test_request_context(
                    '/api/v4/conversations/c/compactions/a?view=post' + suffix):
                g.auth_ctx = local_admin_context()
                status, body, response = await _body(
                    await route.get_compaction('c', 'a'))
            assert status == 200
            results.append(body)
        read, downloaded = results
        assert downloaded == {key: value for key, value in read.items() if key != 'ok'}
        assert downloaded['postMessages'] == messages
        assert downloaded['postSnapshot']['status'] == (
            'unavailable' if messages is None else 'captured')
        assert 'messages' not in downloaded and 'ok' not in downloaded
        assert response.headers['Content-Disposition'].endswith('-post.json"')
        assert store.include_messages == [False, False]
        assert store.include_post_messages == [True, True]
        assert store.owners == [1, 1]

    asyncio.run(run())


def test_post_download_preserves_owner_scoped_not_found(monkeypatch):
    async def run():
        import routes.conversations_compaction as route
        from quart import g
        from lib.api_keys import local_admin_context

        seen = []

        class MissingArchiveStore:
            def get_compaction_archive(self, conv_id, archive_id, *, user_id,
                                       include_messages, include_post_messages):
                seen.append((conv_id, archive_id, user_id))
                return None

        monkeypatch.setattr(route, 'get_conversation_store', MissingArchiveStore)
        app = _make_app()
        async with app.test_request_context(
                '/api/v4/conversations/c/compactions/a?view=post&download=true'):
            g.auth_ctx = local_admin_context()
            status, body, response = await _body(await route.get_compaction('c', 'a'))
        assert status == 404
        assert 'postMessages' not in body
        assert 'Content-Disposition' not in response.headers
        assert seen == [('c', 'a', 1)]

    asyncio.run(run())
