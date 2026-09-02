"""Project-brain dispatch provenance across board, queue, and turn storage.

The contract is intentionally end-to-end: every kickoff names its epic,
origin, route, and dispatch seam; the durable queue retains that record; and
the canonical conversation projection exposes it to the frontend together
with a stable message id and the post-append revision.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit
pytest_plugins = ('tests._chat_sidecar',)

TEST_OWNER_USER_ID = 1

_PROJECT_PATHS = (
    '/bp/1', '/bp/2', '/bp/3', '/bp/4', '/bp/6', '/bp/7', '/bp/ans',
    '/bp/d', '/bp/dep', '/bp/idle', '/bp/mid', '/bp/post', '/bp/q',
    '/bp/rev',
)
_CONVERSATION_IDS = ('cORIG', 'cPOSTER', 'cDRAIN', 'cMSGID', 'cREV', 'cP')
_TEST_TASK_IDS: set[str] = set()


@pytest.fixture(autouse=True)
def _clean(chat_sidecar, monkeypatch):
    from tests._seed import clear_board, clear_events, delete_conversation
    _clear_task_registry()
    clear_board(*_PROJECT_PATHS, user_id=TEST_OWNER_USER_ID)
    clear_events()
    for conversation_id in _CONVERSATION_IDS:
        delete_conversation(conversation_id, user_id=TEST_OWNER_USER_ID)
    monkeypatch.setattr('lib.agent_core.push.push_event', lambda *a, **k: None)
    try:
        yield
    finally:
        _clear_task_registry()
        clear_board(*_PROJECT_PATHS, user_id=TEST_OWNER_USER_ID)
        clear_events()
        for conversation_id in _CONVERSATION_IDS:
            delete_conversation(conversation_id, user_id=TEST_OWNER_USER_ID)


def _seed_conv(conv_id, title='Origin conv', project_path=''):
    from tests._seed import seed_conversation
    seed_conversation(
        conv_id,
        title=title,
        messages=[{'role': 'user', 'content': 'seed'}],
        settings={'projectPath': project_path, 'projectEnabled': True},
    )


def _mark_busy(conv_id, task_id='busytask0000001'):
    """Register a fake LIVE task so post_task's on_epic_posted seam DEFERS
    (busy target) — otherwise the epic is claimed+dispatched at post time and
    the explicit dispatch under test is refused. Clear with
    _clear_task_registry()."""
    from tests.support.chat_tasks import chat_task_fixture_guard as tasks_lock, chat_task_registry as tasks
    with tasks_lock:
        tasks[task_id] = {'id': task_id, 'convId': conv_id,
                          '_userId': TEST_OWNER_USER_ID,
                          'status': 'running', 'aborted': False,
                          'config': {}, 'toolRounds': []}
        _TEST_TASK_IDS.add(task_id)


def _clear_task_registry():
    try:
        from tests.support.chat_tasks import chat_task_fixture_guard as tasks_lock, chat_task_registry as tasks
        with tasks_lock:
            for task_id in list(_TEST_TASK_IDS):
                tasks.pop(task_id, None)
            _TEST_TASK_IDS.clear()
    except Exception:
        pass


def _stub_dispatch(monkeypatch):
    """Capture the event seam's dispatch decision without mutating the board."""
    import lib.conversations.project_dispatch as dispatch
    captured = {}

    def _fake(project_path, epic, target_conv_id, **kwargs):
        captured.update({
            'project_path': project_path,
            'epic': dict(epic),
            'target': target_conv_id,
            'user_id': kwargs['user_id'],
        })
        return {'ok': True, 'queueId': 'captured'}

    monkeypatch.setattr(dispatch, 'dispatch_epic', _fake)
    return captured


# ════════════════════════════════════════════════════════════════════
#  _brain_meta — derivation matrix
# ════════════════════════════════════════════════════════════════════

def test_meta_creator_route_with_resolved_title():
    """The common case: epic routed to ITS CREATOR — route='creator', the
    originator title resolved from the conversations table (the card shows a
    human title, never a bare id)."""
    from lib.conversations.project_dispatch import _brain_meta
    _seed_conv('cORIG', title='调研：订阅中继')
    epic = {'id': 'pt_meta1', 'title': 'the epic', 'created_by_conv': 'cORIG'}
    meta = _brain_meta(epic, 'cORIG', user_id=TEST_OWNER_USER_ID)
    assert meta['epicId'] == 'pt_meta1'
    assert meta['epicTitle'] == 'the epic'
    assert meta['originatorConv'] == 'cORIG'
    assert meta['originatorTitle'] == '调研：订阅中继'
    assert meta['route'] == 'creator'
    assert meta['method'] == 'heartbeat', 'no _via → the heartbeat default'
    assert meta['answered'] is False


