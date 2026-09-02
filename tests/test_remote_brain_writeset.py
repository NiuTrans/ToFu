"""tests/test_remote_brain_writeset.py — RWA P5:Project Brain write_set 集成.

docs/modules/remote_execution.md:远程根纳入 write_set 声明 ——
  * post/claim 时,若会话的项目是伪路径绑定(``remote:<agent>:<root>``),
    该 token 自动并入 epic 的 write_set(幂等去重);
  * 伪路径经既有 ``_paths_intersect`` 语义:同 token 冲突、不同根/不同
    agent/兄弟后缀(``app`` vs ``app2``)不冲突(`:` 分隔天然安全);
  * 效果:两会话绑定同一远程根 → 重叠 epic 被软降级不同时 dispatch;
    不同根不互斥。

Run:  pytest tests/test_remote_brain_writeset.py -m unit -v
"""

from __future__ import annotations

_AUDIT_SYNTHETIC_REPO_PATHS = {
    'lib/q.py', 'lib/x.py', 'lib/y.py', 'lib/z.py',
}

import os

import pytest

pytest_plugins = ('tests._chat_sidecar',)
pytestmark = [pytest.mark.unit, pytest.mark.usefixtures('chat_sidecar')]

TEST_OWNER_USER_ID = 1


@pytest.fixture(autouse=True)
def _clean():
    from tests._seed import clear_board, clear_events
    clear_board(_PROJ, user_id=TEST_OWNER_USER_ID)
    clear_events()
    try:
        yield
    finally:
        clear_board(_PROJ, user_id=TEST_OWNER_USER_ID)
        clear_events()


def _mk_conv(flask_app, conv_id, project_path=''):
    """Create a canonical conversation carrying the requested project path."""
    del flask_app
    from tests._seed import delete_conversation, seed_conversation
    delete_conversation(conv_id, user_id=TEST_OWNER_USER_ID)
    seed_conversation(
        conv_id,
        user_id=TEST_OWNER_USER_ID,
        title=conv_id,
        settings={'projectPath': project_path},
        created_at=1,
        updated_at=1,
    )


def _ws_of(flask_app, task_id):
    del flask_app
    from lib.conversations.project_board import read_board
    row = next(
        task for task in read_board(
            _PROJ, user_id=TEST_OWNER_USER_ID)['tasks']
        if task['id'] == task_id
    )
    return list(row.get('write_set') or [])


_PROJ = os.path.abspath('/tmp/rwa-brain-p5')
TOKEN = 'remote:agent-A:myapp'


# ═══════════════════════════════════════════════════════════
#  post:发 epic 时并入远程 token
# ═══════════════════════════════════════════════════════════

def test_post_merges_remote_token(flask_app):
    from lib.conversations.project_board import post_task
    _mk_conv(flask_app, 'convR', TOKEN)
    with flask_app.app_context():
        tid = post_task(
            _PROJ, 'convR', 'refactor the thing',
            user_id=TEST_OWNER_USER_ID,
        )['id']
    assert TOKEN in _ws_of(flask_app, tid)


def test_post_local_conv_unchanged(flask_app):
    from lib.conversations.project_board import post_task
    _mk_conv(flask_app, 'convL', '/srv/code/app')
    with flask_app.app_context():
        tid = post_task(_PROJ, 'convL', 'local work',
                        user_id=TEST_OWNER_USER_ID,
                        write_set=['lib/x.py'])['id']
    assert _ws_of(flask_app, tid) == ['lib/x.py']


def test_post_dedups_explicit_token(flask_app):
    from lib.conversations.project_board import post_task
    _mk_conv(flask_app, 'convR2', TOKEN)
    with flask_app.app_context():
        tid = post_task(_PROJ, 'convR2', 'already declared',
                        user_id=TEST_OWNER_USER_ID,
                        write_set=['lib/y.py', TOKEN])['id']
    assert _ws_of(flask_app, tid) == ['lib/y.py', TOKEN]


def test_post_missing_conv_no_crash(flask_app):
    from lib.conversations.project_board import post_task
    with flask_app.app_context():
        tid = post_task(
            _PROJ, 'ghost-conv', 'no conv row',
            user_id=TEST_OWNER_USER_ID,
        )['id']
    assert _ws_of(flask_app, tid) == []


# ═══════════════════════════════════════════════════════════
#  claim:认领时并入(claimed write_set 是 dispatch 降级的输入)
# ═══════════════════════════════════════════════════════════

