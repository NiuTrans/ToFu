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

from tests._seed import delete_conversation, seed_conversation

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_OWNER_USER_ID = 1


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
        seed_conversation(
            conv_id,
            user_id=TEST_OWNER_USER_ID,
            title='Async Handler Test',
            messages=[
                {'role': 'user', 'content': 'hello async', 'timestamp': now},
                {'role': 'assistant', 'content': 'hi from async', 'timestamp': now + 1},
            ],
            created_at=now,
            updated_at=now,
        )
        yield conv_id
        delete_conversation(conv_id, user_id=TEST_OWNER_USER_ID)

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
        # charter#0 envelope (api-contract migration — the deliberate
        # contract; direction-aligned from the old bare-array pin)
        assert data.get('ok') is True and isinstance(data.get('items'), list), (
            f'list envelope drifted: {data!r}')
        assert a_conv in [c['id'] for c in data['items']]

    def test_list_convs_default_is_metadata_only(self, flask_client, a_conv):
        """The default list must NOT ship message BODIES (over-fetch fix) — it
        returns msgCount instead. A headless caller opts into bodies via
        ?full=1."""
        resp = flask_client.get('/api/v1/conversations')
        assert resp.status_code == 200
        row = next(c for c in resp.get_json()['items'] if c['id'] == a_conv)
        assert 'messages' not in row, (
            'default list leaked message bodies — should be metadata-only')
        assert row.get('msgCount') == 2, f'msgCount wrong: {row.get("msgCount")}'

    def test_list_convs_full_is_rejected(self, flask_client, a_conv):
        """Transcript bodies have one authority: the detail/snapshot APIs."""
        resp = flask_client.get('/api/v1/conversations?full=1')
        assert resp.status_code == 400
        assert 'removed' in resp.get_json()['error'].lower()

    def test_list_convs_meta_only(self, flask_client, a_conv):
        resp = flask_client.get('/api/v1/conversations?meta=1')
        assert resp.status_code == 200
        # The monotonic DB rev is part of the sidebar sync protocol. Without
        # it the browser falls back to skew-prone wall-clock comparisons.
        row = next(c for c in resp.get_json()['items'] if c['id'] == a_conv)
        assert isinstance(row.get('rev'), int)
        assert row['rev'] >= 0
        assert 'ETag' in resp.headers

    def test_list_convs_matching_etag_remains_not_modified(
            self, flask_client, a_conv):
        first = flask_client.get('/api/v1/conversations?meta=1')
        assert first.status_code == 200

        unchanged = flask_client.get(
            '/api/v1/conversations?meta=1',
            headers={'If-None-Match': first.headers['ETag']},
        )

        assert unchanged.status_code == 304
        assert unchanged.get_data() == b''

    def test_list_convs_meta_prefetch(self, flask_client, a_conv):
        resp = flask_client.get(f'/api/v1/conversations?meta=1&prefetch={a_conv}')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'items' in data
        assert 'prefetched' in data
        assert data['prefetched'] is not None
        assert data['prefetched']['id'] == a_conv
        assert len(data['prefetched']['messages']) == 2

    def test_list_convs_meta_prefetch_windowed(self, flask_client, a_conv):
        """Startup prefetch must not deserialize/ship the full transcript.

        No ``window`` above intentionally pins legacy compatibility. The
        first-party shape below asks for one tail row and must carry enough
        absolute-count/cursor metadata for the browser to page upward without
        ever mistaking the slice for the complete conversation.
        """
        resp = flask_client.get(
            f'/api/v1/conversations?meta=1&prefetch={a_conv}&window=1')
        assert resp.status_code == 200
        prefetched = resp.get_json()['prefetched']
        assert prefetched['windowed'] is True
        # Windowing omits older rows but does not content-trim the selected row.
        assert prefetched['trimmed'] is False
        assert prefetched['totalCount'] == 2
        assert prefetched['firstLoadedSeq'] == 1
        assert prefetched['lastLoadedSeq'] == 1
        assert prefetched['hasMore'] is True
        assert [m['content'] for m in prefetched['messages']] == ['hi from async']



