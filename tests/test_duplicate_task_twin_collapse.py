#!/usr/bin/env python3
"""A parent reply committed TWICE — same ``_taskId``, two assistant rows.

THE BUG (measured across the whole production DB, not a sample)
---------------------------------------------------------------
87 of 4163 conversations carry 138 EXTRA assistant rows: two (or more) rows
sharing ONE ``_taskId``, byte-identical content, differing only in identity
(``9acb2ffa-…`` server UUID vs ``tmp_3bec8833…`` client id) and in which
payload fields survived. Flagship: conv ``ms0z3wedmvs5l9`` msgs[17]/[20]. Worst
case: ``mrx1eknh7k8t63`` where ONE reply is stored EIGHT times.

The user sees the same answer twice; one copy is often thinking-less (5075
chars of reasoning lost on the twin), and when the poorer copy sorts LAST it
becomes the turn's "final state" source.

Provenance is the ``tmp_`` prefix: the server mints a UUID (or honours the
client's ``_assistantMsgId``) on commit — it can NEVER write a ``tmp_`` id. So
these rows are CLIENT twins that rode a full PUT. ``_rebaseUnackedTail``
(``conv_persist_helpers.js``) already carries a ``_taskId`` dedup for exactly
this twin, but it runs ONLY on the 409-CAS rescue-PUT path; a twin that lands
via a normal full PUT never meets it.

WHAT THIS SUITE PINS
--------------------
The verdict belongs in ``reconcile_conversation_messages`` — the ONE pure
predicate both seams already call (``_save_conv_blocking`` sweeps the incoming
PUT payload with it; ``get_conv`` heals stored rows with it). One function
therefore closes the write seam AND heals the 138 rows already on disk, instead
of a bespoke dedup bolted onto the PUT entry.

SAFETY BOUNDARY — measured, not assumed. A blind ``_taskId`` dedup would
DESTROY data, so the collapse fires only when all three hold:

  (1) both rows are assistants sharing a ``_taskId``;
  (2) NO user turn sits between them — a span across a user turn is two
      different exchanges, and collapsing it would reshape history (34 of 121
      live dup groups span a user turn);
  (3) the dropped row is LOSSLESSLY SUBSUMED: every payload field it carries is
      byte-equal to the keeper's after transient-diagnostic normalization, and
      it holds no terminal fact the keeper lacks.

Measured against the production DB this fires on 57 groups / 120 rows and
DELIBERATELY leaves 64 groups alone — those have genuinely divergent
toolRounds/thinking/segments (78 true-rival field instances), where a drop
would lose real content. Honest partial coverage beats silent data loss.

Two collateral-damage checks are pinned as tests because the whole-DB scan
proved they matter: endpoint rows (planner/worker share ONE task dict) and
autopilot-VU rows must never be collapsed. Live evidence: only 9 of 514
endpoint-marked rows carry a ``_taskId`` and ZERO endpoint groups share one; VU
rows carry none at all — but a future change could alter that, so the guard is
asserted, not inferred.
"""
import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.unit


def _rc(messages, prefix=0):
    from lib.conversations.reconcile import reconcile_conversation_messages
    return reconcile_conversation_messages(copy.deepcopy(messages), prefix)


def _asst(task_id, content='ANSWER', **kw):
    m = {'role': 'assistant', 'content': content, 'thinking': '',
         'finishReason': 'stop', '_taskId': task_id}
    m.update(kw)
    return m


def _user(content='q', **kw):
    m = {'role': 'user', 'content': content}
    m.update(kw)
    return m


