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


def _row(flask_app, project_path, task_id):
    from lib.database import DOMAIN_CHAT, get_thread_db
    with flask_app.app_context():
        db = get_thread_db(DOMAIN_CHAT)
        r = db.execute(
            'SELECT blocked_until, block_count, block_reason, block_question, '
            '       human_answer, status '
            'FROM project_tasks WHERE id=? AND project_path=?',
            (task_id, project_path)).fetchone()
    return dict(r) if r else None


def _feed(flask_app, project_path):
    from lib.conversations.project_feed import read_project_feed
    with flask_app.app_context():
        return read_project_feed(project_path, limit=500)['events']


def _block_with_question(proj, tid):
    from lib.conversations.project_board import block_task
    return block_task(
        proj, 'cA', tid, '[human-gated] owner decides the push default',
        question='Force-push on divergence, or abort?',
        options=[{'label': 'Keep force-on-diverge (safely scoped)'},
                 {'label': 'Abort on divergence', 'description': 'add a flag'}])


from tests._nc_harness import patch_restore as _patch_restore  # noqa: E402


# ════════════════════════════════════════════════════════════════════
#  Schema — the two columns exist (migration fired)
# ════════════════════════════════════════════════════════════════════

def test_schema_has_question_and_answer_columns(flask_app):
    from lib.database import DOMAIN_CHAT, get_thread_db
    with flask_app.app_context():
        db = get_thread_db(DOMAIN_CHAT)
        cols = {r['name'] for r in db.execute('PRAGMA table_info(project_tasks)')}
    assert 'block_question' in cols and 'human_answer' in cols


# ════════════════════════════════════════════════════════════════════
#  block_task with a question — persists JSON, supersedes stale answer
# ════════════════════════════════════════════════════════════════════

def test_block_with_question_persists_structured_json(flask_app):
    from lib.conversations.project_board import post_task, read_board
    with flask_app.app_context():
        tid = post_task('/q/1', 'cA', 'epic needing a human decision')['id']
        res = _block_with_question('/q/1', tid)
        assert res['ok']
        board = read_board('/q/1')
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
        tid = post_task('/q/2', 'cA', 'plain sibling block')['id']
        block_task('/q/2', 'cA', tid, '[sibling] path=lib/x.py wait for commit')
        board = read_board('/q/2')
    t = next(x for x in board['tasks'] if x['id'] == tid)
    assert t['block_question'] is None and t['human_answer'] == ''


def test_fresh_block_supersedes_a_stale_answer(flask_app):
    from lib.conversations.project_board import answer_task, post_task
    with flask_app.app_context():
        tid = post_task('/q/3', 'cA', 'epic')['id']
        _block_with_question('/q/3', tid)
        answer_task('/q/3', 'human', tid, 'B — abort on divergence')
        _block_with_question('/q/3', tid)  # blocked AGAIN with a new question
    row = _row(flask_app, '/q/3', tid)
    assert row['human_answer'] == '', \
        'a fresh block must void the previous answer (it answered the OLD question)'
    assert json.loads(row['block_question'])['q'].startswith('Force-push')


# ════════════════════════════════════════════════════════════════════
#  select_dispatchable — a pending question suppresses dispatch (the
#  billed-turn-loop fix), even after the cooldown lapses
# ════════════════════════════════════════════════════════════════════

def test_pending_question_suppresses_dispatch_after_cooldown_expiry(flask_app):
    from lib.conversations.project_board import post_task
    from lib.conversations.project_dispatch import select_dispatchable
    from lib.database import DOMAIN_CHAT, get_thread_db
    with flask_app.app_context():
        tid = post_task('/q/4', 'cA', 'question-gated epic')['id']
        _block_with_question('/q/4', tid)
        db = get_thread_db(DOMAIN_CHAT)
        db.execute('UPDATE project_tasks SET blocked_until=1 WHERE id=?', (tid,))
        db.commit()  # cooldown fully expired — legacy block WOULD retry now
        cands = [c['id'] for c in select_dispatchable('/q/4')]
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
        tid = post_task('/q/5', 'cA', 'epic')['id']
        _block_with_question('/q/5', tid)
        assert tid not in [c['id'] for c in select_dispatchable('/q/5')]
        res = answer_task('/q/5', 'human', tid, 'B — abort on divergence')
        assert res['ok']
        cands = [c['id'] for c in select_dispatchable('/q/5')]
    row = _row(flask_app, '/q/5', tid)
    assert row['human_answer'] == 'B — abort on divergence'
    assert row['blocked_until'] == 0 and row['block_count'] == 0
    assert (row['block_reason'] or '') == '' and (row['block_question'] or '') == ''
    assert tid in cands, 'an answered epic must be dispatchable again immediately'


