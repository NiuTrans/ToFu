"""Event source witnesses through the selected real SQL authority."""

from __future__ import annotations

import time
import uuid
import pytest
from lib.storage import StorageError
from lib.task_event_contract import EVENT_READ_GUARD_CONTRACT, EVENT_READ_GUARD_SCOPED_CONTRACT
from tests.storage.test_storage_sidecar_contract import storage as storage

pytestmark = pytest.mark.unit


def bounds(client, task, guarded=True, types=None):
    payload = {"task_id": task}
    if guarded:
        payload["include_read_guard"] = True
    if types is not None:
        payload['types'] = types
    value = client.query("event.bounds", payload)
    if guarded:
        assert value["read_guard"]["contract"] == (
            EVENT_READ_GUARD_CONTRACT if types is None else EVENT_READ_GUARD_SCOPED_CONTRACT)
        if types is not None:
            assert value['read_guard']['types'] == sorted(set(types))
        stamp = value["read_guard"]["stamp"]
        assert len(stamp) == 64 and set(stamp) <= set("0123456789abcdef")
    return value


def append(client, task, sequence, value="row", event_type="round_end"):
    return client.command(
        "event.append",
        {
            "task_id": task,
            "sequence": sequence,
            "event": {"type": event_type, "value": value},
        },
        None,
    )


def stamp(client, task):
    return bounds(client, task)["read_guard"]["stamp"]


def prune(client, cutoff):
    return client.command(
        "event.prune",
        {"created_before_ms": cutoff, "retention_class": "structural", "limit": 1000},
        None,
    )


def test_guard_is_opt_in_and_stable_across_pooled_reads(storage):
    c = storage.client
    task = uuid.uuid4().hex
    assert bounds(c, task, False) == {
        "retained_count": 0,
        "base_cursor": 0,
        "next_cursor": 0,
    }
    original = stamp(c, task)
    assert {stamp(c, task) for _ in range(8)} == {original}
    assert stamp(c, uuid.uuid4().hex) != original
    append(c, task, 0)
    changed = stamp(c, task)
    assert changed != original
    assert not append(c, task, 0)["inserted"]
    assert stamp(c, task) == changed
    with pytest.raises(StorageError) as error:
        append(c, task, 0, "conflict")
    assert error.value.code == "database_conflict"
    assert stamp(c, task) == changed
    append(c, uuid.uuid4().hex, 0)
    assert stamp(c, task) == changed


def test_retention_and_sequence_reuse_cannot_hide_aba(storage):
    c = storage.client
    task = uuid.uuid4().hex
    append(c, task, 1, "before")
    middle = c.query("event.list", {"task_id": task})[0]
    cutoff = middle["created_at_ms"] + 1
    time.sleep(0.02)
    append(c, task, 0, "left")
    append(c, task, 2, "right")
    before = bounds(c, task)
    plain = bounds(c, task, False)
    assert prune(c, cutoff)["deleted"] == 1
    assert stamp(c, task) != before["read_guard"]["stamp"]
    append(c, task, 1, "after")
    assert bounds(c, task, False) == plain
    assert stamp(c, task) != before["read_guard"]["stamp"]


def test_empty_source_cannot_recycle_initial_guard_after_append_and_prune(storage):
    c = storage.client
    task = uuid.uuid4().hex
    initial = stamp(c, task)
    append(c, task, 0)
    active = stamp(c, task)
    assert prune(c, 4_000_000_000_000)["deleted"] == 1
    assert bounds(c, task, False) == {
        "retained_count": 0,
        "base_cursor": 0,
        "next_cursor": 0,
    }
    empty = stamp(c, task)
    assert empty not in {initial, active}
    append(c, task, 0, "recreated")
    assert stamp(c, task) not in {initial, active, empty}


def test_batch_rollback_and_dedup_do_not_publish_a_guard(storage):
    c = storage.client
    task = uuid.uuid4().hex
    append(c, task, 0, "original")
    before = stamp(c, task)
    with pytest.raises(StorageError):
        c.command(
            "event.append_batch",
            {
                "events": [
                    {
                        "task_id": task,
                        "sequence": 1,
                        "event": {"type": "round_end", "value": "new"},
                    },
                    {
                        "task_id": task,
                        "sequence": 0,
                        "event": {"type": "round_end", "value": "different"},
                    },
                ]
            },
            None,
        )
    assert stamp(c, task) == before
    assert bounds(c, task, False)["retained_count"] == 1
    result = c.command(
        "event.append_batch",
        {
            "events": [
                {
                    "task_id": task,
                    "sequence": 0,
                    "event": {"type": "round_end", "value": "original"},
                }
            ]
        },
        None,
    )
    assert result["deduplicated"] == 1 and stamp(c, task) == before


