# HOT_PATH — called before every LLM API call.
"""Per-Turn Attachments — dynamic context injection on every turn.

Inspired by Claude Code's ``attachments.ts`` (3997 lines), which computes
per-turn injections including file attachments, delta announcements, memory
surfacing, TODO reminders, and memory discoveries.

Tofu adaptation: because we inject all context into the system message (not
via separate `user` messages like Claude Code), our attachments are appended
to the last user message as <system-reminder> blocks.

Why we CAN'T replicate Claude Code's full attachment system:
  - Claude Code uses 40+ attachment types including hook outputs, teammate
    mailbox messages, diagnostic injections, and speculation overlay context.
    These require the Hook system, Coordinator mode, and Speculation system
    which are not architecturally present in Tofu.
  - Claude Code's per-turn relevant memory surfacing uses hierarchical
    CLAUDE.md files with @include directives.  Tofu uses flat project
    context and memory.

What we CAN implement:
  1. Recently modified files reminder
  2. Periodic TODO/next-step reminders
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)

# Tool names that count as a "write" for the reminder trigger.
_WRITE_TOOLS = frozenset({
    'write_file', 'apply_diff', 'apply_diffs',
    'insert_content', 'insert_contents',
})

# Marker that uniquely identifies a previously-injected reminder in history.
_REMINDER_MARKER = '## Recently Modified Files'

# Minimum number of messages that must separate the most recent write from
# the conversation tail before the reminder fires. Small enough to nudge the
# model after it "moves on", large enough not to nag mid-edit-burst.
_MIN_GAP_MESSAGES = 6


# ═══════════════════════════════════════════════════════════════════════════════
#  Attachment 1: Recently Modified Files Reminder
# ═══════════════════════════════════════════════════════════════════════════════
#
# TRIGGER IS A PURE MESSAGE SCAN — no per-conversation round counters.
#
# The previous implementation kept a module-level ``_attachment_state`` keyed
# by conv_id and gated on ``round_num``. That had two defects (B2/B3):
#   • DEAD FEATURE across tasks. ``round_num`` resets to 0 every task, but the
#     per-conv state persisted, so a stale ``last_reminder_round`` (pushed high
#     in task 1) made ``(round_num - last_reminder_round) < 5`` permanently
#     true in task 2 → the reminder never fired again for that conversation.
#   • UNBOUNDED LEAK. The state dict was never evicted.
# Both vanish when the trigger is derived purely from the message list (which
# the orchestrator already carries the full, cross-task history of): the
# messages ARE the state, so there is no counter to desync and no dict to leak.


def _msg_has_write(msg: dict) -> bool:
    """True if *msg* is an assistant turn that called a write tool."""
    for tc in msg.get('tool_calls', []) or []:
        if (tc.get('function') or {}).get('name', '') in _WRITE_TOOLS:
            return True
    return False


def _msg_has_reminder(msg: dict) -> bool:
    """True if *msg* is a previously-injected modified-files reminder."""
    content = msg.get('content', '')
    if isinstance(content, str):
        return _REMINDER_MARKER in content
    if isinstance(content, list):
        return any(isinstance(b, dict) and b.get('type') == 'text'
                   and _REMINDER_MARKER in b.get('text', '')
                   for b in content)
    return False


def _get_modified_files_attachment(messages: list, project_path: str) -> str | None:
    """Generate a reminder about recently modified files, or None.

    Fires when, scanning the message list, ALL hold:
      1. A write tool was called at some point (there is something to verify).
      2. The most recent write is at least ``_MIN_GAP_MESSAGES`` messages back
         from the tail — i.e. the model has "moved on" (not mid-edit-burst).
      3. No reminder has been injected SINCE that most recent write — otherwise
         we'd stack duplicate reminders every turn (B3). A new write AFTER the
         last reminder legitimately re-arms the trigger.

    Purely a function of the message list — no round_num, no per-conv state.
    """
    last_write_idx = -1
    last_reminder_idx = -1
    for i, msg in enumerate(messages):
        if _msg_has_write(msg):
            last_write_idx = i
        if _msg_has_reminder(msg):
            last_reminder_idx = i

    # (1) nothing written yet → nothing to remind about.
    if last_write_idx < 0:
        return None
    # (2) still mid-edit (write too close to the tail) → don't nag.
    if (len(messages) - 1 - last_write_idx) < _MIN_GAP_MESSAGES:
        return None
    # (3) a reminder already followed the most recent write → don't restack.
    if last_reminder_idx > last_write_idx:
        return None

    from lib.tasks_pkg.compaction import _extract_recently_accessed_files
    files = _extract_recently_accessed_files(messages, max_files=5)
    if not files:
        return None

    file_list = '\n'.join(f'  - {f}' for f in files)
    return (
        '<system-reminder>\n'
        f'{_REMINDER_MARKER}\n'
        'Files that were modified earlier in this conversation. '
        'Consider re-reading them if you need to verify current state:\n'
        f'{file_list}\n'
        '</system-reminder>'
    )


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

    Returns a list of attachment text blocks.  The orchestrator appends these
    to the last user message (or injects as a new user message).

    Lightweight: no LLM calls, just state-based decisions.
    """
    attachments = []

    # 1. Modified files reminder. Trigger is a pure message scan (see
    #    _get_modified_files_attachment) — no round_num gate, so it works
    #    across tasks where round_num resets to 0.
    if project_enabled and project_path:
        files_reminder = _get_modified_files_attachment(messages, project_path)
        if files_reminder:
            attachments.append(files_reminder)

    if attachments:
        _chars = sum(len(a) for a in attachments)
        logger.debug('[Context] conv=%s round=%d inject block=modified_files '
                     'count=%d chars=%d',
                     conv_id[:8] if conv_id else '?', round_num,
                     len(attachments), _chars)

    return attachments


def inject_attachments(messages: list, attachments: list[str],
                        conv_id: str | None = None):
    """Inject computed attachments into the messages list.

    ★ CACHE-CRITICAL: attachments are appended as a NEW trailing message,
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
    which is the correct, cache-friendly home for per-turn context (mirrors
    Claude Code's attachment placement in the latest turn).

    Args:
        messages:    Message list mutated in place (a message is appended).
        attachments: Pre-computed attachment blocks from compute_turn_attachments.
        conv_id:     Unused for cache bookkeeping now (no prefix mutation
            occurs), kept for signature stability / logging.
    """
    if not attachments:
        return

    combined = '\n\n'.join(attachments)

    # Append as a NEW trailing user message. Marked _isMeta so the debug
    # panel / token counter / persistence layers recognise it as synthetic
    # injected context rather than a real user turn (same convention as the
    # CLAUDE.md context message in system_context._insert_user_context_message).
    messages.append({
        'role': 'user',
        'content': combined,
        '_isMeta': True,
    })

