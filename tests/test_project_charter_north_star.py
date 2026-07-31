"""tests/test_project_charter_north_star.py — the north star must REACH the model.

## The defect this pins (measured on the live chatui charter, 2026-07-27)

The project's goal was stored as the 5th entry of the ``decisions`` array
(``[Goal — promoted by owner] tofu项目需要具有长期扩展性…``) because the
dedicated ``content`` column was EMPTY. The per-turn injection renders only
``decisions[-20:]``, and the charter had grown to 27 decisions — so the goal
sat at index 4, outside the window, and the measured answer to "is the goal in
the injected block?" was **False**. Every sibling conversation was reading 20
implementation decisions as its "authoritative shared intent" while the actual
north star was invisible.

Worse, ``_MAX_DECISIONS`` eviction is ``decisions[-100:]`` — a blind FIFO tail
slice that drops the OLDEST. A simulation over the real charter showed the goal
is **permanently deleted** from the database once ~80 further decisions land.

## Why these assertions are shaped this way (charter discipline)

Per the committed decision *"测试守卫必须断言「结果」而非「实现」"*, every test
here asserts an OBSERVABLE RESULT — "the goal text appears in the string the
model receives", "the goal survives eviction" — and never that some constant
equals a value. Re-tuning ``_MAX_DECISIONS`` or the ``[-20:]`` window, or
rewriting the renderer entirely, must NOT turn these red; only losing the
north star may.

The complement test (``test_the_decision_window_is_still_bounded``) exists
because a guard that only says "inject the goal" could be satisfied by
injecting EVERYTHING — which would re-create the context-bloat problem the
window is there to prevent. Both directions are pinned.
"""

from __future__ import annotations

import os

import pytest

from tests._nc_harness import patch_restore as _patch_restore

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
_CHARTER_SRC = os.path.join(ROOT, 'lib', 'conversations', 'project_charter.py')

# A north star long enough to be unmistakable in the rendered block, and shaped
# like the real one (the live goal is 126 chars of Chinese).
_GOAL = ('tofu项目需要具有长期扩展性。在低成本、高性能、高速度的稳定完成长时间任务。'
         '我们项目的每一次迭代都不记成本的只选择最完美的方案。')
_PROJ = '/tmp/tofu-north-star-guard'


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
        db.execute('DELETE FROM project_charter')
        db.execute('DELETE FROM project_events')
        db.commit()
    yield


@pytest.fixture(autouse=True)
def _stub_push(monkeypatch):
    monkeypatch.setattr('lib.agent_core.push.push_event', lambda *a, **k: None)


def _commit_goal(flask_app, goal=_GOAL):
    from lib.conversations.project_charter import commit_charter
    with flask_app.app_context():
        res = commit_charter(_PROJ, content=goal, updated_by_conv='owner')
    assert res.get('ok'), res
    return res


def _commit_decisions(flask_app, n, prefix='filler decision'):
    from lib.conversations.project_charter import commit_charter
    with flask_app.app_context():
        for i in range(n):
            res = commit_charter(_PROJ, add_decision=f'{prefix} #{i}',
                                 updated_by_conv='agent')
            assert res.get('ok'), res


def _render(flask_app):
    # The INJECTION renderer — what the model actually receives each turn
    # (render_charter_block is now the tool's full-text detail path; guarding
    # the detail path while the injection path could regress would be the
    # "helper, not the call-site" gap the charter warns about).
    from lib.conversations.project_charter import (
        render_charter_injection_block)
    with flask_app.app_context():
        return render_charter_injection_block(_PROJ)


def test_the_north_star_reaches_the_model_even_behind_many_decisions(flask_app):
    """THE regression. 40 decisions (double the 20-wide window) must not be able
    to push the goal out of the injected block."""
    _commit_goal(flask_app)
    _commit_decisions(flask_app, 40)

    block = _render(flask_app)
    assert _GOAL in block, (
        'The north star is NOT in the block the model receives. This is the '
        'exact production defect: the goal was invisible while 20 '
        'implementation decisions were presented as "authoritative shared '
        f'intent". Rendered block was {len(block)} chars.')


def test_the_north_star_is_rendered_before_the_decisions(flask_app):
    """Order is load-bearing: the goal must be the FIRST thing read, not buried
    under a wall of decisions the model may never reach."""
    _commit_goal(flask_app)
    _commit_decisions(flask_app, 5)

    block = _render(flask_app)
    assert _GOAL in block, 'north star missing entirely'
    assert 'filler decision #0' in block, 'decisions missing — fixture broken'
    assert block.index(_GOAL) < block.index('filler decision #0'), (
        'The north star must precede the committed decisions in the injected '
        'block.')


