"""tests/test_project_board_answer.py — the STRUCTURED human gate on a block.

The defect this closes (owner complaint 2026-07-24, against live board state):
an epic blocked ``[human-gated]`` showed only a bare "Reopen"/"Done" pair with
the decision buried in a long English reason string. The human had NO way to
answer "A or B?", so "Reopen" re-ran the agent into the same gate → re-block →
cooldown → heartbeat re-dispatch → re-discover the same gate: the billed-turn
loop (pt_39b79cc4 hit 11×, pt_8dc03017 8×, pt_6598ae21/pt_a4c9d33e/pt_871a26c7
5×). Cooldown escalation only stretched the period; it never closed the loop.

The redesign makes a [human-gated] block an ask_human-style STRUCTURED
question:

  • ``block_task(..., question=..., options=[...])`` persists the question on
    the row (JSON ``{"q", "options": [{label, description?}]}``) and CLEARS
    any stale answer from an earlier round.
  • ``select_dispatchable`` suppresses an epic with a PENDING question
    (block_question set, human_answer empty) REGARDLESS of cooldown state —
    the epic waits for the ANSWER, not for time.
  • ``answer_task`` (new) stamps ``human_answer``, clears the whole block
    state, emits an ``answered`` feed event, and triggers an IMMEDIATE
    re-dispatch (``on_epic_answered``); ``dispatch_epic`` injects the answer
    into the kickoff so the assignee proceeds on it directly.
  • ``render_board_block`` partitions pending-question epics into their own
    "Waiting for the human's answer" lane (never the Open "claim me" lane).
  • ``complete_task`` / ``reopen_task`` reset both columns (terminal transition
    voids the Q&A).

Load-bearing negative controls:
  • NC-1 — revert the ``select_dispatchable`` pending-question skip → a
    question-blocked epic LEAKS back into the candidate set (the loop returns).
  • NC-2 — revert the ``answer_task`` → ``on_epic_answered`` trigger → the
    answer no longer re-dispatches immediately (heartbeat-only fallback).
"""

from __future__ import annotations

import json
import os

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures('chat_sidecar')]

TEST_OWNER_USER_ID = 1
pytest_plugins = ('tests._chat_sidecar',)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
_BOARD_SRC = os.path.join(ROOT, 'lib', 'conversations', 'project_board.py')
_DISPATCH_SRC = os.path.join(ROOT, 'lib', 'conversations', 'project_dispatch.py')


@pytest.fixture(autouse=True)
def _stub_push(monkeypatch):
    monkeypatch.setattr('lib.agent_core.push.push_event', lambda *a, **k: None)


def _row(flask_app, project_path, task_id):
    """Sidecar replacement for the legacy raw project_tasks SELECT."""
    from lib.conversations.project_board import read_board
    task = next((t for t in read_board(project_path, user_id=TEST_OWNER_USER_ID)['tasks']
                 if t['id'] == task_id), None)
    return task


def _feed(flask_app, project_path):
    from lib.conversations.project_feed import read_project_feed
    with flask_app.app_context():
        return read_project_feed(project_path, limit=500, user_id=TEST_OWNER_USER_ID)['events']


def _block_with_question(proj, tid):
    from lib.conversations.project_board import block_task
    return block_task(
        proj, 'cA', tid, '[human-gated] owner decides the push default',
        question='Force-push on divergence, or abort?',
        options=[{'label': 'Keep force-on-diverge (safely scoped)'},
                 {'label': 'Abort on divergence', 'description': 'add a flag'}], user_id=TEST_OWNER_USER_ID)


from tests._nc_harness import patch_restore as _patch_restore  # noqa: E402


# ════════════════════════════════════════════════════════════════════
#  block_task with a question — persists JSON, supersedes stale answer
# ════════════════════════════════════════════════════════════════════