def test_meta_migrated_route():
    """dispatch_target override pointing at a NON-creator target = the
    idle-sibling migration shape → route='migrated'."""
    from lib.conversations.project_dispatch import _brain_meta
    epic = {'id': 'pt_meta2', 'title': 'migrated epic',
            'created_by_conv': 'cDEAD', 'dispatch_target': 'cNEW'}
    meta = _brain_meta(epic, 'cNEW', user_id=TEST_OWNER_USER_ID)
    assert meta['route'] == 'migrated'
    assert meta['originatorConv'] == 'cDEAD', \
        'authorship is immutable — the card still credits the creator'


def test_meta_fallback_route():
    """Target is neither the creator nor a dispatch_target override (the
    on_epic_completed completing-conv fallback) → route='fallback'."""
    from lib.conversations.project_dispatch import _brain_meta
    epic = {'id': 'pt_meta3', 'title': 'orphan epic', 'created_by_conv': ''}
    assert _brain_meta(
        epic, 'cCOMPLETER', user_id=TEST_OWNER_USER_ID)['route'] == 'fallback'


def test_meta_via_tokens_and_unknown_fallback():
    """An explicit _via flows through verbatim; an unknown token degrades to
    the heartbeat default (fail-closed on a typo'd seam, never a raw leak)."""
    from lib.conversations.project_dispatch import _brain_meta
    epic = {'id': 'pt_meta4', 'title': 'e', 'created_by_conv': 'cA',
            '_via': 'posted'}
    assert _brain_meta(epic, 'cA', user_id=TEST_OWNER_USER_ID)['method'] == 'posted'
    epic2 = {'id': 'pt_meta5', 'title': 'e', 'created_by_conv': 'cA',
             '_via': 'pigeon'}
    assert _brain_meta(
        epic2, 'cA', user_id=TEST_OWNER_USER_ID)['method'] == 'heartbeat'


def test_meta_answered_flag_and_title_cap():
    """human_answer → answered=True (the card's green chip); a pathological
    title is display-capped in the meta while the kickoff text keeps it all."""
    from lib.conversations.project_dispatch import _brain_meta
    long_title = 'x' * 1000
    epic = {'id': 'pt_meta6', 'title': long_title, 'created_by_conv': 'cA',
            'human_answer': 'B — abort'}
    meta = _brain_meta(epic, 'cA', user_id=TEST_OWNER_USER_ID)
    assert meta['answered'] is True
    assert len(meta['epicTitle']) == 300


def test_meta_title_resolve_never_raises(monkeypatch):
    """A DB failure in the title lookup degrades to '' — the dispatch itself
    must never fail on a display-only field."""
    import lib.conversations.project_dispatch as pd
    monkeypatch.setattr(pd, '_resolve_conv_title', lambda c, **_: '')
    epic = {'id': 'pt_meta7', 'title': 'e', 'created_by_conv': 'cGONE'}
    assert pd._brain_meta(
        epic, 'cGONE', user_id=TEST_OWNER_USER_ID)['originatorTitle'] == ''


# ════════════════════════════════════════════════════════════════════
#  The queue payload + the PERSISTED turn carry the record
# ════════════════════════════════════════════════════════════════════

