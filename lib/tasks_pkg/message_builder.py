"""Message-building helpers — URL prefetch injection and tool-history restoration.

Extracted from ``orchestrator.py`` to isolate the logic that mutates the
``messages`` list before the main LLM tool loop begins.
"""

from lib.log import get_logger
from lib.model_info import (
    model_requires_thinking_signature_replay,
    model_requires_thought_signature_on_tool_calls,
)

logger = get_logger(__name__)


def inject_prefetched_urls(messages, prefetched, task):
    """Inject auto-fetched URL content into the last user message.

    For each ``(url, content)`` pair in *prefetched*, builds a labelled
    text block (distinguishing PDF vs Web Page) and appends the combined
    block to the last ``role='user'`` message.  Handles both plain-string
    and structured-list content formats.

    Parameters
    ----------
    messages : list[dict]
        Conversation message list — mutated in-place.
    prefetched : list[tuple[str, str]]
        List of ``(url, fetched_content)`` pairs from ``_prefetch_user_urls``.
    task : dict
        Live task dict (used to read ``task['toolRounds']`` count).

    Returns
    -------
    int
        Updated ``tool_round_num`` based on how many tool rounds already
        exist after prefetch.
    """
    if not prefetched:
        return len(task.get('toolRounds', []))

    url_blocks = []
    for url, content in prefetched:
        is_pdf = url.lower().rstrip('/').endswith('.pdf') or content.startswith('[Page ')
        label = 'PDF Document' if is_pdf else 'Web Page'
        url_blocks.append(
            f"=== {label}: {url} ===\n({len(content):,} characters)\n\n{content}"
        )
    urls_text = '\n\n' + ('═' * 40 + '\n\n').join(url_blocks)

    # Walk backwards to find the last user message and append there
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get('role') != 'user':
            continue
        mc = messages[i].get('content', '')
        if isinstance(mc, str):
            messages[i] = {
                **messages[i],
                'content': mc + '\n\n[Auto-fetched URL content:]\n' + urls_text,
            }
        elif isinstance(mc, list):
            messages[i] = {
                **messages[i],
                'content': mc + [{'type': 'text', 'text': '\n\n[Auto-fetched URL content:]\n' + urls_text}],
            }
        break

    return len(task.get('toolRounds', []))


# ══════════════════════════════════════════════════════════════
#  Continue / Resume: per-provider capability matrix
# ══════════════════════════════════════════════════════════════
#
# When the user clicks "Continue" on an interrupted assistant turn we
# replay the already-completed tool rounds so the model can pick up
# right after the last tool result.  What each provider's API accepts
# on that replayed assistant turn:
#
#   Provider            | tool_use replay | thinking replay          | Prefill
#   --------------------+-----------------+--------------------------+---------
#   Anthropic (Claude)  | required        | thinking{} block WITH    | NO
#                       | tool_calls +    |   opaque `signature` —   | (API rejects
#                       | tool results    |   mandatory when tools   |  trailing
#                       |                 |   ran with extended      |  assistant
#                       |                 |   thinking; else API 400 |  turn)
#   Gemini (openai-     | required        | extra_content.google.    | tolerated
#   compat proxy)       |                 |   thought_signature on   | (best-effort)
#   OpenAI / DeepSeek / | standard        | NOT re-accepted          | tolerated
#   Qwen / GLM / Kimi / | tool_calls +    |   (reasoning_content     | (best-effort
#   Doubao / MiniMax    | tool role msgs  |   stripped server-side)  |  — the model
#   ERNIE / LongCat     |                 |                          |  may or may
#                       |                 |                          |  not honour)
#
# Consequence for this function: we ALWAYS inject tool_calls + tool
# results (they're universally accepted), but we OPTIONALLY attach
# thinking / thought_signature / extra_content blocks only for the
# providers whose API actually consumes them.  Unsupported providers
# get the plain shape — exactly what they got before this change.
#
# Anthropic's "no assistant prefill" restriction is a hard ceiling:
# free-form text the model wrote BETWEEN tool batches can never be
# re-injected as a prefill against Claude.  We therefore do not try.

