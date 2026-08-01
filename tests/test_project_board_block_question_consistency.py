"""tests/test_project_board_block_question_consistency.py — Tier-2 bleeding-control.

Guards the consistency constraint between a block's PROSE (``block_reason``) and
its STRUCTURED human gate (``block_question``).

Measured defect (2026-07-31, epic pt_d689f2016ecf4311). Two epics sat parked for
~19 h while the Project Brain reported ``needsYou: 0`` — "nothing needs you":

  • ``pt_3879f00e``'s ``block_reason`` said, verbatim, *"STILL AWAITING owner
    one-click on the 4-option question card"* — while its ``block_question``
    column was ``None``. **The card it named never existed.**
  • Both reasons were exactly 2000 chars (``_TITLE_MAX_CHARS``), truncated
    mid-word ("no early ex" / "(exten") — the enumerated options the author
    wrote were in the clipped tail, and nothing logged that a cut happened.

Why that is a DOUBLE loss, not a cosmetic one:

  • **Invisible to the human.** ``project_attention._board_questions`` builds a
    ``blocking`` item keyed on ``block_question`` PRESENCE. The ``[human-gated]``
    prefix in the prose is matched nowhere in the codebase (that module's own
    docstring says so). No question column ⇒ no card ⇒ no "Needs you" entry.
  • **Invisible to the machine.** ``select_dispatchable`` only skips a row whose
    ``block_question`` is set. So the epic ALSO never really stopped: once the
    cooldown lapsed it was dispatchable again, and every heartbeat could spend a
    billed agent turn re-discovering a gate no one could answer.

Root cause is the API shape, not the data: ``reason`` is free text and
``question`` is an optional kwarg with NO consistency constraint, so
"I wrote a question" and "a question is registered" could disagree silently.

The rule pinned here mirrors the ``summary_required`` precedent
(``update_decision``, 2026-07-30): when a caller's prose ASSERTS that a
structured human question exists, refusing loudly is strictly better than
silently parking the epic behind a card that was never created — and the refusal
must happen BEFORE any mutation, so a rejected call cannot half-apply.

Deliberately NARROW: an ordinary ``[human-gated]`` block that makes no such
claim is still perfectly legal without a question (see the over-firing
complement). This guard is about prose that LIES, not about mandating questions.
"""

from __future__ import annotations

import logging

import pytest

pytestmark = pytest.mark.unit


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


# The exact prose that caused the measured 19 h silent park.
_LYING_REASON = ('[human-gated] sub-parts 1 & 3 STILL AWAITING owner one-click '
                 'on the 4-option question card: (a) continue slice 11; '
                 '(b) sub-part 1 slice 4 pack-split wire-up')


# ════════════════════════════════════════════════════════════════════
#  (a) The refusal — prose that claims a card it did not create
# ════════════════════════════════════════════════════════════════════

def test_reason_claiming_a_question_card_without_one_is_refused(flask_app):
    """The measured defect. A reason asserting an awaiting-owner question card,
    with no ``question=``, must be REFUSED — never silently accepted."""
    from lib.conversations.project_board import block_task, post_task
    with flask_app.app_context():
        tid = post_task('/bqc/1', 'cA', 'epic with a lying block reason')['id']
        res = block_task('/bqc/1', 'cA', tid, _LYING_REASON)
    assert res.get('ok') is False, \
        'a reason claiming a question card must not be accepted without one'
    assert res.get('error') == 'question_required', \
        f"expected error='question_required', got {res.get('error')!r}"


def test_refusal_happens_before_any_mutation(flask_app):
    """A rejected block must not half-apply: no cooldown, no block_count bump,
    no reason written. (The ``summary_required`` precedent: validate first.)"""
    from lib.conversations.project_board import block_task, post_task
    with flask_app.app_context():
        tid = post_task('/bqc/2', 'cA', 'epic that must stay pristine')['id']
        before = _row(flask_app, '/bqc/2', tid)
        block_task('/bqc/2', 'cA', tid, _LYING_REASON)
        after = _row(flask_app, '/bqc/2', tid)
    for field in ('blocked_until', 'block_count', 'block_reason',
                  'block_question', 'status'):
        assert before[field] == after[field], \
            f'refused block mutated {field}: {before[field]!r} → {after[field]!r}'


