# HOT_PATH — called before every LLM API call.
"""Per-Turn Attachments — dynamic context injection on every turn.

Inspired by Claude Code's ``attachments.ts`` (3997 lines), which computes
per-turn injections including file attachments, delta announcements, memory
surfacing, TODO reminders, and skill discoveries.

ChatUI adaptation: because we inject all context into the system message (not
via separate `user` messages like Claude Code), our attachments are appended
to the last user message as <system-reminder> blocks.

Why we CAN'T replicate Claude Code's full attachment system:
  - Claude Code uses 40+ attachment types including hook outputs, teammate
    mailbox messages, diagnostic injections, and speculation overlay context.
    These require the Hook system, Coordinator mode, and Speculation system
    which are not architecturally present in ChatUI.
  - Claude Code's per-turn relevant memory surfacing uses hierarchical
    CLAUDE.md files with @include directives.  ChatUI uses flat project
    context and skills.

What we CAN implement:
  1. Session memory injection (from session_memory.py)
  2. Recently modified files reminder
  3. Periodic TODO/next-step reminders
  4. Tool announcement deltas (new tools discovered via tool_search)
"""

from __future__ import annotations

import time
from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
#  Per-conversation attachment state
# ═══════════════════════════════════════════════════════════════════════════════

_attachment_state: dict[str, dict[str, Any]] = {}
"""Per-conv_id state tracking: last_write_round, last_reminder_round, discovered_tools"""


def _get_state(conv_id: str) -> dict[str, Any]:
    """Get or create per-conversation attachment state."""
    if conv_id not in _attachment_state:
        _attachment_state[conv_id] = {
            'last_write_round': -1,
            'last_reminder_round': -1,
            'discovered_tools': set(),
            'rounds_since_write': 0,
        }
    return _attachment_state[conv_id]


# ═══════════════════════════════════════════════════════════════════════════════
#  Attachment 1: Session Memory
# ═══════════════════════════════════════════════════════════════════════════════

def _get_session_memory_attachment(conv_id: str) -> str | None:
    """Get session memory for injection into the conversation.

    Returns formatted session memory block or None.
    """
    from lib.tasks_pkg.session_memory import get_session_memory_for_prompt
    return get_session_memory_for_prompt(conv_id)


# ═══════════════════════════════════════════════════════════════════════════════
#  Attachment 2: Recently Modified Files Reminder
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
            if fn_name in ('write_file', 'apply_diff'):
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
#  Attachment 3: Tool Discovery Delta
# ═══════════════════════════════════════════════════════════════════════════════

def _get_tool_discovery_delta(task: dict, conv_id: str) -> str | None:
    """Announce newly discovered tools from tool_search.

    Inspired by Claude Code's getDeferredToolsDelta() — only announces
    changes since the last announcement.
    """
    state = _get_state(conv_id)
    current_discovered = set()

    # Scan task for dynamically discovered tools
    discovered_in_task = task.get('_discovered_tool_names', set())
    if isinstance(discovered_in_task, (set, frozenset)):
        current_discovered = set(discovered_in_task)
    elif isinstance(discovered_in_task, list):
        current_discovered = set(discovered_in_task)

    if not current_discovered:
        return None

    previously_announced = state.get('discovered_tools', set())
    new_tools = current_discovered - previously_announced

    if not new_tools:
        return None

    state['discovered_tools'] = current_discovered

    tool_list = ', '.join(sorted(new_tools))
    return (
        '<system-reminder>\n'
        f'## Newly Available Tools\n'
        f'The following tools have been discovered and are now available: '
        f'{tool_list}\n'
        f'You can call them directly.\n'
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

    # 1. Session memory
    if conv_id:
        mem = _get_session_memory_attachment(conv_id)
        if mem:
            attachments.append(mem)

    # 2. Modified files reminder
    if project_enabled and project_path and round_num > 5:
        files_reminder = _get_modified_files_attachment(
            messages, project_path, conv_id, round_num)
        if files_reminder:
            attachments.append(files_reminder)

    # 3. Tool discovery delta
    if task:
        tool_delta = _get_tool_discovery_delta(task, conv_id)
        if tool_delta:
            attachments.append(tool_delta)

    if attachments:
        logger.debug('[Attachments] conv=%s round=%d injecting %d attachment(s)',
                     conv_id[:8] if conv_id else '?', round_num, len(attachments))

    return attachments


def inject_attachments(messages: list, attachments: list[str]):
    """Inject computed attachments into the messages list.

    Appends attachments to the last user message as additional text blocks.
    If no user message exists, creates one.
    """
    if not attachments:
        return

    combined = '\n\n'.join(attachments)

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
            return

    # No user message found — append as a new one
    messages.append({'role': 'user', 'content': combined})