def test_block_with_question_persists_structured_json(flask_app):
    from lib.conversations.project_board import post_task, read_board
    with flask_app.app_context():
        tid = post_task('/q/1', 'cA', 'epic needing a human decision', user_id=TEST_OWNER_USER_ID)['id']
        res = _block_with_question('/q/1', tid)
        assert res['ok']
        board = read_board('/q/1', user_id=TEST_OWNER_USER_ID)
    t = next(x for x in board['tasks'] if x['id'] == tid)
    assert t['block_question'] is not None, 'question must be exposed as a dict'
    assert t['block_question']['q'] == 'Force-push on divergence, or abort?'
    labels = [o['label'] for o in t['block_question']['options']]
    assert labels == ['Keep force-on-diverge (safely scoped)', 'Abort on divergence']
    assert t['block_question']['options'][1]['description'] == 'add a flag'
    assert t['human_answer'] == ''
    # legacy fields untouched
    assert t['block_count'] == 1 and '[human-gated]' in t['block_reason']


def test_block_without_question_stays_legacy(flask_app):
    from lib.conversations.project_board import block_task, post_task, read_board
    with flask_app.app_context():
        tid = post_task('/q/2', 'cA', 'plain sibling block', user_id=TEST_OWNER_USER_ID)['id']
        block_task('/q/2', 'cA', tid, '[sibling] path=lib/x.py wait for commit', user_id=TEST_OWNER_USER_ID)
        board = read_board('/q/2', user_id=TEST_OWNER_USER_ID)
    t = next(x for x in board['tasks'] if x['id'] == tid)
    assert t['block_question'] is None and t['human_answer'] == ''


def test_fresh_block_supersedes_a_stale_answer(flask_app):
    from lib.conversations.project_board import answer_task, post_task
    with flask_app.app_context():
        tid = post_task('/q/3', 'cA', 'epic', user_id=TEST_OWNER_USER_ID)['id']
        _block_with_question('/q/3', tid)
        answer_task('/q/3', 'human', tid, 'B — abort on divergence', user_id=TEST_OWNER_USER_ID)
        _block_with_question('/q/3', tid)  # blocked AGAIN with a new question
    row = _row(flask_app, '/q/3', tid)
    assert row['human_answer'] == '', \
        'a fresh block must void the previous answer (it answered the OLD question)'
    # read_board decodes block_question to a dict (both storage modes).
    assert row['block_question']['q'].startswith('Force-push')


# ════════════════════════════════════════════════════════════════════
#  select_dispatchable — a pending question suppresses dispatch (the
#  billed-turn-loop fix), even after the cooldown lapses
# ════════════════════════════════════════════════════════════════════

def test_pending_question_suppresses_dispatch_after_cooldown_expiry(
        flask_app, monkeypatch):
    import time
    from lib.conversations.project_board import post_task
    from lib.conversations.project_dispatch import select_dispatchable
    with flask_app.app_context():
        tid = post_task('/q/4', 'cA', 'question-gated epic', user_id=TEST_OWNER_USER_ID)['id']
        _block_with_question('/q/4', tid)
        blocked_until = int(_row(flask_app, '/q/4', tid)['blocked_until'])
        monkeypatch.setattr(
            time, 'time', lambda: (blocked_until + 1_000) / 1_000)
        cands = [c['id'] for c in select_dispatchable('/q/4', user_id=TEST_OWNER_USER_ID)]
    assert tid not in cands, \
        'a pending question must wait for the ANSWER, not for time — ' \
        'auto-retry here is exactly the billed-turn loop being killed'


def test_question_sanitizer_caps_and_drops_malformed():
    from lib.conversations.project_board import _clean_block_question
    out = json.loads(_clean_block_question('Q?', [
        {'label': 'ok'},
        {'label': ''},          # empty label → dropped
        'plain-string-option',   # str tolerated
        42,                      # garbage → dropped
        {'label': 'x' * 500},    # capped at _OPTION_LABEL_MAX
    ] + [{'label': f'o{i}'} for i in range(10)]))  # > _OPTION_MAX → truncated
    labels = [o['label'] for o in out['options']]
    assert 'ok' in labels and 'plain-string-option' in labels
    assert len(labels) == 6, 'options are capped at _OPTION_MAX'
    assert all(len(l) <= 120 for l in labels)
    assert _clean_block_question('', None) == ''
    assert _clean_block_question('   ', [{'label': 'a'}]) == ''


# ════════════════════════════════════════════════════════════════════
#  answer_task — closes the gate, clears block state, emits 'answered'
# ════════════════════════════════════════════════════════════════════