def test_answer_requires_a_pending_question(flask_app):
    from lib.conversations.project_board import answer_task, block_task, post_task
    with flask_app.app_context():
        tid = post_task('/q/6', 'cA', 'epic')['id']
        res1 = answer_task('/q/6', 'human', tid, 'answer to nothing')
        block_task('/q/6', 'cA', tid, '[sibling] legacy block, no question')
        res2 = answer_task('/q/6', 'human', tid, 'answer to a legacy block')
        res3 = answer_task('/q/6', 'human', tid, '')
        res4 = answer_task('/q/6', 'human', 'pt_missing', 'x')
    assert res1 == {'ok': False, 'error': 'no_pending_question'}
    assert res2 == {'ok': False, 'error': 'no_pending_question'}
    assert res3 == {'ok': False, 'error': 'missing answer'}
    assert res4 == {'ok': False, 'error': 'task not found'}


def test_answer_emits_answered_feed_event(flask_app):
    from lib.conversations.project_board import answer_task, post_task
    with flask_app.app_context():
        tid = post_task('/q/7', 'cA', 'push-default epic')['id']
        _block_with_question('/q/7', tid)
        answer_task('/q/7', 'human', tid, 'A — keep force-on-diverge')
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
                        lambda p, e, t, config=None: calls.append((p, e, t)) or {'ok': True})
    monkeypatch.setattr(pd, '_conv_has_live_task', lambda c: False)
    monkeypatch.setattr(pd, '_epic_already_queued', lambda c, t: False)
    monkeypatch.setattr(pd, '_drain_idle_target', lambda c: None)
    with flask_app.app_context():
        tid = post_task('/q/8', 'cA', 'epic')['id']
        _block_with_question('/q/8', tid)
        answer_task('/q/8', 'human', tid, 'B — add the flag')
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
        assert pd.on_epic_answered('/q/9', 'pt_missing') == 0
    assert calls == []


# ════════════════════════════════════════════════════════════════════
#  dispatch_epic — the kickoff CARRIES the answer
# ════════════════════════════════════════════════════════════════════

def _capture_kickoff(monkeypatch):
    captured = {}
    monkeypatch.setattr('lib.conversations.project_board.claim_task',
                        lambda *a, **k: {'ok': True})

    def _fake_enqueue(conv, payload, cfg, kind=None):
        captured['payload'] = payload
        return {'ok': True, 'queueId': 'q1'}
    monkeypatch.setattr('lib.message_queue.enqueue_message', _fake_enqueue)
    return captured


def test_kickoff_injects_the_human_answer(monkeypatch):
    from lib.conversations.project_dispatch import dispatch_epic
    captured = _capture_kickoff(monkeypatch)
    epic = {'id': 'pt_x', 'title': 'push default',
            'human_answer': 'B — abort on divergence'}
    res = dispatch_epic('/q/10', epic, 'cA')
    assert res['ok']
    text = captured['payload']['text']
    assert 'B — abort on divergence' in text
    assert 'blocked waiting on a human decision' in text


def test_kickoff_without_answer_is_unchanged(monkeypatch):
    from lib.conversations.project_dispatch import dispatch_epic
    captured = _capture_kickoff(monkeypatch)
    res = dispatch_epic('/q/11', {'id': 'pt_y', 'title': 'plain epic'}, 'cA')
    assert res['ok']
    assert 'human decision' not in captured['payload']['text']


# ════════════════════════════════════════════════════════════════════
#  render_board_block — the "Waiting for the human's answer" lane
# ════════════════════════════════════════════════════════════════════

def test_render_partitions_pending_question_into_answer_lane(flask_app):
    from lib.conversations.project_board import (
        post_task, render_board_block,
    )
    with flask_app.app_context():
        tid = post_task('/q/12', 'cA', 'Epic Q gated on owner')['id']
        _block_with_question('/q/12', tid)
        block = render_board_block('/q/12', current_conv_id='cR')
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
        tid = post_task('/q/13', 'cA', 'epic')['id']
        _block_with_question('/q/13', tid)
        answer_task('/q/13', 'human', tid, 'A')
        block = render_board_block('/q/13', current_conv_id='cR')
    assert "Waiting for the human's answer" not in block


# ════════════════════════════════════════════════════════════════════
#  Tool executor — question/options flow through + the return text teaches
#  the wait-for-answer semantics
# ════════════════════════════════════════════════════════════════════

def test_executor_passes_question_and_teaches_semantics(flask_app):
    from lib.conversations.project_board import execute_board_tool, read_board
    with flask_app.app_context():
        from lib.conversations.project_board import post_task
        tid = post_task('/q/14', 'cA', 'epic')['id']
        out = execute_board_tool(
            'project_board_block',
            {'task_id': tid, 'reason': '[human-gated] decision needed',
             'question': 'Which default?',
             'options': [{'label': 'A'}, {'label': 'B'}]},
            current_conv_id='cA', project_path='/q/14')
        board = read_board('/q/14')
    t = next(x for x in board['tasks'] if x['id'] == tid)
    assert t['block_question']['q'] == 'Which default?'
    assert 'NOT auto-retry' in out and 'one-click options' in out


