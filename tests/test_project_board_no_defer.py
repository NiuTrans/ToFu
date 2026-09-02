"""tests/test_project_board_no_defer.py — the shelving (``deferred`` / park)
mechanism has been REMOVED at the source.

Operator problem this pins: a human would reopen a §10-gated epic ("no longer
shelve"), but an autonomous conversation re-called ``project_board_defer`` and
re-parked it — silently overriding the human decision, so the epic looked
permanently shelved. Occam's-razor fix: delete the park state entirely. The
board now has exactly three epic states (open / claimed / done); every open
epic is pushed forward at full speed, never held pending a human decision.

What this guards:
  • ``defer_task`` no longer exists on the board module, and
    ``project_board_defer`` is not routed through ``execute_board_tool``.
  • The agent-facing tool schema does NOT advertise ``project_board_defer``
    (an agent can no longer re-shelve an epic — the root cause).
  • The ``/api/v1/project/board/defer`` HTTP route is gone (404).
  • A retired ``deferred`` row is revived to ``open`` and dispatchable again.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

TEST_OWNER_USER_ID = 1
pytest_plugins = ('tests._chat_sidecar',)


@pytest.fixture(autouse=True)
def _sidecar_authority(chat_sidecar):
    return chat_sidecar


@pytest.fixture(autouse=True)
def _stub_push(monkeypatch):
    monkeypatch.setattr('lib.agent_core.push.push_event', lambda *a, **k: None)


# ════════════════════════════════════════════════════════════════════
#  The park primitive + agent tool are GONE
# ════════════════════════════════════════════════════════════════════

def test_defer_task_removed_from_board_module():
    import lib.conversations.project_board as pb
    assert not hasattr(pb, 'defer_task'), \
        'defer_task must be removed — the shelving mechanism no longer exists'
    assert 'defer_task' not in pb.__all__


def test_board_tool_defer_unknown(flask_app):
    """project_board_defer routed through execute_board_tool must be an
    unknown tool now (the branch was deleted)."""
    from lib.conversations.project_board import execute_board_tool, post_task
    with flask_app.app_context():
        tid = post_task('/nd/tool', 'cA', 'epic', user_id=TEST_OWNER_USER_ID)['id']
        out = execute_board_tool(
            'project_board_defer', {'task_id': tid},
            current_conv_id='cAGENT', project_path='/nd/tool', user_id=TEST_OWNER_USER_ID)
    assert "Unknown board tool 'project_board_defer'" in out


def test_defer_tool_not_in_schema_or_name_set():
    """The model-facing tool schema must NOT advertise project_board_defer —
    this is the root-cause guard: an agent can no longer re-shelve an epic a
    human just reopened."""
    from lib.tools.conversation import BOARD_TOOLS, BOARD_TOOL_NAMES
    names = [t['function']['name'] for t in BOARD_TOOLS]
    assert 'project_board_defer' not in names
    assert 'project_board_defer' not in BOARD_TOOL_NAMES


def test_registry_does_not_route_defer():
    """The real dispatch table must not resolve project_board_defer to a
    handler (it fell out of BOARD_TOOL_NAMES)."""
    from lib.tasks_pkg.executor import tool_registry
    assert tool_registry.lookup('project_board_defer', {}) is None


# ════════════════════════════════════════════════════════════════════
#  The board renders no Parked lane; every open epic stays dispatchable
# ════════════════════════════════════════════════════════════════════

def test_render_board_block_has_no_parked_lane(flask_app):
    from lib.conversations.project_board import post_task, render_board_block
    with flask_app.app_context():
        post_task('/nd/render', 'cA', 'live epic', user_id=TEST_OWNER_USER_ID)
        block = render_board_block('/nd/render', current_conv_id='cREADER', user_id=TEST_OWNER_USER_ID)
    assert 'Parked' not in block and 'deferred' not in block
    assert 'Open (unclaimed' in block


def test_open_epic_is_dispatchable(flask_app):
    from lib.conversations.project_board import post_task
    from lib.conversations.project_dispatch import select_dispatchable
    with flask_app.app_context():
        tid = post_task('/nd/disp', 'cPOSTER', 'epic', user_id=TEST_OWNER_USER_ID)['id']
        cands = [c['id'] for c in select_dispatchable('/nd/disp', user_id=TEST_OWNER_USER_ID)]
    assert tid in cands, 'an open epic must be dispatchable (pushed forward)'


# ════════════════════════════════════════════════════════════════════
#  A legacy ``deferred`` row is revived to open (human "no longer shelve")
# ════════════════════════════════════════════════════════════════════

def test_legacy_deferred_row_reopens_and_dispatches(flask_app):
    """A row left in the retired 'deferred' status (e.g. written by an old
    build before the migration ran) must NOT stay shelved: reopen_task revives
    it and it dispatches. Simulates the legacy row directly (defer_task gone)."""
    from lib.conversations.project_board import read_board, reopen_task
    from lib.conversations.project_dispatch import select_dispatchable
    from tests._seed import seed_board_task
    with flask_app.app_context():
        tid = 'pt_legacy_deferred'
        seed_board_task(
            tid, '/nd/legacy', user_id=TEST_OWNER_USER_ID,
            title='stale parked epic', status='deferred',
            created_by_conv='cA')
        res = reopen_task('/nd/legacy', 'cHUMAN', tid, user_id=TEST_OWNER_USER_ID)
        board = read_board('/nd/legacy', user_id=TEST_OWNER_USER_ID)
        cands = [c['id'] for c in select_dispatchable('/nd/legacy', user_id=TEST_OWNER_USER_ID)]
    assert res['ok'] and res['from'] == 'deferred'
    assert board['tasks'][0]['status'] == 'open'
    assert tid in cands


# ════════════════════════════════════════════════════════════════════
#  HTTP: the /board/defer route is gone
# ════════════════════════════════════════════════════════════════════

def test_defer_route_removed(flask_client):
    resp = flask_client.post('/api/v1/project/board/defer', json={
        'path': '/nd/http', 'taskId': 'pt_x', 'convId': 'cHUMAN'})
    assert resp.status_code == 404, \
        'the /board/defer route must no longer exist'