# ─────────────────────────────────────────────────────────────────────────
# 1. The live artifact — conv ms0z3wedmvs5l9 msgs[17]/[20] in miniature.
# ─────────────────────────────────────────────────────────────────────────
def test_tmp_twin_of_a_committed_reply_is_collapsed():
    """★ THE INVARIANT. The client twin (tmp_ id, thinking lost) is dropped and
    the server-committed row — with its 5075 chars of reasoning — survives."""
    msgs = [
        _user('the real question', _msgId='u1'),
        _asst('task-A', 'THE ANSWER', thinking='LONG REASONING',
              _msgId='9acb2ffa-3a9b-45c4-8577-b0d08bb42c0f',
              usage={'output_tokens': 10}),
        _asst('task-A', 'THE ANSWER', thinking='',
              _msgId='tmp_3bec8833-1334-4eac-a67b-00ce31453bc6'),
    ]
    out, changed = _rc(msgs)
    assert changed, 'the duplicate twin was not collapsed'
    assert len(out) == 2, (
        f'expected [user, assistant], got '
        f'{[(m["role"], (m.get("_msgId") or "")[:12]) for m in out]}')
    kept = out[-1]
    assert kept['_msgId'].startswith('9acb2ffa'), (
        f'the SERVER-committed row must win, kept {kept["_msgId"]!r}')
    assert kept['thinking'] == 'LONG REASONING', (
        'the richer row was dropped — reasoning lost, which is the WORSE half '
        'of this bug')


def test_eight_way_duplication_collapses_to_one():
    """conv mrx1eknh7k8t63 stores ONE reply 8 times. All 7 twins go."""
    msgs = [_user('q', _msgId='u1')]
    msgs.append(_asst('task-X', 'SAME', _msgId='53d7eedf-real'))
    for i in range(7):
        msgs.append(_asst('task-X', 'SAME', _msgId=f'tmp_{i}'))
    out, changed = _rc(msgs)
    assert changed
    assert len(out) == 2, f'expected 2 rows, got {len(out)}'
    assert out[-1]['_msgId'] == '53d7eedf-real'


def test_empty_twin_is_collapsed_into_the_bearing_row():
    """A twin whose payload fields are all EMPTY is subsumed by definition."""
    msgs = [
        _user(_msgId='u1'),
        _asst('task-A', 'REAL ANSWER', thinking='T', _msgId='srv-1',
              toolRounds=[{'status': 'done', 'toolContent': 'out'}]),
        {'role': 'assistant', 'content': '', 'thinking': '', '_taskId': 'task-A',
         '_msgId': 'tmp_empty'},
    ]
    out, changed = _rc(msgs)
    assert changed
    assert [m.get('_msgId') for m in out] == ['u1', 'srv-1']


# ─────────────────────────────────────────────────────────────────────────
# 2. SAFETY — the three guards, each with a measured live counterpart.
# ─────────────────────────────────────────────────────────────────────────
def test_divergent_payload_is_never_collapsed():
    """64 live groups have genuinely different toolRounds/thinking/segments.
    Dropping either row loses real content — so BOTH must survive."""
    msgs = [
        _user(_msgId='u1'),
        _asst('task-A', 'ANSWER', _msgId='a1',
              toolRounds=[{'status': 'done', 'toolContent': 'FIRST'}]),
        _asst('task-A', 'ANSWER', _msgId='a2',
              toolRounds=[{'status': 'done', 'toolContent': 'SECOND — different'}]),
    ]
    out, changed = _rc(msgs)
    assert len(out) == 3, (
        'a group with DIVERGENT toolRounds was collapsed — that destroys real '
        'content; honest partial coverage is required instead')
    assert not changed


def test_divergent_thinking_is_never_collapsed():
    msgs = [
        _user(_msgId='u1'),
        _asst('task-A', 'ANSWER', thinking='REASONING ONE', _msgId='a1'),
        _asst('task-A', 'ANSWER', thinking='REASONING TWO', _msgId='a2'),
    ]
    out, _ = _rc(msgs)
    assert len(out) == 3, 'divergent thinking must not be collapsed'


def test_rows_spanning_a_user_turn_are_never_collapsed():
    """34 live dup groups span a user turn — those are two exchanges. Collapsing
    would reshape history (and leave a user turn with no reply)."""
    msgs = [
        _user('first', _msgId='u1'),
        _asst('task-A', 'ANSWER', _msgId='a1'),
        _user('second', _msgId='u2'),
        _asst('task-A', 'ANSWER', _msgId='a2'),   # same taskId, different exchange
    ]
    out, changed = _rc(msgs)
    assert len(out) == 4, (
        'a duplicate SPANNING a user turn was collapsed — that leaves the '
        'second user turn unanswered and reorders history')
    assert not changed


