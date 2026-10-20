"""tests/provider_probe/test_probe_snapshot_alignment.py — column identity.

Probe cell keys are positional (``key_idx::wire``). Columns are ordered by the
provider's current credential list, so deleting a key shifts every later
column onto the next key's index. Before the snapshot named its columns, a
deleted key's 401 verdicts stayed behind and repainted a healthy key entirely
unauthorized — the access matrix then showed that key with no models at all.

``align_probe_snapshot`` re-binds persisted evidence to current credential
identities: evidence for a credential that no longer exists is dropped, never
re-attributed to whichever key inherited the index.
"""

import pytest

from lib.provider.provider_probe import (
    align_probe_snapshot,
    probe_cell_key,
    public_probe_snapshot,
)


pytestmark = pytest.mark.unit


def _cell(key_idx, credential_id, wire, status='ok'):
    return {
        'key_idx': key_idx,
        'credential_id': credential_id,
        'model_id': wire,
        'status': status,
        'probe_surface': 'chat',
        'proof': 'generated_text',
        'recommend_disable': status == 'unauthorized',
    }


def _task(credential_ids, cells):
    return {
        'provider_id': 'u1_example_corp',
        'credential_ids': credential_ids,
        'status': 'done',
        'started_at': 1,
        'finished_at': 2,
        'total': len(cells),
        'done_count': len(cells),
        'cells': cells,
        'summary': {'ok': 0, 'disable': 0},
        'error': None,
    }


def test_public_snapshot_records_ordered_credential_identities():
    task = _task(['cred-a', 'cred-b'], {probe_cell_key(1, 'm1'): _cell(1, 'cred-b', 'm1')})
    snap = public_probe_snapshot(task)
    assert snap['credential_ids'] == ['cred-a', 'cred-b']
    assert snap['cells'][probe_cell_key(1, 'm1')]['credential_id'] == 'cred-b'


def test_deleted_key_verdicts_do_not_migrate_to_the_next_column():
    # Column 3 was the dead key (every wire unauthorized); it has since been
    # deleted, so the surviving key now occupies index 3.
    snapshot = {
        'status': 'done',
        'credential_ids': ['cred-0', 'cred-1', 'cred-2', 'cred-dead'],
        'cells': {
            probe_cell_key(0, 'm1'): _cell(0, 'cred-0', 'm1'),
            probe_cell_key(3, 'm1'): _cell(3, 'cred-dead', 'm1', 'unauthorized'),
        },
    }
    aligned = align_probe_snapshot(snapshot, ['cred-0', 'cred-1', 'cred-2'])
    assert probe_cell_key(3, 'm1') not in aligned['cells']
    assert [c['status'] for c in aligned['cells'].values()] == ['ok']
    assert aligned['credential_ids'] == ['cred-0', 'cred-1', 'cred-2']


def test_reordered_columns_follow_their_credential():
    snapshot = {
        'status': 'done',
        'credential_ids': ['cred-a', 'cred-b'],
        'cells': {
            probe_cell_key(0, 'm1'): _cell(0, 'cred-a', 'm1', 'unauthorized'),
            probe_cell_key(1, 'm1'): _cell(1, 'cred-b', 'm1'),
        },
    }
    aligned = align_probe_snapshot(snapshot, ['cred-b', 'cred-a'])
    assert aligned['cells'][probe_cell_key(1, 'm1')]['status'] == 'unauthorized'
    assert aligned['cells'][probe_cell_key(0, 'm1')]['status'] == 'ok'
    assert aligned['cells'][probe_cell_key(1, 'm1')]['key_idx'] == 1


def test_new_column_never_inherits_another_keys_evidence():
    snapshot = {
        'status': 'done',
        'credential_ids': ['cred-a'],
        'cells': {probe_cell_key(0, 'm1'): _cell(0, 'cred-a', 'm1', 'unauthorized')},
    }
    aligned = align_probe_snapshot(snapshot, ['cred-new', 'cred-a'])
    assert sorted(aligned['cells']) == [probe_cell_key(1, 'm1')]


def test_snapshot_without_column_identities_is_not_attributed():
    legacy = {'status': 'done', 'cells': {probe_cell_key(3, 'm1'): _cell(3, '', 'm1', 'unauthorized')}}
    aligned = align_probe_snapshot(legacy, ['cred-0', 'cred-1', 'cred-2', 'cred-3'])
    assert aligned['cells'] == {}
    assert aligned['credential_ids'] == ['cred-0', 'cred-1', 'cred-2', 'cred-3']


def test_positional_cells_are_resolved_through_recorded_identities():
    snapshot = {
        'status': 'done',
        'credential_ids': ['cred-a', 'cred-b'],
        'cells': {
            probe_cell_key(0, 'm1'): {k: v for k, v in _cell(0, 'cred-a', 'm1', 'unauthorized').items()
                                      if k != 'credential_id'},
        },
    }
    aligned = align_probe_snapshot(snapshot, ['cred-b', 'cred-a'])
    assert aligned['cells'][probe_cell_key(1, 'm1')]['credential_id'] == 'cred-a'