def test_answer_closes_gate_and_restores_dispatchability(flask_app, monkeypatch):
    from lib.conversations.project_board import answer_task, post_task
    from lib.conversations.project_dispatch import select_dispatchable
    import lib.conversations.project_dispatch as pd
    # Isolate the assertion from the immediate-dispatch trigger: a REAL
    # dispatch_epic would CLAIM the epic (status→claimed), which is separately
    # covered by test_answer_triggers_immediate_dispatch_with_answer.
    monkeypatch.setattr(pd, 'dispatch_epic',
                        lambda p, e, t, config=None: {'ok': True})
    monkeypatch.setattr(pd, '_conv_has_live_task', lambda c: False)
    monkeypatch.setattr(pd, '_epic_already_queued', lambda c, t: False)
    with flask_app.app_context():
        tid = post_task('/q/5', 'cA', 'epic', user_id=TEST_OWNER_USER_ID)['id']
        _block_with_question('/q/5', tid)
        assert tid not in [c['id'] for c in select_dispatchable('/q/5', user_id=TEST_OWNER_USER_ID)]
        res = answer_task('/q/5', 'human', tid, 'B — abort on divergence', user_id=TEST_OWNER_USER_ID)
        assert res['ok']
        cands = [c['id'] for c in select_dispatchable('/q/5', user_id=TEST_OWNER_USER_ID)]
    row = _row(flask_app, '/q/5', tid)
    assert row['human_answer'] == 'B — abort on divergence'
    assert row['blocked_until'] == 0 and row['block_count'] == 0
    assert (row['block_reason'] or '') == '' and (row['block_question'] or '') == ''
    assert tid in cands, 'an answered epic must be dispatchable again immediately'


def test_answer_requires_a_pending_question(flask_app):
    from lib.conversations.project_board import answer_task, block_task, post_task
    with flask_app.app_context():
        tid = post_task('/q/6', 'cA', 'epic', user_id=TEST_OWNER_USER_ID)['id']
        res1 = answer_task('/q/6', 'human', tid, 'answer to nothing', user_id=TEST_OWNER_USER_ID)
        block_task('/q/6', 'cA', tid, '[sibling] legacy block, no question', user_id=TEST_OWNER_USER_ID)
        res2 = answer_task('/q/6', 'human', tid, 'answer to a legacy block', user_id=TEST_OWNER_USER_ID)
        res3 = answer_task('/q/6', 'human', tid, '', user_id=TEST_OWNER_USER_ID)
        res4 = answer_task('/q/6', 'human', 'pt_missing', 'x', user_id=TEST_OWNER_USER_ID)
    assert res1 == {'ok': False, 'error': 'no_pending_question'}
    assert res2 == {'ok': False, 'error': 'no_pending_question'}
    assert res3 == {'ok': False, 'error': 'missing answer'}
    assert res4 == {'ok': False, 'error': 'task not found'}


def test_answer_emits_answered_feed_event(flask_app):
    from lib.conversations.project_board import answer_task, post_task
    with flask_app.app_context():
        tid = post_task('/q/7', 'cA', 'push-default epic', user_id=TEST_OWNER_USER_ID)['id']
        _block_with_question('/q/7', tid)
        answer_task('/q/7', 'human', tid, 'A — keep force-on-diverge', user_id=TEST_OWNER_USER_ID)
    ev = next(e for e in _feed(flask_app, '/q/7') if e['kind'] == 'answered')
    assert 'push-default epic' in ev['summary']
    assert 'A — keep force-on-diverge' in ev['summary']
    assert ev['payload']['question'].startswith('Force-push')
    assert ev['payload']['answer'] == 'A — keep force-on-diverge'


