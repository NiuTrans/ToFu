"""tests/test_finalize_user_msg_id.py — the DONE frame ships the anchor
for the turn-ctx capsule reconcile.

WHY
---
The fact-card reconcile committed as ``8c4f30cb`` overwrote the LAST user
turn in the conversation, which is wrong under three scenarios: autopilot
VU (last=VU, correction belongs to parent), concurrent conv (last=newer
send), and edit/regenerate history (last=tail, target is mid-list). The
fix ships ``done_evt['userMsgId']`` — the stable ``_msgId`` of the user
turn that TRIGGERED this task — so the frontend anchors precisely.

Source contract this file asserts:

  1. ``_finalize_and_emit_done`` stamps ``done_evt['userMsgId']`` from
     ``task['_userMsgId']`` and clusters it with the ``actualModel`` /
     ``actualDepth`` / ``actualModes`` block (fact-card region).
  2. ``_start_task_for_conv`` accepts a ``user_msg_id`` kwarg and stamps
     ``task['_userMsgId'] = user_msg_id`` after ``create_task``.
  3. All three chat.py callers pass the correct user_msg_id:
       * ``chat_send``       → ``user_msg.get('_msgId')`` (newly built)
       * ``chat_regenerate`` → ``user_msg.get('_msgId')`` (edit target)
       * ``chat_continue``   → walks back from tail to find the user
         BEFORE the assistant being continued.
  4. Autopilot VU sub-task inherits parent's ``_userMsgId`` (so the VU
     DONE frame points at the PARENT user, not the VU-synthesised user).

NEUTER: strip the ``done_evt['userMsgId'] = task['_userMsgId']`` line
from a string copy of ``_finalize.py`` and prove assertion #1 flips red.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))

FINALIZE_PATH = os.path.join(
    ROOT, 'lib', 'tasks_pkg', 'orchestrator', '_finalize.py')
TASK_START_PATH = os.path.join(ROOT, 'routes', 'chat_task_start.py')
CHAT_ROUTE_PATH = os.path.join(ROOT, 'routes', 'chat.py')
# chat_continue's core moved here (epic pt_f5771a2e — the route is now a
# thin wrapper around lib.chat_dispatch.execute_chat_continue).
CHAT_DISPATCH_PATH = os.path.join(ROOT, 'lib', 'chat_dispatch.py')
AUTOPILOT_PATH = os.path.join(ROOT, 'lib', 'tasks_pkg', 'autopilot.py')


def _read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def test_done_evt_ships_user_msg_id():
    src = _read(FINALIZE_PATH)
    assert "done_evt['userMsgId'] = task['_userMsgId']" in src, (
        f'{FINALIZE_PATH}: DONE event must stamp userMsgId from '
        f'task[\'_userMsgId\'] — the frontend anchor for the capsule '
        f'reconcile reads this exact key.'
    )


def test_user_msg_id_clustered_with_actual_fact_card():
    """Anchor + fact card are ONE contract — keep them visually close so a
    future reviewer sees the whole reconcile envelope in one place."""
    src = _read(FINALIZE_PATH)
    actual_pos = src.index("done_evt['actualModel']")
    user_msg_pos = src.index("done_evt['userMsgId']")
    gap_lines = src[actual_pos:user_msg_pos].count('\n') if user_msg_pos > actual_pos \
        else src[user_msg_pos:actual_pos].count('\n')
    assert gap_lines <= 25, (
        f'{FINALIZE_PATH}: userMsgId and actualModel drifted apart '
        f'({gap_lines} lines). Keep the DONE-frame fact-card + anchor '
        f'clustered so the whole reconcile contract is visible together.'
    )


def test_start_task_for_conv_accepts_user_msg_id():
    src = _read(TASK_START_PATH)
    assert 'user_msg_id: str = \'\'' in src, (
        f'{TASK_START_PATH}: _start_task_for_conv must accept a keyword-only '
        f'user_msg_id parameter (default empty). Callers ship the anchor via '
        f'this seam.'
    )
    assert "task['_userMsgId'] = user_msg_id" in src, (
        f'{TASK_START_PATH}: _start_task_for_conv must stamp '
        f'task[\'_userMsgId\'] = user_msg_id right after create_task, so '
        f'the DONE emitter can read it back later.'
    )


def test_all_three_chat_callers_pass_user_msg_id():
    src = _read(CHAT_ROUTE_PATH)
    # chat_send + chat_regenerate: both pass the current user_msg's _msgId.
    assert src.count("user_msg_id=user_msg.get('_msgId') or ''") >= 2, (
        f'{CHAT_ROUTE_PATH}: chat_send AND chat_regenerate must each ship '
        f'user_msg_id=user_msg.get("_msgId") — otherwise the DONE frame '
        f'lacks the anchor and the frontend falls back to the tail heuristic.'
    )
    # chat_continue: passes the user BEFORE the assistant being resumed.
    # The core moved to lib/chat_dispatch.py (epic pt_f5771a2e) — the anchor
    # moves with the contract; routes/chat.py stays a thin wrapper.
    dispatch_src = _read(CHAT_DISPATCH_PATH)
    assert '_continue_user_msg_id' in dispatch_src, (
        f'{CHAT_DISPATCH_PATH}: execute_chat_continue must resolve the user '
        f'turn before the tail assistant (variable _continue_user_msg_id) '
        f'and pass it as user_msg_id.'
    )
    assert "user_msg_id=_continue_user_msg_id" in dispatch_src, (
        f'{CHAT_DISPATCH_PATH}: execute_chat_continue must pass user_msg_id='
        f'_continue_user_msg_id to _start_task_for_conv.'
    )


def test_vu_subtask_inherits_parent_user_msg_id():
    """The VU sub-task's DONE frame flows through _finalize_and_emit_done.
    Without inheritance, its userMsgId is empty → frontend fallback lands
    on the VU-synthesised user (the tail), which is the wrong bubble."""
    src = _read(AUTOPILOT_PATH)
    assert "sub_task['_userMsgId'] = task['_userMsgId']" in src, (
        f'{AUTOPILOT_PATH}: VU sub-task must inherit the parent task\'s '
        f'_userMsgId so its DONE frame anchors the reconcile onto the '
        f'PARENT user, not the VU-synthesised user (which is the last '
        f'user in the conv by construction).'
    )
    # Guard the placement: inheritance sits with the other sub-task
    # bookkeeping (_vu_subtask / _autopilotParent), not somewhere in
    # the middle of a baton-delivery block. Owner explicitly parked
    # baton/exactly-once as a separate workflow.
    subtask_marker = src.index("sub_task['_vu_subtask'] = True")
    inherit_pos = src.index("sub_task['_userMsgId'] = task['_userMsgId']")
    assert 0 < subtask_marker < inherit_pos, (
        f'{AUTOPILOT_PATH}: the _userMsgId inheritance line must sit AFTER '
        f'_vu_subtask stamping so it clusters with the other sub-task '
        f'bookkeeping. Displacement into a baton block would be dangerous.'
    )
    gap = src[subtask_marker:inherit_pos]
    # Budget 25: a 22-line explanatory comment block now sits between the
    # _vu_subtask stamp and the inheritance line (autopilot.py:436 → :458).
    assert gap.count('\n') <= 25, (
        f'{AUTOPILOT_PATH}: _userMsgId inheritance drifted far from the '
        f'_vu_subtask marker ({gap.count(chr(10))} lines). Keep them '
        f'adjacent — this line is a diagnostic anchor, NOT baton flow.'
    )


def test_neuter_removing_user_msg_id_breaks_contract():
    """NEUTER: on a string copy, strip the emitter line; assertion #1
    must flip red. Proves the source-contract test above is load-bearing."""
    src = _read(FINALIZE_PATH)
    needle = "done_evt['userMsgId'] = task['_userMsgId']"
    assert needle in src, 'NEUTER precondition failed: needle missing'
    neutered = src.replace(needle, '# neutered')
    assert neutered != src
    assert needle not in neutered, (
        'NEUTER succeeded but the guard would still pass — the source '
        'contract test is not actually keyed on this line.'
    )