@pytest.mark.api
class TestFolderScopedConversationQuery:
    """C1 — folder members are resolved by their real ``folderId`` (in the
    settings JSON), INDEPENDENT of the global most-recent-N sidebar window.

    A folder whose members ALL sort past the sidebar cap must still be returned
    in full by ``GET /api/v1/conversations?folderId=X``. We prove independence
    from the cap by SHRINKING ``TOFU_SIDEBAR_MAX`` to a tiny value (rather than
    creating thousands of rows): the folder members are deliberately made OLDER
    (smaller updated_at) than a batch of decoy convs, so under the shrunk cap
    the cached top-N sidebar list excludes every folder member — yet the
    folderId query still returns them all.
    """

    def _seed(self, conv_id, title, updated_at, *, folder_id=''):
        seed_conversation(
            conv_id,
            user_id=TEST_OWNER_USER_ID,
            title=title,
            messages=[{
                'role': 'user', 'content': 'x', 'timestamp': updated_at,
            }],
            settings={'folderId': folder_id} if folder_id else {},
            created_at=updated_at,
            updated_at=updated_at,
        )

    @pytest.fixture()
    def foldered_convs(self, flask_client):
        base = int(time.time() * 1000)
        folder_id = f'fld-{base}'
        star_folder_id = f'star-{base}'
        created = []
        # 3 members deliberately OLD (updated_at well below the decoys).
        members = [f'fmem-{base}-{i}' for i in range(3)]
        star_members = [f'star-mem-{base}-{i}' for i in range(2)]
        for i, cid in enumerate(members):
            self._seed(
                cid, f'member {i}', base - 100000 + i,
                folder_id=folder_id,
            )
            created.append(cid)
        for i, cid in enumerate(star_members):
            self._seed(
                cid, f'star member {i}', base - 90000 + i,
                folder_id=star_folder_id,
            )
            created.append(cid)
        # Decoys: NEWER, unfoldered — these would fill a shrunk sidebar window.
        decoys = [f'decoy-{base}-{i}' for i in range(6)]
        for i, cid in enumerate(decoys):
            self._seed(cid, f'decoy {i}', base + 1000 + i)
            created.append(cid)
        yield {'folder_id': folder_id, 'members': members,
               'star_folder_id': star_folder_id, 'star_members': star_members,
               'decoys': decoys}
        for cid in created:
            delete_conversation(cid, user_id=TEST_OWNER_USER_ID)

    def test_folderId_query_returns_all_members(self, flask_client, foldered_convs):
        resp = flask_client.get(
            f'/api/v1/conversations?folderId={foldered_convs["folder_id"]}')
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, dict) and 'items' in data
        ids = {c['id'] for c in data['items']}
        assert set(foldered_convs['members']) <= ids, (
            f'folderId query missed members: '
            f'{set(foldered_convs["members"]) - ids}')
        # Envelope carries the real member count so the frontend can tell a
        # genuinely empty folder from an unloaded one.
        assert data['page']['totalCount'] == len(foldered_convs['members'])
        # No decoys leaked into the folder result set.
        assert not (set(foldered_convs['decoys']) & ids)

    def test_star_folder_members_also_returned(self, flask_client, foldered_convs):
        """The auto-migrated '⭐ 置顶' folder benefits identically — its members
        may also sort past the cap, and must be returned in full."""
        resp = flask_client.get(
            f'/api/v1/conversations?folderId={foldered_convs["star_folder_id"]}')
        assert resp.status_code == 200
        ids = {c['id'] for c in resp.get_json()['items']}
        assert set(foldered_convs['star_members']) <= ids

    def test_folderId_query_is_metadata_only(self, flask_client, foldered_convs):
        """Folder query rows are metadata-only (no message bodies), same shape
        as the sidebar rows so the frontend merges them via the existing
        shell-construction path."""
        resp = flask_client.get(
            f'/api/v1/conversations?folderId={foldered_convs["folder_id"]}')
        rows = resp.get_json()['items']
        assert rows, 'expected member rows'
        for r in rows:
            assert 'messages' not in r
            assert 'msgCount' in r

    def test_empty_folder_returns_zero_count(self, flask_client):
        """A folder with no members returns an empty list + totalCount 0 — the
        signal the frontend uses to render a genuine empty state (not 'members
        not loaded')."""
        resp = flask_client.get('/api/v1/conversations?folderId=no-such-folder-xyz')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['items'] == []
        assert data['page']['totalCount'] == 0


@pytest.mark.api
class TestConversationListKeysetPagination:
    """C3 — the global list is paginated via a keyset cursor so conversations
    past the first window remain reachable instead of being silently dropped."""

    @pytest.fixture()
    def paging_convs(self, flask_client):
        base = int(time.time() * 1000)
        ids = [f'page-{base}-{i}' for i in range(5)]
        for i, cid in enumerate(ids):
            seed_conversation(
                cid,
                user_id=TEST_OWNER_USER_ID,
                title=f'page {i}',
                messages=[{
                    'role': 'user', 'content': 'x', 'timestamp': base + i,
                }],
                created_at=base + i,
                updated_at=base + i,
            )
        yield ids
        for cid in ids:
            delete_conversation(cid, user_id=TEST_OWNER_USER_ID)

    def test_before_cursor_pages_older_rows(self, flask_client, paging_convs):
        # Page 1: newest 2.
        r1 = flask_client.get('/api/v1/conversations?limit=2&before=99999999999999')
        assert r1.status_code == 200
        d1 = r1.get_json()
        assert isinstance(d1, dict) and 'items' in d1
        assert len(d1['items']) == 2
        assert d1['page']['hasMore'] is True
        assert 'nextBefore' in d1['page'] and 'nextBeforeId' in d1['page']
        # Page 2: strictly older than page 1's last row — no overlap.
        page1_ids = {c['id'] for c in d1['items']}
        r2 = flask_client.get(
            f'/api/v1/conversations?limit=2&before={d1["page"]["nextBefore"]}'
            f'&before_id={d1["page"]["nextBeforeId"]}')
        d2 = r2.get_json()
        page2_ids = {c['id'] for c in d2['items']}
        assert not (page1_ids & page2_ids), 'keyset pages overlapped'