def test_real_enqueue_payload_carries_meta(flask_app):
    """End of the write path (no stubs): post a real epic, dispatch it, read
    the message_queue row — the payload JSON carries _brainEpic."""
    from lib.conversations.project_board import post_task
    from lib.conversations.project_dispatch import (
        dispatch_epic, select_dispatchable)
    from lib.storage import get_storage_client
    from lib.message_queue import KIND_WORKFLOW
    with flask_app.app_context():
        _seed_conv('cPOSTER', title='海报会话')
        _mark_busy('cPOSTER')
        epic_id = post_task('/bp/q', 'cPOSTER', 'payload epic', user_id=TEST_OWNER_USER_ID)['id']
        _clear_task_registry()
        epic = select_dispatchable('/bp/q', user_id=TEST_OWNER_USER_ID)[0]
        assert dispatch_epic('/bp/q', epic, 'cPOSTER', user_id=TEST_OWNER_USER_ID)['ok']
        rows = get_storage_client().query(
            'queue.list', {'conv_id': 'cPOSTER', 'user_id': TEST_OWNER_USER_ID})
        rows = [row for row in rows if row.get('kind') == KIND_WORKFLOW]
    assert len(rows) == 1
    meta = rows[0]['payload'].get('_brainEpic')
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
    import lib.tasks_pkg.spawn as task_spawn
    spawned = []
    monkeypatch.setattr(
        task_spawn, 'spawn_task', lambda task: spawned.append(task))
    with flask_app.app_context():
        _seed_conv('cDRAIN', title='排水会话', project_path='/bp/d')
        _mark_busy('cDRAIN')
        epic_id = post_task('/bp/d', 'cDRAIN', 'drain epic', user_id=TEST_OWNER_USER_ID)['id']
        _clear_task_registry()
        epic = select_dispatchable('/bp/d', user_id=TEST_OWNER_USER_ID)[0]
        assert dispatch_epic('/bp/d', epic, 'cDRAIN', user_id=TEST_OWNER_USER_ID)['ok']
        assert dispatch_next_queued('cDRAIN', user_id=TEST_OWNER_USER_ID), 'the drain must spawn a task'
        from tests._seed import conv_document
        msgs = conv_document('cDRAIN')['messages']
    assert len(spawned) == 1
    last_user = [m for m in msgs if m.get('role') == 'user'][-1]
    origin = last_user.get('origin') or {}
    meta = origin.get('brain')
    assert meta, 'the persisted turn must carry the provenance record'
    assert meta['epicId'] == epic_id
    assert meta['originatorTitle'] == '排水会话'
    assert origin.get('initiator') == 'brain'
    assert origin.get('boardTaskId') == epic_id


def test_persisted_turn_carries_server_minted_turn_id(flask_app, monkeypatch):
    """Every engine-built kickoff receives a stable canonical turn identity."""
    import uuid as _uuid
    from lib.conversations.project_board import post_task
    from lib.conversations.project_dispatch import (
        dispatch_epic, select_dispatchable)
    from lib.message_queue import dispatch_next_queued
    import lib.tasks_pkg.spawn as task_spawn
    monkeypatch.setattr(task_spawn, 'spawn_task', lambda task: None)
    with flask_app.app_context():
        _seed_conv('cMSGID', title='m', project_path='/bp/mid')
        _mark_busy('cMSGID')
        post_task('/bp/mid', 'cMSGID', 'msgid epic', user_id=TEST_OWNER_USER_ID)
        _clear_task_registry()
        epic = select_dispatchable('/bp/mid', user_id=TEST_OWNER_USER_ID)[0]
        assert dispatch_epic('/bp/mid', epic, 'cMSGID', user_id=TEST_OWNER_USER_ID)['ok']
        assert dispatch_next_queued('cMSGID', user_id=TEST_OWNER_USER_ID)
        from tests._seed import conv_document
        last_user = [m for m in conv_document('cMSGID')['messages']
                     if m.get('role') == 'user'][-1]
    minted = last_user.get('_turnId')
    assert minted, 'the persisted kickoff must carry a server-minted turn id'
    _uuid.UUID(minted)  # raises unless it is a well-formed uuid


def test_dispatch_notify_carries_content_rev(flask_app, monkeypatch):
    """The dispatch notification carries the authoritative post-append rev."""
    from lib.conversations.project_board import post_task
    from lib.conversations.project_dispatch import (
        dispatch_epic, select_dispatchable)
    from lib.message_queue import dispatch_next_queued
    import lib.tasks_pkg.spawn as task_spawn
    monkeypatch.setattr(task_spawn, 'spawn_task', lambda task: None)
    notified = []
    monkeypatch.setattr('lib.conversations.notify_conv_changed',
                        lambda conv_id, **kw: notified.append(kw))
    with flask_app.app_context():
        _seed_conv('cREV', title='r', project_path='/bp/rev')
        _mark_busy('cREV')
        post_task('/bp/rev', 'cREV', 'rev epic', user_id=TEST_OWNER_USER_ID)
        _clear_task_registry()
        epic = select_dispatchable('/bp/rev', user_id=TEST_OWNER_USER_ID)[0]
        assert dispatch_epic('/bp/rev', epic, 'cREV', user_id=TEST_OWNER_USER_ID)['ok']
        assert dispatch_next_queued('cREV', user_id=TEST_OWNER_USER_ID)
        from tests._seed import conv_document
        revision = conv_document('cREV')['metadata']['rev']
    assert notified, 'the dispatch must emit a conv_changed notify'
    last = notified[-1]
    assert last.get('rev') is not None and last['rev'] == revision, (
        f"notify carried rev={last.get('rev')!r} but the row's content rev "
        f"is {revision!r} — a metadata-only notify strands the injected "
        f"turn on every open tab")