def test_eviction_never_deletes_the_north_star(flask_app):
    """The complement to the window test, at the STORAGE layer.

    The measured FIFO eviction (``decisions[-100:]``) permanently deleted the
    goal once enough decisions landed. Storing the goal in its own column makes
    that structurally impossible — this asserts the OUTCOME (goal survives),
    not the storage mechanism, so a future re-design of eviction stays free.
    """
    _commit_goal(flask_app)
    _commit_decisions(flask_app, 130)  # well past the 100-decision cap

    from lib.conversations.project_charter import read_charter
    with flask_app.app_context():
        rec = read_charter(_PROJ)
    assert _GOAL in (rec.get('content') or ''), (
        'The north star was EVICTED by decision churn. It must not live in a '
        'FIFO-truncated collection.')
    assert _GOAL in _render(flask_app), (
        'north star survived storage but vanished from the injected block')


def test_a_missing_north_star_is_visible_not_silent(flask_app):
    """An absent goal must ANNOUNCE itself.

    The production failure stayed latent precisely because an empty ``content``
    rendered as nothing at all — neither the human nor the model could tell the
    project had no stated goal. Absence must be observable.
    """
    from lib.conversations.project_charter import commit_charter
    with flask_app.app_context():
        commit_charter(_PROJ, add_decision='some decision',
                       updated_by_conv='agent')

    block = _render(flask_app)
    assert block, 'a charter with decisions must still render'
    assert 'some decision' in block
    lowered = block.lower()
    assert ('no north-star' in lowered or 'not set' in lowered
            or 'no goal' in lowered), (
        'A charter with NO north star renders without any signal that the goal '
        'is missing — that silence is what let the production defect hide. The '
        f'block must say so explicitly. Got: {block[:400]!r}')


def test_the_decision_window_is_still_bounded(flask_app):
    """COMPLEMENT: injecting the goal must not become "inject everything".

    Without this, a renderer that dumped all 130 decisions would satisfy every
    other test here while re-creating the context-bloat the window prevents.
    """
    _commit_goal(flask_app)
    _commit_decisions(flask_app, 130)

    block = _render(flask_app)
    shown = sum(1 for i in range(130) if f'filler decision #{i}\n' in block + '\n')
    assert shown < 130, (
        'Every one of the 130 decisions was injected — the bound on the '
        'decision window is gone.')
    assert 'filler decision #129' in block, (
        'the MOST RECENT decision must still be injected')


# ── Negative controls ────────────────────────────────────────────────────────
# Each neuters ONE load-bearing behaviour and asserts the matching guard turns
# RED. Without these, a renderer that dropped the north star entirely could
# still keep this file green.

def test_NC1_dropping_the_content_render_breaks_visibility(flask_app):
    """NEUTER the unconditional north-star render → the visibility test fails."""
    _commit_goal(flask_app)
    _commit_decisions(flask_app, 40)

    def run(_mod=None):
        block = _render(flask_app)
        assert _GOAL not in block, (
            'NC-1 did not bite: the north star still rendered after its render '
            'branch was neutered — the guard may be passing for another reason.')

    _patch_restore(
        _CHARTER_SRC,
        "    lines.append(rec['content'].strip())",
        "    pass",
        run)


def test_NC2_dropping_the_missing_goal_signal_breaks_observability(flask_app):
    """NEUTER the explicit missing-goal line → the observability test fails."""
    from lib.conversations.project_charter import commit_charter
    with flask_app.app_context():
        commit_charter(_PROJ, add_decision='some decision',
                       updated_by_conv='agent')

    def run(_mod=None):
        block = _render(flask_app)
        lowered = block.lower()
        assert not ('no north-star' in lowered or 'not set' in lowered
                    or 'no goal' in lowered), (
            'NC-2 did not bite: the missing-goal signal survived its neuter.')

    # Neuter the CALL SITE, not the constant's definition: a guard that only
    # proved the constant exists would stay green if the renderer stopped
    # emitting it ("测了 helper 不等于测了接线").
    _patch_restore(
        _CHARTER_SRC,
        '        lines.append(_NO_GOAL_NOTICE)',
        '        pass  # NC-2 neutered',
        run)
