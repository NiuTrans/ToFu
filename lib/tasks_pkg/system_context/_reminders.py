"""System-message manipulation primitives — reminders + append helpers.

Extracted from ``lib.tasks_pkg.system_context`` (facade-preserving split).

Holds the low-level, dependency-free helpers used by the injection
orchestrator: the ``<system-reminder>`` wrapper, the first-system-message
appender, the plain-text system extractor, and the old-timestamp stripper.
"""

from lib.log import get_logger

logger = get_logger(__name__)

_TIMESTAMP_PREFIX = 'Current date and time: '


def _strip_old_timestamp(text: str) -> str:
    """Remove a previously injected timestamp line from user message text."""
    lines = text.split('\n')
    cleaned = [ln for ln in lines if not ln.strip().startswith(_TIMESTAMP_PREFIX)]
    # Also strip trailing blank lines left behind
    result = '\n'.join(cleaned).rstrip()
    return result


def _wrap_system_reminder(text: str) -> str:
    """Wrap text in <system-reminder> tags.

    Claude Code wraps all mid-conversation system-level injections in these
    tags to distinguish them from user-authored content.  The model is trained
    to treat <system-reminder> content as authoritative system instructions.

    We use the same convention for dynamic injected context (project, memory,
    search addendum, swarm) so that:
      1. The model clearly distinguishes system instructions from user text.
      2. Compaction can identify and preserve system-reminder blocks.
      3. Context is consistent with Claude Code's convention.
    """
    return f'<system-reminder>\n{text}\n</system-reminder>'


def _append_to_system_message(messages, text, *, as_separate_block=False):
    """Append text to the first system message, or create one if absent.

    Helper used by _inject_system_contexts to avoid repeating the
    str-vs-list content detection pattern.

    Args:
        messages: The messages list (mutated in-place).
        text: The text to append.
        as_separate_block: If True and content is already a list,
            append as a separate text block (for cache segmentation).
            If content is a string, convert to list-of-blocks first.
    """
    if messages and messages[0].get('role') == 'system':
        sc = messages[0].get('content', '')
        if as_separate_block:
            # Force list-of-blocks format for cache segmentation
            if isinstance(sc, str):
                messages[0]['content'] = [
                    {'type': 'text', 'text': sc},
                    {'type': 'text', 'text': text},
                ]
            elif isinstance(sc, list):
                messages[0]['content'].append({'type': 'text', 'text': text})
            else:
                messages[0]['content'] = [{'type': 'text', 'text': text}]
        else:
            if isinstance(sc, str):
                messages[0]['content'] = sc + '\n\n' + text
            elif isinstance(sc, list):
                # Merge into last text block to avoid block proliferation
                if sc and isinstance(sc[-1], dict) and sc[-1].get('type') == 'text':
                    sc[-1] = {**sc[-1], 'text': sc[-1]['text'] + '\n\n' + text}
                else:
                    messages[0]['content'].append({'type': 'text', 'text': text})
    else:
        # No system message yet — create one.
        # Respect as_separate_block so callers that want downstream cache
        # segmentation don't get stuck with a string content.
        if as_separate_block:
            messages.insert(0, {'role': 'system',
                                'content': [{'type': 'text', 'text': text}]})
        else:
            messages.insert(0, {'role': 'system', 'content': text.strip()})


def _system_text(messages) -> str:
    """Return the plain-text concatenation of the first system message.

    Used for idempotency checks in ``_inject_system_contexts`` — callers
    can look for a known marker substring (e.g. ``[PROJECT CO-PILOT MODE]``,
    ``Function Result Clearing``) to detect whether a context block has
    already been injected.  Returns empty string when there is no system
    message.
    """
    if not messages or messages[0].get('role') != 'system':
        return ''
    sc = messages[0].get('content', '')
    if isinstance(sc, str):
        return sc
    if isinstance(sc, list):
        parts = []
        for b in sc:
            if isinstance(b, dict) and b.get('type') == 'text':
                parts.append(b.get('text', '') or '')
        return '\n\n'.join(parts)
    return ''
