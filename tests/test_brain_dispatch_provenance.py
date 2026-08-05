"""tests/test_brain_dispatch_provenance.py — the kickoff carries its OWN
provenance (owner ask 2026-08-04).

Before this, a brain-dispatched kickoff bubble showed only the raw English
instruction wall — the human could not see WHICH epic it was, WHO posted it
(bare conv id at best), or HOW/WHY the brain routed it to this conversation.
``dispatch_epic`` now stamps a display-only ``_brainEpic`` record onto the
queue payload (creator conv + resolved title, dispatch seam ``method``,
routing reason ``route``, ``answered`` flag); ``message_queue`` propagates it
onto the persisted user turn, and the frontend renders the provenance card.

Pinned here:

  * ``_brain_meta`` derivation — route creator / migrated / fallback, method
    default + explicit ``_via`` (+ unknown-token fallback), answered flag,
    epic-title display cap, originator title resolution (never raises).
  * The queue payload carries ``_brainEpic`` (real enqueue_message).
  * The PERSISTED user message carries ``_brainEpic`` after a real drain
    (stubbed spawn only) — the frontend renders from the conv row.
  * Each event seam stamps its own ``_via`` (dependency_done / answered /
    posted / conv_idle); the sweep stays on the heartbeat default.

NC — the ``_brainEpic`` propagation line in message_queue is load-bearing:
with it removed the persisted turn has NO provenance record (the bug).
"""

from __future__ import annotations

import os
import time

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
_QUEUE_SRC = os.path.join(ROOT, 'lib', 'message_queue.py')


@pytest.fixture(scope='module', autouse=True)
def _ensure_schema(flask_app):
    from lib.database import init_db
    with flask_app.app_context():
        init_db()
    yield


@pytest.fixture(autouse=True)
def _clean(flask_app, monkeypatch):
    from lib.database import DOMAIN_CHAT, get_thread_db
    with flask_app.app_context():
        db = get_thread_db(DOMAIN_CHAT)
        for tbl in ('project_tasks', 'project_events', 'message_queue',
                    'conversations'):
            db.execute(f'DELETE FROM {tbl}')
        db.commit()
    monkeypatch.setattr('lib.agent_core.push.push_event', lambda *a, **k: None)
    yield


def _seed_conv(flask_app, conv_id, title='Origin conv', project_path=''):
    from lib.database import DOMAIN_CHAT, get_thread_db, json_dumps_pg
    with flask_app.app_context():
        db = get_thread_db(DOMAIN_CHAT)
        now = int(time.time() * 1000)
        db.execute(
            'INSERT INTO conversations (id, user_id, title, messages, '
            ' settings, created_at, updated_at, search_text) '
            'VALUES (?, 1, ?, ?, ?, ?, ?, ?)',
            (conv_id, title,
             json_dumps_pg([{'role': 'user', 'content': 'seed'}]),
             json_dumps_pg({'projectPath': project_path,
                            'projectEnabled': True}),
             now, now, 'seed'))
        db.commit()


def _mark_busy(conv_id, task_id='busytask0000001'):
    """Register a fake LIVE task so post_task's on_epic_posted seam DEFERS
    (busy target) — otherwise the epic is claimed+dispatched at post time and
    the explicit dispatch under test is refused. Clear with
    _clear_task_registry()."""
    from lib.tasks_pkg.manager import tasks, tasks_lock
    with tasks_lock:
        tasks[task_id] = {'id': task_id, 'convId': conv_id,
                          'status': 'running', 'aborted': False,
                          'config': {}, 'toolRounds': []}


def _clear_task_registry():
    try:
        from lib.tasks_pkg.manager import tasks, tasks_lock
        with tasks_lock:
            tasks.clear()
    except Exception:
        pass


def _stub_claim_enqueue(monkeypatch):
    """dispatch_epic without a board row / queue write — capture the payload."""
    captured = {}
    monkeypatch.setattr('lib.conversations.project_board.claim_task',
                        lambda *a, **k: {'ok': True})

    def _fake_enqueue(conv, payload, cfg, kind=None):
        captured['conv'] = conv
        captured['payload'] = payload
        return {'ok': True, 'queueId': 'q1'}
    monkeypatch.setattr('lib.message_queue.enqueue_message', _fake_enqueue)
    return captured


