"""Regression tests for the 37.6 MB sidebar ``?meta=1`` response.

Root cause: the Sidecar branch of ``list_convs`` shipped the FULL
``settings`` blob for every conversation.  Per-conversation settings can carry
``autopilotSummaries`` / ``autopilotObjective`` (tens to hundreds of KiB), so a
4,645-conversation account serialized ~15 MB of settings — and the route then
serialized that same list TWICE (``items`` + ``conversations``) into ~37.6 MB.

Pin:
  * ``_sidecar_conversation_meta`` projects settings to the sidebar whitelist
    (``_SIDEBAR_SETTINGS_KEYS``) and truncates the free-text title.
  * the ``?meta=1`` (non-prefetch) response is the normal ``{ok, items}``
    envelope — no duplicate ``conversations`` key.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from routes.conversations import _metadata  # noqa: E402


def _sidecar_conversation_meta(document):
    return _metadata(document, sidebar=True)


def _document(conv_id, *, title='T', settings=None, msg_count=2, rev=5):
    return {
        'metadata': {
            'id': conv_id,
            'title': title,
            'msg_count': msg_count,
            'created_at': 1000,
            'updated_at': 2000,
            'settings': settings or {},
            'rev': rev,
        },
        'messages': [],
    }


@pytest.mark.unit
class TestSidecarConversationMetaProjection:
    def test_settings_are_whitelisted_to_sidebar_keys(self):
        meta = _sidecar_conversation_meta(_document(
            'conv-1',
            settings={
                'folderId': 'fld-1',
                'lastMsgRole': 'assistant',
                'lastFinishReason': 'stop',
                'lastMsgError': False,
                'lastMsgHasOutput': True,
                'activeTaskId': 'task-9',
                # These must never reach the sidebar list:
                'autopilotSummaries': {'run-1': {'content': 'x' * 100000}},
                'autopilotObjective': 'y' * 50000,
                'model': 'deepseek-chat',
                'thinkingDepth': 'max',
                'projectSummary': 'z' * 20000,
                # The project mount DOES reach the sidebar (2026-08-21
                # incident: shells without it lost the bar on switch and
                # laundered '' into the settings column on v2 send):
                'projectPath': '/workspace/p',
                'projectPaths': ['/workspace/p'],
                'readOnlyPaths': [],
            },
        ))
        assert meta['id'] == 'conv-1'
        assert meta['settings'] == {
            'folderId': 'fld-1',
            'lastMsgRole': 'assistant',
            'lastFinishReason': 'stop',
            'lastMsgError': False,
            'lastMsgHasOutput': True,
            'activeTaskId': 'task-9',
            'projectPath': '/workspace/p',
            'projectPaths': ['/workspace/p'],
            'readOnlyPaths': [],
        }
        assert 'autopilotSummaries' not in meta['settings']
        assert 'autopilotObjective' not in meta['settings']
        assert 'model' not in meta['settings']

    def test_title_is_truncated(self):
        meta = _sidecar_conversation_meta(_document(
            'conv-1', title='x' * 500))
        assert len(meta['title']) == 200
        assert meta['title'] == 'x' * 200

    def test_missing_settings_still_yields_empty_dict(self):
        meta = _sidecar_conversation_meta(_document('conv-1', settings=None))
        assert meta['settings'] == {}
        assert meta['msgCount'] == 2
        assert meta['rev'] == 5


class _FakeSidecarClient:
    def __init__(self, documents):
        self.documents = documents
        self.list_calls = []
        self.count_calls = []

    def query(self, operation, payload=None):
        if operation == 'conversation.list':
            self.list_calls.append(payload)
            return self.documents
        if operation == 'conversation.count':
            self.count_calls.append(payload)
            return {'count': len(self.documents)}
        if operation == 'conversation.get':
            return None
        raise AssertionError(f'unexpected operation: {operation}')

    def list_metadata(self, **payload):
        self.list_calls.append(payload)
        return [dict(document['metadata']) for document in self.documents]


@pytest.mark.api
class TestSidecarMetaRouteShape:
    def test_meta_route_returns_single_items_envelope_with_projected_settings(
            self, flask_client, monkeypatch):
        fake = _FakeSidecarClient([
            _document('conv-1', title='One', settings={
                'folderId': 'fld-1', 'lastMsgRole': 'user',
                'autopilotSummaries': {'r': {'content': 'x' * 100000}},
                'model': 'm',
            }, msg_count=3, rev=11),
            _document('conv-2', title='Two', settings={'folderId': 'fld-1'}),
        ])
        monkeypatch.setattr(
            'routes.conversations.list_conversation_metadata',
            fake.list_metadata,
        )

        resp = flask_client.get('/api/v1/conversations?meta=1')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['ok'] is True
        assert isinstance(data['items'], list) and len(data['items']) == 2
        # The hot sidebar poll must NOT duplicate the list under a second key.
        assert 'conversations' not in data
        assert resp.headers.get('X-Total-Count') == '2'

        rows = {c['id']: c for c in data['items']}
        assert rows['conv-1']['settings'] == {
            'folderId': 'fld-1', 'lastMsgRole': 'user'}
        assert 'autopilotSummaries' not in rows['conv-1']['settings']
        assert 'model' not in rows['conv-1']['settings']
        assert rows['conv-1']['msgCount'] == 3
        assert rows['conv-1']['rev'] == 11

        # The route asked the sidecar to project settings before shipping.
        assert fake.list_calls
        assert 'settings_keys' in fake.list_calls[0]
        assert 'folderId' in fake.list_calls[0]['settings_keys']

    def test_meta_route_truncates_title(self, flask_client, monkeypatch):
        fake = _FakeSidecarClient([_document('conv-1', title='x' * 500)])
        monkeypatch.setattr(
            'routes.conversations.list_conversation_metadata',
            fake.list_metadata,
        )

        resp = flask_client.get('/api/v1/conversations?meta=1')
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data['items'][0]['title']) == 200

    def test_meta_route_honors_sidebar_cap_and_reports_total(
            self, flask_client, monkeypatch):
        fake = _FakeSidecarClient(
            [_document(f'conv-{i}', title=f'C{i}') for i in range(5)])
        monkeypatch.setattr(
            'routes.conversations.list_conversation_metadata',
            fake.list_metadata,
        )

        resp = flask_client.get('/api/v1/conversations?meta=1&limit=2')
        assert resp.status_code == 200
        data = resp.get_json()
        # The list is bounded to the sidebar cap; the authoritative total
        # still travels so the browser never mistakes a page for a deletion.
        assert len(data['items']) == 2
        assert resp.headers.get('X-Total-Count') == '5'
        assert not fake.count_calls, (
            'metadata listing already supplies the authoritative total; a '
            'second count query would add a race and an extra round trip')
