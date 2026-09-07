"""tests/test_msgid_dedupe.py — _assign_message_ids duplicate-id heal.

Measured data (conv ms8bx7089s3268): idx1 (fr=aborted, no _taskId) and
idx2 (fr=stop) share ONE _msgId (tmp_196fedef) — an aborted streaming
residue persisted with the client id, then its retry committing with the
same id. Every id-keyed consumer (frontend surgical reconcile / order
assertion, PATCH /messages/by-id) collapses onto the FIRST match; the
RENDER ORDER VIOLATION beacon fired on the user's screen at 22:22:22.

The heal (in _assign_message_ids, the single write-side chokepoint):
on a duplicate, the EARLIER (stale, no-longer-live) occurrence is
re-minted; the LATEST one keeps the id (the live/committed turn the
client reconciles by id — rescue-PUT rebase, translation frames).

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_msgid_dedupe.py -v
"""

import logging

import pytest

from lib.tasks_pkg.manager._events import _assign_message_ids

pytestmark = pytest.mark.unit


def test_duplicate_reminted_earlier_keeps_latest(caplog):
    msgs = [
        {'role': 'user', '_msgId': 'u1', 'content': 'q'},
        {'role': 'assistant', '_msgId': 'tmp_dup', 'content': 'aborted',
         'finishReason': 'aborted'},
        {'role': 'assistant', '_msgId': 'tmp_dup', 'content': 'final',
         'finishReason': 'stop'},
    ]
    with caplog.at_level(logging.WARNING):
        changed = _assign_message_ids(msgs)
    assert changed is True
    assert msgs[2]['_msgId'] == 'tmp_dup', (
        'the LATEST occurrence (the live/committed turn) must keep the id — '
        'the client reconciles its in-flight bubble by it')
    assert msgs[1]['_msgId'] != 'tmp_dup', (
        'the earlier (stale aborted residue) occurrence must be re-minted')
    assert msgs[1]['_msgId'] != msgs[2]['_msgId']
    assert msgs[0]['_msgId'] == 'u1', 'unrelated messages untouched'
    assert any('duplicate _msgId' in r.message for r in caplog.records), (
        'a healed duplicate must leave a trace in the log (§2 discipline)')


def test_idempotent_second_run_no_change(caplog):
    msgs = [
        {'role': 'assistant', '_msgId': 'tmp_dup', 'content': 'a'},
        {'role': 'assistant', '_msgId': 'tmp_dup', 'content': 'b'},
    ]
    assert _assign_message_ids(msgs) is True
    snapshot = [m['_msgId'] for m in msgs]
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        changed2 = _assign_message_ids(msgs)
    assert changed2 is False, 'a healed list must be stable on the next pass'
    assert [m['_msgId'] for m in msgs] == snapshot
    assert not any('duplicate _msgId' in r.message for r in caplog.records)


def test_missing_ids_still_minted():
    msgs = [{'role': 'user', 'content': 'q'}, {'role': 'assistant', 'content': 'a'}]
    assert _assign_message_ids(msgs) is True
    assert all(m.get('_msgId') for m in msgs)
    assert msgs[0]['_msgId'] != msgs[1]['_msgId']


def test_unique_ids_untouched():
    msgs = [{'role': 'assistant', '_msgId': 'x1'}, {'role': 'assistant', '_msgId': 'x2'}]
    assert _assign_message_ids(msgs) is False
    assert [m['_msgId'] for m in msgs] == ['x1', 'x2']


def test_triple_duplicate_keeps_only_latest():
    msgs = [{'role': 'assistant', '_msgId': 'tmp_t', 'content': c}
            for c in ('a', 'b', 'c')]
    assert _assign_message_ids(msgs) is True
    assert msgs[2]['_msgId'] == 'tmp_t'
    ids = [m['_msgId'] for m in msgs]
    assert len(set(ids)) == 3, 'three occurrences must end with three distinct ids'


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