def test_reopened_authority_invalidates_guard_without_changing_source(storage):
    c = storage.client
    task = uuid.uuid4().hex
    append(c, task, 0)
    before = stamp(c, task)
    rows = c.query("event.list", {"task_id": task})
    storage.stop()
    storage.start()
    assert storage.client.query("event.list", {"task_id": task}) == rows
    assert stamp(storage.client, task) != before


@pytest.mark.parametrize("value", [None, 1, "true", {}, []])
def test_guard_flag_must_be_boolean(storage, value):
    with pytest.raises(StorageError) as error:
        storage.client.query(
            "event.bounds", {"task_id": uuid.uuid4().hex, "include_read_guard": value}
        )
    assert error.value.code == "database_protocol_error"


def test_scoped_guard_ignores_noise_binds_exact_types_and_fences_late_revisions(storage):
    c, task = storage.client, uuid.uuid4().hex
    scope = ['messages_snapshot', 'tool_wire_projection']
    append(c, task, 4, event_type='messages_snapshot')
    before = bounds(c, task, types=scope)
    whole_before = stamp(c, task)
    assert before['retained_count'] == 1 and before['base_cursor'] == 4 and before['next_cursor'] == 5
    append(c, task, 100, event_type='round_usage')
    append(c, task, 2, event_type='delta')
    c.command('event.append_batch', {'events': [
        {'task_id': task, 'sequence': seq, 'event': {'type':'round_usage'}}
        for seq in [101, 102, 101]
    ]}, None)
    assert bounds(c, task, types=scope) == before and stamp(c, task) != whole_before
    assert bounds(c, task, types=scope[::-1] + scope) == before
    assert bounds(c, task, types=['messages_snapshot'])['read_guard'] != before['read_guard']
    assert bounds(c, task, False, scope) == {k:v for k,v in before.items() if k != 'read_guard'}
    append(c, task, 3, 'late', event_type='messages_snapshot')
    revised = bounds(c, task, types=scope)
    assert revised['read_guard'] != before['read_guard'] and revised['base_cursor'] == 3
    append(c, task, 105, event_type='tool_wire_projection')
    assert bounds(c, task, types=scope)['read_guard'] != revised['read_guard']


def test_scoped_retention_aba_and_reopen_invalidate_but_preserve_exact_rows(storage):
    c, task = storage.client, uuid.uuid4().hex
    scope = ['round_end']
    append(c, task, 1, 'before')
    cutoff = c.query('event.list', {'task_id':task})[0]['created_at_ms'] + 1
    time.sleep(.02)
    append(c, task, 0, 'left')
    append(c, task, 2, 'right')
    before = bounds(c, task, types=scope)
    assert prune(c, cutoff)['deleted'] == 1
    append(c, task, 1, 'replacement')
    after = bounds(c, task, types=scope)
    assert {k:v for k,v in before.items() if k != 'read_guard'} == {k:v for k,v in after.items() if k != 'read_guard'}
    assert before['read_guard'] != after['read_guard']
    rows = c.query('event.list', {'task_id':task})
    storage.stop(); storage.start()
    assert storage.client.query('event.list', {'task_id':task}) == rows
    assert bounds(storage.client, task, types=scope)['read_guard'] != after['read_guard']


def test_scoped_empty_source_and_unselected_retention_are_fenced(storage):
    c, task = storage.client, uuid.uuid4().hex
    scope = ['messages_snapshot']
    append(c, task, 0, event_type='round_usage')
    # The first SQL mutation publishes a legacy witness even for unselected
    # rows; after publication, ordinary noise appends cannot restart a scan.
    before = bounds(c, task, types=scope)
    assert before['retained_count'] == before['next_cursor'] == before['base_cursor'] == 0
    append(c, task, 1, event_type='round_usage')
    assert bounds(c, task, types=scope) == before
    assert prune(c, 4_000_000_000_000)['deleted'] == 2
    assert bounds(c, task, types=scope)['read_guard'] != before['read_guard']


@pytest.mark.parametrize('types', [None, [], '', 'round_end', True, {}, [None], [1], [''], ['a'*129], ['a']*65])
def test_scoped_guard_rejects_invalid_type_scope(storage, types):
    with pytest.raises(StorageError) as error:
        storage.client.query('event.bounds', {'task_id':uuid.uuid4().hex, 'types':types, 'include_read_guard':True})
    assert error.value.code == 'database_protocol_error'