# ════════════════════════════════════════════════════════════════════
#  _brain_meta — derivation matrix
# ════════════════════════════════════════════════════════════════════

def test_meta_creator_route_with_resolved_title(flask_app, monkeypatch):
    """The common case: epic routed to ITS CREATOR — route='creator', the
    originator title resolved from the conversations table (the card shows a
    human title, never a bare id)."""
    from lib.conversations.project_dispatch import dispatch_epic
    _seed_conv(flask_app, 'cORIG', title='调研：订阅中继')
    captured = _stub_claim_enqueue(monkeypatch)
    epic = {'id': 'pt_meta1', 'title': 'the epic', 'created_by_conv': 'cORIG'}
    with flask_app.app_context():
        res = dispatch_epic('/bp/1', epic, 'cORIG')
    assert res['ok']
    meta = captured['payload']['_brainEpic']
    assert meta['epicId'] == 'pt_meta1'
    assert meta['epicTitle'] == 'the epic'
    assert meta['originatorConv'] == 'cORIG'
    assert meta['originatorTitle'] == '调研：订阅中继'
    assert meta['route'] == 'creator'
    assert meta['method'] == 'heartbeat', 'no _via → the heartbeat default'
    assert meta['answered'] is False


def test_meta_migrated_route(monkeypatch):
    """dispatch_target override pointing at a NON-creator target = the
    idle-sibling migration shape → route='migrated'."""
    from lib.conversations.project_dispatch import dispatch_epic
    captured = _stub_claim_enqueue(monkeypatch)
    epic = {'id': 'pt_meta2', 'title': 'migrated epic',
            'created_by_conv': 'cDEAD', 'dispatch_target': 'cNEW'}
    res = dispatch_epic('/bp/2', epic, 'cNEW')
    assert res['ok']
    meta = captured['payload']['_brainEpic']
    assert meta['route'] == 'migrated'
    assert meta['originatorConv'] == 'cDEAD', \
        'authorship is immutable — the card still credits the creator'


def test_meta_fallback_route(monkeypatch):
    """Target is neither the creator nor a dispatch_target override (the
    on_epic_completed completing-conv fallback) → route='fallback'."""
    from lib.conversations.project_dispatch import dispatch_epic
    captured = _stub_claim_enqueue(monkeypatch)
    epic = {'id': 'pt_meta3', 'title': 'orphan epic', 'created_by_conv': ''}
    res = dispatch_epic('/bp/3', epic, 'cCOMPLETER')
    assert res['ok']
    assert captured['payload']['_brainEpic']['route'] == 'fallback'


def test_meta_via_tokens_and_unknown_fallback(monkeypatch):
    """An explicit _via flows through verbatim; an unknown token degrades to
    the heartbeat default (fail-closed on a typo'd seam, never a raw leak)."""
    from lib.conversations.project_dispatch import dispatch_epic
    captured = _stub_claim_enqueue(monkeypatch)
    epic = {'id': 'pt_meta4', 'title': 'e', 'created_by_conv': 'cA',
            '_via': 'posted'}
    assert dispatch_epic('/bp/4', epic, 'cA')['ok']
    assert captured['payload']['_brainEpic']['method'] == 'posted'
    epic2 = {'id': 'pt_meta5', 'title': 'e', 'created_by_conv': 'cA',
             '_via': 'pigeon'}
    assert dispatch_epic('/bp/4', epic2, 'cA')['ok']
    assert captured['payload']['_brainEpic']['method'] == 'heartbeat'


def test_meta_answered_flag_and_title_cap(monkeypatch):
    """human_answer → answered=True (the card's green chip); a pathological
    title is display-capped in the meta while the kickoff text keeps it all."""
    from lib.conversations.project_dispatch import dispatch_epic
    captured = _stub_claim_enqueue(monkeypatch)
    long_title = 'x' * 1000
    epic = {'id': 'pt_meta6', 'title': long_title, 'created_by_conv': 'cA',
            'human_answer': 'B — abort'}
    assert dispatch_epic('/bp/6', epic, 'cA')['ok']
    meta = captured['payload']['_brainEpic']
    assert meta['answered'] is True
    assert len(meta['epicTitle']) == 300
    assert long_title in captured['payload']['text'], \
        'the kickoff text (what the model reads) keeps the FULL title'


