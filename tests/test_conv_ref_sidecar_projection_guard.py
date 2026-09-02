# Incident anchor: 2026-08-20 — list_conversations timed out on 100% of calls
# ("Storage request timed out" after ~17-18s, i.e. 3 attempts x (5s+1s grace)).
"""conv_ref must NEVER ask the sidecar for the transcript archive.

The sidecar ``conversation.list`` default is ``include_messages=True``: every
``messages_json`` + ``search_text`` blob rides one 64 MiB-capped RPC frame.
Measured on the live DB (2026-08-20): 1000 rows = ~3.1 GB / 5.6s just to
read — the request physically cannot beat the 5s(+1s) client deadline, so the
tool failed always, not occasionally. The sidebar learned the same lesson in
e80d66e2 (settings-whitelist projection); conv_ref was the leftover caller.

The contract this pins, in both directions:

  * listing is metadata-only (``include_messages: False`` + a settings_keys
    projection), so the payload stays KiB-scale; and
  * keyword body-search rides the bounded server-side ``conversation.search``
    op instead of client-side filtering over hauled ``search_text`` — a naive
    "just add include_messages: False" fix would silently degrade keyword
    search to titles only, which is the regression the owner explicitly
    forbade when approving this fix.
"""

import pytest

pytestmark = pytest.mark.unit


class _FakeStorageClient:
    """Captures every (operation, payload) the tool sends to the sidecar."""

    def __init__(self, documents, search_hits=()):
        self.calls = []
        self._documents = list(documents)
        self._search_hits = list(search_hits)

    def query(self, operation, payload=None):
        self.calls.append((operation, dict(payload or {})))
        if operation == 'conversation.list':
            project_path = (payload or {}).get('project_path')
            if project_path is None:
                return self._documents
            return [
                document for document in self._documents
                if (document.get('metadata', {}).get('settings', {})
                    .get('projectPath')) == project_path
            ]
        if operation == 'conversation.search':
            return self._search_hits
        raise AssertionError(f'unexpected sidecar operation: {operation}')


def _doc(cid, title, project_path=None, **extra):
    settings = {}
    if project_path is not None:
        settings['projectPath'] = project_path
    metadata = {
        'id': cid, 'title': title, 'settings': settings,
        'msg_count': 3, 'updated_at': 1700000000000, 'created_at': 1690000000000,
    }
    metadata.update(extra)
    return {'metadata': metadata, 'messages': []}


def _run(monkeypatch, client, **kwargs):
    monkeypatch.setattr(
        'lib.storage.get_storage_client', lambda write=False: client)
    from lib.conv_ref._query import list_conversations
    kwargs.setdefault('user_id', 1)
    return list_conversations(**kwargs)


def _list_payloads(client):
    return [p for op, p in client.calls if op == 'conversation.list']


class TestNeverRequestsTheTranscriptArchive:
    def test_listing_is_metadata_only(self, monkeypatch):
        client = _FakeStorageClient([_doc('c1', 'Alpha')])
        out = _run(monkeypatch, client, scope='all')
        assert 'c1' in out
        payloads = _list_payloads(client)
        assert len(payloads) == 1
        assert payloads[0].get('include_messages') is False, (
            'conv_ref asked the sidecar for the full transcript archive — '
            'this is the 2026-08-20 100%-timeout incident shape')

    def test_settings_projection_is_whitelisted(self, monkeypatch):
        client = _FakeStorageClient([_doc('c1', 'Alpha')])
        _run(monkeypatch, client, scope='all')
        keys = _list_payloads(client)[0].get('settings_keys')
        assert keys == ['projectPath'], (
            'full settings_json blobs (autopilot summaries et al.) must not '
            'ride the listing frame either')

    def test_keyword_search_still_never_requests_blobs(self, monkeypatch):
        client = _FakeStorageClient([_doc('c1', 'Alpha')],
                                    search_hits=[{'id': 'c1', 'snippet': ''}])
        _run(monkeypatch, client, keyword='alpha', scope='all')
        for payload in _list_payloads(client):
            assert payload.get('include_messages') is False


class TestKeywordBodySearchRidesTheSearchOp:
    def test_body_hit_without_title_hit_is_returned(self, monkeypatch):
        """A conversation matching only in its body must still be found."""
        client = _FakeStorageClient(
            [_doc('c1', 'Unrelated title'), _doc('c2', 'Also unrelated')],
            search_hits=[{'id': 'c2', 'snippet': '…sidecar wedge…'}])
        out = _run(monkeypatch, client, keyword='wedge', scope='all')
        assert 'c2' in out and 'c1' not in out
        search = [p for op, p in client.calls if op == 'conversation.search']
        assert len(search) == 1, 'keyword path must use conversation.search'
        assert search[0]['query'] == 'wedge'

    def test_title_hit_still_works_without_a_body_hit(self, monkeypatch):
        client = _FakeStorageClient([_doc('c1', 'Storage wedge postmortem')])
        out = _run(monkeypatch, client, keyword='wedge', scope='all')
        assert 'c1' in out

    def test_no_match_anywhere_returns_the_guidance_message(self, monkeypatch):
        client = _FakeStorageClient([_doc('c1', 'Alpha')])
        out = _run(monkeypatch, client, keyword='zzz-nomatch', scope='all')
        assert "No conversations found matching 'zzz-nomatch'" in out


class TestUserIdPassThrough:
    def test_explicit_user_id_reaches_both_operations(self, monkeypatch):
        """The old branch hard-coded DEFAULT_USER_ID — tenant isolation bug."""
        client = _FakeStorageClient([_doc('c1', 'Alpha')],
                                    search_hits=[{'id': 'c1', 'snippet': ''}])
        _run(monkeypatch, client, keyword='alpha', scope='all', user_id=7)
        for op, payload in client.calls:
            assert payload.get('user_id') == 7, (
                f'{op} saw user_id={payload.get("user_id")} instead of 7')

    def test_default_user_id_is_preserved_for_single_user(self, monkeypatch):
        client = _FakeStorageClient([_doc('c1', 'Alpha')])
        _run(monkeypatch, client, scope='all')
        assert _list_payloads(client)[0]['user_id'] == 1


class TestProjectScopeSurvivesTheProjection:
    def test_scope_filter_is_pushed_into_storage(self, monkeypatch):
        client = _FakeStorageClient([
            _doc('c1', 'In project', project_path='/p/this'),
            _doc('c2', 'Other project', project_path='/p/other'),
            _doc('c3', 'No project'),
        ])
        out = _run(monkeypatch, client, scope='project', project_path='/p/this')
        assert 'c1' in out and 'c2' not in out and 'c3' not in out
        payload = _list_payloads(client)[0]
        assert payload['project_path'] == '/p/this'
        assert payload['settings_keys'] == ['projectPath']

    def test_mixed_version_unfiltered_rows_fail_closed(self, monkeypatch):
        client = _FakeStorageClient([
            _doc('c1', 'In project', project_path='/p/this'),
            _doc('c2', 'Other project', project_path='/p/other'),
        ])
        client.query = lambda operation, payload=None: (
            client._documents if operation == 'conversation.list' else []
        )

        out = _run(
            monkeypatch,
            client,
            scope='project',
            project_path='/p/this',
        )

        assert 'c1' in out
        assert 'c2' not in out