def test_answer_triggers_immediate_dispatch_with_answer(flask_app, monkeypatch):
    """The answer is the CLOSE of the loop: on_epic_answered fires synchronously
    and hands the epic (carrying human_answer) to dispatch_epic — no heartbeat
    wait."""
    import lib.conversations.project_dispatch as pd
    from lib.conversations.project_board import answer_task, post_task
    calls = []
    monkeypatch.setattr(pd, 'dispatch_epic',
                        lambda p, e, t, **_k: calls.append((p, e, t)) or {'ok': True})
    monkeypatch.setattr(pd, 'on_epic_posted', lambda *_a, **_k: 0)
    monkeypatch.setattr(pd, '_conv_has_live_task', lambda *_a, **_k: False)
    monkeypatch.setattr(pd, '_epic_already_queued', lambda *_a, **_k: False)
    monkeypatch.setattr(pd, '_drain_idle_target', lambda *_a, **_k: None)
    with flask_app.app_context():
        tid = post_task('/q/8', 'cA', 'epic', user_id=TEST_OWNER_USER_ID)['id']
        _block_with_question('/q/8', tid)
        answer_task('/q/8', 'human', tid, 'B — add the flag', user_id=TEST_OWNER_USER_ID)
    assert len(calls) == 1, 'the answer must trigger ONE immediate dispatch'
    proj, epic, target = calls[0]
    assert proj == '/q/8' and epic['id'] == tid and target == 'cA'
    assert epic['human_answer'] == 'B — add the flag'


def test_no_dispatch_when_nothing_pending(flask_app, monkeypatch):
    import lib.conversations.project_dispatch as pd
    calls = []
    monkeypatch.setattr(pd, 'dispatch_epic',
                        lambda p, e, t, config=None: calls.append(1) or {'ok': True})
    with flask_app.app_context():
        assert pd.on_epic_answered('/q/9', 'pt_missing', user_id=TEST_OWNER_USER_ID) == 0
    assert calls == []


# ════════════════════════════════════════════════════════════════════
#  dispatch_epic — the kickoff CARRIES the answer
# ════════════════════════════════════════════════════════════════════

def _capture_kickoff(monkeypatch):
    import lib.conversations.project_dispatch as pd

    captured = {}

    class _CaptureClient:
        def command(self, operation, payload, command_id):
            assert operation == 'board.dispatch'
            captured['payload'] = payload['message']
            captured['user_id'] = payload['user_id']
            captured['command_id'] = command_id
            return {
                'ok': True, 'queueId': payload['queue_id'],
                'transitioned': False,
            }

    monkeypatch.setattr(
        pd, 'get_storage_client', lambda *, write=False: _CaptureClient())
    return captured


def test_kickoff_injects_the_human_answer(monkeypatch):
    from lib.conversations.project_dispatch import dispatch_epic
    captured = _capture_kickoff(monkeypatch)
    epic = {'id': 'pt_x', 'title': 'push default',
            'human_answer': 'B — abort on divergence'}
    res = dispatch_epic(
        '/q/10', epic, 'cA', user_id=TEST_OWNER_USER_ID,
        config={'model': 'test-model'})
    assert res['ok']
    assert captured['user_id'] == 1
    text = captured['payload']['text']
    assert 'B — abort on divergence' in text
    assert 'human answered the earlier gate' in text


def test_kickoff_without_answer_is_unchanged(monkeypatch):
    from lib.conversations.project_dispatch import dispatch_epic
    captured = _capture_kickoff(monkeypatch)
    res = dispatch_epic(
        '/q/11', {'id': 'pt_y', 'title': 'plain epic'}, 'cA',
        user_id=TEST_OWNER_USER_ID, config={'model': 'test-model'})
    assert res['ok']
    assert 'human answered the earlier gate' not in captured['payload']['text']


# ════════════════════════════════════════════════════════════════════
#  render_board_block — the "Waiting for the human's answer" lane
# ════════════════════════════════════════════════════════════════════