def test_meta_title_resolve_never_raises(monkeypatch):
    """A DB failure in the title lookup degrades to '' — the dispatch itself
    must never fail on a display-only field."""
    import lib.conversations.project_dispatch as pd
    monkeypatch.setattr(pd, '_resolve_conv_title', lambda c: '')
    captured = _stub_claim_enqueue(monkeypatch)
    epic = {'id': 'pt_meta7', 'title': 'e', 'created_by_conv': 'cGONE'}
    assert pd.dispatch_epic('/bp/7', epic, 'cGONE')['ok']
    assert captured['payload']['_brainEpic']['originatorTitle'] == ''


# ════════════════════════════════════════════════════════════════════
#  The queue payload + the PERSISTED turn carry the record
# ════════════════════════════════════════════════════════════════════

def test_real_enqueue_payload_carries_meta(flask_app):
    """End of the write path (no stubs): post a real epic, dispatch it, read
    the message_queue row — the payload JSON carries _brainEpic."""
    import json as _json
    from lib.conversations.project_board import post_task
    from lib.conversations.project_dispatch import (
        dispatch_epic, select_dispatchable)
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.message_queue import KIND_WORKFLOW
    with flask_app.app_context():
        _seed_conv(flask_app, 'cPOSTER', title='海报会话')
        _mark_busy('cPOSTER')
        epic_id = post_task('/bp/q', 'cPOSTER', 'payload epic')['id']
        _clear_task_registry()
        epic = select_dispatchable('/bp/q')[0]
        assert dispatch_epic('/bp/q', epic, 'cPOSTER')['ok']
        db = get_thread_db(DOMAIN_CHAT)
        rows = db.execute(
            'SELECT payload FROM message_queue WHERE conv_id=? AND kind=?',
            ('cPOSTER', KIND_WORKFLOW)).fetchall()
    assert len(rows) == 1
    meta = _json.loads(rows[0]['payload']).get('_brainEpic')
    assert meta and meta['epicId'] == epic_id
    assert meta['originatorTitle'] == '海报会话'
    assert meta['route'] == 'creator' and meta['method'] == 'heartbeat'


def test_persisted_turn_carries_meta(flask_app, monkeypatch):
    """The frontend renders from the PERSISTED conversation row — prove the
    record survives the real drain (dispatch_next_queued; spawn stubbed)."""
    from lib.conversations.project_board import post_task
    from lib.conversations.project_dispatch import (
        dispatch_epic, select_dispatchable)
    from lib.message_queue import dispatch_next_queued
    import lib.tasks_pkg as tp
    spawned = []
    monkeypatch.setattr(tp, 'spawn_task', lambda task: spawned.append(task))
    with flask_app.app_context():
        _seed_conv(flask_app, 'cDRAIN', title='排水会话', project_path='/bp/d')
        _mark_busy('cDRAIN')
        epic_id = post_task('/bp/d', 'cDRAIN', 'drain epic')['id']
        _clear_task_registry()
        epic = select_dispatchable('/bp/d')[0]
        assert dispatch_epic('/bp/d', epic, 'cDRAIN')['ok']
        assert dispatch_next_queued('cDRAIN'), 'the drain must spawn a task'
        import json as _json
        from lib.database import DOMAIN_CHAT, get_thread_db
        row = get_thread_db(DOMAIN_CHAT).execute(
            'SELECT messages FROM conversations WHERE id=?', ('cDRAIN',),
        ).fetchone()
        msgs = _json.loads(row['messages'])
    assert len(spawned) == 1
    last_user = [m for m in msgs if m.get('role') == 'user'][-1]
    meta = last_user.get('_brainEpic')
    assert meta, 'the persisted turn must carry the provenance record'
    assert meta['epicId'] == epic_id
    assert meta['originatorTitle'] == '排水会话'
    assert last_user.get('_brainDispatch') is True
    assert last_user.get('_boardTaskId') == epic_id


