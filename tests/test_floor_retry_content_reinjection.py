"""Regression: FloorRetry adopted-resend content-track convergence.

Root cause (conv mrwwmp0z6u0gkn, task 5ddea609): a round that floor-collapsed
on its first attempt, then RECOVERED on a FloorRetry resend, billed the full
answer (4528 output tokens) but persisted only the FIRST attempt's partial
preamble (215 chars). The two content tracks diverged:

  * task['content'] — accumulated ONLY from the FIRST attempt's on_content
    deltas (the resend streams with on_content=None, so its text never lands
    here).
  * the returned assistant_msg['content'] — the ADOPTED resend's full text.

The terminal turn event projects from ``task['content']``, so the two tracks
must converge before finalization.

The fix (lib/tasks_pkg/manager/_stream.py) converges ``task['content']`` /
``task['thinking']`` from the adopted ``msg`` AFTER the floor-retry loop —
covering BOTH adoption doors:
  1. RECOVERED (a resend's usage is no longer floor-collapsed) — line ~289.
  2. still-floored, loop exhausted (the last resend's msg is returned while
     usage stays floored) — line ~293.

Both stream with on_content=None, so both leave task['content'] at the first-
attempt residue without the post-loop convergence.

These tests drive ``stream_llm_response`` directly with a fake ``dispatch_stream``
(patched through the manager facade) and patched floor_retry predicates, then
assert the FULL text ends up in task['content']/['thinking'] AND propagates into
the assembled terminal segment (the second persisted column).

NEUTER counter-proof: comment out the ``if _fr_adopted:`` convergence block in
_stream.py and BOTH divergence tests flip red (task['content'] stays at the
partial preamble); restore → green.
"""
from __future__ import annotations

import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Distinctive text markers so a partial-vs-full mixup is unambiguous.
_PARTIAL = 'PARTIAL_PREAMBLE_FIRST_ATTEMPT'          # what the 1st attempt streamed
_FULL = 'FULL_DELIVERABLE_' + ('X' * 3400)           # the adopted resend's real answer
_PARTIAL_THINK = 'partial-thinking'
_FULL_THINK = 'FULL_THINKING_' + ('Y' * 2200)
_TEST_OWNER_USER_ID = 1


def _make_task(tid='floor-reinject-1', conv='convFR'):
    return {
        'id': tid,
        'convId': conv,
        '_userId': _TEST_OWNER_USER_ID,
        'aborted': False,
        'content': '',
        'thinking': '',
        'content_lock': threading.Lock(),
        'events': [],
        'events_lock': threading.Lock(),
    }


def _patch_common(monkeypatch, dispatch_fn):
    """Patch the floor-retry gate ON, its predicates, and the side-effecting
    seams (append_event / checkpoint) so the test exercises only the
    content-convergence logic. dispatch_fn is installed at the concrete
    streaming module's dependency binding.
    """
    import lib.tasks_pkg.floor_retry as _fr
    import lib.tasks_pkg.manager._stream as _stream

    # Gate ON + always-eligible predicates (isolate the convergence, not the
    # eligibility heuristics — those have their own tests).
    monkeypatch.setattr(_fr, 'floor_retry_enabled', lambda: True)
    monkeypatch.setattr(_fr, 'floor_retry_max', lambda: 2)
    # is_floor_collapse reads a synthetic 'floor' marker on our fake usage.
    monkeypatch.setattr(_fr, 'is_floor_collapse', lambda u: bool((u or {}).get('floor')))
    monkeypatch.setattr(
        _fr,
        'wire_prefix_stable',
        lambda conv, u, *, user_id: user_id == _TEST_OWNER_USER_ID,
    )

    monkeypatch.setattr(_stream, 'dispatch_stream', dispatch_fn)

    # Silence the event/persistence seams.
    monkeypatch.setattr(_stream, 'append_event', lambda *a, **k: None)
    monkeypatch.setattr(_stream, 'checkpoint_task_partial', lambda *a, **k: None)


