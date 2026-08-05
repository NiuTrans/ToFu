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


@pytest.mark.unit
class TestOnOpenNarrationBackfillWiring:
    """Guard against silently dropping the on-open narration backfill.

    The forward drainer (``_maybe_backfill_narration_on_open`` →
    ``backfill_conv_narration_segments``) is what backfills the interleaved tool
    narration for turns whose deliverable was translated before their segments
    were stamped. A -X-ours merge repair (commit 8a374f9) once deleted the
    helper + all its ``get_conv`` call sites as collateral, silently reverting
    the fix so the historical backlog could never drain. This source-inspection
    test asserts the helper exists AND that every ``get_conv`` return path is
    wired to it, so a future merge/refactor that drops the wiring fails loudly.
    """

    def _src(self):
        import os
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            'routes', 'conversations.py')
        with open(path, encoding='utf-8') as f:
            return f.read()

    def test_helper_defined(self):
        assert 'def _maybe_backfill_narration_on_open(' in self._src()

    def test_every_get_conv_return_path_is_wired(self):
        src = self._src()
        get_conv_start = src.index('async def get_conv(')
        next_route = src.index('@conversations_bp.route', get_conv_start + 1)
        body = src[get_conv_start:next_route]
        # get_conv has 4 successful-serve return paths (windowed, live-task,
        # reconcile-success, reconcile-failure); each MUST spawn the backfill.
        call_sites = body.count('_maybe_backfill_narration_on_open(conv_id')
        assert call_sites >= 4, (
            f'get_conv wires the on-open narration backfill on only {call_sites} '
            'return path(s); expected >=4 (windowed / live-task / reconcile-ok / '
            'reconcile-fail). A merge or refactor likely dropped a call site.')

    def test_drainer_symbols_still_exported(self):
        from lib.translate.segment_backfill import (
            backfill_conv_narration_segments, conv_has_backfill_candidates)
        assert callable(backfill_conv_narration_segments)
        assert callable(conv_has_backfill_candidates)


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

    def test_list_convs_full_includes_bodies(self, flask_client, a_conv):
        """?full=1 restores the legacy shape WITH message bodies."""
        resp = flask_client.get('/api/v1/conversations?full=1')
        assert resp.status_code == 200
        row = next(c for c in resp.get_json()['items'] if c['id'] == a_conv)
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

    def _put(self, client, conv_id, title, updated_at):
        resp = client.put(f'/api/v1/conversations/{conv_id}', json={
            'title': title,
            'messages': [{'role': 'user', 'content': 'x', 'timestamp': updated_at}],
            'createdAt': updated_at, 'updatedAt': updated_at,
        })
        assert resp.status_code == 200, resp.data

    def _assign_folder(self, client, conv_id, folder_id):
        resp = client.patch(f'/api/v1/conversations/{conv_id}/settings',
                            json={'folderId': folder_id})
        assert resp.status_code == 200, resp.data

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
            self._put(flask_client, cid, f'member {i}', base - 100000 + i)
            self._assign_folder(flask_client, cid, folder_id)
            created.append(cid)
        for i, cid in enumerate(star_members):
            self._put(flask_client, cid, f'star member {i}', base - 90000 + i)
            self._assign_folder(flask_client, cid, star_folder_id)
            created.append(cid)
        # Decoys: NEWER, unfoldered — these would fill a shrunk sidebar window.
        decoys = [f'decoy-{base}-{i}' for i in range(6)]
        for i, cid in enumerate(decoys):
            self._put(flask_client, cid, f'decoy {i}', base + 1000 + i)
            created.append(cid)
        yield {'folder_id': folder_id, 'members': members,
               'star_folder_id': star_folder_id, 'star_members': star_members,
               'decoys': decoys}
        for cid in created:
            flask_client.delete(f'/api/v1/conversations/{cid}')

    def test_folderId_query_returns_all_members(self, flask_client, foldered_convs):
        resp = flask_client.get(
            f'/api/v1/conversations?folderId={foldered_convs["folder_id"]}')
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, dict) and 'conversations' in data
        ids = {c['id'] for c in data['conversations']}
        assert set(foldered_convs['members']) <= ids, (
            f'folderId query missed members: '
            f'{set(foldered_convs["members"]) - ids}')
        # Envelope carries the real member count so the frontend can tell a
        # genuinely empty folder from an unloaded one.
        assert data['totalCount'] == len(foldered_convs['members'])
        # No decoys leaked into the folder result set.
        assert not (set(foldered_convs['decoys']) & ids)

    def test_folderId_query_independent_of_sidebar_cap(self, flask_client,
                                                       foldered_convs, monkeypatch):
        """The load-bearing C5 guard: shrink TOFU_SIDEBAR_MAX so the cached
        sidebar list drops every (older) folder member, and prove the folderId
        query STILL returns them all — recovery does NOT depend on enlarging
        the cap."""
        monkeypatch.setenv('TOFU_SIDEBAR_MAX', '3')
        # Force a fresh meta-cache computation under the shrunk cap.
        from lib.conversations import meta_cache
        meta_cache.invalidate_meta_cache()

        # Sidebar (?meta=1) under cap=3 shows only the 3 newest decoys — no
        # folder member is visible there.
        meta_resp = flask_client.get('/api/v1/conversations?meta=1')
        assert meta_resp.status_code == 200
        meta_list = meta_resp.get_json()
        rows = meta_list if isinstance(meta_list, list) else meta_list.get('conversations', [])
        sidebar_ids = {c['id'] for c in rows}
        for m in foldered_convs['members']:
            assert m not in sidebar_ids, (
                f'test precondition broken: member {m} appeared in the '
                f'cap-3 sidebar — it should be excluded')

        # …yet the folderId query returns every member regardless of the cap.
        resp = flask_client.get(
            f'/api/v1/conversations?folderId={foldered_convs["folder_id"]}')
        assert resp.status_code == 200
        ids = {c['id'] for c in resp.get_json()['conversations']}
        assert set(foldered_convs['members']) <= ids

    def test_star_folder_members_also_returned(self, flask_client, foldered_convs):
        """The auto-migrated '⭐ 置顶' folder benefits identically — its members
        may also sort past the cap, and must be returned in full."""
        resp = flask_client.get(
            f'/api/v1/conversations?folderId={foldered_convs["star_folder_id"]}')
        assert resp.status_code == 200
        ids = {c['id'] for c in resp.get_json()['conversations']}
        assert set(foldered_convs['star_members']) <= ids

    def test_folderId_query_is_metadata_only(self, flask_client, foldered_convs):
        """Folder query rows are metadata-only (no message bodies), same shape
        as the sidebar rows so the frontend merges them via the existing
        shell-construction path."""
        resp = flask_client.get(
            f'/api/v1/conversations?folderId={foldered_convs["folder_id"]}')
        rows = resp.get_json()['conversations']
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
        assert data['conversations'] == []
        assert data['totalCount'] == 0