def test_twin_carrying_a_terminal_fact_the_keeper_lacks_is_never_collapsed():
    """The twin holds an `error` the keeper does not — dropping it hides a
    real terminal outcome."""
    msgs = [
        _user(_msgId='u1'),
        _asst('task-A', 'ANSWER', _msgId='a1'),
        _asst('task-A', 'ANSWER', _msgId='a2',
              error={'kind': 'server_offline', 'message': 'x'}),
    ]
    out, _ = _rc(msgs)
    assert len(out) == 3, 'a twin bearing an unmatched terminal fact was dropped'


def test_endpoint_rows_sharing_a_task_id_are_never_collapsed():
    """Endpoint mode runs planner + worker_1..N on ONE task dict. If those rows
    ever carry a _taskId, a blind dedup would destroy the whole session."""
    msgs = [
        _user(_msgId='u1'),
        _asst('task-A', 'PLAN', _msgId='p1', _isEndpointPlanner=True),
        _asst('task-A', 'PLAN', _msgId='w1', _epIteration=1),
    ]
    out, _ = _rc(msgs)
    assert len(out) == 3, (
        'endpoint planner/worker rows sharing one _taskId were collapsed — '
        'that destroys an endpoint session')


def test_virtual_user_rows_are_never_collapsed():
    msgs = [
        _user(_msgId='u1'),
        _asst('task-A', 'ANSWER', _msgId='a1'),
        {'role': 'assistant', 'content': 'ANSWER', '_taskId': 'task-A',
         '_msgId': 'vu1', '_isVirtualUser': True},
    ]
    out, _ = _rc(msgs)
    assert len(out) == 3, 'a virtual-user row was collapsed'


def test_rows_without_a_task_id_are_never_collapsed():
    """No _taskId = no identity claim. Two identical-looking replies could be a
    genuine regenerate pair."""
    msgs = [
        _user(_msgId='u1'),
        {'role': 'assistant', 'content': 'SAME', '_msgId': 'a1'},
        {'role': 'assistant', 'content': 'SAME', '_msgId': 'a2'},
    ]
    out, _ = _rc(msgs)
    assert len(out) == 3, 'rows lacking _taskId must not be collapsed'


def test_cache_prefix_rows_are_never_collapsed():
    """Removing an in-prefix message shifts every following byte and busts the
    prompt cache — the same guard the buried-ghost sweep honours."""
    msgs = [
        _user(_msgId='u1'),
        _asst('task-A', 'ANSWER', _msgId='a1'),
        _asst('task-A', 'ANSWER', _msgId='tmp_twin'),
        _user('later', _msgId='u2'),
    ]
    out, changed = _rc(msgs, prefix=4)     # whole history is cache-pinned
    assert len(out) == 4, (
        'a duplicate inside the immutable cache prefix was removed — that '
        'shifts prefix bytes and busts the prompt cache')
    assert not changed


# ─────────────────────────────────────────────────────────────────────────
# 3. Transient-diagnostic normalization — the reason a naive byte-compare
#    under-fires. Live: apiRounds[].usage._wire_routing present on ONE copy
#    made all 23 rounds of ms0z3wedmvs5l9 compare unequal.
# ─────────────────────────────────────────────────────────────────────────
def test_transient_wire_diagnostics_do_not_block_the_collapse():
    msgs = [
        _user(_msgId='u1'),
        _asst('task-A', 'ANSWER', _msgId='a1',
              apiRounds=[{'round': 1, 'usage': {'output_tokens': 5}}]),
        _asst('task-A', 'ANSWER', _msgId='tmp_twin',
              apiRounds=[{'round': 1, 'usage': {'output_tokens': 5,
                                                '_wire_routing': {'k': 'v'}}}]),
    ]
    out, changed = _rc(msgs)
    assert changed and len(out) == 2, (
        'a twin differing ONLY by a transient wire diagnostic was kept — the '
        'live ms0z3wedmvs5l9 pair differs exactly this way on all 23 rounds')
    assert out[-1]['_msgId'] == 'a1'