def test_persisted_turn_carries_server_minted_msgid(flask_app, monkeypatch):
    """Engine-built turns arrive with no client-minted _msgId — the drain must
    stamp one so the persisted kickoff carries the stable identity every
    reconciliation lane keys on (the windowed-tail anchor, the frontend's
    injected-turn dedup). Its absence was one of the reasons the kickoff in
    conv msebjymx5b4a25 (2026-08-05) stayed invisible mid-task."""
    import uuid as _uuid
    from lib.conversations.project_board import post_task
    from lib.conversations.project_dispatch import (
        dispatch_epic, select_dispatchable)
    from lib.message_queue import dispatch_next_queued
    import lib.tasks_pkg as tp
    monkeypatch.setattr(tp, 'spawn_task', lambda task: None)
    with flask_app.app_context():
        _seed_conv(flask_app, 'cMSGID', title='m', project_path='/bp/mid')
        _mark_busy('cMSGID')
        post_task('/bp/mid', 'cMSGID', 'msgid epic')
        _clear_task_registry()
        epic = select_dispatchable('/bp/mid')[0]
        assert dispatch_epic('/bp/mid', epic, 'cMSGID')['ok']
        assert dispatch_next_queued('cMSGID')
        import json as _json
        from lib.database import DOMAIN_CHAT, get_thread_db
        row = get_thread_db(DOMAIN_CHAT).execute(
            'SELECT messages FROM conversations WHERE id=?', ('cMSGID',),
        ).fetchone()
        last_user = [m for m in _json.loads(row['messages'])
                     if m.get('role') == 'user'][-1]
    minted = last_user.get('_msgId')
    assert minted, 'the persisted kickoff must carry a server-minted _msgId'
    _uuid.UUID(minted)  # raises unless it is a well-formed uuid


def test_dispatch_notify_carries_content_rev(flask_app, monkeypatch):
    """The dispatch notify must carry the REAL post-append content rev — the
    DB trigger bumps rev on the messages append, so reporting rev=None
    ("metadata-only") was a lie that told every open tab "no body change":
    no refetch ever fired and the injected turn stayed invisible for the
    whole task (conv msebjymx5b4a25, 2026-08-05)."""
    from lib.conversations.project_board import post_task
    from lib.conversations.project_dispatch import (
        dispatch_epic, select_dispatchable)
    from lib.message_queue import dispatch_next_queued
    import lib.tasks_pkg as tp
    monkeypatch.setattr(tp, 'spawn_task', lambda task: None)
    notified = []
    monkeypatch.setattr('lib.conversations.notify_conv_changed',
                        lambda conv_id, **kw: notified.append(kw))
    with flask_app.app_context():
        _seed_conv(flask_app, 'cREV', title='r', project_path='/bp/rev')
        _mark_busy('cREV')
        post_task('/bp/rev', 'cREV', 'rev epic')
        _clear_task_registry()
        epic = select_dispatchable('/bp/rev')[0]
        assert dispatch_epic('/bp/rev', epic, 'cREV')['ok']
        assert dispatch_next_queued('cREV')
        from lib.database import DOMAIN_CHAT, get_thread_db
        row = get_thread_db(DOMAIN_CHAT).execute(
            'SELECT rev FROM conversations WHERE id=?', ('cREV',)).fetchone()
    assert notified, 'the dispatch must emit a conv_changed notify'
    last = notified[-1]
    assert last.get('rev') is not None and last['rev'] == row['rev'], (
        f"notify carried rev={last.get('rev')!r} but the row's content rev "
        f"is {row['rev']!r} — a metadata-only notify strands the injected "
        f"turn on every open tab")


# ════════════════════════════════════════════════════════════════════
#  Every event seam stamps its own _via
# ════════════════════════════════════════════════════════════════════
# ════════════════════════════════════════════════════════════════════
#  Every event seam stamps its own _via
# ════════════════════════════════════════════════════════════════════

def test_seam_dependency_done(flask_app, monkeypatch):
    """on_epic_completed → the dependent's kickoff says method=dependency_done."""
    from lib.conversations.project_board import post_task
    import lib.conversations.project_dispatch as pd
    from lib.database import DOMAIN_CHAT, get_thread_db
    captured = _stub_claim_enqueue(monkeypatch)
    with flask_app.app_context():
        dep = post_task('/bp/dep', 'cA', 'dep')['id']
        post_task('/bp/dep', 'cA', 'dependent', depends_on=[dep])
        db = get_thread_db(DOMAIN_CHAT)
        db.execute("UPDATE project_tasks SET status='done' WHERE id=?", (dep,))
        db.commit()
        assert pd.on_epic_completed('/bp/dep', 'cA') == 1
    assert captured['payload']['_brainEpic']['method'] == 'dependency_done'