@pytest.mark.api
class TestConversationListKeysetPagination:
    """C3 — the global list is paginated via a keyset cursor so conversations
    past the first window remain reachable instead of being silently dropped."""

    @pytest.fixture()
    def paging_convs(self, flask_client):
        base = int(time.time() * 1000)
        ids = [f'page-{base}-{i}' for i in range(5)]
        for i, cid in enumerate(ids):
            resp = flask_client.put(f'/api/v1/conversations/{cid}', json={
                'title': f'page {i}',
                'messages': [{'role': 'user', 'content': 'x', 'timestamp': base + i}],
                'createdAt': base + i, 'updatedAt': base + i,
            })
            assert resp.status_code == 200, resp.data
        yield ids
        for cid in ids:
            flask_client.delete(f'/api/v1/conversations/{cid}')

    def test_before_cursor_pages_older_rows(self, flask_client, paging_convs):
        # Page 1: newest 2.
        r1 = flask_client.get('/api/v1/conversations?limit=2&before=99999999999999')
        assert r1.status_code == 200
        d1 = r1.get_json()
        assert isinstance(d1, dict) and 'conversations' in d1
        assert len(d1['conversations']) == 2
        assert d1['hasMore'] is True
        assert 'nextBefore' in d1 and 'nextBeforeId' in d1
        # Page 2: strictly older than page 1's last row — no overlap.
        page1_ids = {c['id'] for c in d1['conversations']}
        r2 = flask_client.get(
            f'/api/v1/conversations?limit=2&before={d1["nextBefore"]}'
            f'&before_id={d1["nextBeforeId"]}')
        d2 = r2.get_json()
        page2_ids = {c['id'] for c in d2['conversations']}
        assert not (page1_ids & page2_ids), 'keyset pages overlapped'