@pytest.fixture
def _no_post_dispatch(monkeypatch):
    """post_task now auto-dispatches a startable epic (on_epic_posted), which
    CLAIMS it for the idle poster conv — a later claim_task by another conv
    then fails with {'ok': False, 'error': 'already_claimed', 'owner': <poster>}.
    These tests exercise claim-time write_set merging, not the post-time
    dispatch trigger, so neuter the trigger seam to keep the epic open."""
    import lib.conversations.project_dispatch as pd
    monkeypatch.setattr(pd, 'on_epic_posted', lambda *a, **k: 0)


def test_claim_merges_remote_token(flask_app, _no_post_dispatch):
    from lib.conversations.project_board import claim_task, post_task
    _mk_conv(flask_app, 'convPoster', '')
    _mk_conv(flask_app, 'convClaimer', TOKEN)
    with flask_app.app_context():
        tid = post_task(_PROJ, 'convPoster', 'clean epic',
                        user_id=TEST_OWNER_USER_ID,
                        write_set=['lib/z.py'])['id']
        r = claim_task(
            _PROJ, 'convClaimer', tid, user_id=TEST_OWNER_USER_ID,
        )
    assert r['ok'], f'claim_task failed: {r}'
    ws = _ws_of(flask_app, tid)
    assert 'lib/z.py' in ws and TOKEN in ws


def test_claim_local_conv_unchanged(flask_app, _no_post_dispatch):
    from lib.conversations.project_board import claim_task, post_task
    _mk_conv(flask_app, 'convP2', '')
    _mk_conv(flask_app, 'convL2', '/srv/code')
    with flask_app.app_context():
        tid = post_task(
            _PROJ, 'convP2', 'clean2', user_id=TEST_OWNER_USER_ID,
            write_set=['a.py'],
        )['id']
        r = claim_task(_PROJ, 'convL2', tid, user_id=TEST_OWNER_USER_ID)
    assert r['ok'], f'claim_task failed: {r}'
    assert _ws_of(flask_app, tid) == ['a.py']


# ═══════════════════════════════════════════════════════════
#  伪路径 intersect 语义
# ═══════════════════════════════════════════════════════════

@pytest.mark.parametrize('a,b,expect', [
    ('remote:agent-A:myapp', 'remote:agent-A:myapp', True),
    ('remote:agent-A:myapp', 'remote:agent-A:other', False),
    ('remote:agent-A:myapp', 'remote:agent-B:myapp', False),
    # 兄弟前缀不得误 containment(':' 分隔天然安全)
    ('remote:agent-A:app', 'remote:agent-A:app2', False),
    # 与服务器路径永不相交
    ('remote:agent-A:myapp', 'remote:agent-A:myapp/x.py', True),
    ('remote:agent-A:myapp', '/srv/code/app', False),
])
def test_paths_intersect_pseudo_semantics(a, b, expect):
    from lib.conversations.project_dispatch import _paths_intersect
    assert _paths_intersect(a, b) is expect


# ═══════════════════════════════════════════════════════════
#  dispatch 集成:同根降级、不同根不互斥
# ═══════════════════════════════════════════════════════════

def _setup_claimed_remote(flask_app):
    """cX(绑 TOKEN)认领 E1 → E1 write_set 带 TOKEN."""
    from lib.conversations.project_board import claim_task, post_task
    _mk_conv(flask_app, 'convX', TOKEN)
    with flask_app.app_context():
        e1 = post_task(
            _PROJ, 'convX', 'E1 claimed by remote conv',
            user_id=TEST_OWNER_USER_ID,
        )['id']
        assert claim_task(
            _PROJ, 'convX', e1, user_id=TEST_OWNER_USER_ID,
        )['ok']
    return e1


def test_dispatch_demotes_same_root_not_others(flask_app):
    from lib.conversations.project_board import post_task
    from lib.conversations.project_dispatch import select_dispatchable
    _setup_claimed_remote(flask_app)
    with flask_app.app_context():
        e_same = post_task(_PROJ, 'convP', 'same-root work',
                           user_id=TEST_OWNER_USER_ID,
                           write_set=[TOKEN])['id']
        e_other = post_task(_PROJ, 'convP', 'other-root work',
                            user_id=TEST_OWNER_USER_ID,
                            write_set=['remote:agent-A:other'])['id']
        e_local = post_task(_PROJ, 'convP', 'local work',
                            user_id=TEST_OWNER_USER_ID,
                            write_set=['lib/q.py'])['id']
        picks = select_dispatchable(_PROJ, user_id=TEST_OWNER_USER_ID)
    ids = [t['id'] for t in picks]
    # 同根 epic 降级到最后,但仍然可 dispatch(软语义)
    assert ids[-1] == e_same
    assert set(ids) == {e_same, e_other, e_local}
    assert ids.index(e_other) < ids.index(e_same)
    assert ids.index(e_local) < ids.index(e_same)