def test_seam_answered(flask_app, monkeypatch):
    """on_epic_answered → method=answered (and the answered chip flag)."""
    from lib.conversations.project_board import answer_task, post_task
    from lib.conversations.project_board import block_task
    import lib.conversations.project_dispatch as pd
    captured = _stub_claim_enqueue(monkeypatch)
    monkeypatch.setattr(pd, '_conv_has_live_task', lambda c: False)
    monkeypatch.setattr(pd, '_epic_already_queued', lambda c, t: False)
    with flask_app.app_context():
        tid = post_task('/bp/ans', 'cA', 'gated epic')['id']
        block_task('/bp/ans', 'cA', tid, '[human-gated] pick one',
                   question='A or B?', options=[{'label': 'A'}, {'label': 'B'}])
        answer_task('/bp/ans', 'human', tid, 'A')
    meta = captured['payload']['_brainEpic']
    assert meta['method'] == 'answered'
    assert meta['answered'] is True


def test_seam_posted(flask_app, monkeypatch):
    """on_epic_posted → method=posted when the epic can start immediately."""
    from lib.conversations.project_board import post_task
    import lib.conversations.project_dispatch as pd
    captured = _stub_claim_enqueue(monkeypatch)
    monkeypatch.setattr(pd, '_conv_has_live_task', lambda c: False)
    monkeypatch.setattr(pd, '_epic_already_queued', lambda c, t: False)
    with flask_app.app_context():
        _seed_conv(flask_app, 'cP', title='p')
        tid = post_task('/bp/post', 'cP', 'posted epic')['id']
        assert pd.on_epic_posted('/bp/post', tid) == 1
    assert captured['payload']['_brainEpic']['method'] == 'posted'


def test_seam_conv_idle(flask_app, monkeypatch):
    """on_conv_idle → method=conv_idle."""
    from lib.conversations.project_board import post_task
    import lib.conversations.project_dispatch as pd
    captured = _stub_claim_enqueue(monkeypatch)
    monkeypatch.setattr(pd, '_conv_has_live_task', lambda c: False)
    with flask_app.app_context():
        post_task('/bp/idle', 'cIDLE', 'idle epic')
        assert pd.on_conv_idle('/bp/idle', 'cIDLE') == 1
    assert captured['payload']['_brainEpic']['method'] == 'conv_idle'


# ════════════════════════════════════════════════════════════════════
#  NC — the message_queue propagation line is load-bearing
# ════════════════════════════════════════════════════════════════════

from tests._nc_harness import patch_restore as _patch_restore  # noqa: E402


def test_NC_meta_propagation_is_load_bearing(flask_app, monkeypatch):
    """NC: drop the `_brainEpic` propagation in message_queue → the persisted
    turn has NO provenance record (the pre-fix bubble: raw wall, no card).
    The POSITIVE half (record present) is test_persisted_turn_carries_meta."""
    def run():
        from lib.conversations.project_board import post_task
        from lib.conversations.project_dispatch import (
            dispatch_epic, select_dispatchable)
        from lib.message_queue import dispatch_next_queued
        import lib.tasks_pkg as tp
        monkeypatch.setattr(tp, 'spawn_task', lambda task: None)
        proj = '/bp/nc'
        with flask_app.app_context():
            _seed_conv(flask_app, 'cNC', title='nc', project_path=proj)
            _mark_busy('cNC')
            post_task(proj, 'cNC', 'nc epic')
            _clear_task_registry()
            epic = select_dispatchable(proj)[0]
            assert dispatch_epic(proj, epic, 'cNC')['ok']
            assert dispatch_next_queued('cNC')
            import json as _json
            from lib.database import DOMAIN_CHAT, get_thread_db
            row = get_thread_db(DOMAIN_CHAT).execute(
                'SELECT messages FROM conversations WHERE id=?', ('cNC',),
            ).fetchone()
            last_user = [m for m in _json.loads(row['messages'])
                         if m.get('role') == 'user'][-1]
        assert '_brainEpic' not in last_user, \
            'NC: without propagation the persisted turn carries NO provenance'

    _patch_restore(
        _QUEUE_SRC,
        "                if payload.get('_brainEpic'):\n"
        "                    user_msg['_brainEpic'] = payload['_brainEpic']",
        "                pass  # NC (propagation dropped)",
        run,
    )