# ════════════════════════════════════════════════════════════════════
#  Every event seam stamps its own _via
# ════════════════════════════════════════════════════════════════════

def test_seam_dependency_done(flask_app, monkeypatch):
    """on_epic_completed → the dependent's kickoff says method=dependency_done."""
    import lib.conversations.project_dispatch as pd
    from tests._seed import seed_board_task
    captured = _stub_dispatch(monkeypatch)
    with flask_app.app_context():
        dep = 'dependency-done'
        seed_board_task(
            dep, '/bp/dep', user_id=TEST_OWNER_USER_ID,
            title='dep', status='done', created_by_conv='cA')
        seed_board_task(
            'dependent-ready', '/bp/dep', user_id=TEST_OWNER_USER_ID,
            title='dependent', depends_on=[dep], created_by_conv='cA')
        assert pd.on_epic_completed('/bp/dep', 'cA', user_id=TEST_OWNER_USER_ID) == 1
    assert captured['epic']['_via'] == 'dependency_done'


def test_seam_answered(flask_app, monkeypatch):
    """on_epic_answered → method=answered (and the answered chip flag)."""
    from lib.conversations.project_board import answer_task, post_task
    from lib.conversations.project_board import block_task
    import lib.conversations.project_dispatch as pd
    captured = _stub_dispatch(monkeypatch)
    monkeypatch.setattr(pd, '_conv_has_live_task', lambda c, **_: False)
    monkeypatch.setattr(pd, '_epic_already_queued', lambda c, t, **_: False)
    with flask_app.app_context():
        tid = post_task('/bp/ans', 'cA', 'gated epic', user_id=TEST_OWNER_USER_ID)['id']
        block_task('/bp/ans', 'cA', tid, '[human-gated] pick one',
                   question='A or B?', options=[{'label': 'A'}, {'label': 'B'}], user_id=TEST_OWNER_USER_ID)
        answer_task('/bp/ans', 'human', tid, 'A', user_id=TEST_OWNER_USER_ID)
    meta = pd._brain_meta(
        captured['epic'], captured['target'], user_id=TEST_OWNER_USER_ID)
    assert meta['method'] == 'answered'
    assert meta['answered'] is True


def test_seam_posted(flask_app, monkeypatch):
    """on_epic_posted → method=posted when the epic can start immediately."""
    from lib.conversations.project_board import post_task
    import lib.conversations.project_dispatch as pd
    captured = _stub_dispatch(monkeypatch)
    monkeypatch.setattr(pd, '_conv_has_live_task', lambda c, **_: False)
    monkeypatch.setattr(pd, '_epic_already_queued', lambda c, t, **_: False)
    with flask_app.app_context():
        _seed_conv('cP', title='p')
        tid = post_task('/bp/post', 'cP', 'posted epic', user_id=TEST_OWNER_USER_ID)['id']
        assert pd.on_epic_posted('/bp/post', tid, user_id=TEST_OWNER_USER_ID) == 1
    assert captured['epic']['_via'] == 'posted'


def test_seam_conv_idle(flask_app, monkeypatch):
    """on_conv_idle → method=conv_idle."""
    from lib.conversations.project_board import post_task
    import lib.conversations.project_dispatch as pd
    captured = _stub_dispatch(monkeypatch)
    monkeypatch.setattr(pd, '_conv_has_live_task', lambda c, **_: False)
    with flask_app.app_context():
        post_task('/bp/idle', 'cIDLE', 'idle epic', user_id=TEST_OWNER_USER_ID)
        assert pd.on_conv_idle('/bp/idle', 'cIDLE', user_id=TEST_OWNER_USER_ID) == 1
    assert captured['epic']['_via'] == 'conv_idle'
