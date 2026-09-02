"""Peer roster labels come from the owner-scoped conversation authority."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

TEST_OWNER_USER_ID = 1
pytest_plugins = ('tests._chat_sidecar',)


@pytest.fixture(scope='module', autouse=True)
def _seed_titles(_chat_sidecar_runtime):
    from tests._seed import seed_conversation

    seed_conversation(
        'convtitled0001', title='Parser refactor work', messages=[])
    seed_conversation(
        'convuntitled02', title='Untitled',
        messages=[{
            'role': 'user',
            'content': 'Fix the SSE reconnect race in chat',
        }])


def _stub_snapshot(monkeypatch, peers):
    def snapshot(_path, *, user_id):
        assert user_id == TEST_OWNER_USER_ID
        return {'peers': peers}

    monkeypatch.setattr(
        'lib.presence.registry.snapshot', snapshot)


def test_titles_by_conv_resolves_stored_title():
    from lib.conversations.project_peer import _titles_by_conv
    out = _titles_by_conv(['convtitled0001'], user_id=TEST_OWNER_USER_ID)
    assert out.get('convtitled0001') == 'Parser refactor work'


def test_titles_by_conv_falls_back_to_opening_snippet():
    from lib.conversations.project_peer import _titles_by_conv
    out = _titles_by_conv(['convuntitled02'], user_id=TEST_OWNER_USER_ID)
    assert 'SSE reconnect race' in out.get('convuntitled02', '')


def test_titles_by_conv_unknown_absent():
    from lib.conversations.project_peer import _titles_by_conv
    out = _titles_by_conv(['nosuchconv0001'], user_id=TEST_OWNER_USER_ID)
    assert 'nosuchconv0001' not in out


def test_build_peer_status_backfills_titles(monkeypatch):
    _stub_snapshot(monkeypatch, [
        {'convId': 'convtitled0001', 'agentId': '', 'title': '',
         'statusLabel': 'generating'},
        {'convId': 'convuntitled02', 'agentId': '', 'title': '',
         'statusLabel': 'working'},
    ])
    from lib.conversations.project_peer import build_peer_status
    status = build_peer_status('/proj', 'someOtherConv', user_id=TEST_OWNER_USER_ID)
    by_id = {peer['convId']: peer for peer in status['peers']}
    assert by_id['convtitled0001']['title'] == 'Parser refactor work'
    assert 'SSE reconnect race' in by_id['convuntitled02']['title']


def test_build_peer_status_keeps_presence_title(monkeypatch):
    _stub_snapshot(monkeypatch, [
        {'convId': 'convtitled0001', 'agentId': '',
         'title': 'Live presence title', 'statusLabel': 'generating'},
    ])
    from lib.conversations.project_peer import build_peer_status
    status = build_peer_status('/proj', 'someOtherConv', user_id=TEST_OWNER_USER_ID)
    by_id = {peer['convId']: peer for peer in status['peers']}
    assert by_id['convtitled0001']['title'] == 'Live presence title'