def test_pre_migration_row_reads_as_no_question():
    """A row mapping PREDATING the two columns must read as no-question /
    no-answer so it is NEVER wrongly suppressed from dispatch."""
    from lib.conversations.project_board import _row_to_task
    row = {
        'id': 'pt_legacy', 'title': 'legacy epic', 'status': 'open',
        'owner_conv_id': '', 'lease_expires_at': 0, 'created_by_conv': 'cA',
        'depends_on': '[]', 'dispatched': 0, 'kind': 'epic',
        'created_at': 0, 'updated_at': 0,
    }
    t = _row_to_task(row, now_ms=1_000_000)
    assert t['block_question'] is None and t['human_answer'] == ''


def test_complete_and_reopen_clear_question_and_answer(flask_app):
    from lib.conversations.project_board import (
        answer_task, block_task, complete_task, post_task, reopen_task,
    )
    with flask_app.app_context():
        tid = post_task('/q/15', 'cA', 'epic')['id']
        _block_with_question('/q/15', tid)
        answer_task('/q/15', 'human', tid, 'A')
        complete_task('/q/15', 'cA', tid)
    row = _row(flask_app, '/q/15', tid)
    assert (row['block_question'] or '') == '' and (row['human_answer'] or '') == ''
    with flask_app.app_context():
        tid2 = post_task('/q/15', 'cA', 'epic 2')['id']
        _block_with_question('/q/15', tid2)
        answer_task('/q/15', 'human', tid2, 'B')
        block_task('/q/15', 'cA', tid2, '[human-gated] re-blocked')
        reopen_task('/q/15', 'human', tid2)
    row2 = _row(flask_app, '/q/15', tid2)
    assert (row2['block_question'] or '') == '' and (row2['human_answer'] or '') == ''


# ════════════════════════════════════════════════════════════════════
#  NC-1 — the select_dispatchable pending-question skip is load-bearing
# ════════════════════════════════════════════════════════════════════

def test_NC_1_pending_question_skip_is_load_bearing(flask_app):
    def run():
        import lib.conversations.project_dispatch as pd
        from lib.conversations.project_board import post_task
        with flask_app.app_context():
            from lib.database import DOMAIN_CHAT, get_thread_db
            get_thread_db(DOMAIN_CHAT).execute(
                "DELETE FROM project_tasks WHERE project_path='/ncq1'")
            get_thread_db(DOMAIN_CHAT).commit()
            tid = post_task('/ncq1', 'cA', 'question-gated epic')['id']
            _block_with_question('/ncq1', tid)
            # Expire the cooldown so the COOLDOWN skip can't mask the leak —
            # only the pending-question skip stands between the epic and a
            # billed-turn re-dispatch.
            get_thread_db(DOMAIN_CHAT).execute(
                'UPDATE project_tasks SET blocked_until=1 WHERE id=?', (tid,))
            get_thread_db(DOMAIN_CHAT).commit()
            cands = [c['id'] for c in pd.select_dispatchable('/ncq1')]
        assert tid in cands, \
            'NC-1: with the pending-question skip removed, a question-blocked ' \
            'epic must LEAK back into the candidate set (the billed-turn loop)'

    _patch_restore(
        _DISPATCH_SRC,
        "        if t.get('block_question') and not (t.get('human_answer') or '').strip():\n"
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
                        lambda p, e, t, config=None: calls.append(1) or {'ok': True})
    monkeypatch.setattr(pd, '_conv_has_live_task', lambda c: False)
    monkeypatch.setattr(pd, '_epic_already_queued', lambda c, t: False)
    monkeypatch.setattr(pd, '_drain_idle_target', lambda c: None)

    def run():
        from lib.conversations.project_board import answer_task, post_task
        with flask_app.app_context():
            from lib.database import DOMAIN_CHAT, get_thread_db
            get_thread_db(DOMAIN_CHAT).execute(
                "DELETE FROM project_tasks WHERE project_path='/ncq2'")
            get_thread_db(DOMAIN_CHAT).commit()
            tid = post_task('/ncq2', 'cA', 'epic')['id']
            _block_with_question('/ncq2', tid)
            answer_task('/ncq2', 'human', tid, 'A')
        assert calls == [], \
            'NC-2: with the trigger removed, answering must NOT re-dispatch ' \
            '(the epic waits for the heartbeat — the immediate loop is gone)'

    _patch_restore(
        _BOARD_SRC,
        "        from lib.conversations.project_dispatch import on_epic_answered\n"
        "        on_epic_answered(project_path, task_id)",
        "        pass  # NC-2: immediate-dispatch trigger removed",
        run,
    )
