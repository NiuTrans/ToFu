"""Model-facing semantic aliases for spilled tool-result artifacts.

The canonical storage reference stays ``tool-result:<sha256>`` (CAS dedup,
manifest children, signed cursor binding, compaction anchors). The sparse
model projection instead carries ``tr:<slug>:<digest-prefix>`` — shorter,
BPE-friendly, and memorable — and a later read/search resolves the alias
back to the digest through a durable ``record.*`` row before any storage
call. These tests pin:

  1. Slug derivation: display preferred, tool-name fallback, ASCII shape.
  2. Deterministic mint: same (slug, content) always yields the same alias;
     a re-spill refreshes expiry instead of forking a new handle.
  3. Collision safety: a same-slug same-prefix collision with DIFFERENT
     content lengthens the suffix; a live row is never overwritten, so an
     alias can never silently point at the wrong evidence.
  4. Resolve: digest passthrough, in-task fast path, durable fallback with
     owner filter + lazy expiry, unknown alias → None (fail loud).
  5. Handler + pipeline integration: the model quotes the alias, storage
     receives the digest, and any mint/store failure keeps the digest
     projection.
"""

from __future__ import annotations

import json

import pytest

import lib.tool_result_artifacts as tra


pytestmark = pytest.mark.unit


REF_A = 'tool-result:' + 'a' * 64
REF_B = 'tool-result:' + 'b' * 64
# Two digests sharing the first 12 hex chars (suffix steps are 8/16/32/64).
REF_C1 = 'tool-result:' + 'deadbeef' + 'cafe' + '1' * 52
REF_C2 = 'tool-result:' + 'deadbeef' + 'cafe' + '2' * 52


class _FakeStorageClient:
    """Minimal in-memory stand-in for the record.* semantic operations."""

    def __init__(self):
        self.records = {}

    def command(self, op, payload, command_id, deadline=None):
        assert op == 'record.put'
        ns, key = payload['namespace'], payload['key']
        current = self.records.get((ns, key))
        version = (current['version'] + 1) if current else 1
        self.records[(ns, key)] = {'value': payload['value'],
                                   'version': version}
        return {'key': key, 'version': version}

    def query(self, op, payload, deadline=None):
        assert op == 'record.get'
        return self.records.get((payload['namespace'], payload['key']))


@pytest.fixture
def store(monkeypatch):
    client = _FakeStorageClient()
    monkeypatch.setattr(tra, 'get_storage_client', lambda write=False: client)
    return client


def _seed(store, alias, *, user_id=7, digest=REF_A, expires_at_ms=10**15):
    store.records[(tra._ALIAS_NAMESPACE, alias)] = {
        'value': {'v': 1, 'user_id': user_id, 'digest': digest,
                  'expiresAtMs': expires_at_ms},
        'version': 1}


# ── Slug derivation ──────────────────────────────────────────────────────────


def test_slug_prefers_display_label():
    assert tra._alias_slug('web_search', 'web_search: citadel') == \
        'web-search-citadel'


def test_slug_falls_back_to_tool_name():
    assert tra._alias_slug('read_files', '') == 'read-files'
    assert tra._alias_slug('read_files', None) == 'read-files'


def test_slug_non_ascii_display_falls_back():
    assert tra._alias_slug('web_search', '搜索： 城堡') == 'web-search'


def test_slug_capped_and_never_empty():
    slug = tra._alias_slug('tool', 'x' * 100)
    assert slug == 'x' * tra._ALIAS_SLUG_MAX
    assert tra._alias_slug('', '') == 'result'


# ── Mint ─────────────────────────────────────────────────────────────────────


def test_mint_is_deterministic_and_wellformed(store):
    task = {}
    first = tra.mint_artifact_alias(
        task, REF_A, tool_name='web_search', display='web_search: citadel',
        user_id=7, now_ms=1000)
    assert first == 'tr:web-search-citadel:' + 'a' * 8
    assert tra.ALIAS_REF_RE.fullmatch(first)

    second = tra.mint_artifact_alias(
        task, REF_A, tool_name='web_search', display='web_search: citadel',
        user_id=7, now_ms=2000)
    assert second == first
    # Same-turn fast path registered.
    assert task['_artifactAliases'][first] == REF_A


def test_mint_respill_refreshes_expiry(store):
    alias = tra.mint_artifact_alias(
        {}, REF_A, tool_name='t', display='', user_id=7, now_ms=1000)
    tra.mint_artifact_alias(
        {}, REF_A, tool_name='t', display='', user_id=7, now_ms=5000)
    value = store.records[(tra._ALIAS_NAMESPACE, alias)]['value']
    assert value['expiresAtMs'] == 5000 + tra.DEFAULT_TTL_MS