def test_a_real_usage_difference_still_blocks_the_collapse():
    """REVERSE of the previous test: normalization must strip ONLY the known
    transient keys, never real metering."""
    msgs = [
        _user(_msgId='u1'),
        _asst('task-A', 'ANSWER', _msgId='a1',
              apiRounds=[{'round': 1, 'usage': {'output_tokens': 5}}]),
        _asst('task-A', 'ANSWER', _msgId='a2',
              apiRounds=[{'round': 1, 'usage': {'output_tokens': 999}}]),
    ]
    out, _ = _rc(msgs)
    assert len(out) == 3, 'a REAL usage difference was normalized away'


# ─────────────────────────────────────────────────────────────────────────
# 4. Non-interference with the existing reconcile passes.
# ─────────────────────────────────────────────────────────────────────────
def test_clean_conversation_is_untouched():
    msgs = [
        _user(_msgId='u1'), _asst('task-A', 'A1', _msgId='a1'),
        _user(_msgId='u2'), _asst('task-B', 'A2', _msgId='a2'),
    ]
    out, changed = _rc(msgs)
    assert not changed and len(out) == 4


def test_ghost_tail_pass_still_fires_alongside():
    """The pre-existing tail verdict must keep working with the new pass."""
    msgs = [
        _user(_msgId='u1'),
        _asst('task-A', 'ANSWER', _msgId='a1'),
        _asst('task-A', 'ANSWER', _msgId='tmp_twin'),
        {'role': 'assistant', 'content': '', 'thinking': '', '_msgId': 'ghost'},
    ]
    out, changed = _rc(msgs)
    assert changed
    assert [m.get('_msgId') for m in out] == ['u1', 'a1'], (
        f'expected twin collapsed AND ghost tail deleted, got '
        f'{[m.get("_msgId") for m in out]}')


def test_collapse_is_idempotent():
    msgs = [
        _user(_msgId='u1'),
        _asst('task-A', 'ANSWER', _msgId='a1'),
        _asst('task-A', 'ANSWER', _msgId='tmp_twin'),
    ]
    once, _ = _rc(msgs)
    twice, changed2 = _rc(once)
    assert not changed2, 'reconcile is not idempotent — it would rewrite forever'
    assert once == twice


# ─────────────────────────────────────────────────────────────────────────
# 5. NEUTER — bypass the predicate and the duplicate must come back.
# ─────────────────────────────────────────────────────────────────────────
def test_neuter_predicate_restores_the_duplicate(monkeypatch):
    import lib.conversations.reconcile as rec
    monkeypatch.setattr(rec, 'is_duplicate_task_twin',
                        lambda messages, idx, **kw: False, raising=True)
    msgs = [
        _user(_msgId='u1'),
        _asst('task-A', 'ANSWER', thinking='T', _msgId='a1'),
        _asst('task-A', 'ANSWER', _msgId='tmp_twin'),
    ]
    out, _ = _rc(msgs)
    assert len(out) == 3, (
        'with the predicate neutered the duplicate must reappear — otherwise '
        'this suite is not exercising the guard it claims to')


# ─────────────────────────────────────────────────────────────────────────
# 6. SEAM check — both write and read paths reach the shared verdict, so the
#    stored 138 rows heal AND new twins can never land.
# ─────────────────────────────────────────────────────────────────────────
def test_both_conv_seams_call_the_shared_reconcile():
    import inspect
    import routes.conversations as rc

    put_src = inspect.getsource(rc._save_conv_blocking)
    assert 'reconcile_conversation_messages' in put_src, (
        'the PUT seam no longer sweeps the incoming payload with the shared '
        'reconcile — a client twin would land unchecked')
    get_src = inspect.getsource(rc._compute_reconcile)
    assert 'reconcile_conversation_messages' in get_src, (
        'the GET seam no longer heals stored rows with the shared reconcile — '
        'the duplicates already on disk would never converge')


def test_predicate_is_exported():
    from lib.conversations.reconcile import __all__, is_duplicate_task_twin
    assert 'is_duplicate_task_twin' in __all__
    assert callable(is_duplicate_task_twin)


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v', '-p', 'no:napari']))
