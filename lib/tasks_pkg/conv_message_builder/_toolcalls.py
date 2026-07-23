"""Structured tool-call reconstruction for the conversation message builder.

Expands stored ``toolRounds`` back into OpenAI-style
``assistant(tool_calls=[...])`` + ``tool(tool_call_id=..., content=...)``
message sequences.  This mirrors what
``lib.tasks_pkg.message_builder.inject_tool_history`` produces for Continue
requests, so the debug preview and the real request see the same structure.
"""

from __future__ import annotations

import json

from lib.log import get_logger

logger = get_logger(__name__)


def build_assistant_tool_call_message(
    *, tool_calls: list, content=None, reasoning_content=None,
    thinking_signature=None) -> dict:
    """THE single source for assembling a normalized assistant/tool_call message.

    Both the LIVE tail (orchestrator ``_run.py`` clean_msg, the in-loop tool
    round) and the REPLAY path (``_reconstruct_tool_call_messages``, the
    server-store-expiry rebuild) call this for the FINAL field assembly, so the
    two paths can NEVER re-diverge on a field — the root cause of the whole
    prefix-cache-drift saga (``.strip()`` raw↔stripped, str↔block ``{content}``,
    thinking-no-signature ``{reasoning_content}`` were all live↔replay
    divergences between two hand-written assemblers).

    Field rules (the canonical, byte-stable form — every historical fix folded
    in here ONCE):
      * ``content`` — STRIPPED; dropped entirely if empty/whitespace-only
        (inter-round narration; leading/trailing whitespace is not semantic and
        the stored ``assistantContent`` snapshot is already stripped).
      * ``reasoning_content`` — carried whenever thinking text is present
        (INDEPENDENT of signature), mirroring the live tail. An UNSIGNED
        thinking block is dropped identically downstream by ``_assistant_blocks``
        / ``_inject_claude_reasoning_details``, so no HTTP 400; DeepSeek's
        ``model_requires_reasoning_content_replay`` is preserved.
      * ``thinking_signature`` — carried only when present AND thinking present
        (a signature without reasoning text is meaningless).
      * key order is FIXED (role, content, reasoning_content, thinking_signature,
        tool_calls) so the serialized wire bytes are deterministic regardless of
        which caller populated the fields.

    Scope: this is the FIELD-ASSEMBLY seam only. Batch grouping, tool_calls
    reconstruction, and tool_result generation stay in each caller (they are
    genuinely different: live has an in-memory OpenAI-shape tool_calls list and
    one current round; replay rebuilds from stored toolRounds and groups by
    llmRound). ``inject_tool_history`` (Continue-only, model-gated,
    intentionally-lossy) DELIBERATELY does NOT use this — its Claude-only gating
    is a different contract.

    Args:
        tool_calls: OpenAI-shape ``[{id,type,function:{name,arguments}}, ...]``.
        content: The assistant's inter-round prose (raw; stripped here).
        reasoning_content: Thinking text (or None/empty).
        thinking_signature: Opaque Claude thinking-block signature (or None).

    Returns:
        A normalized assistant message dict in canonical key order.
    """
    _content = (content or '').strip()
    _reasoning = reasoning_content or ''
    _sig = thinking_signature or ''
    msg: dict = {'role': 'assistant'}
    if _content:
        msg['content'] = _content
    if _reasoning:
        msg['reasoning_content'] = _reasoning
        if _sig:
            msg['thinking_signature'] = _sig
    msg['tool_calls'] = tool_calls
    return msg


def _is_reconstructable_round(r: dict) -> bool:
    """A round can contribute a valid assistant(tool_use)+tool(result) PAIR.

    The identity + result fields must all be present:
      * ``toolCallId`` (non-empty) — pairs the tool_use with its tool_result
      * ``toolName`` (non-empty)   — the function name
      * ``toolContent`` is not None — the result the model saw

    Keyed on field COMPLETENESS, NOT on ``status``. ``status`` is only the label
    the last-touching path stamped (``done`` / ``aborted`` / ``error`` / a future
    lane); the real invariant for wire reconstruction is "does this row have the
    data to form a legal pair". So an interrupted round that DID capture a real
    result (``toolContent`` present) is a legitimate pair and is KEPT, while an
    orphan announcement round left result-less by a discarded FloorRetry /
    stream-retry attempt is dropped regardless of what status it was swept to.
    """
    return (bool(r.get('toolCallId'))
            and bool(r.get('toolName'))
            and r.get('toolContent') is not None)