def test_mint_collision_lengthens_suffix(store):
    first = tra.mint_artifact_alias(
        {}, REF_C1, tool_name='web_search', display='', user_id=7,
        now_ms=1000)
    assert first.endswith(':deadbeef')
    second = tra.mint_artifact_alias(
        {}, REF_C2, tool_name='web_search', display='', user_id=7,
        now_ms=1000)
    # Same slug + same 8-hex prefix but different content → longer suffix.
    assert second == 'tr:web-search:' + 'deadbeefcafe2222'
    # Both aliases stay valid and point at their OWN evidence.
    assert tra.resolve_artifact_ref({}, first, user_id=7, now_ms=1001) == REF_C1
    assert tra.resolve_artifact_ref({}, second, user_id=7, now_ms=1001) == REF_C2


def test_mint_never_overwrites_foreign_owner(store):
    _seed(store, 'tr:web-search:' + 'a' * 8, user_id=8, digest=REF_B)
    alias = tra.mint_artifact_alias(
        {}, REF_A, tool_name='web_search', display='', user_id=7, now_ms=1000)
    # The foreign handle was not hijacked; ours lengthened instead.
    assert alias == 'tr:web-search:' + 'a' * 16
    assert tra.resolve_artifact_ref(
        {}, 'tr:web-search:' + 'a' * 8, user_id=8, now_ms=1001) == REF_B


def test_mint_may_recycle_expired_row(store):
    _seed(store, 'tr:web-search:' + 'a' * 8, user_id=8, digest=REF_B,
          expires_at_ms=500)
    alias = tra.mint_artifact_alias(
        {}, REF_A, tool_name='web_search', display='', user_id=7, now_ms=1000)
    assert alias == 'tr:web-search:' + 'a' * 8
    assert tra.resolve_artifact_ref({}, alias, user_id=7, now_ms=1001) == REF_A


def test_mint_requires_digest_and_owner(store):
    assert tra.mint_artifact_alias(
        {}, 'tool-result:nothex', tool_name='t', display='', user_id=7) is None
    assert tra.mint_artifact_alias(
        {}, REF_A, tool_name='t', display='', user_id=None) is None
    assert store.records == {}


def test_mint_store_hiccup_returns_none(monkeypatch):
    class _Boom:
        def command(self, *a, **k):
            raise RuntimeError('store down')

        def query(self, *a, **k):
            raise RuntimeError('store down')

    monkeypatch.setattr(tra, 'get_storage_client', lambda write=False: _Boom())
    assert tra.mint_artifact_alias(
        {}, REF_A, tool_name='t', display='', user_id=7) is None


# ── Resolve ──────────────────────────────────────────────────────────────────


def test_resolve_digest_passthrough_without_store():
    assert tra.resolve_artifact_ref(None, REF_A) == REF_A
    # Legacy/garbage strings pass through to the storage validator.
    assert tra.resolve_artifact_ref(None, 'tool-result:legacy') == \
        'tool-result:legacy'
    assert tra.resolve_artifact_ref(None, '') == ''


def test_resolve_in_task_fast_path_needs_no_owner(store):
    task = {'_artifactAliases': {'tr:web-search:' + 'a' * 8: REF_A}}
    assert tra.resolve_artifact_ref(task, 'tr:web-search:' + 'a' * 8) == REF_A


def test_resolve_durable_fallback(store):
    _seed(store, 'tr:web-search:' + 'a' * 8, user_id=7, digest=REF_A)
    foreign_turn_task = {}
    assert tra.resolve_artifact_ref(
        foreign_turn_task, 'tr:web-search:' + 'a' * 8, user_id=7) == REF_A


def test_resolve_filters_foreign_owner(store):
    _seed(store, 'tr:web-search:' + 'a' * 8, user_id=7, digest=REF_A)
    assert tra.resolve_artifact_ref(
        {}, 'tr:web-search:' + 'a' * 8, user_id=8) is None


def test_resolve_respects_lazy_expiry(store):
    _seed(store, 'tr:web-search:' + 'a' * 8, user_id=7, digest=REF_A,
          expires_at_ms=1000)
    assert tra.resolve_artifact_ref(
        {}, 'tr:web-search:' + 'a' * 8, user_id=7, now_ms=999) == REF_A
    assert tra.resolve_artifact_ref(
        {}, 'tr:web-search:' + 'a' * 8, user_id=7, now_ms=1000) is None


def test_resolve_unknown_alias_returns_none(store):
    assert tra.resolve_artifact_ref(
        {}, 'tr:web-search:00000000', user_id=7) is None
    # Without an owner the durable fallback stays off.
    assert tra.resolve_artifact_ref({}, 'tr:web-search:00000000') is None


