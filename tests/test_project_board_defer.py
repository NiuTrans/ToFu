"""tests/test_project_board_defer.py — the ``deferred`` (parked) board status.

The operator problem this closes: a human-gated epic (``§10-GATED /
design-first``) cannot reach ``project_board_complete`` autonomously, so the
heartbeat sweep re-dispatches it every ~30 min and it oscillates
``open→claimed→lease-expires→open`` forever — real token waste, and the reason
it "looks consistently unclaimed". ``defer_task`` sets a terminal-ish
``deferred`` status that STOPS that cycle while keeping the epic visible.

Two load-bearing properties, each with a byte-reverting negative control:

  • (a) A ``deferred`` epic is EXCLUDED from ``select_dispatchable`` (so the
    sweep never re-dispatches it). The guard is the ``status != 'open'`` skip
    in ``select_dispatchable`` — NC-A byte-reverts it to a ``claimed``-only
    skip → the deferred epic leaks into the candidate set → the exclusion
    assertion FLIPS.
  • (b) ``_effective_status`` does NOT reclaim a ``deferred`` epic on lease
    expiry (its reclaim is specific to ``claimed``) → no oscillation. NC-B
    byte-reverts the reclaim condition to fire for ``deferred`` too → an
    expired-lease deferred epic reads ``open`` → the no-reclaim assertion
    FLIPS.

Un-park is the existing ``reopen_task`` (``deferred → open``) — the same human
lever that revives a done/claimed epic — so normal dispatch resumes.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
_BOARD_SRC = os.path.join(ROOT, 'lib', 'conversations', 'project_board.py')
_DISPATCH_SRC = os.path.join(ROOT, 'lib', 'conversations', 'project_dispatch.py')


@pytest.fixture(scope='module', autouse=True)
def _ensure_schema(flask_app):
    from lib.database import init_db
    with flask_app.app_context():
        init_db()
    yield


@pytest.fixture(autouse=True)
def _clean(flask_app):
    from lib.database import DOMAIN_CHAT, get_thread_db
    with flask_app.app_context():
        db = get_thread_db(DOMAIN_CHAT)
        db.execute('DELETE FROM project_tasks')
        db.execute('DELETE FROM project_events')
        db.execute('DELETE FROM message_queue')
        db.commit()
    yield


@pytest.fixture(autouse=True)
def _stub_push(monkeypatch):
    monkeypatch.setattr('lib.agent_core.push.push_event', lambda *a, **k: None)


def _feed_kinds(flask_app, project_path):
    from lib.conversations.project_feed import read_project_feed
    with flask_app.app_context():
        return [e['kind'] for e in read_project_feed(project_path, limit=500)['events']]


def _set_lease(flask_app, project_path, task_id, lease_ms):
    from lib.database import DOMAIN_CHAT, get_thread_db
    with flask_app.app_context():
        db = get_thread_db(DOMAIN_CHAT)
        db.execute('UPDATE project_tasks SET lease_expires_at=? WHERE id=? AND project_path=?',
                   (lease_ms, task_id, project_path))
        db.commit()


def _patch_restore(path, old, new, run):
    """Byte-revert a guard, run the neutered assertion, restore byte-identical."""
    with open(path, encoding='utf-8') as f:
        original = f.read()
    assert old in original, f'anchor not found in {path}'
    try:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(original.replace(old, new, 1))
        run()
    finally:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(original)
    with open(path, encoding='utf-8') as f:
        assert f.read() == original, 'source not restored byte-identical'


# ════════════════════════════════════════════════════════════════════
#  defer_task — behaviour
# ════════════════════════════════════════════════════════════════════

def test_defer_open_sets_deferred(flask_app):
    from lib.conversations.project_board import defer_task, post_task, read_board
    with flask_app.app_context():
        tid = post_task('/b/def1', 'cA', 'parked epic')['id']
        res = defer_task('/b/def1', 'cHUMAN', tid, 'awaiting infra decision')
        board = read_board('/b/def1')
    assert res['ok'] and res['from'] == 'open'
    t = board['tasks'][0]
    assert t['status'] == 'deferred'
    assert t['owner_conv_id'] == '' and t['lease_expires_at'] == 0
    assert 'note' in _feed_kinds(flask_app, '/b/def1')


def test_defer_claimed_clears_owner_and_lease(flask_app):
    """Parking a live-claimed epic drops the claim (owner + lease cleared)."""
    from lib.conversations.project_board import (
        claim_task, defer_task, post_task, read_board,
    )
    with flask_app.app_context():
        tid = post_task('/b/def2', 'cA', 'held epic')['id']
        claim_task('/b/def2', 'cOWNER', tid)
        res = defer_task('/b/def2', 'cHUMAN', tid)
        board = read_board('/b/def2')
    assert res['ok'] and res['from'] == 'claimed'
    t = board['tasks'][0]
    assert t['status'] == 'deferred'
    assert t['owner_conv_id'] == '' and t['lease_expires_at'] == 0


def test_defer_done_is_refused(flask_app):
    from lib.conversations.project_board import complete_task, defer_task, post_task
    with flask_app.app_context():
        tid = post_task('/b/def3', 'cA', 'finished')['id']
        complete_task('/b/def3', 'cA', tid)
        res = defer_task('/b/def3', 'cHUMAN', tid)
    assert res['ok'] is False and res['error'] == 'already_done'


def test_defer_twice_is_idempotent_refusal(flask_app):
    from lib.conversations.project_board import defer_task, post_task
    with flask_app.app_context():
        tid = post_task('/b/def4', 'cA', 'epic')['id']
        assert defer_task('/b/def4', 'cHUMAN', tid)['ok']
        res = defer_task('/b/def4', 'cHUMAN', tid)
    assert res['ok'] is False and res['error'] == 'already_deferred'


def test_deferred_counts_and_renders_in_own_lane(flask_app):
    """A deferred epic is visible on the board (distinct from done/open) in a
    dedicated 'Parked' lane that explains it is NOT auto-dispatched."""
    from lib.conversations.project_board import defer_task, post_task, render_board_block
    with flask_app.app_context():
        tid = post_task('/b/def5', 'cA', 'Epic X design-first')['id']
        defer_task('/b/def5', 'cHUMAN', tid)
        block = render_board_block('/b/def5', current_conv_id='cREADER')
    assert 'Parked (deferred' in block, 'deferred epic must render in a Parked lane'
    assert 'Epic X design-first' in block
    assert 'Open (unclaimed' not in block, 'a deferred epic must NOT appear as open'


# ════════════════════════════════════════════════════════════════════
#  Un-park: reopen_task takes deferred → open (normal dispatch resumes)
# ════════════════════════════════════════════════════════════════════

def test_reopen_unparks_deferred_to_open(flask_app):
    from lib.conversations.project_board import (
        defer_task, post_task, read_board, reopen_task,
    )
    from lib.conversations.project_dispatch import select_dispatchable
    with flask_app.app_context():
        tid = post_task('/b/def6', 'cPOSTER', 'epic')['id']
        defer_task('/b/def6', 'cHUMAN', tid)
        # while deferred, NOT dispatchable
        assert tid not in [c['id'] for c in select_dispatchable('/b/def6')]
        res = reopen_task('/b/def6', 'cHUMAN', tid)
        board = read_board('/b/def6')
        # after un-park, dispatchable again
        cands = [c['id'] for c in select_dispatchable('/b/def6')]
    assert res['ok'] and res['from'] == 'deferred'
    assert board['tasks'][0]['status'] == 'open'
    assert tid in cands, 'un-parked epic must be dispatchable again'


# ════════════════════════════════════════════════════════════════════
#  (a) select_dispatchable EXCLUDES deferred + NC-A (byte-revert the guard)
# ════════════════════════════════════════════════════════════════════

def test_deferred_excluded_from_dispatchable(flask_app):
    """The property the operator asked for: a parked epic is not re-selected
    by the heartbeat sweep."""
    from lib.conversations.project_board import defer_task, post_task
    from lib.conversations.project_dispatch import select_dispatchable
    with flask_app.app_context():
        tid = post_task('/nc_a', 'cA', 'parked epic')['id']
        defer_task('/nc_a', 'cHUMAN', tid)
        cands = [c['id'] for c in select_dispatchable('/nc_a')]
    assert tid not in cands, 'a deferred epic must be EXCLUDED from dispatch'


def test_NC_A_open_only_skip_is_load_bearing(flask_app):
    """NC-A: byte-revert the ``status != 'open'`` skip in select_dispatchable to
    a ``claimed``-only skip → a deferred epic leaks into the candidate set →
    the exclusion assertion flips (proves that exact line excludes deferred)."""
    import importlib

    def run():
        import lib.conversations.project_dispatch as pd
        importlib.reload(pd)
        from lib.conversations.project_board import defer_task, post_task
        with flask_app.app_context():
            from lib.database import DOMAIN_CHAT, get_thread_db
            get_thread_db(DOMAIN_CHAT).execute(
                "DELETE FROM project_tasks WHERE project_path='/nc_a2'")
            get_thread_db(DOMAIN_CHAT).commit()
            tid = post_task('/nc_a2', 'cA', 'parked epic')['id']
            defer_task('/nc_a2', 'cHUMAN', tid)
            cands = [c['id'] for c in pd.select_dispatchable('/nc_a2')]
        assert tid in cands, \
            'NC-A: with the open-only skip weakened to claimed-only, a ' \
            'deferred epic must LEAK into the candidate set'

    _patch_restore(
        _DISPATCH_SRC,
        "        if t['status'] != 'open':\n            continue",
        "        if t['status'] == 'claimed':  # NC-A (deferred no longer excluded)\n            continue",
        run,
    )
    import lib.conversations.project_dispatch as pd
    importlib.reload(pd)


# ════════════════════════════════════════════════════════════════════
#  (b) _effective_status does NOT reclaim deferred + NC-B (byte-revert)
# ════════════════════════════════════════════════════════════════════

def test_deferred_not_reclaimed_on_lease_expiry(flask_app):
    """The anti-oscillation property: a deferred epic with an EXPIRED lease
    still reads ``deferred`` (never flips back to ``open`` the way a claimed
    epic does). This is what stops the open↔claimed cycling."""
    from lib.conversations.project_board import defer_task, post_task, read_board
    with flask_app.app_context():
        tid = post_task('/nc_b', 'cA', 'parked epic')['id']
        defer_task('/nc_b', 'cHUMAN', tid)
    # Force a non-zero EXPIRED lease directly (defer clears it to 0; we set an
    # expired value to prove _effective_status still would not reclaim it).
    _set_lease(flask_app, '/nc_b', tid, 1)
    with flask_app.app_context():
        board = read_board('/nc_b')
    assert board['tasks'][0]['status'] == 'deferred', \
        'a deferred epic must NOT be reclaimed to open on lease expiry'
    assert board['open'] == 0 and board.get('deferred', 0) == 1


def test_effective_status_deferred_unit():
    from lib.conversations.project_board import _effective_status
    now = 1_000_000
    # deferred is passed through untouched regardless of lease.
    assert _effective_status('deferred', now - 5000, now) == 'deferred'
    assert _effective_status('deferred', 0, now) == 'deferred'
    # (claimed still reclaims — the specificity that spares deferred)
    assert _effective_status('claimed', now - 5000, now) == 'open'


def test_NC_B_reclaim_specificity_is_load_bearing(flask_app):
    """NC-B: byte-revert the reclaim condition so it fires for ``deferred`` too
    → an expired-lease deferred epic reads ``open`` → the no-reclaim assertion
    flips (proves the ``claimed``-specificity is what spares deferred)."""
    import importlib

    def run():
        import lib.conversations.project_board as pb
        importlib.reload(pb)
        with flask_app.app_context():
            from lib.database import DOMAIN_CHAT, get_thread_db
            get_thread_db(DOMAIN_CHAT).execute(
                "DELETE FROM project_tasks WHERE project_path='/nc_b2'")
            get_thread_db(DOMAIN_CHAT).commit()
            tid = pb.post_task('/nc_b2', 'cA', 'parked epic')['id']
            pb.defer_task('/nc_b2', 'cHUMAN', tid)
            get_thread_db(DOMAIN_CHAT).execute(
                "UPDATE project_tasks SET lease_expires_at=1 WHERE id=?", (tid,))
            get_thread_db(DOMAIN_CHAT).commit()
            board = pb.read_board('/nc_b2')
        assert board['tasks'][0]['status'] == 'open', \
            'NC-B: with the reclaim widened to deferred, an expired-lease ' \
            'deferred epic must read open (reproduces the oscillation)'

    _patch_restore(
        _BOARD_SRC,
        "    if stored_status == 'claimed' and lease_expires_at and lease_expires_at <= now_ms:\n        return 'open'\n    return stored_status",
        "    if stored_status in ('claimed', 'deferred') and lease_expires_at and lease_expires_at <= now_ms:  # NC-B\n        return 'open'\n    return stored_status",
        run,
    )
    import lib.conversations.project_board as pb
    importlib.reload(pb)


# ════════════════════════════════════════════════════════════════════
#  Tool surface: project_board_defer through execute_board_tool
# ════════════════════════════════════════════════════════════════════

def test_execute_board_tool_defer(flask_app):
    from lib.conversations.project_board import (
        execute_board_tool, post_task, read_board,
    )
    with flask_app.app_context():
        tid = post_task('/b/tool', 'cA', 'epic')['id']
        out = execute_board_tool(
            'project_board_defer', {'task_id': tid, 'reason': 'gated'},
            current_conv_id='cAGENT', project_path='/b/tool')
        board = read_board('/b/tool')
    assert 'Parked (deferred)' in out
    assert board['tasks'][0]['status'] == 'deferred'


# ════════════════════════════════════════════════════════════════════
#  AGENT REACHABILITY — the tool must be invocable end-to-end, not just a
#  handler branch. Criterion #1 ("a conversation can park an epic") requires
#  the schema entry + name-set registration + registry routing to defer_task.
#  These drive the REAL agent dispatch path, NOT execute_board_tool directly.
# ════════════════════════════════════════════════════════════════════

_CONV_SRC = os.path.join(ROOT, 'lib', 'tools', 'conversation.py')


def test_defer_tool_in_schema_and_name_set_with_reason():
    """The schema the model sees MUST contain project_board_defer with both
    task_id (required) and reason (so the agent records WHY it parked)."""
    from lib.tools import BOARD_TOOLS, BOARD_TOOL_NAMES
    names = [t['function']['name'] for t in BOARD_TOOLS]
    assert 'project_board_defer' in names, \
        'project_board_defer MUST be in the tool schema (else the agent can ' \
        'never call it — phantom-tool trap)'
    assert 'project_board_defer' in BOARD_TOOL_NAMES, \
        'project_board_defer MUST be in the gate/name set (else dispatch never routes)'
    spec = [t for t in BOARD_TOOLS if t['function']['name'] == 'project_board_defer'][0]
    props = spec['function']['parameters']['properties']
    assert 'task_id' in props and 'task_id' in spec['function']['parameters']['required']
    assert 'reason' in props, 'the reason arg MUST be in the schema so the ' \
        'agent can record why the epic is parked (it reaches the feed)'


def test_registry_routes_defer_to_board_handler():
    """The tool_registry (the real dispatch table an agent turn uses) MUST
    resolve project_board_defer to the board handler via BOARD_TOOL_NAMES."""
    from lib.tasks_pkg.executor import tool_registry
    from lib.tasks_pkg.handlers.misc import _handle_board_tool
    handler = tool_registry.lookup('project_board_defer', {})
    assert handler is _handle_board_tool, \
        'project_board_defer must route to _handle_board_tool (the set-based ' \
        'registration keyed on BOARD_TOOL_NAMES)'


def _drive_agent_tool(flask_app, project_path, conv_id, tid, reason):
    """Invoke project_board_defer through the REAL agent dispatch entry
    (_execute_tool_one → tool_registry.lookup → _handle_board_tool →
    execute_board_tool → defer_task), exactly as an LLM tool call would."""
    import threading
    from lib.tasks_pkg.executor import _execute_tool_one
    task = {
        'id': 'tdefer01', 'convId': conv_id, 'toolRounds': [], 'messages': [],
        'events': [], 'events_lock': threading.Lock(),
    }
    round_entry = {'roundNum': 1, 'query': 'project_board_defer',
                   'results': None, 'status': 'searching',
                   'toolName': 'project_board_defer'}
    task['toolRounds'].append(round_entry)
    fn_args = {'task_id': tid, 'reason': reason}
    with flask_app.app_context():
        tc_id, content, _is_search = _execute_tool_one(
            task, {'id': 'tc1', 'function': {'name': 'project_board_defer'}},
            'project_board_defer', 'tc1', fn_args, 1, round_entry,
            {'model': 'x'}, project_path, True, None)
    return content


def test_defer_reachable_end_to_end_via_agent_dispatch(flask_app):
    """END-TO-END: an agent tool call for project_board_defer must actually
    park the epic (status→deferred) AND land the reason in the feed — driven
    through _execute_tool_one, the same path a live LLM turn uses."""
    from lib.conversations.project_board import post_task, read_board
    from lib.conversations.project_feed import read_project_feed
    with flask_app.app_context():
        tid = post_task('/b/e2e', 'cAGENT', 'design-first epic')['id']
    content = _drive_agent_tool(flask_app, '/b/e2e', 'cAGENT', tid,
                                'awaiting Redis managed-vs-self-run decision')
    with flask_app.app_context():
        board = read_board('/b/e2e')
        events = read_project_feed('/b/e2e', limit=500)['events']
    assert board['tasks'][0]['status'] == 'deferred', \
        'the agent-dispatched defer call must actually park the epic'
    assert 'Parked (deferred)' in content
    # the reason reaches the feed (criterion #3)
    note = [e for e in events if e['kind'] == 'note'
            and e.get('payload', {}).get('deferred')]
    assert note, 'defer must emit an observable note event'
    assert 'Redis managed-vs-self-run' in (note[0].get('summary', '')), \
        'the reason must be recorded in the feed note'


def test_NC_C_schema_registration_is_load_bearing(flask_app):
    """NC-C: byte-remove project_board_defer from BOTH the BOARD_TOOLS list and
    BOARD_TOOL_NAMES set (the schema + gate registration) → the agent dispatch
    path can no longer route it → the epic is NOT parked and dispatch returns
    the 'unknown tool' error. Proves the registration — not just the handler
    branch — is what makes the tool reachable. Byte-identical restore."""
    import importlib

    def run():
        import lib.tools.conversation as conv
        importlib.reload(conv)
        # Rebuild the facade + executor's view of the registry so the reloaded
        # (defer-less) name set is what routing sees.
        import lib.tools as _tools
        importlib.reload(_tools)
        import lib.tasks_pkg.handlers.misc as _misc
        importlib.reload(_misc)
        import lib.tasks_pkg.executor as _ex
        importlib.reload(_ex)
        from lib.conversations.project_board import post_task, read_board
        with flask_app.app_context():
            from lib.database import DOMAIN_CHAT, get_thread_db
            get_thread_db(DOMAIN_CHAT).execute(
                "DELETE FROM project_tasks WHERE project_path='/nc_c'")
            get_thread_db(DOMAIN_CHAT).commit()
            tid = post_task('/nc_c', 'cAGENT', 'epic')['id']
            round_entry = {'roundNum': 1, 'query': 'project_board_defer',
                           'results': None, 'status': 'searching',
                           'toolName': 'project_board_defer'}
            import threading as _threading
            task = {'id': 't', 'convId': 'cAGENT', 'toolRounds': [round_entry],
                    'messages': [], 'events': [],
                    'events_lock': _threading.Lock()}
            _tc_id, content, _s = _ex._execute_tool_one(
                task, {'id': 'tc1', 'function': {'name': 'project_board_defer'}},
                'project_board_defer', 'tc1', {'task_id': tid}, 1, round_entry,
                {'model': 'x'}, '/nc_c', True, None)
            board = read_board('/nc_c')
        # With the registration removed, routing misses → unknown tool, and the
        # epic stays open (NOT parked).
        assert board['tasks'][0]['status'] == 'open', \
            'NC-C: without the schema/name-set registration the agent call ' \
            'must NOT park the epic'
        assert 'unknown tool' in content.lower(), \
            'NC-C: an unregistered tool name must fall through to the ' \
            'unknown-tool error'

    _patch_restore(
        _CONV_SRC,
        "BOARD_TOOLS = [BOARD_READ_TOOL, BOARD_POST_TOOL, BOARD_CLAIM_TOOL,\n"
        "               BOARD_COMPLETE_TOOL, BOARD_BLOCK_TOOL, BOARD_DEFER_TOOL]\n"
        "BOARD_TOOL_NAMES = {'project_board_read', 'project_board_post',\n"
        "                    'project_board_claim', 'project_board_complete',\n"
        "                    'project_board_block', 'project_board_defer'}",
        "BOARD_TOOLS = [BOARD_READ_TOOL, BOARD_POST_TOOL, BOARD_CLAIM_TOOL,\n"
        "               BOARD_COMPLETE_TOOL, BOARD_BLOCK_TOOL]  # NC-C (defer unregistered)\n"
        "BOARD_TOOL_NAMES = {'project_board_read', 'project_board_post',\n"
        "                    'project_board_claim', 'project_board_complete',\n"
        "                    'project_board_block'}",
        run,
    )
    # Restore every reloaded module from the byte-identical source so later
    # tests (and the live process) see the registered tool again.
    import lib.tasks_pkg.executor as _ex
    import lib.tasks_pkg.handlers.misc as _misc
    import lib.tools as _tools
    import lib.tools.conversation as conv
    importlib.reload(conv)
    importlib.reload(_tools)
    importlib.reload(_misc)
    importlib.reload(_ex)
