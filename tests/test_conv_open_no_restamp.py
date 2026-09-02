#!/usr/bin/env python3
"""Opening/browsing a conversation must NOT rewrite its recency.

Owner directive 2026-08-14: clicking a conversation in the sidebar is a READ.
The former behaviour (commit 43666f21) bumped ``updatedAt`` on open — frontend
``_bumpConvOnOpen`` re-sorted the sidebar and PATCHed the server's reserved
``touchUpdatedAt`` flag so ``UPDATE conversations SET updated_at=now`` fired on
every click, making finished conversations fly to the top of the list. Both
halves were removed.

These tests pin the invariant in both directions:

  • behavioral (api): PATCH /settings with the removed ``touchUpdatedAt`` flag
    (a stale bundle may still send it) must leave ``updated_at`` byte-identical
    AND must not smuggle the flag into the settings JSON.
  • source (unit): the frontend has no open-bump call on the conv-click path
    and never PATCHes the flag; the backend settings-PATCH body contains no
    ``updated_at`` UPDATE.
"""
from __future__ import annotations

import os
import sys

import pytest

from tests._seed import delete_conversation, seed_conversation

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_OLD_TS = 1_700_000_000_000  # deliberately ancient — any restamp would show
TEST_OWNER_USER_ID = 1


@pytest.mark.api
class TestSettingsPatchNeverRestampsRecency:
    @pytest.fixture()
    def old_conv(self, flask_client):
        conv_id = 'no-restamp-conv'
        seed_conversation(
            conv_id,
            user_id=TEST_OWNER_USER_ID,
            title='No Restamp',
            messages=[
                {'role': 'user', 'content': 'hi', 'timestamp': _OLD_TS},
                {'role': 'assistant', 'content': 'done', 'timestamp': _OLD_TS + 1},
            ],
            created_at=_OLD_TS,
            updated_at=_OLD_TS,
        )
        yield conv_id
        delete_conversation(conv_id, user_id=TEST_OWNER_USER_ID)

    def _updated_at(self, flask_client, conv_id):
        resp = flask_client.get(f'/api/v1/conversations/{conv_id}')
        assert resp.status_code == 200, resp.data
        body = resp.get_json()
        return int(body.get('updatedAt') or body.get('updated_at') or 0)

    def test_touch_flag_is_rejected_and_updated_at_unchanged(
        self, flask_client, old_conv,
    ):
        before = self._updated_at(flask_client, old_conv)
        assert before == _OLD_TS, f'precondition: seeded recency, got {before}'
        # A stale bundle still sending the removed control flag.
        resp = flask_client.patch(f'/api/v1/conversations/{old_conv}/settings',
                                  json={'touchUpdatedAt': True, 'model': 'keep-me'})
        assert resp.status_code == 400, resp.data
        after = self._updated_at(flask_client, old_conv)
        assert after == _OLD_TS, (
            f'browsing/PATCH restamped recency: updated_at {before} → {after}. '
            'Opening a conversation must never rewrite updated_at.')
        # The flag must not leak into the persisted settings JSON either.
        data = flask_client.get(f'/api/v1/conversations/{old_conv}').get_json()
        settings = data.get('settings') or {}
        assert 'model' not in settings, settings
        assert 'touchUpdatedAt' not in settings, (
            f'control flag leaked into settings JSON: {settings}')

    def test_plain_settings_patch_keeps_recency(self, flask_client, old_conv):
        """Ordinary settings-only PATCH (the legit callers: folder move, pin,
        activeTaskId) also never restamps — the invariant is not flag-scoped."""
        before = self._updated_at(flask_client, old_conv)
        resp = flask_client.patch(f'/api/v1/conversations/{old_conv}/settings',
                                  json={'pinned': True})
        assert resp.status_code == 200, resp.data
        assert self._updated_at(flask_client, old_conv) == before


@pytest.mark.unit
class TestOpenBumpRemovedAtSource:
    def _frontend_src(self):
        with open(os.path.join(REPO, 'frontend', 'src', 'runtime',
                               'app-runtime.js'), encoding='utf-8') as f:
            return f.read()

    def _routes_src(self):
        with open(os.path.join(REPO, 'routes', 'conversations.py'),
                  encoding='utf-8') as f:
            return f.read()

    def test_frontend_has_no_open_bump(self):
        src = self._frontend_src()
        assert 'function _bumpConvOnOpen' not in src, (
            '_bumpConvOnOpen resurrected — opening a conversation must not '
            'rewrite its updatedAt recency')
        assert "touchUpdatedAt: true" not in src, (
            'frontend still PATCHes the removed touchUpdatedAt flag')

    def test_conv_click_path_calls_load_only(self):
        src = self._frontend_src()
        start = src.index('function _handleConvClick')
        end = src.index('.addEventListener("click", _handleConvClick)')
        body = src[start:end]
        assert '_bumpConvOnOpen' not in body, (
            'conv-click path reintroduced a recency rewrite')
        assert 'loadConversation(item.dataset.convId)' in body, (
            'conv-click path no longer opens the conversation — wiring broken')

    def test_settings_patch_body_has_no_updated_at_write(self):
        src = self._routes_src()
        start = src.index('async def patch_conv_settings(')
        end = src.index('@conversations_bp.route', start + 1)
        body = src[start:end]
        assert 'SET updated_at' not in body, (
            'patch_conv_settings writes updated_at again — a settings-only '
            'PATCH must never restamp recency')
        # A stale bundle is rejected as one atomic settings mutation, so the
        # retired control flag and any sibling fields cannot leak into storage.
        assert 'if "touchUpdatedAt" in updates:' in body
        assert 'Browsing cannot mutate conversation recency' in body


if __name__ == '__main__':
    for name, fn in list(globals().items()):
        if name.startswith('test_') and callable(fn):
            fn()
            print(f'PASS {name}')
    print('ALL GREEN')
