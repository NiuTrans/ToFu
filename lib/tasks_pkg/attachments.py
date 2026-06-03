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

from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
#  Per-conversation attachment state
# ═══════════════════════════════════════════════════════════════════════════════

_attachment_state: dict[str, dict[str, Any]] = {}
"""Per-conv_id state tracking: last_write_round, last_reminder_round."""


def _get_state(conv_id: str) -> dict[str, Any]:
    """Get or create per-conversation attachment state."""
    if conv_id not in _attachment_state:
        _attachment_state[conv_id] = {
            'last_write_round': -1,
            'last_reminder_round': -1,
            'rounds_since_write': 0,
        }
    return _attachment_state[conv_id]


# ═══════════════════════════════════════════════════════════════════════════════
#  Attachment 1: Recently Modified Files Reminder
# ═══════════════════════════════════════════════════════════════════════════════

def _get_modified_files_attachment(messages: list, project_path: str,
                                    conv_id: str, round_num: int) -> str | None:
    """Generate a reminder about recently modified files.

    Inspired by Claude Code's TODO reminder pattern: fires after N turns
    since last write, then every M turns.  Reminds the model what files
    were changed so it can verify its work.

    Only fires every 5+ rounds after a write operation.
    """
    state = _get_state(conv_id)

    # Scan messages for recent write tool calls
    has_write_in_recent = False
    for msg in messages[-10:]:  # last 10 messages
        for tc in msg.get('tool_calls', []):
            fn_name = tc.get('function', {}).get('name', '')
            if fn_name in ('write_file', 'apply_diff', 'apply_diffs', 'insert_content', 'insert_contents'):
                has_write_in_recent = True
                state['last_write_round'] = round_num
                state['rounds_since_write'] = 0
                break

    if has_write_in_recent:
        state['rounds_since_write'] = 0
    else:
        state['rounds_since_write'] += 1

    # Don't fire if no writes happened, or if we just wrote
    if state['last_write_round'] < 0:
        return None
    if state['rounds_since_write'] < 5:
        return None
    if (round_num - state.get('last_reminder_round', -999)) < 5:
        return None

    state['last_reminder_round'] = round_num

    # Extract modified file paths from messages
    from lib.tasks_pkg.compaction import _extract_recently_accessed_files
    files = _extract_recently_accessed_files(messages, max_files=5)
    if not files:
        return None

    file_list = '\n'.join(f'  - {f}' for f in files)
    return (
        '<system-reminder>\n'
        '## Recently Modified Files\n'
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

    # 1. Modified files reminder
    if project_enabled and project_path and round_num > 5:
        files_reminder = _get_modified_files_attachment(
            messages, project_path, conv_id, round_num)
        if files_reminder:
            attachments.append(files_reminder)

    if attachments:
        logger.debug('[Attachments] conv=%s round=%d injecting %d attachment(s)',
                     conv_id[:8] if conv_id else '?', round_num, len(attachments))

    return attachments


def inject_attachments(messages: list, attachments: list[str],
                        conv_id: str | None = None):
    """Inject computed attachments into the messages list.

    Appends attachments to the last user message as additional text blocks.
    If no user message exists, creates one.

    Args:
        messages:    Message list mutated in place.
        attachments: Pre-computed attachment blocks from compute_turn_attachments.
        conv_id:     If provided, notify cache_tracking that we legitimately
            mutated a message inside the cache prefix. Without this, the
            next detect_cache_break() call false-positives as
            'PREFIX MUTATION DETECTED' (the original user message lives
            inside messages[0:N-2] once any tool round has been emitted).
    """
    if not attachments:
        return

    combined = '\n\n'.join(attachments)
    mutated_existing = False

    # Find last user message
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get('role') == 'user':
            content = messages[i].get('content', '')
            if isinstance(content, str):
                messages[i]['content'] = content + '\n\n' + combined
            elif isinstance(content, list):
                messages[i]['content'].append({
                    'type': 'text',
                    'text': '\n\n' + combined,
                })
            mutated_existing = True
            break
    else:
        # No user message found — append as a new one
        messages.append({'role': 'user', 'content': combined})

    if mutated_existing and conv_id:
        try:
            from lib.tasks_pkg.cache_tracking import notify_compaction
            notify_compaction(conv_id)
        except Exception as e:
            logger.debug('[Attachments] notify_compaction unavailable: %s', e)