def test_refused_block_never_produces_a_silent_park(flask_app):
    """End-to-end statement of the bug: the rejected call must not leave the
    epic in the measured state — parked behind a card, while the human-facing
    attention surface reports that nothing needs them."""
    from lib.conversations.project_attention import build_attention_items
    from lib.conversations.project_board import block_task, post_task
    with flask_app.app_context():
        tid = post_task('/bqc/3', 'cA', 'epic that must not park silently')['id']
        block_task('/bqc/3', 'cA', tid, _LYING_REASON)
        att = build_attention_items('/bqc/3')
        row = _row(flask_app, '/bqc/3', tid)
    parked = int(row['blocked_until'] or 0) > 0
    assert not (parked and att['needsYou'] == 0), (
        'silent park reproduced: epic is blocked but needsYou==0 — '
        'the human is told nothing needs them while work is stopped')


# ════════════════════════════════════════════════════════════════════
#  (b) The complement — the structured channel still works
# ════════════════════════════════════════════════════════════════════

def test_same_reason_is_accepted_when_the_question_is_registered(flask_app):
    """The fix must not make the case unreachable: the SAME prose, with the
    question passed structurally, is accepted and surfaces as a real card."""
    from lib.conversations.project_attention import build_attention_items
    from lib.conversations.project_board import block_task, post_task
    with flask_app.app_context():
        tid = post_task('/bqc/4', 'cA', 'epic with a real question')['id']
        res = block_task('/bqc/4', 'cA', tid, _LYING_REASON,
                         question='Which sub-part should slice 11 advance?',
                         options=[{'label': 'continue sub-part 2'},
                                  {'label': 'sub-part 1 pack-split'}])
        att = build_attention_items('/bqc/4')
    assert res.get('ok') is True, f'structured question rejected: {res!r}'
    row = _row(flask_app, '/bqc/4', tid)
    assert row['block_question'], 'question column must be populated'
    assert att['needsYou'] == 1 and att['blocking'] == 1, \
        f'a registered question must surface as a blocking card, got {att!r}'
    item = att['items'][0]
    assert item['type'] == 'board_question'
    assert len(item['options']) == 2


def test_ordinary_human_gated_block_without_a_question_still_allowed(flask_app):
    """Over-firing complement — the guard targets prose that LIES about an
    existing card, NOT every human-gated block. A plain gate with no such claim
    must still be accepted questionless, or every legitimate block breaks."""
    from lib.conversations.project_board import block_task, post_task
    with flask_app.app_context():
        tid = post_task('/bqc/5', 'cA', 'ordinary human gate')['id']
        res = block_task('/bqc/5', 'cA', tid,
                         '[human-gated] needs infra sign-off before rollout')
    assert res.get('ok') is True, \
        f'a plain [human-gated] block must not be refused: {res!r}'
    assert _row(flask_app, '/bqc/5', tid)['block_count'] == 1


def test_sibling_block_is_never_refused(flask_app):
    """A [sibling] block auto-resolves on a peer's commit — it has no human
    question by construction and must never trip this guard."""
    from lib.conversations.project_board import block_task, post_task
    with flask_app.app_context():
        tid = post_task('/bqc/6', 'cA', 'sibling-gated epic')['id']
        res = block_task('/bqc/6', 'cA', tid,
                         '[sibling] path=lib/x.py awaiting the owner of that file')
    assert res.get('ok') is True, f'[sibling] block must not be refused: {res!r}'


# ════════════════════════════════════════════════════════════════════
#  (c) Truncation must be LOUD — the clipped options half of the defect
# ════════════════════════════════════════════════════════════════════

def test_oversized_reason_truncation_is_logged(flask_app, caplog):
    """Both measured reasons were exactly 2000 chars, cut mid-word, with the
    author's enumerated options in the discarded tail — and NOTHING recorded
    that a cut happened. Silent truncation turns "I wrote the options" into
    "the options do not exist"; it must at least leave a trace."""
    from lib.conversations.project_board import (
        _TITLE_MAX_CHARS, block_task, post_task,
    )
    long_reason = '[human-gated] ' + ('x' * (_TITLE_MAX_CHARS + 500))
    with flask_app.app_context():
        tid = post_task('/bqc/7', 'cA', 'epic with an oversized reason')['id']
        with caplog.at_level(logging.WARNING, logger='lib.conversations.project_board'):
            res = block_task('/bqc/7', 'cA', tid, long_reason)
    assert res.get('ok') is True
    row = _row(flask_app, '/bqc/7', tid)
    assert len(row['block_reason']) == _TITLE_MAX_CHARS, 'reason should still be capped'
    assert any('truncat' in r.message.lower() or 'truncat' in r.getMessage().lower()
               for r in caplog.records), \
        'truncating a block reason must be logged, not silent'