def _reconstruct_tool_call_messages(rounds: list[dict]) -> list[dict] | None:
    """Expand ``toolRounds`` into structured assistant/tool message pairs.

    Returns a list of messages on success, or ``None`` when NO round survives
    the entry filter (i.e. there is nothing reconstructable at all). Callers
    fall back to the legacy summary placeholder on ``None``.

    Per-round requirements (see ``_is_reconstructable_round``): ``toolCallId`` +
    ``toolName`` + non-None ``toolContent``. A row lacking any of these is
    DROPPED and the turn is rebuilt from the survivors — it no longer collapses
    the WHOLE turn (see the entry-filter rationale below).

    ``toolArgs`` is best-effort normalized to a JSON string suitable for
    ``function.arguments``.  ``assistantContent`` on the first round of
    a batch becomes the batch's assistant ``content`` (text written
    alongside the tool_calls, à la Claude).
    """
    # ── Wire-purity guard ──
    # Drop rows that cannot contribute a valid assistant(tool_use)+tool(result)
    # PAIR — at the single entry seam, so the reconstructor is immune to partial
    # rows from ANY source and rebuilds from the survivors instead of collapsing
    # the whole turn. TWO classes are dropped:
    #
    #   1. Synthetic inbox-inject rows (async <swarm-update> / peer / user-steer
    #      display chips): a lane marker, no tool_call data — persisted as a
    #      display-only underscore sidecar, never on the wire. See
    #      lib/tasks_pkg/segments/_types.is_synthetic_inbox_round.
    #   2. Result-less / identity-less rounds (``_is_reconstructable_round`` is
    #      False): e.g. an orphan 'searching' round left by a discarded FloorRetry
    #      or stream-retry attempt (reused on_tool_call_ready announced a round
    #      whose tc_id never survived into the final assistant_msg) and later
    #      swept to 'aborted' with an EMPTY result. Such a round cannot form a
    #      pair; keeping it USED TO fail the all-or-nothing validation and
    #      collapse the ENTIRE turn — dozens of completed tool calls — into the
    #      lossy toolSummary placeholder, erasing them from the model's context
    #      AND shifting the prefix-cache bytes. Dropping ONLY the unreconstructable
    #      row preserves every completed call. (Verified on live conv
    #      mrw0rubcbb5qv9: a single such orphan collapsed a 66-tool-call turn to
    #      one 1871-char text blob.)
    from lib.tasks_pkg.segments._types import is_synthetic_inbox_round
    rounds = [
        r for r in rounds
        if not is_synthetic_inbox_round(r) and _is_reconstructable_round(r)
    ]
    if not rounds:
        return None

    # Group into batches by llmRound (preferred) or roundNum gap (legacy).
    has_llm_round = any(r.get('llmRound') is not None for r in rounds)
    batches: list[list[dict]] = []
    current: list[dict] = []
    prev_key = None
    for r in rounds:
        if has_llm_round:
            key = r.get('llmRound')
        else:
            key = r.get('roundNum')
            if current and isinstance(prev_key, int) and isinstance(key, int):
                # legacy: gap > 1 in roundNum → new batch
                if key > prev_key + 1:
                    batches.append(current)
                    current = []
        if current and has_llm_round and key != prev_key:
            batches.append(current)
            current = []
        current.append(r)
        prev_key = key
    if current:
        batches.append(current)

    out: list[dict] = []
    for batch in batches:
        tool_calls = []
        tool_results = []
        assistant_text = ''
        assistant_thinking = ''
        assistant_thinking_sig = ''
        for r in batch:
            tc_id = r['toolCallId']
            args_raw = r.get('toolArgs')
            if isinstance(args_raw, str):
                args_str = args_raw
            elif isinstance(args_raw, dict):
                try:
                    args_str = json.dumps(args_raw, ensure_ascii=False)
                except (TypeError, ValueError) as _e_audit:
                    logger.debug('[conv_message_builder] _reconstruct_tool_call_messages caught %s: %s', type(_e_audit).__name__, _e_audit)
                    args_str = '{}'
            else:
                args_str = '{}'
            # Defense-in-depth: if a stored toolArgs string is itself not
            # valid JSON, replay it as ``'{}'`` so the upstream gateway
            # doesn't HTTP 400 ``invalid function arguments json string``.
            # The matching tool_result still tells the model the original
            # call failed, so this replay path stays equivalent to live
            # execution. See orchestrator.py:1364 (live sanitizer) and
            # the May 2026 incident memory.
            try:
                json.loads(args_str)
            except (json.JSONDecodeError, TypeError) as _e_audit:
                logger.debug('[conv_message_builder] _reconstruct_tool_call_messages caught %s: %s', type(_e_audit).__name__, _e_audit)
                args_str = '{}'
            tc_entry: dict = {
                'id': tc_id,
                'type': 'function',
                'function': {
                    'name': r['toolName'],
                    'arguments': args_str,
                },
            }
            # Gemini: echo back thought_signature verbatim — the OpenAI-compat
            # proxy requires it on every replayed tool_call or returns HTTP 400.
            # Unused by other providers (they strip unknown fields server-side).
            if r.get('extraContent'):
                tc_entry['extra_content'] = r['extraContent']
            tool_calls.append(tc_entry)
            tool_results.append({
                'role': 'tool',
                'tool_call_id': tc_id,
                'content': r['toolContent'] or '',
            })
            # First-seen assistantContent / thinking in the batch become the
            # assistant message's text + reasoning (Claude-style prefix).
            if not assistant_text and r.get('assistantContent'):
                assistant_text = r['assistantContent']
            if not assistant_thinking and r.get('thinking'):
                assistant_thinking = r['thinking']
            if not assistant_thinking_sig and r.get('thinkingSignature'):
                assistant_thinking_sig = r['thinkingSignature']

        # ★ SINGLE SOURCE: field assembly goes through
        #   build_assistant_tool_call_message so this replay path and the live
        #   tail (_run.py clean_msg) can never re-diverge on a field. All the
        #   historical gates (.strip() content, reasoning_content-when-thinking,
        #   signature-when-present, canonical key order) live there ONCE.
        asst_msg = build_assistant_tool_call_message(
            tool_calls=tool_calls, content=assistant_text or None,
            reasoning_content=assistant_thinking or None,
            thinking_signature=assistant_thinking_sig or None)
        out.append(asst_msg)
        out.extend(tool_results)

    return out
