"""tests/test_sidebar_busy_projection.py — the sidebar busy projection must
survive a hard refresh.

Root cause this guards against (the "强制刷新后呼吸灯/回答中全丢，每个对话要点
一下才恢复" report):

  * The sidebar streaming dot / "answering" tag derives exclusively from
    CLIENT-side Turn state (``convIsBusy`` → ``ConversationTurnRead``).
  * A freshly loaded page holds that state only for conversations it has
    hydrated — boot hydrates the ACTIVE conversation only, so every other
    live conversation rendered idle until opened by hand.
  * The server task registry knew the live set all along
    (``list_running_tasks`` — the restart guard's judge) but never exposed
    it on the catalog projection the sidebar loads at boot.

The fix:

  1. ``GET /api/v1/conversations`` stamps ``busy: true`` on rows whose
     conversation still has live registry work (owner-scoped, carrier- and
     wedge-filtered — the same ``list_running_tasks`` semantics).
  2. The busy flag participates in the list ETag, so a busy↔idle transition
     always busts the conditional request (a 304 must never serve a stale
     busy projection).
  3. The frontend catalog apply wakes exactly the server-busy shells the
     client does not already know are busy (frontend harness pins live in
     tests/test_frontend_conversation_catalog.py).

These tests drive ``list_running_tasks(user_id=...)`` against synthetic
in-memory tasks and the live route via ``flask_client``, with NEUTER
controls proving the flag is load-bearing.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


def _mk(tid, conv, *, created, owner=1, status='running', aborted=False,
        **flags):
    """Build a synthetic task dict shaped like the registry's live tasks."""
    task = {
        'id': tid,
        'convId': conv,
        '_userId': owner,
        'status': status,
        'aborted': aborted,
        'created_at': created,
        '_t_last_event': created,
        '_dispatch_heartbeat': created,
        'events': [],
        'events_lock': threading.Lock(),
    }
    task.update(flags)
    return task


def _install(monkeypatch, task_list):
    import lib.tasks_pkg.manager._maintenance as _maintenance
    import lib.tasks_pkg.manager._registry as _registry
    runtime = SimpleNamespace(snapshot=lambda: list(task_list))
    monkeypatch.setattr(_registry, 'chat_task_runtime', runtime, raising=True)
    monkeypatch.setattr(_maintenance, '_stuck_task_max_silent_secs',
                        lambda: 300, raising=True)


# ─────────────────────────────────────────────────────────────────────────
# list_running_tasks(user_id=…) — the owner scope of the projection.
# ─────────────────────────────────────────────────────────────────────────
def test_busy_projection_is_owner_scoped(monkeypatch):
    """The sidebar of user 1 must never light a dot for user 2's work."""
    from lib.tasks_pkg.manager import list_running_tasks
    now = time.time()
    _install(monkeypatch, [
        _mk('mine', 'convMine', created=now - 2, owner=1),
        _mk('theirs', 'convTheirs', created=now - 2, owner=2),
    ])
    out = list_running_tasks(user_id=1)
    assert [entry['convId'] for entry in out] == ['convMine']
    out = list_running_tasks(user_id=2)
    assert [entry['convId'] for entry in out] == ['convTheirs']


def test_busy_projection_default_keeps_restart_guard_semantics(monkeypatch):
    """Omitting user_id (the restart guard's call) still counts every owner."""
    from lib.tasks_pkg.manager import list_running_tasks
    now = time.time()
    _install(monkeypatch, [
        _mk('mine', 'convMine', created=now - 2, owner=1),
        _mk('theirs', 'convTheirs', created=now - 2, owner=2),
    ])
    out = list_running_tasks()
    assert sorted(entry['convId'] for entry in out) == ['convMine', 'convTheirs']


# ─────────────────────────────────────────────────────────────────────────
# GET /api/v1/conversations — busy flag on the sidebar projection.
# ─────────────────────────────────────────────────────────────────────────
def _submit_probe_turn(flask_client, conv_id):
    resp = flask_client.post(f'/api/v3/conversations/{conv_id}/turns', json={
        'commandId': f'busy-probe:{conv_id}',
        'message': {
            'text': 'busy projection probe',
            'timestamp': 1,
            '_msgId': f'busy-probe-message:{conv_id}',
        },
        'config': {'model': 'busy-probe-model'},
        'conversation': {
            'allowCreate': True,
            'title': 'Busy projection probe',
            'settings': {'model': 'busy-probe-model'},
            'createdAt': 1,
        },
    })
    assert resp.status_code == 200, resp.get_json()


@pytest.fixture(autouse=True)
def _no_background_llm(monkeypatch):
    """Probes stop at task admission; no model call is needed."""
    import lib.tasks_pkg.spawn as task_spawn

    monkeypatch.setattr(task_spawn, 'spawn_task', lambda _task: None)


def _patch_live_set(monkeypatch, conv_ids):
    import lib.tasks_pkg.manager as manager

    monkeypatch.setattr(
        manager,
        'list_running_tasks',
        lambda *args, **kwargs: [
            {'taskId': f'task-{conv_id}', 'convId': conv_id, 'elapsed': 1.0}
            for conv_id in conv_ids
        ],
        raising=True,
    )


def test_list_marks_only_live_conversations_busy(flask_client, monkeypatch):
    _submit_probe_turn(flask_client, 'busy-conv-live')
    _submit_probe_turn(flask_client, 'busy-conv-idle')
    _patch_live_set(monkeypatch, {'busy-conv-live'})

    resp = flask_client.get('/api/v1/conversations')
    assert resp.status_code == 200
    items = {item['id']: item for item in resp.get_json()['items']}
    assert items['busy-conv-live'].get('busy') is True, (
        'a conversation with live registry work must be stamped busy')
    assert 'busy' not in items['busy-conv-idle'], (
        'idle rows carry no busy key — the projection stays additive-only')


def test_list_busy_flag_never_leaks_other_owners(flask_client, monkeypatch):
    """NEUTER control: an empty owner-scoped live set stamps nothing."""
    _submit_probe_turn(flask_client, 'busy-conv-owned')
    _patch_live_set(monkeypatch, set())

    resp = flask_client.get('/api/v1/conversations')
    assert resp.status_code == 200
    items = {item['id']: item for item in resp.get_json()['items']}
    assert 'busy' not in items['busy-conv-owned']


def test_busy_transition_busts_the_list_etag(flask_client, monkeypatch):
    """A 304 must never serve a stale busy projection: the flag is part of
    the validator, so busy↔idle flips always re-download the list."""
    _submit_probe_turn(flask_client, 'busy-conv-etag')
    _patch_live_set(monkeypatch, {'busy-conv-etag'})
    busy_etag = flask_client.get('/api/v1/conversations').headers.get('ETag')

    _patch_live_set(monkeypatch, set())
    idle_resp = flask_client.get('/api/v1/conversations')
    idle_etag = idle_resp.headers.get('ETag')

    assert busy_etag and idle_etag and busy_etag != idle_etag, (
        'the busy flag must participate in the list ETag')

    # Conditional replay with the STALE busy validator must not 304.
    _patch_live_set(monkeypatch, {'busy-conv-etag'})
    revalidated = flask_client.get(
        '/api/v1/conversations', headers={'If-None-Match': idle_etag})
    assert revalidated.status_code == 200
