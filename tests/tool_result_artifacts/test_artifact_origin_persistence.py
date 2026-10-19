"""Durable artifact-origin persistence (record-backed), cross-turn read-back.

The in-task ``_artifactProvenance`` map dies with its turn, so a later
``read_tool_artifact`` read-back used to lose the source round and render the
bare digest label. The origin is now ALSO mirrored into the generic
``record.*`` store (already certified on both TofuDB and the SQL sidecar),
keyed by content digest. These tests pin:

  1. persist → load round-trips the origin fields through the record store.
  2. ``artifact_provenance`` falls back to the durable store when the in-task
     map is absent (the cross-turn case), but only with a resolvable owner.
  3. Owner filtering: a record written by another user reads as absent.
  4. Lazy expiry: an origin past its artifact TTL reads as absent.
  5. Store hiccups degrade silently to the in-memory map (never crash settle).
"""

from __future__ import annotations

import pytest

import lib.tool_results.tool_result_artifacts as tra


pytestmark = pytest.mark.unit


class _FakeStorageClient:
    """Minimal in-memory stand-in for the record.* semantic operations."""

    def __init__(self):
        self.records = {}

    def command(self, op, payload, command_id, deadline=None):
        assert op == 'record.put'
        ns, key = payload['namespace'], payload['key']
        current = self.records.get((ns, key))
        version = (current['version'] + 1) if current else 1
        self.records[(ns, key)] = {'value': payload['value'], 'version': version}
        return {'key': key, 'version': version}

    def query(self, op, payload, deadline=None):
        assert op == 'record.get'
        return self.records.get((payload['namespace'], payload['key']))


@pytest.fixture
def store(monkeypatch):
    client = _FakeStorageClient()
    monkeypatch.setattr(tra, 'get_storage_client', lambda write=False: client)
    return client


REF = 'tool-result:abc123def456'
ORIGIN = dict(tool_name='web_search', display='web_search: citadel',
              llm_round=9, tool_call_id='tc_1')


def test_persist_then_load_round_trip(store):
    tra.persist_artifact_origin(7, REF, **ORIGIN, now_ms=1000)

    loaded = tra.load_artifact_origin(7, REF, now_ms=1001)
    assert loaded == {
        'toolName': 'web_search',
        'display': 'web_search: citadel',
        'llmRound': 9,
        'toolCallId': 'tc_1',
    }


def test_artifact_provenance_falls_back_to_durable_store(store):
    # The source turn is over: its in-task map is gone (empty task dict).
    # Use the wall clock (not a stubbed now_ms) so the lazy-expiry check
    # inside artifact_provenance's fallback sees a live record.
    tra.persist_artifact_origin(7, REF, **ORIGIN)
    foreign_turn_task = {}

    origin = tra.artifact_provenance(foreign_turn_task, REF, user_id=7)
    assert origin is not None
    assert origin['llmRound'] == 9
    assert origin['toolName'] == 'web_search'


def test_artifact_provenance_prefers_in_task_map(store):
    tra.persist_artifact_origin(7, REF, **ORIGIN)
    task = {'_artifactProvenance': {REF: {
        'toolName': 'read_files', 'display': 'read 1 file', 'llmRound': 3}}}

    origin = tra.artifact_provenance(task, REF, user_id=7)
    assert origin['toolName'] == 'read_files'  # in-task wins over the store


def test_artifact_provenance_without_owner_skips_durable_fallback(store):
    tra.persist_artifact_origin(7, REF, **ORIGIN)
    # No user_id → the durable fallback stays off (transient runtime).
    assert tra.artifact_provenance({}, REF) is None


def test_load_filters_foreign_owner(store):
    tra.persist_artifact_origin(7, REF, **ORIGIN, now_ms=1000)
    # A different owner sees the shared record namespace as empty.
    assert tra.load_artifact_origin(8, REF, now_ms=1001) is None


def test_load_respects_lazy_expiry(store):
    tra.persist_artifact_origin(7, REF, **ORIGIN, now_ms=1000, ttl_ms=50)
    assert tra.load_artifact_origin(7, REF, now_ms=1001) is not None
    assert tra.load_artifact_origin(7, REF, now_ms=2000) is None


def test_respill_overwrites_with_latest_origin(store):
    tra.persist_artifact_origin(7, REF, **ORIGIN, now_ms=1000)
    tra.persist_artifact_origin(7, REF, tool_name='read_files',
                                display='read 1 file', llm_round=20,
                                now_ms=2000)
    loaded = tra.load_artifact_origin(7, REF, now_ms=2001)
    assert loaded['toolName'] == 'read_files'
    assert loaded['llmRound'] == 20


def test_store_hiccup_degrades_silently(monkeypatch):
    class _Boom:
        def command(self, *a, **k):
            raise RuntimeError('store down')

        def query(self, *a, **k):
            raise RuntimeError('store down')

    monkeypatch.setattr(tra, 'get_storage_client', lambda write=False: _Boom())
    # Neither call propagates: provenance is advisory, never fatal.
    tra.persist_artifact_origin(7, REF, **ORIGIN)
    assert tra.load_artifact_origin(7, REF) is None
    assert tra.artifact_provenance({}, REF, user_id=7) is None
