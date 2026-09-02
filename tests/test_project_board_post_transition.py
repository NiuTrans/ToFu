"""tests/test_project_board_post_transition.py — the board "post" tool card
must actually show WHAT was posted.

Reported bug: the ``project_board_post`` tool card in the transcript rendered a
bare verb badge ("发布" / "posted") with no epic title — "doesn't show what was
released at all". Root cause was in the handler's ``_post_build``
(``lib/tasks_pkg/handlers/misc/_brain.py``): the structured ``boardTransition``
meta the frontend card renders was built by looking up the epic BY ``task_id``
FROM THE TOOL ARGS — but a POST has NO ``task_id`` in its args (the id is minted
server-side and only appears in the result string). So ``title``/``status`` came
back blank and the card showed nothing after the verb.

The fix keys a POST off the args ``title`` + the id parsed from the result
string ("Posted epic <id> to the board.") with ``status='open'`` (a freshly
posted epic is always open), preferring the authoritative board row when the id
resolves. Non-post mutations still key off the ``task_id`` arg.

These drive the REAL ``_post_build`` closure directly (capturing it via a
stubbed ``simple_call``, the same technique as
``tests/test_mcp_tool_links.py::PostBuildTitleTest``) so no live DB / dispatch
registry / module reload is needed — the board re-read is monkeypatched to a
controllable in-memory board. Every load-bearing assertion has a byte-reverting
negative control.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
_BRAIN_SRC = os.path.join(ROOT, 'lib', 'tasks_pkg', 'handlers', 'misc', '_brain.py')

from tests._nc_harness import neutered_source  # noqa: E402


def _run_board_post_build(fn_name, fn_args, tool_content, board_tasks,
                          *, brain_module=None):
    """Capture and drive _handle_board_tool's _post_build closure.

    Stubs ``simple_call`` (grabbing the ``post_build`` callback) and the board
    read (returning a controllable in-memory task list), then invokes the
    callback against a fresh ``meta`` dict. Returns that ``meta``.

    ``brain_module`` lets a negative control pass a neutered variant of the
    handler module; defaults to the shipped one.
    """
    import lib.tasks_pkg.handlers.misc._brain as _shipped
    brain = brain_module or _shipped

    captured = {}

    def _fake_simple_call(task, fn, args, rn, round_entry, tc_id,
                          *, executor, source, module_tag='', badge='',
                          post_build=None, **_kw):
        captured['post_build'] = post_build
        return tc_id, tool_content, False

    orig_simple_call = brain.simple_call
    # Patch read_board where the closure imports it from (a function-body
    # ``from lib.conversations.project_board import read_board``).
    import lib.conversations.project_board as pb
    orig_read_board = pb.read_board
    brain.simple_call = _fake_simple_call
    pb.read_board = lambda _p, **_kw: {'tasks': list(board_tasks),
                                       'open': 0, 'claimed': 0, 'done': 0}
    try:
        brain._handle_board_tool(
            {'convId': 'cPoster', '_userId': 1}, {}, fn_name, 'tc1', fn_args,
            1, {}, {},
            '/proj/x', True)
        meta = {}
        assert captured.get('post_build') is not None, \
            'the board handler must pass a post_build closure to simple_call'
        captured['post_build'](meta, tool_content, fn_args)
        return meta
    finally:
        brain.simple_call = orig_simple_call
        pb.read_board = orig_read_board


# ════════════════════════════════════════════════════════════════════
#  The reported bug: a POST card must carry the epic title + id + open.
# ════════════════════════════════════════════════════════════════════

def test_post_transition_carries_title_id_and_open_status():
    title = 'Redesign the release dashboard so it shows what shipped'
    meta = _run_board_post_build(
        'project_board_post', {'title': title},
        'Posted epic pt_abc123def456 to the board.',
        board_tasks=[{'id': 'pt_abc123def456', 'title': title, 'status': 'open'}],
    )
    tr = meta.get('boardTransition') or {}
    assert tr.get('verb') == 'post'
    assert tr.get('title') == title, \
        ('the POST card must surface the posted epic TITLE, got '
         f'{tr.get("title")!r} — this is the "shows nothing" bug')
    assert tr.get('status') == 'open', 'a freshly posted epic is always open'
    assert tr.get('taskId') == 'pt_abc123def456', \
        'the minted id must be parsed from the result string for the id chip'


def test_post_transition_title_survives_empty_board_row():
    """If the board re-read races/returns nothing for the id, the args title is
    the fallback so the card is never blank."""
    title = 'Some epic that will not be in the board read'
    meta = _run_board_post_build(
        'project_board_post', {'title': title},
        'Posted epic pt_deadbeef0000 to the board.',
        board_tasks=[],  # id resolves to nothing
    )
    tr = meta.get('boardTransition') or {}
    assert tr.get('title') == title, \
        'the args title must survive an empty board re-read'
    assert tr.get('status') == 'open'
    assert tr.get('taskId') == 'pt_deadbeef0000', \
        'the id is still parsed from the result even when the row is absent'


def test_post_transition_prefers_board_row_title_over_args():
    """When the id resolves, the authoritative (length-capped) board title wins
    over the raw args title."""
    meta = _run_board_post_build(
        'project_board_post', {'title': 'raw args title'},
        'Posted epic pt_cafe12345678 to the board.',
        board_tasks=[{'id': 'pt_cafe12345678', 'title': 'stored board title',
                      'status': 'open'}],
    )
    assert (meta.get('boardTransition') or {}).get('title') == 'stored board title'


def test_post_transition_no_id_in_result_still_shows_title():
    """A malformed / id-less result string must still yield the args title
    (verb+title card), just without an id chip."""
    meta = _run_board_post_build(
        'project_board_post', {'title': 'Epic without a parseable id'},
        'Error posting epic: board full (coarse epics only).',
        board_tasks=[],
    )
    tr = meta.get('boardTransition') or {}
    assert tr.get('title') == 'Epic without a parseable id'
    assert tr.get('taskId') == '', 'no id chip when the result carries no pt_ id'


# ════════════════════════════════════════════════════════════════════
#  A FAILED mutation must carry ok=false + error (the reported "no visible
#  failure, only in the raw model text" bug) and NOT a guessed 'open' status.
# ════════════════════════════════════════════════════════════════════

def test_failed_post_carries_error_and_no_guessed_status():
    meta = _run_board_post_build(
        'project_board_post', {'title': 'A doomed epic'},
        'Error posting epic: board full: 200 active epics '
        '(complete or reopen some before posting more).',
        board_tasks=[],
    )
    tr = meta.get('boardTransition') or {}
    assert tr.get('ok') is False, 'a failed mutation must carry ok=false'
    assert 'board full' in (tr.get('error') or ''), \
        'the error message must be surfaced on the transition meta'
    assert tr.get('status') == '', \
        "a failed post posts NOTHING — a guessed 'open' status would be a lie"
    assert tr.get('title') == 'A doomed epic', 'the attempted title still shows'


def test_failed_claim_carries_error():
    meta = _run_board_post_build(
        'project_board_claim', {'task_id': 'pt_taken0001'},
        'NOT claimed — epic is already being advanced by conversation cOther.',
        board_tasks=[{'id': 'pt_taken0001', 'title': 'Contended epic',
                      'status': 'claimed'}],
    )
    tr = meta.get('boardTransition') or {}
    assert tr.get('ok') is False
    assert 'already being advanced' in (tr.get('error') or '')
    assert tr.get('status') == ''


def test_successful_mutation_marks_ok_true():
    meta = _run_board_post_build(
        'project_board_post', {'title': 'A good epic'},
        'Posted epic pt_good00001111 to the board.',
        board_tasks=[{'id': 'pt_good00001111', 'title': 'A good epic',
                      'status': 'open'}],
    )
    tr = meta.get('boardTransition') or {}
    assert tr.get('ok') is True, 'a successful mutation must carry ok=true'
    assert not (tr.get('error') or ''), 'no error on success'
    assert tr.get('status') == 'open'


# ════════════════════════════════════════════════════════════════════
#  Non-post mutations still key off the task_id arg (regression guard).
# ════════════════════════════════════════════════════════════════════

def test_claim_transition_still_keys_off_task_id():
    meta = _run_board_post_build(
        'project_board_claim', {'task_id': 'pt_claim0001'},
        'Claimed.',
        board_tasks=[{'id': 'pt_claim0001', 'title': 'Claimable epic',
                      'status': 'claimed'}],
    )
    tr = meta.get('boardTransition') or {}
    assert tr.get('verb') == 'claim'
    assert tr.get('taskId') == 'pt_claim0001'
    assert tr.get('title') == 'Claimable epic', \
        'claim/complete/block still resolve the title via the task_id arg lookup'
    assert tr.get('status') == 'claimed'


def test_board_read_produces_snapshot_not_transition():
    """Regression: a READ yields boardSnapshot (mini-kanban), not a transition —
    the post fix only touches the mutation branch."""
    meta = _run_board_post_build(
        'project_board_read', {},
        'RAW BOARD PROSE',
        board_tasks=[{'id': 'pt_r1', 'title': 'An epic', 'status': 'open'}],
    )
    assert meta.get('boardSnapshot') and not meta.get('boardTransition')


# ════════════════════════════════════════════════════════════════════
#  NEGATIVE CONTROL — byte-reverting (in-memory), proving the fix is
#  load-bearing: with the POST branch removed, a post reverts to the OLD
#  task_id-arg lookup and resolves a BLANK title (the reported bug).
# ════════════════════════════════════════════════════════════════════

def test_NC_post_branch_is_load_bearing():
    title = 'NC epic title that must vanish without the post branch'
    with neutered_source(
        _BRAIN_SRC,
        '            if fn_name == "project_board_post":',
        '            if fn_name == "__nc_never_matches__":',
    ) as neutered:
        meta = _run_board_post_build(
            'project_board_post', {'title': title},
            'Posted epic pt_abc123def456 to the board.',
            board_tasks=[{'id': 'pt_abc123def456', 'title': title,
                          'status': 'open'}],
            brain_module=neutered,
        )
        tr = meta.get('boardTransition') or {}
        assert tr.get('title') != title, \
            ('NC: with the POST branch removed a post falls back to the OLD '
             'task_id-arg lookup (no task_id in args) → BLANK title, proving '
             f'the branch is what fixes the bug; got {tr.get("title")!r}')
        assert not (tr.get('title') or '').strip(), \
            'the OLD lookup must resolve an empty title for a post'


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
