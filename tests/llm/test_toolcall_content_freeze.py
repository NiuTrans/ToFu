"""Verification guardrail for the {content} byte-drift freeze (epic pt_34454c657af942ba).

THE LAST residual prefix-cache byte-drift source. The class-③ key-order fix
(``_canonical_wire``, commit 2f27074) froze key ORDER, but the field-level
tracer (a353842) then proved the remaining ``WIRE PREFIX CHANGED`` flips are all
the ``assistant/tool_call(...){content}`` FIELD — the ``content`` VALUE of an
already-cached tool_call turn differing round-over-round.

Root cause (read-only diagnosis, this conversation):
  * live-stream finalize (``lib/llm/_sse_core.py``) sets a tool_call turn's
    ``content`` from ``self.content`` — the raw accumulated streamed text.
  * history-replay (``lib/tasks_pkg/conv_message_builder/_toolcalls.py``) sets it
    from the stored ``assistantContent`` snapshot (first-seen batch round).
  Same logical turn, but the two shapes can differ in exact bytes (trailing
  whitespace, ``''`` vs an ABSENT key, a ``\\n\\n``-join of multi-round batches),
  so the turn is live-shaped the round it's produced and replay-shaped every
  round after → a per-round ``{content}`` byte flip INSIDE the cached prefix.

This suite is the EXECUTABLE ACCEPTANCE CRITERION for the freeze: it asserts
that after the send-time normalization pass (``build_body`` →
``canonicalize_messages_inplace``), the SAME logical tool_call turn in its
live-shape and its replay-shape emits BYTE-IDENTICAL wire bytes for ``content``.

Owned/implemented by sibling conversation mroozrve7i6hf0 (epic
pt_34454c657af942ba); this test file is intentionally NEW (touches none of the
implementation files) so it can be landed independently as the regression guard.
It is RED until the freeze lands and GREEN once it does — that flip is the
proof, complementing the post-restart traffic check (WIRE PREFIX CHANGED → 0).

Marked xfail(strict=False) so it does not break the shared gate while the fix is
in flight; drop the xfail in the same commit that lands the freeze so a future
regression turns it red again.
"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.unit

from lib.llm.body._canonical_wire import canonicalize_message_order


def _wire_content(msg: dict) -> str:
    """Serialize a canonicalized message exactly as transport does, then return
    the wire bytes so a byte comparison is apples-to-apples."""
    return json.dumps(canonicalize_message_order(msg), ensure_ascii=False,
                      sort_keys=False)


# ── The SAME logical tool_call turn, built by the two paths ──
def _live_shape(content) -> dict:
    """live-stream finalize shape: content from self.content (may be absent)."""
    m = {'role': 'assistant',
         'tool_calls': [{'id': 'c1', 'type': 'function',
                         'function': {'name': 'run_command',
                                      'arguments': '{"cmd": "ls"}'}}]}
    if content is not None:
        m['content'] = content
    return m


def _replay_shape(content) -> dict:
    """history-replay shape: content from stored assistantContent (may be absent)."""
    m = {'role': 'assistant',
         'tool_calls': [{'id': 'c1', 'type': 'function',
                         'function': {'name': 'run_command',
                                      'arguments': '{"cmd": "ls"}'}}]}
    if content is not None:
        m['content'] = content
    return m


@pytest.mark.xfail(strict=False, reason='content freeze not landed yet '
                   '(epic pt_34454c657af942ba, sibling mroozrve7i6hf0)')
def test_empty_vs_absent_content_converges():
    """A tool_call turn with empty-string content (live) and one with the
    content key ABSENT (replay) must serialize identically after normalization.
    This is the cleanest sub-case: '' and missing are the same semantic 'no
    prose alongside the call'."""
    assert _wire_content(_live_shape('')) == _wire_content(_replay_shape(None))


@pytest.mark.xfail(strict=False, reason='content freeze not landed yet '
                   '(epic pt_34454c657af942ba, sibling mroozrve7i6hf0)')
def test_trailing_whitespace_content_converges():
    """Same prose but one shape carries a trailing newline (raw streamed) and
    the other is trimmed (snapshot) — must converge to identical bytes."""
    assert _wire_content(_live_shape('Let me check.\n')) == \
        _wire_content(_replay_shape('Let me check.'))


@pytest.mark.xfail(strict=False, reason='content freeze not landed yet '
                   '(epic pt_34454c657af942ba, sibling mroozrve7i6hf0)')
def test_build_body_end_to_end_content_converges():
    """End-to-end through the real send-time builder: the live-shape and
    replay-shape of the same tool_call turn produce byte-identical bytes for
    the ASSISTANT turn (the acceptance criterion the traffic check mirrors).

    A matching tool_result + trailing user turn are included so the assistant
    tool_call is NOT orphan-stripped by ``_fix_orphaned_tool_calls`` (else
    messages[0] would collapse to the user turn and the comparison is vacuous).
    Uses the trailing-whitespace sub-case, which build_body does not yet
    converge (the empty-vs-absent sub-case is already normalized upstream)."""
    from lib.llm.body import build_body

    def _seq(content):
        return [
            _live_shape(content),
            {'role': 'tool', 'tool_call_id': 'c1', 'content': 'ok'},
            {'role': 'user', 'content': 'go'},
        ]
    live = build_body('aws.claude-opus-4.8', _seq('Let me check.\n'))
    replay = build_body('aws.claude-opus-4.8', _seq('Let me check.'))
    # Locate the assistant tool_call turn in each (index-agnostic).
    def _asst(b):
        return next(m for m in b['messages'] if m.get('tool_calls'))
    assert json.dumps(_asst(live), ensure_ascii=False, sort_keys=False) \
        == json.dumps(_asst(replay), ensure_ascii=False, sort_keys=False)


def test_NEUTER_divergence_is_real_without_freeze():
    """NEUTER / precondition: WITHOUT the freeze the two shapes really DO
    diverge — proving the guard above is testing a live gap, not a no-op. This
    must PASS now (documenting the bug) and stays as the load-bearing control
    after the freeze lands (the freeze makes the xfail tests pass; this asserts
    the raw, un-normalized shapes were genuinely different to begin with)."""
    raw_live = json.dumps(_live_shape(''), ensure_ascii=False, sort_keys=False)
    raw_replay = json.dumps(_replay_shape(None), ensure_ascii=False, sort_keys=False)
    assert raw_live != raw_replay, (
        'precondition: empty-string vs absent content must differ pre-freeze')
    raw_live2 = json.dumps(_live_shape('x\n'), ensure_ascii=False, sort_keys=False)
    raw_replay2 = json.dumps(_replay_shape('x'), ensure_ascii=False, sort_keys=False)
    assert raw_live2 != raw_replay2, (
        'precondition: trailing-whitespace content must differ pre-freeze')