# ── Handler integration ──────────────────────────────────────────────────────


class _RecordingRepository:
    def __init__(self):
        self.seen = []

    def read_range(self, *, user_id, artifact_ref, offset=0, limit=1,
                   selector=None, now_ms=None):
        self.seen.append(artifact_ref)
        return {'artifactRef': artifact_ref, 'offset': 0, 'content': 'x',
                'nextCursor': '1'}

    def search(self, *, user_id, artifact_ref, query, cursor=0, limit=1,
               now_ms=None):
        self.seen.append(artifact_ref)
        return {'artifactRef': artifact_ref, 'items': [], 'nextCursor': ''}


def test_handler_resolves_alias_before_storage(store):
    from lib.tasks_pkg.handlers.tool_result_artifacts import _execute_one

    # Wall-clock mint: the handler resolve below uses the real clock, and a
    # stubbed epoch-ms now would already be past the alias's lazy expiry.
    alias = tra.mint_artifact_alias(
        {}, REF_A, tool_name='web_search', display='web_search: citadel',
        user_id=1)
    repo = _RecordingRepository()
    result = _execute_one('read_tool_artifact',
                          {'artifact_ref': alias, 'limit': 8192},
                          user_id=1, repository=repo, token_budget=1500,
                          task={})
    assert result['status'] == 'ok'
    assert repo.seen == [REF_A]            # storage saw the canonical digest
    assert result['artifactRef'] == alias  # the model keeps its own handle


def test_handler_unknown_alias_fails_loud_without_storage(store):
    from lib.tasks_pkg.handlers.tool_result_artifacts import _execute_one

    repo = _RecordingRepository()
    result = _execute_one('read_tool_artifact',
                          {'artifact_ref': 'tr:web-search:00000000'},
                          user_id=1, repository=repo, token_budget=1500,
                          task={})
    assert result['status'] == 'error'
    assert result['error']['code'] == 'artifact_unavailable'
    assert result['error']['retryable'] is False
    assert repo.seen == []  # no storage guess was attempted


def test_handler_digest_passthrough(store):
    from lib.tasks_pkg.handlers.tool_result_artifacts import _execute_one

    repo = _RecordingRepository()
    result = _execute_one('read_tool_artifact',
                          {'artifact_ref': REF_A, 'limit': 8192},
                          user_id=1, repository=repo, token_budget=1500,
                          task={})
    assert result['status'] == 'ok'
    assert repo.seen == [REF_A]
    assert result['artifactRef'] == REF_A


# ── Pipeline projection ──────────────────────────────────────────────────────


def _envelope(artifact_ref):
    return json.dumps(
        {'status': 'partial', 'summary': 'bounded preview',
         'artifactRef': artifact_ref, 'cursor': '0', 'truncated': True},
        ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def test_projection_swaps_only_model_text(store, monkeypatch):
    from lib.tasks_pkg.tool_dispatch import _pipeline

    monkeypatch.setattr(
        'lib.tasks_pkg.manager.task_user_id', lambda task: 7, raising=False)
    internal = _envelope(REF_A)
    model_text = _envelope(REF_A)
    round_entry = {'query': 'web_search: citadel'}
    projected = _pipeline._project_spilled_artifact_alias(
        {}, round_entry, 'web_search', internal, model_text)

    assert json.loads(projected)['artifactRef'] == \
        'tr:web-search-citadel:' + 'a' * 8
    # The canonical pointer is untouched in the internal envelope.
    assert json.loads(internal)['artifactRef'] == REF_A


def test_projection_keeps_digest_when_mint_fails(monkeypatch):
    from lib.tasks_pkg.tool_dispatch import _pipeline

    class _Boom:
        def command(self, *a, **k):
            raise RuntimeError('store down')

        def query(self, *a, **k):
            raise RuntimeError('store down')

    monkeypatch.setattr(tra, 'get_storage_client', lambda write=False: _Boom())
    monkeypatch.setattr(
        'lib.tasks_pkg.manager.task_user_id', lambda task: 7, raising=False)
    model_text = _envelope(REF_A)
    projected = _pipeline._project_spilled_artifact_alias(
        {}, {'query': 'x'}, 'web_search', _envelope(REF_A), model_text)
    assert projected == model_text
    assert json.loads(projected)['artifactRef'] == REF_A


def test_projection_skips_non_digest_envelopes():
    from lib.tasks_pkg.tool_dispatch import _pipeline

    inline = json.dumps({'status': 'ok', 'summary': 'fine'})
    assert _pipeline._project_spilled_artifact_alias(
        {}, {'query': 'x'}, 'tool', inline, inline) == inline
