"""tests/test_project_board_injection_diet.py — the board INJECTION is a
coordination summary; the TOOL is the detail channel.

## The measured problem

``render_board_block`` served two consumers with one output:

  * the per-turn ``[PROJECT BOARD]`` prompt injection, and
  * the ``project_board_read`` agent tool.

On the live chatui board that output was **16,764 chars across 16 epics**, with
a single epic "title" reaching **2,063 chars** — whole technical specs stored in
the title field (the cap is 2000, so it was effectively saturated). Every turn
of every project conversation paid for all of it.

But the injection only has to answer ONE question: *who is doing what, so I
don't collide with them.* The full spec matters when you PICK UP an epic, which
is a deliberate act the model can spend a tool round on.

## The two directions, both pinned

A guard that only said "the injection is small" could be satisfied by gutting
the tool too — destroying the model's only route to the detail. A guard that
only said "the tool is complete" is satisfied by changing nothing. So both are
asserted here, and the NCs bite in both directions.

## Why the truncation must ANNOUNCE itself

Charter discipline (the ``read_files`` 800k-ceiling precedent): a silently
shortened result is worse than a long one, because the model cannot tell that
what it holds is partial and will reason confidently over a fragment. The
injected line must therefore say it is abridged AND name the tool that returns
the rest.
"""

from __future__ import annotations

import os

import pytest

from tests._nc_harness import patch_restore as _patch_restore

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
_BOARD_SRC = os.path.join(ROOT, 'lib', 'conversations', 'project_board.py')

# Shaped like the real offenders: a one-line headline followed by a full spec.
_HEADLINE = '[arch] Split the orchestrator run loop into phase seams'
_SPEC_TAIL = 'TAIL_SENTINEL_deadbeef_END'
_LONG_TITLE = (
    _HEADLINE + '\n'
    + ('Detailed rationale paragraph that belongs in the ticket body rather '
       'than in every single prompt of every sibling conversation. ' * 12)
    + _SPEC_TAIL
)
_PROJ = '/tmp/tofu-board-diet-guard'


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
        db.commit()
    yield


@pytest.fixture(autouse=True)
def _stub_push(monkeypatch):
    monkeypatch.setattr('lib.agent_core.push.push_event', lambda *a, **k: None)


def _seed(flask_app, title=_LONG_TITLE):
    from lib.conversations.project_board import post_task
    with flask_app.app_context():
        r = post_task(_PROJ, 'cA', title)
    assert r['ok'], r
    return r['id']


def _injected(flask_app, conv='cREADER'):
    from lib.conversations.project_board import render_board_injection_block
    with flask_app.app_context():
        return render_board_injection_block(_PROJ, current_conv_id=conv)


def _tool_output(flask_app, conv='cREADER'):
    from lib.conversations.project_board import execute_board_tool
    with flask_app.app_context():
        return execute_board_tool('project_board_read', {},
                                  current_conv_id=conv, project_path=_PROJ)


def test_the_injection_does_not_carry_the_full_spec(flask_app):
    """THE regression: a 2,000-char spec must not ride every turn."""
    _seed(flask_app)
    block = _injected(flask_app)
    assert _SPEC_TAIL not in block, (
        'The full epic spec is still being injected into every turn. That is '
        f'the measured defect. Block is {len(block)} chars.')
    assert len(block) < len(_LONG_TITLE), (
        'the injected block is no smaller than a single epic title')


def test_the_injection_still_carries_what_coordination_needs(flask_app):
    """COMPLEMENT #1: slimming must not destroy the coordination signal.

    Deleting the board block entirely would satisfy the test above; this
    pins the four facts a sibling actually needs to avoid a collision.
    """
    from lib.conversations.project_board import claim_task
    tid = _seed(flask_app)
    with flask_app.app_context():
        claim_task(_PROJ, 'cOWNER', tid)

    block = _injected(flask_app)
    assert '[PROJECT BOARD]' in block, 'the marker must survive'
    assert tid in block, 'the epic id must survive (it addresses the detail read)'
    assert _HEADLINE.split(']')[1].strip()[:30] in block, \
        'the headline (first line) must survive'
    assert 'cOWNER' in block, 'the owner conversation must survive'
    assert 'do not redo' in block.lower(), \
        'the avoid-duplication hint is the whole point of the injection'


