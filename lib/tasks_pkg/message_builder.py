"""Message-building helpers — URL prefetch injection and tool-history restoration.

Extracted from ``orchestrator.py`` to isolate the logic that mutates the
``messages`` list before the main LLM tool loop begins.
"""

from lib.log import get_logger

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
        Live task dict (used to read ``task['searchRounds']`` count).

    Returns
    -------
    int
        Updated ``search_round_num`` based on how many search rounds already
        exist after prefetch.
    """
    if not prefetched:
        return len(task.get('searchRounds', []))

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

    return len(task.get('searchRounds', []))


def inject_tool_history(messages, cfg, task, model):
    """Restore interrupted tool-call context from a "Continue…" message.

    When the frontend sends a continuation request it includes a
    ``toolHistory`` list in the config.  Each entry describes one
    assistant→tool round that happened before the interruption.  This
    function splices those rounds back into *messages* so the LLM sees
    the full conversation context.

    Parameters
    ----------
    messages : list[dict]
        Conversation message list — mutated in-place.
    cfg : dict
        Task configuration dict (reads ``cfg['toolHistory']``).
    task : dict
        Live task dict (used for logging ``task['id']``).
    model : str
        Current model identifier (used for logging).

    Returns
    -------
    int
        Number of individual tool call entries injected (0 if none).
        Each toolHistory round may contain multiple tool calls; this
        returns the TOTAL across all rounds — useful for offsetting
        ``search_round_num`` in the orchestrator so new rounds get
        non-conflicting roundNum values.
    """
    tool_history = cfg.get('toolHistory') or []
    if not tool_history:
        return 0

    tid = task['id'][:8]

    # Insertion point: AFTER the last user message (i.e. at the end).
    # The tool history represents the assistant's interrupted response to
    # that user question — it must come after the user's message so the
    # LLM sees: [..., user_question, assistant(tool_calls), tool_results].
    insert_idx = len(messages)

    injected_msgs = []
    injected = 0
    for th_round in tool_history:
        tc_list = th_round.get('toolCalls') or []
        tr_list = th_round.get('toolResults') or []
        if not tc_list:
            continue
        # Build assistant message with tool_calls
        clean_assistant = {'role': 'assistant'}
        clean_assistant['tool_calls'] = [
            {
                'id': tc['id'],
                'type': 'function',
                'function': {'name': tc['name'], 'arguments': tc['arguments']},
            }
            for tc in tc_list
        ]
        ac = th_round.get('assistantContent')
        if ac:
            clean_assistant['content'] = ac
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
            '[Task %s] Restored %d tool round(s) (%d tool calls) from '
            'continue context, inserted at position %d, model=%s',
            tid, injected, total_tool_calls, insert_idx, model,
        )
        return total_tool_calls

    return 0
