# HOT_PATH — called before every LLM API call.
"""Per-Turn Attachments — dynamic context injection on every turn.

Computes per-turn injections including file attachments, delta
announcements, memory surfacing, TODO reminders, and memory discoveries.

Runtime attachments are emitted through the shared context composer as
new tail ``user`` messages containing <system-reminder> blocks. Existing
system/history message content is not edited.

Scope is deliberately small:
  - No hook-output / teammate-mailbox / diagnostic / speculation-overlay
    attachment types — those presuppose subsystems Tofu does not have.
  - Relevant-memory surfacing uses Tofu's flat project context and
    memory, not hierarchical rule files with @include directives.

What ships:
  1. Checklist continuity (delegates to the required context projection)
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
#  Attachment: Host-side checklist continuity after compaction
# ═══════════════════════════════════════════════════════════════════════════════

def _get_todo_attachment(messages: list, task: dict) -> str | None:
    """Delegate legacy attachment callers to the required context owner."""
    from lib.tasks_pkg.context_composer.checklist import build_checklist_context_block
    block = build_checklist_context_block(messages, task)
    return block.content if block is not None else None


# ═══════════════════════════════════════════════════════════════════════════════
#  Main entry point
# ═══════════════════════════════════════════════════════════════════════════════

def compute_turn_attachments(
    messages: list,
    task: dict,
    round_num: int,
    conv_id: str,
    project_path: str = '',
    project_enabled: bool = False,
) -> list[str]:
    """Compute all per-turn attachments to inject before the LLM call.

    Returns attachment text blocks for new tail user carriers. Checklist
    state delegates to the required composer owner, not an advisory budget.

    Lightweight: no LLM calls, just state-based decisions.
    """
    attachments = []

    # 1. The checklist lives outside message history and therefore survives
    #    L2. Re-expose it only if its newest canonical state is no longer in
    #    the model-visible messages (normally immediately after compaction).
    todo_reminder = _get_todo_attachment(messages, task)
    if todo_reminder:
        attachments.append(todo_reminder)

    if attachments:
        _chars = sum(len(a) for a in attachments)
        logger.debug('[Context] conv=%s round=%d inject block=per_turn '
                     'count=%d chars=%d',
                     conv_id[:8] if conv_id else '?', round_num,
                     len(attachments), _chars)

    return attachments


def inject_attachments(messages: list, attachments: list[str],
                        conv_id: str | None = None, *, task: dict | None = None,
                        round_num: int = 0, model: str = ''):
    """Inject computed attachments into the messages list.

    CACHE-CRITICAL: attachments are appended as a NEW trailing message,
    NOT merged into the last historical user message.

    The old behaviour walked backward to the last ``user`` message and
    edited it in place. After even one tool round that message lives at
    index 1 — well inside the prompt-cache prefix (``messages[0:N-2]``).
    Editing it changed the cached bytes, so the next round could not read
    the previously-written prefix and re-billed the whole thing uncached
    (cache_read=0, full cache_write). The code papered over this by calling
    ``notify_compaction`` to silence the PREFIX-MUTATION warning — but the
    cache miss (and its cost) still happened every time the reminder fired.

    Appending a fresh trailing message keeps the entire historical prefix
    byte-identical, so round N+1 reads round N's cache. The new message sits
    at the conversation tail where the volatile (5m) cache breakpoint lives,
    which is the correct, cache-friendly home for per-turn context.

    Args:
        messages:    Message list mutated in place (a message is appended).
        attachments: Pre-computed attachment blocks from compute_turn_attachments.
        conv_id:     Unused for cache bookkeeping now (no prefix mutation
            occurs), kept for signature stability / logging.
    """
    if not attachments:
        return

    from lib.tasks_pkg.context_composer import (
        ComposeRequest,
        append_context_blocks,
    )

    from lib.tasks_pkg.context_composer import context_block
    from lib.tasks_pkg.context_composer.checklist import (
        CHECKLIST_HEADING, checklist_context_block,
    )
    blocks = []
    optional = []
    for attachment in attachments:
        if attachment.startswith((CHECKLIST_HEADING + '\n',
                                  '<system-reminder>\n' + CHECKLIST_HEADING + '\n')):
            blocks.append(checklist_context_block(attachment))
        else:
            optional.append(attachment)
    if optional:
        blocks.append(context_block(
            'modified_files', '\n\n'.join(optional),
            provenance={'round': round_num, 'count': len(optional)},
        ))
    append_context_blocks(messages, blocks, ComposeRequest(
        conv_id=conv_id or '', model=model, task=task,
    ))