def test_the_abridgement_announces_itself_and_names_the_detail_route(flask_app):
    """COMPLEMENT #2: a silently shortened epic is a trap.

    The model must be able to tell it is holding a summary, and must be told
    how to obtain the rest — otherwise it reasons confidently over a fragment.
    """
    _seed(flask_app)
    block = _injected(flask_app)
    assert 'project_board_read' in block, (
        'the injected block abridges epics but never names the tool that '
        'returns the full text')


def test_a_short_epic_is_not_marked_as_abridged(flask_app):
    """COMPLEMENT #3: the abridgement notice must be CONDITIONAL.

    Stamping every epic as truncated would make the marker meaningless — and
    an unconditional notice would keep the test above green even if the
    truncation logic were deleted.
    """
    _seed(flask_app, title='Short epic that fits fine')
    block = _injected(flask_app)
    assert 'Short epic that fits fine' in block
    assert '…' not in block.split('Short epic that fits fine')[1].split('\n')[0], \
        'a short epic must not be decorated with an ellipsis/abridged marker'


def test_the_tool_still_returns_the_complete_spec(flask_app):
    """COMPLEMENT #4 — the load-bearing one.

    Injection slimming is only acceptable because the detail remains
    reachable. If the tool loses the full text the model can NEVER recover it,
    which is strictly worse than the bloat we removed.
    """
    _seed(flask_app)
    out = _tool_output(flask_app)
    assert _SPEC_TAIL in out, (
        'project_board_read no longer returns the full epic text — the detail '
        'channel was destroyed along with the injection bloat.')


def test_storage_remains_byte_identical(flask_app):
    """The original write-time-clip regression, still pinned at the source."""
    from lib.conversations.project_board import read_board
    _seed(flask_app)
    with flask_app.app_context():
        stored = read_board(_PROJ)['tasks'][0]['title']
    assert stored == _LONG_TITLE, 'stored title must be BYTE-IDENTICAL (uncapped)'


# ── Negative controls ────────────────────────────────────────────────────────

def test_NC1_restoring_full_titles_to_the_injection_breaks_the_diet(flask_app):
    """NEUTER the per-epic abridgement → the diet test fails."""
    _seed(flask_app)

    def run(_mod=None):
        block = _injected(flask_app)
        # With abridgement neutered the spec floods back into the injection
        # — i.e. the diet test above would now FAIL. That is the proof the
        # diet is enforced by _abridge_title and not by something incidental.
        assert _SPEC_TAIL in block, (
            'NC-1 did not bite: the spec stayed OUT of the injection even with '
            'abridgement neutered — the diet must be coming from somewhere '
            'else, so the guard is not pinning what it claims to pin.')

    _patch_restore(
        _BOARD_SRC,
        'def _abridge_title(title: str) -> str:',
        'def _abridge_title(title: str) -> str:\n    return title',
        run)


def test_NC2_abridging_the_tool_output_breaks_the_detail_channel(flask_app):
    """NEUTER the tool's full-text path → the detail-channel test fails.

    This is the direction that protects against "slim both and call it done".
    """
    _seed(flask_app)

    def run(_mod=None):
        out = _tool_output(flask_app)
        # Pointing the TOOL at the abridged renderer destroys the detail
        # channel — the complement test above would now FAIL. This is the
        # direction that stops "slim both and call it done".
        assert _SPEC_TAIL not in out, (
            'NC-2 did not bite: the tool still returned the full spec after '
            'its renderer was swapped for the abridged one — the detail-channel '
            'test is not actually pinned to the full-text render.')

    _patch_restore(
        _BOARD_SRC,
        "            block = render_board_block(project_path, current_conv_id)",
        "            block = render_board_injection_block(project_path, current_conv_id)",
        run)