def test_render_partitions_pending_question_into_answer_lane(flask_app):
    from lib.conversations.project_board import (
        post_task, render_board_block,
    )
    with flask_app.app_context():
        tid = post_task('/q/12', 'cA', 'Epic Q gated on owner', user_id=TEST_OWNER_USER_ID)['id']
        _block_with_question('/q/12', tid)
        block = render_board_block('/q/12', current_conv_id='cR', user_id=TEST_OWNER_USER_ID)
    assert "Waiting for the human's answer" in block
    assert 'Force-push on divergence, or abort?' in block, \
        'the question itself must be visible to every sibling prompt'
    lines = block.splitlines()
    open_idx = next((i for i, ln in enumerate(lines) if ln.startswith('Open (')), None)
    if open_idx is not None:
        assert 'Epic Q gated on owner' not in '\n'.join(lines[open_idx:]), \
            'a question-gated epic must NOT read as "claim me" in the Open lane'
    # and it is NOT in the plain auto-retry lane (its retry is answer-driven)
    gate_idx = next((i for i, ln in enumerate(lines)
                     if ln.startswith('Waiting on an external gate')), None)
    if gate_idx is not None:
        assert 'Epic Q gated on owner' not in '\n'.join(
            lines[gate_idx:gate_idx + 3])


def test_render_answered_epic_leaves_answer_lane(flask_app):
    from lib.conversations.project_board import (
        answer_task, post_task, render_board_block,
    )
    with flask_app.app_context():
        tid = post_task('/q/13', 'cA', 'epic', user_id=TEST_OWNER_USER_ID)['id']
        _block_with_question('/q/13', tid)
        answer_task('/q/13', 'human', tid, 'A', user_id=TEST_OWNER_USER_ID)
        block = render_board_block('/q/13', current_conv_id='cR', user_id=TEST_OWNER_USER_ID)
    assert "Waiting for the human's answer" not in block


# ════════════════════════════════════════════════════════════════════
#  Tool executor — question/options flow through + the return text teaches
#  the wait-for-answer semantics
# ════════════════════════════════════════════════════════════════════

def test_executor_passes_question_and_teaches_semantics(flask_app):
    from lib.conversations.project_board import execute_board_tool, read_board
    with flask_app.app_context():
        from lib.conversations.project_board import post_task
        tid = post_task('/q/14', 'cA', 'epic', user_id=TEST_OWNER_USER_ID)['id']
        out = execute_board_tool(
            'project_board_block',
            {'task_id': tid, 'reason': '[human-gated] decision needed',
             'question': 'Which default?',
             'options': [{'label': 'A'}, {'label': 'B'}]},
            current_conv_id='cA', project_path='/q/14', user_id=TEST_OWNER_USER_ID)
        board = read_board('/q/14', user_id=TEST_OWNER_USER_ID)
    t = next(x for x in board['tasks'] if x['id'] == tid)
    assert t['block_question']['q'] == 'Which default?'
    assert 'NOT auto-retry' in out and 'one-click options' in out


def test_pre_migration_row_reads_as_no_question(flask_app):
    """A row mapping PREDATING the two columns must read as no-question /
    no-answer so it is NEVER wrongly suppressed from dispatch."""
    from lib.conversations.project_board import read_board
    from lib.storage import get_storage_client
    document = {
        'id': 'pt_legacy', 'title': 'legacy epic', 'status': 'open',
        'project_path': '/q/legacy',
        'owner_conv_id': '', 'lease_expires_at': 0, 'created_by_conv': 'cA',
        'depends_on': '[]', 'dispatched': 0, 'kind': 'epic',
        'created_at': 0, 'updated_at': 0,
    }
    with flask_app.app_context():
        get_storage_client(write=True).command(
            'board.import_batch', {
                'user_id': TEST_OWNER_USER_ID, 'documents': [document],
            }, 'seed-pre-migration-board-row')
        t = read_board(
            '/q/legacy', user_id=TEST_OWNER_USER_ID)['tasks'][0]
    assert t['block_question'] is None and t['human_answer'] == ''


def test_complete_and_reopen_clear_question_and_answer(flask_app):
    from lib.conversations.project_board import (
        answer_task, block_task, complete_task, post_task, reopen_task,
    )
    with flask_app.app_context():
        tid = post_task('/q/15', 'cA', 'epic', user_id=TEST_OWNER_USER_ID)['id']
        _block_with_question('/q/15', tid)
        answer_task('/q/15', 'human', tid, 'A', user_id=TEST_OWNER_USER_ID)
        complete_task('/q/15', 'cA', tid, user_id=TEST_OWNER_USER_ID)
    row = _row(flask_app, '/q/15', tid)
    assert (row['block_question'] or '') == '' and (row['human_answer'] or '') == ''
    with flask_app.app_context():
        tid2 = post_task('/q/15', 'cA', 'epic 2', user_id=TEST_OWNER_USER_ID)['id']
        _block_with_question('/q/15', tid2)
        answer_task('/q/15', 'human', tid2, 'B', user_id=TEST_OWNER_USER_ID)
        block_task('/q/15', 'cA', tid2, '[human-gated] re-blocked', user_id=TEST_OWNER_USER_ID)
        reopen_task('/q/15', 'human', tid2, user_id=TEST_OWNER_USER_ID)
    row2 = _row(flask_app, '/q/15', tid2)
    assert (row2['block_question'] or '') == '' and (row2['human_answer'] or '') == ''


