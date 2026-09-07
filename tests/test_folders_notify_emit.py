#!/usr/bin/env python3
"""tests/test_folders_notify_emit.py — emit-side coverage for the event-driven
cross-device FOLDER sync signal.

WHY
---
Folders live in a SEPARATE per-install store (``data/config/folders.json``), not
the ``conversations`` table, so they don't ride the ``conv_changed`` rev signal.
Before this pass a folder created / renamed / deleted / reordered on one device
stayed invisible on another until a manual refresh — the same "forced refresh"
class the auto-sync objective eliminates.

``routes/api_v1/folders.py::_notify_folders_changed`` now pushes a dedicated
frame on the SAME ``notify`` channel the conversation-sync subscription already
listens on fleet-wide:

    { type:'folders_changed', deletedFolderId?, userId }

This suite drives the REAL folder routes via the app test client (open mode →
loopback synthetic admin) with the folder store redirected to a temp file, and
captures the frame the route publishes (monkeypatching
``lib.agent_core.push.push_event``). It proves each of the four mutating ops
(create / update / delete / reorder) emits exactly one ``folders_changed``
frame, and that DELETE carries ``deletedFolderId`` so every device can unassign
its conversations off the removed folder.

NEUTER: stub ``_notify_folders_changed`` on the route module to a no-op and
assert no frame is captured — proving the emit call (not the JSON write) is what
carries the signal.

Standalone:
    TOFU_DB_BACKEND=sqlite TOFU_DB_PATH=/tmp/folders_emit.db \
        python3 tests/test_folders_notify_emit.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def folder_store(tmp_path, monkeypatch):
    """Redirect the folders.json store to a temp file for the test."""
    import routes.api_v1.folders as folders_mod
    p = str(tmp_path / 'folders.json')
    monkeypatch.setattr(folders_mod, '_FOLDERS_PATH', p)
    return folders_mod


@pytest.fixture
def captured(monkeypatch):
    """Capture every push_event(channel, task_id, payload) the seam emits."""
    frames = []

    def _capture(channel, task_id, payload, *, user_id):
        assert payload['userId'] == user_id
        frames.append({
            'channel': channel,
            'taskId': task_id,
            'payload': payload,
            'ownerUserId': user_id,
        })

    import lib.agent_core.push as push_mod
    monkeypatch.setattr(push_mod, 'push_event', _capture)
    return frames


def _folder_frames(frames):
    return [f for f in frames
            if f['channel'] == 'notify'
            and f['payload'].get('type') == 'folders_changed']


# ─────────────────────────────────────────────────────────────────────────────
#  Emit coverage — one frame per mutating op
# ─────────────────────────────────────────────────────────────────────────────

def test_create_folder_emits_frame(flask_client, folder_store, captured):
    r = flask_client.post('/api/v1/folders', json={'name': 'Work', 'color': '#0af'})
    assert r.status_code == 201, r.get_data(as_text=True)
    frames = _folder_frames(captured)
    assert len(frames) == 1, f'create must emit exactly one frame, got {frames}'
    assert 'deletedFolderId' not in frames[0]['payload']
    assert frames[0]['payload'].get('userId') == 1
    assert frames[0]['ownerUserId'] == 1


def test_update_folder_emits_frame(flask_client, folder_store, captured):
    fid = flask_client.post('/api/v1/folders', json={'name': 'A'}).get_json()['id']
    captured.clear()
    r = flask_client.put(f'/api/v1/folders/{fid}', json={'name': 'A2', 'color': '#f00'})
    assert r.status_code == 200, r.get_data(as_text=True)
    frames = _folder_frames(captured)
    assert len(frames) == 1, f'update must emit exactly one frame, got {frames}'


def test_update_missing_folder_emits_no_frame(flask_client, folder_store, captured):
    """A 404 (no folder mutated) must NOT emit — the signal tracks real changes."""
    r = flask_client.put('/api/v1/folders/does-not-exist', json={'name': 'X'})
    assert r.status_code == 404
    assert _folder_frames(captured) == [], 'a no-op 404 must emit no frame'


def test_delete_folder_emits_frame_with_deleted_id(flask_client, folder_store, captured):
    fid = flask_client.post('/api/v1/folders', json={'name': 'Trash me'}).get_json()['id']
    captured.clear()
    r = flask_client.delete(f'/api/v1/folders/{fid}')
    assert r.status_code == 200, r.get_data(as_text=True)
    frames = _folder_frames(captured)
    assert len(frames) == 1, f'delete must emit exactly one frame, got {frames}'
    # THE cross-device reconcile hook: the deleted id must ride the frame so a
    # second device can unassign its conversations off the removed folder.
    assert frames[0]['payload'].get('deletedFolderId') == fid


def test_reorder_folders_emits_frame(flask_client, folder_store, captured):
    a = flask_client.post('/api/v1/folders', json={'name': 'a'}).get_json()['id']
    b = flask_client.post('/api/v1/folders', json={'name': 'b'}).get_json()['id']
    captured.clear()
    r = flask_client.post('/api/v1/folders/reorder', json={'order': [b, a]})
    assert r.status_code == 200, r.get_data(as_text=True)
    frames = _folder_frames(captured)
    assert len(frames) == 1, f'reorder must emit exactly one frame, got {frames}'


# ─────────────────────────────────────────────────────────────────────────────
#  NEUTER — proves the emit call is load-bearing
# ─────────────────────────────────────────────────────────────────────────────

def test_NEUTER_no_emit_no_frame(flask_client, folder_store, captured, monkeypatch):
    """Stub _notify_folders_changed → the route still writes folders.json but no
    frame is captured. Restores nothing (monkeypatch auto-undoes)."""
    monkeypatch.setattr(folder_store, '_notify_folders_changed',
                        lambda **k: None)
    r = flask_client.post('/api/v1/folders', json={'name': 'Silent'})
    assert r.status_code == 201
    assert _folder_frames(captured) == [], 'neutered route must emit no frame'
    # ...and the folder was still persisted (write path intact).
    listing = flask_client.get('/api/v1/folders').get_json()['items']  # envelope
    assert any(f.get('name') == 'Silent' for f in listing)


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