def inject_tool_history(messages, cfg, task, model):
    """Restore interrupted tool-call context from a "Continue…" message.

    When the frontend sends a continuation request it includes a
    ``toolHistory`` list in the config.  Each entry describes one
    assistant→tool round that happened before the interruption.  This
    function splices those rounds back into *messages* so the LLM sees
    the full conversation context.

    Each ``toolHistory`` entry accepts (all optional except ``toolCalls``):

        {
          'assistantContent': str,    # text the model wrote alongside calls
          'thinking': str,            # reasoning trace (Claude-family only)
          'thinkingSignature': str,   # opaque signature for thinking block
          'toolCalls': [
              {'id', 'name', 'arguments',
               'extraContent': {...}  # Gemini thought_signature lives here
              }, ...
          ],
          'toolResults': [{'tool_call_id', 'content'}, ...],
        }

    Per-provider behaviour:

    * Claude extended-thinking models → the assistant turn is emitted with
      a ``thinking`` block containing the prior reasoning and its
      ``signature``.  Required by the Messages API when tools were used
      with extended thinking, otherwise the follow-up call 400s.
    * Gemini → each replayed ``tool_call`` entry is annotated with
      ``extra_content.google.thought_signature`` exactly as the live
      stream produced it.
    * All other OpenAI-compatible providers → the standard shape only.
      Extra fields are ignored silently rather than sent, since those
      APIs strip vendor extensions server-side anyway.

    Parameters
    ----------
    messages : list[dict]
        Conversation message list — mutated in-place.
    cfg : dict
        Task configuration dict (reads ``cfg['toolHistory']``).
    task : dict
        Live task dict (used for logging ``task['id']``).
    model : str
        Current model identifier (used for logging + capability probes).

    Returns
    -------
    int
        Number of individual tool call entries injected (0 if none).
        Each toolHistory round may contain multiple tool calls; this
        returns the TOTAL across all rounds — useful for offsetting
        ``tool_round_num`` in the orchestrator so new rounds get
        non-conflicting roundNum values.
    """
    tool_history = cfg.get('toolHistory') or []
    if not tool_history:
        return 0

    tid = task['id'][:8]
    conv_id_short = (task.get('convId') or '')[:8]

    # Per-provider capability gates — None→Python-bool so log output is clean.
    _wants_thinking_block = model_requires_thinking_signature_replay(model)
    _wants_thought_sig = model_requires_thought_signature_on_tool_calls(model)

    # Insertion point: AFTER the last user message (i.e. at the end).
    # The tool history represents the assistant's interrupted response to
    # that user question — it must come after the user's message so the
    # LLM sees: [..., user_question, assistant(tool_calls), tool_results].
    insert_idx = len(messages)

    injected_msgs = []
    injected = 0
    _thinking_blocks_attached = 0
    _thought_sigs_attached = 0
    for th_round in tool_history:
        tc_list = th_round.get('toolCalls') or []
        tr_list = th_round.get('toolResults') or []
        if not tc_list:
            continue
        # ── Build tool_calls[] with optional extra_content passthrough ──
        built_tool_calls = []
        for tc in tc_list:
            tc_entry = {
                'id': tc['id'],
                'type': 'function',
                'function': {'name': tc['name'], 'arguments': tc['arguments']},
            }
            # Gemini: echo back thought_signature or the API 400s.
            extra = tc.get('extraContent')
            if _wants_thought_sig and extra:
                tc_entry['extra_content'] = extra
                _thought_sigs_attached += 1
            elif extra and not _wants_thought_sig:
                logger.debug(
                    '[Task %s] conv=%s model=%s — dropping extraContent on '
                    'replayed tool_call (%s) since provider does not require it',
                    tid, conv_id_short, model, tc.get('name', '?'),
                )
            built_tool_calls.append(tc_entry)

        # Build assistant message with tool_calls
        clean_assistant = {'role': 'assistant', 'tool_calls': built_tool_calls}
        ac = th_round.get('assistantContent')
        if ac:
            clean_assistant['content'] = ac

        # Anthropic extended-thinking: re-emit the thinking block with its
        # opaque signature so the API can verify tool-use continuity.
        # Without this, Claude 4.x with extended thinking returns HTTP 400
        # ("Expected `thinking` block with signature") on the follow-up
        # request that immediately calls a tool.
        th_text = th_round.get('thinking') or ''
        th_sig = th_round.get('thinkingSignature') or ''
        if _wants_thinking_block and th_text and th_sig:
            # Structured block format — Anthropic Messages proxy translates
            # OpenAI-shape back to native blocks when it sees this list.
            clean_assistant['reasoning_content'] = th_text
            # `signature` is the field name the Anthropic API expects on
            # the thinking block — keep the wire name stable for the proxy.
            clean_assistant['thinking_signature'] = th_sig
            _thinking_blocks_attached += 1
        elif th_text and not _wants_thinking_block:
            logger.debug(
                '[Task %s] conv=%s model=%s — dropping %d chars of checkpoint '
                'thinking on replay; provider does not accept thinking replay',
                tid, conv_id_short, model, len(th_text),
            )
        elif _wants_thinking_block and th_text and not th_sig:
            # Claude: text without signature can't be replayed — the API
            # will still accept the message without a thinking block, but
            # continuity degrades to "fresh reasoning".  Warn once.
            logger.warning(
                '[Task %s] conv=%s model=%s — checkpoint has %d chars of '
                'thinking but NO signature; not re-injecting (Claude would '
                'reject without signature). This is a lossy continuation.',
                tid, conv_id_short, model, len(th_text),
            )

        injected_msgs.append(clean_assistant)

        # Build corresponding tool result messages
        tr_by_id = {tr['tool_call_id']: tr['content'] for tr in tr_list}
        for tc in tc_list:
            tc_id = tc['id']
            tc_content = tr_by_id.get(tc_id, f'[Tool result lost for {tc["name"]}]')
            injected_msgs.append({
                'role': 'tool',
                'tool_call_id': tc_id,
                'content': tc_content,
            })
        injected += 1

    if injected_msgs:
        # Count total individual tool calls (across all rounds) for roundNum offset
        total_tool_calls = sum(
            len(th.get('toolCalls') or [])
            for th in tool_history if th.get('toolCalls')
        )
        messages[insert_idx:insert_idx] = injected_msgs
        logger.debug(
            '[Task %s] conv=%s Restored %d tool round(s) (%d tool calls) '
            'from continue context, inserted at position %d, model=%s, '
            'thinking_blocks=%d thought_sigs=%d',
            tid, conv_id_short, injected, total_tool_calls, insert_idx, model,
            _thinking_blocks_attached, _thought_sigs_attached,
        )
        return total_tool_calls

    return 0