# ════════════════════════════════════════════════════════════════════
#  NC-1 — the select_dispatchable pending-question skip is load-bearing
# ════════════════════════════════════════════════════════════════════

def test_NC_1_pending_question_skip_is_load_bearing(flask_app, monkeypatch):
    def run():
        import lib.conversations.project_dispatch as pd
        from lib.conversations.project_board import post_task
        with flask_app.app_context():
            tid = post_task('/ncq1', 'cA', 'question-gated epic', user_id=TEST_OWNER_USER_ID)['id']
            _block_with_question('/ncq1', tid)
            # Expire the cooldown so the COOLDOWN skip can't mask the leak —
            # only the pending-question skip stands between the epic and a
            # billed-turn re-dispatch. Sidecar equivalent of the legacy
            # UPDATE project_tasks SET blocked_until=1: push the dispatch
            # clock past the cooldown.
            import time as _real_time
            _orig_time = _real_time.time
            monkeypatch.setattr('time.time',
                                lambda: _orig_time() + 7200)
            cands = [c['id'] for c in pd.select_dispatchable('/ncq1', user_id=TEST_OWNER_USER_ID)]
        assert tid in cands, \
            'NC-1: with the pending-question skip removed, a question-blocked ' \
            'epic must LEAK back into the candidate set (the billed-turn loop)'

    _patch_restore(
        _DISPATCH_SRC,
        '        if t.get("block_question") and not (t.get("human_answer") or "").strip():\n'
        "            continue\n",
        "        if False:  # NC-1 (pending-question skip disabled)\n            continue\n",
        run,
    )


# ════════════════════════════════════════════════════════════════════
#  NC-2 — the answer → immediate-dispatch trigger is load-bearing
# ════════════════════════════════════════════════════════════════════

def test_NC_2_answer_dispatch_trigger_is_load_bearing(flask_app, monkeypatch):
    import lib.conversations.project_dispatch as pd
    calls = []
    monkeypatch.setattr(pd, 'dispatch_epic',
                        lambda p, e, t, **_k: calls.append(1) or {'ok': True})
    monkeypatch.setattr(pd, 'on_epic_posted', lambda *_a, **_k: 0)
    monkeypatch.setattr(pd, '_conv_has_live_task', lambda *_a, **_k: False)
    monkeypatch.setattr(pd, '_epic_already_queued', lambda *_a, **_k: False)
    monkeypatch.setattr(pd, '_drain_idle_target', lambda *_a, **_k: None)

    def run():
        from lib.conversations.project_board import answer_task, post_task
        with flask_app.app_context():
            tid = post_task('/ncq2', 'cA', 'epic', user_id=TEST_OWNER_USER_ID)['id']
            _block_with_question('/ncq2', tid)
            answer_task('/ncq2', 'human', tid, 'A', user_id=TEST_OWNER_USER_ID)
        assert calls == [], \
            'NC-2: with the trigger removed, answering must NOT re-dispatch ' \
            '(the epic waits for the heartbeat — the immediate loop is gone)'

    # The anchor is the SIDECAR branch's trigger call (16-space indent inside
    # try:) — the 8-space form matches the legacy branch, which never runs here.
    _patch_restore(
        _BOARD_SRC,
        "        from lib.conversations.project_dispatch import on_epic_answered\n\n"
        "        on_epic_answered(normalized_path, task_id, user_id=int(user_id))",
        "        pass  # NC-2: immediate-dispatch trigger removed",
        run,
    )