@pytest.mark.unit
class TestFloorRetryContentReinjection:
    """Both FloorRetry adoption doors must converge task['content']/['thinking']."""

    def test_floor_retry_recover_reinjects_full_content(self, monkeypatch):
        """RECOVERED door (line ~289): first attempt floor-collapses and streams
        a partial preamble into task['content']; the resend RECOVERS (usage no
        longer floored) with the full answer. After the loop, task['content']
        must be the FULL adopted text, not the 215-char partial residue."""
        calls = {'n': 0}

        def fake_dispatch(body, on_thinking=None, on_content=None,
                          on_tool_call_ready=None, **kw):
            calls['n'] += 1
            if on_content is not None:
                # PRIMARY attempt — streams the partial preamble into task['content']
                # (exactly as production _on_content does) then floor-collapses.
                on_content(_PARTIAL)
                if on_thinking is not None:
                    on_thinking(_PARTIAL_THINK)
                return ({'role': 'assistant', 'content': _PARTIAL,
                         'reasoning_content': _PARTIAL_THINK},
                        'stop', {'floor': True})
            # RESEND — on_content is None (announces nothing); full answer,
            # RECOVERED (no floor marker) → adopted + break.
            return ({'role': 'assistant', 'content': _FULL,
                     'reasoning_content': _FULL_THINK},
                    'stop', {'floor': False})

        _patch_common(monkeypatch, fake_dispatch)
        from lib.tasks_pkg.manager._stream import stream_llm_response

        task = _make_task()
        msg, finish, usage = stream_llm_response(task, {'model': 'aws.claude-opus-4.8'}, tag='R4')

        # A resend happened and was adopted.
        assert calls['n'] >= 2
        assert msg['content'] == _FULL
        # ★ The persisted track (task['content']) must carry the FULL answer,
        #   NOT the first-attempt partial preamble.
        assert task['content'] == _FULL, \
            f"task['content'] not converged: {task['content'][:40]!r}"
        assert task['thinking'] == _FULL_THINK
        assert _PARTIAL not in task['content']

    def test_floor_retry_loop_exhausted_reinjects_last_resend(self, monkeypatch):
        """Loop-exhausted door (line ~293): every resend stays floor-collapsed,
        so the loop runs to exhaustion and returns the LAST resend's msg (a full
        fresh generation) while usage remains floored. task['content'] must be
        that last resend's content — the SECOND entry point the per-branch fix
        would have missed."""
        calls = {'n': 0}

        def fake_dispatch(body, on_thinking=None, on_content=None,
                          on_tool_call_ready=None, **kw):
            calls['n'] += 1
            if on_content is not None:
                on_content(_PARTIAL)
                if on_thinking is not None:
                    on_thinking(_PARTIAL_THINK)
                return ({'role': 'assistant', 'content': _PARTIAL,
                         'reasoning_content': _PARTIAL_THINK},
                        'stop', {'floor': True})
            # RESEND — full body, but usage STILL floored → never recovers,
            # loop exhausts. The last resend's msg is what gets returned.
            return ({'role': 'assistant', 'content': _FULL,
                     'reasoning_content': _FULL_THINK},
                    'stop', {'floor': True})

        _patch_common(monkeypatch, fake_dispatch)
        from lib.tasks_pkg.manager._stream import stream_llm_response

        task = _make_task(tid='floor-reinject-2')
        msg, finish, usage = stream_llm_response(task, {'model': 'aws.claude-opus-4.8'}, tag='R4')

        # 1 primary + 2 resends (floor_retry_max=2), all floored.
        assert calls['n'] == 3
        assert msg['content'] == _FULL
        # ★ Even without RECOVERED, the returned msg replaced the primary — so
        #   task['content'] must follow it, not stay at the partial residue.
        assert task['content'] == _FULL, \
            f"loop-exhausted door not converged: {task['content'][:40]!r}"
        assert task['thinking'] == _FULL_THINK
        assert _PARTIAL not in task['content']

    def test_no_floor_retry_leaves_streamed_content_intact(self, monkeypatch):
        """Control: when the primary attempt does NOT floor-collapse, no resend
        fires and task['content'] keeps exactly what was streamed (the
        convergence block must not clobber a healthy normal round)."""
        calls = {'n': 0}

        def fake_dispatch(body, on_thinking=None, on_content=None,
                          on_tool_call_ready=None, **kw):
            calls['n'] += 1
            if on_content is not None:
                on_content(_FULL)  # healthy round streams the full answer directly
            return ({'role': 'assistant', 'content': _FULL,
                     'reasoning_content': _FULL_THINK},
                    'stop', {'floor': False})  # NOT floored → no resend

        _patch_common(monkeypatch, fake_dispatch)
        from lib.tasks_pkg.manager._stream import stream_llm_response

        task = _make_task(tid='floor-reinject-3')
        stream_llm_response(task, {'model': 'aws.claude-opus-4.8'}, tag='R1')

        assert calls['n'] == 1  # no resend
        assert task['content'] == _FULL


@pytest.mark.unit
class TestSegmentsPickUpReinjectedContent:
    """The SECOND persisted column: assemble_segments derives the terminal
    deliverable segment from task['content'] / task['thinking'], so the
    convergence must propagate there too — otherwise the DB `content` column is
    fixed but the `segments` column still renders the stale 215-char preamble."""

    def test_terminal_segment_reflects_converged_content(self):
        """After convergence task['content']=_FULL, the terminal deliverable
        text segment (and terminal thinking segment) must be the full text."""
        from lib.tasks_pkg.segments._assemble import assemble_segments

        task = {
            'id': 'seg-1', 'convId': 'c',
            'content': _FULL,          # post-convergence value
            'thinking': _FULL_THINK,
            'finishReason': 'stop',
        }
        # merged=[] → no tool rounds; isolate the terminal segments.
        segs = assemble_segments(task, merged=[])

        term_texts = [s for s in segs
                      if s.get('type') == 'text' and s.get('terminal')]
        term_thinks = [s for s in segs
                       if s.get('type') == 'thinking' and s.get('terminal')]
        assert len(term_texts) == 1
        assert term_texts[0]['text'] == _FULL
        assert term_texts[0].get('deliverable') is True
        assert len(term_thinks) == 1
        assert term_thinks[0]['text'] == _FULL_THINK

    def test_terminal_segment_would_be_partial_without_convergence(self):
        """Anchor the failure mode: if task['content'] held the PARTIAL residue
        (the pre-fix state), the terminal segment would carry the partial text.
        This proves the segment column is a pure projection of task['content'],
        so fixing task['content'] is BOTH necessary and sufficient for segments."""
        from lib.tasks_pkg.segments._assemble import assemble_segments

        task = {
            'id': 'seg-2', 'convId': 'c',
            'content': _PARTIAL,       # simulated pre-convergence residue
            'thinking': _PARTIAL_THINK,
            'finishReason': 'stop',
        }
        segs = assemble_segments(task, merged=[])
        term_texts = [s for s in segs
                      if s.get('type') == 'text' and s.get('terminal')]
        assert term_texts[0]['text'] == _PARTIAL  # projection of task['content']


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
