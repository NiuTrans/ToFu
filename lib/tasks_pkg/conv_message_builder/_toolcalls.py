"""Structured tool-call reconstruction for the conversation message builder.

Expands stored ``toolRounds`` back into OpenAI-style
``assistant(tool_calls=[...])`` + ``tool(tool_call_id=..., content=...)``
message sequences.  This mirrors what
``lib.tasks_pkg.message_builder.inject_tool_history`` produces for Continue
requests, so the debug preview and the real request see the same structure.
"""

from __future__ import annotations

from lib.log import get_logger
from lib.tool_round_identity import tool_round_batches
from lib.tool_round_replay import (
    scan_replayable_tool_round_prefix,
)

logger = get_logger(__name__)


def build_assistant_tool_call_message(
    *, tool_calls: list, content=None, reasoning_content=None,
    thinking_signature=None, responses_items=None,
    anthropic_content_blocks=None) -> dict:
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
      * ``_responses_items`` — opaque OpenAI Responses reasoning/compaction
        state, carried without interpretation for stateless replay.
      * ``_anthropic_content_blocks`` — opaque Anthropic hosted-tool blocks,
        carried unchanged so server_tool_use continuity survives replay.
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
        responses_items: Opaque OpenAI Responses output items (or None).
        anthropic_content_blocks: Opaque Anthropic assistant blocks (or None).

    Returns:
        A normalized assistant message dict in canonical key order.
    """
    _content = content.strip() if isinstance(content, str) else ''
    _reasoning = (reasoning_content
                  if isinstance(reasoning_content, str) else '')
    _sig = thinking_signature if isinstance(thinking_signature, str) else ''
    msg: dict = {'role': 'assistant'}
    if _content:
        msg['content'] = _content
    if _reasoning:
        msg['reasoning_content'] = _reasoning
        if _sig:
            msg['thinking_signature'] = _sig
    if isinstance(responses_items, (list, tuple)) and responses_items:
        msg['_responses_items'] = [dict(item) for item in responses_items
                                   if isinstance(item, dict)]
    if (isinstance(anthropic_content_blocks, (list, tuple))
            and anthropic_content_blocks):
        msg['_anthropic_content_blocks'] = [dict(block)
                                             for block in anthropic_content_blocks
                                             if isinstance(block, dict)]
    msg['tool_calls'] = tool_calls
    return msg


def _is_reconstructable_round(r: dict) -> bool:
    """A round can contribute a valid assistant(tool_use)+tool(result) PAIR.

    The identity + result fields must all be present:
      * ``toolCallId`` (non-empty) — pairs the tool_use with its tool_result
      * ``toolName`` (non-empty)   — the function name
      * ``toolContent`` is text       — the exact result the model saw

    Keyed on field COMPLETENESS, NOT on ``status``. ``status`` is only the label
    the last-touching path stamped (``done`` / ``aborted`` / ``error`` / a future
    lane); the real invariant for wire reconstruction is "does this row have the
    data to form a legal pair". So an interrupted round that DID capture a real
    result (``toolContent`` present) is a legitimate pair and is KEPT, while an
    orphan announcement round explicitly marked as a discarded FloorRetry /
    stream-retry attempt is transparent regardless of its swept status.
    """
    replay_prefix = scan_replayable_tool_round_prefix([r])
    return len(replay_prefix.rounds) == 1


def _reconstruct_tool_call_messages(rounds: list[dict]) -> list[dict] | None:
    """Expand ``toolRounds`` into structured assistant/tool message pairs.

    Returns a list of messages on success, or ``None`` when NO round survives
    the entry filter (i.e. there is nothing reconstructable at all). Callers
    fall back to the legacy summary placeholder on ``None``.

    Per-round requirements (see ``_is_reconstructable_round``): ``toolCallId`` +
    ``toolName`` + text ``toolContent`` plus valid caller/argument envelopes.
    Identity-free display carriers and explicitly superseded provider-attempt
    artifacts are transparent. Any other identity-bearing malformed row is a
    causal gap: reconstruction stops there instead of replaying later calls as
    if the missing result had existed.

    ``toolArgs`` is normalized by the shared replay boundary to a JSON string
    suitable for ``function.arguments``. ``assistantContent`` on the first round of
    a batch becomes the batch's assistant ``content`` (text written
    alongside the tool_calls, à la Claude).
    """
    # ── Wire-purity + causality guard ──
    # The same scanner owns Continue, checkpoint settlement, segment replay,
    # and this cold-history path. That prevents one path from skipping an
    # unknown execution gap while another truncates it. Explicitly marked
    # discarded-attempt rows remain transparent, so known transport artifacts
    # cannot collapse an otherwise complete historical turn.
    if not isinstance(rounds, (list, tuple)):
        return None
    replay_prefix = scan_replayable_tool_round_prefix(rounds)
    rounds = list(replay_prefix.rounds)
    if replay_prefix.blocked_position is not None:
        logger.warning(
            '[conv_message_builder] Stopped tool replay at causal gap position '
            '%d (%s); later occurrences remain audit-only',
            replay_prefix.blocked_position, replay_prefix.blocked_reason)
    if not rounds:
        return None

    # todo_write is a state-sync protocol, not a sequence of independent
    # observations. Replaying 0/3 → 1/3 → 2/3 teaches the model to churn the
    # checklist and wastes context. Keep the latest effective carrier on the
    # model wire while the untouched persisted toolRounds remain the audit log.
    from lib.tools.todo import compact_todo_rounds_for_replay
    rounds = compact_todo_rounds_for_replay(rounds)

    # One durable Turn may contain several attempts whose counters all start
    # at zero.  Use the shared ordered identity helper so two attempts' R17s
    # can never become one synthetic assistant(tool_calls) message.
    batches = tool_round_batches(rounds)

    out: list[dict] = []
    for batch in batches:
        tool_calls = []
        tool_results = []
        assistant_text = ''
        assistant_thinking = ''
        assistant_thinking_sig = ''
        assistant_responses_items = None
        assistant_anthropic_blocks = None
        for r in batch:
            tc_id = r['toolCallId']
            args_str = r['toolArgs']
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
            if isinstance(r.get('extraContent'), dict) and r['extraContent']:
                tc_entry['extra_content'] = dict(r['extraContent'])
            if r.get('caller') is not None:
                tc_entry['caller'] = dict(r['caller'])
            tool_calls.append(tc_entry)
            tool_result = {
                'role': 'tool',
                'tool_call_id': tc_id,
                'content': r['toolContent'] or '',
            }
            if r.get('caller') is not None:
                tool_result['caller'] = dict(r['caller'])
            tool_results.append(tool_result)
            # First-seen assistantContent / thinking in the batch become the
            # assistant message's text + reasoning (Claude-style prefix).
            if not assistant_text and r.get('assistantContent'):
                assistant_text = r['assistantContent']
            if not assistant_thinking and r.get('thinking'):
                assistant_thinking = r['thinking']
            if not assistant_thinking_sig and r.get('thinkingSignature'):
                assistant_thinking_sig = r['thinkingSignature']
            if assistant_responses_items is None and r.get('_responsesItems'):
                assistant_responses_items = r['_responsesItems']
            if (assistant_anthropic_blocks is None
                    and r.get('_anthropicContentBlocks')):
                assistant_anthropic_blocks = r['_anthropicContentBlocks']

        # SINGLE SOURCE: field assembly goes through
        #   build_assistant_tool_call_message so this replay path and the live
        #   tail (_run.py clean_msg) can never re-diverge on a field. All the
        #   historical gates (.strip() content, reasoning_content-when-thinking,
        #   signature-when-present, canonical key order) live there ONCE.
        asst_msg = build_assistant_tool_call_message(
            tool_calls=tool_calls, content=assistant_text or None,
            reasoning_content=assistant_thinking or None,
            thinking_signature=assistant_thinking_sig or None,
            responses_items=assistant_responses_items,
            anthropic_content_blocks=assistant_anthropic_blocks)
        out.append(asst_msg)
        out.extend(tool_results)

    return out
