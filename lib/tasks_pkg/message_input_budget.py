"""Bound optional mailbox input before existing complete-prompt admission.

Choose whole original messages only. Oversized/unfit rows remain inspectable,
submitted and unacknowledged; no compaction/model work is started here.
"""

def select_whole_messages(task, messages, rows, render, *, max_tokens=4096, max_bytes=24576,
                          max_messages=None):
    """Scan bounded mailbox order until enough whole originals fit the budget.

    Callers supply eligible, not-already-present rows from their bounded mailbox.
    Unfit rows do not spend the count limit or prevent later small inputs.
    """
    if max_messages is not None and max_messages <= 0:
        return [], []
    from lib.tasks_pkg.context_composer._render import _count_tokens
    from lib.tasks_pkg.compaction._tokens import _get_context_limit, _estimate_total_tokens
    model = str(task.get('model') or '')
    context_limit = _get_context_limit(task)
    # Reserve output/schema/admission headroom rather than consuming the last
    # available input tokens. The canonical final admission remains authority.
    headroom = context_limit - _estimate_total_tokens(messages) - context_limit // 4
    budget = min(max_tokens, max(0, context_limit // 16), max(0, headroom))
    selected, blocks = [], []
    token_sum = 0
    byte_sum = 0
    for row in rows:
        text = render(row)
        text_bytes = len(text.encode('utf-8'))
        if byte_sum + text_bytes > max_bytes:
            continue
        tokens = _count_tokens(text, model)
        if token_sum + tokens > budget:
            continue
        selected.append(row)
        blocks.append(text)
        byte_sum += text_bytes
        token_sum += tokens
        if max_messages is not None and len(selected) >= max_messages:
            break
    return selected, blocks
