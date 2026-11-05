"""Complete owner-scoped transcript export.

Responsibility: render EVERY message of one conversation as one Markdown
document, regardless of length, for a user-initiated download.

This is deliberately separate from :func:`lib.conv_ref.get_conversation`.
That renderer is the model-facing reference read: it keeps a HEAD+TAIL window
and clamps the result to ``MAX_CHARS`` so one reference cannot consume the
context window. An export has the opposite contract — dropping messages from a
file the user downloads as a record is silent data loss, so this module pages
the authority through the existing bounded window read and concatenates the
whole transcript.

Entry point: :func:`render_full_transcript`.
Dependencies: the owner-scoped conversation repository and the shared text/
tool-round formatters in ``_detail`` for message shape parity.
"""

from __future__ import annotations

from lib.conv_ref._detail import (
    _extract_text,
    _format_tool_rounds,
    _read_conversation_snapshot,
)

#: Messages per authority read. The window read rejects ``tail > 500``, so the
#: page size stays inside that documented bound.
EXPORT_PAGE_SIZE = 200

#: Hard ceiling on one exported document. This is a runaway-work guard, not a
#: content budget: a million-message conversation is a defect, not a transcript.
MAX_EXPORT_MESSAGES = 100_000


class TranscriptExportError(RuntimeError):
    """The transcript could not be read as one consistent snapshot."""


def _message_markdown(
    index: int, message: dict, *, include_tool_details: bool,
) -> list[str]:
    """Render one message with the same shape the single-conversation read uses."""
    lines: list[str] = []
    role = message.get('role', 'unknown')
    content = message.get('content', '')
    if role == 'user':
        lines.append(f"## User Message #{index + 1}")
        lines.append("")
        text = _extract_text(content)
        if text:
            lines.append(text)
            lines.append("")
        if message.get('images'):
            lines.append(f"_[Contains {len(message['images'])} image(s)]_")
            lines.append("")
        for attachment in message.get('attachments') or []:
            if not isinstance(attachment, dict):
                continue
            lines.append(
                f"_[Attachment: {attachment.get('name', 'unknown')} "
                f"({attachment.get('kind', 'media')}, "
                f"{attachment.get('status', 'unknown')})]_"
            )
            lines.append("")
        for pdf in message.get('pdfTexts') or []:
            lines.append(
                f"_[PDF: {pdf.get('name', 'unknown')} — "
                f"{pdf.get('pages', '?')} pages]_"
            )
            lines.append("")
            if include_tool_details and pdf.get('text'):
                lines.append('```text')
                lines.append(str(pdf['text']))
                lines.append('```')
                lines.append("")
        return lines

    if role == 'assistant':
        lines.append(f"## Assistant Response #{index + 1}")
        lines.append("")
        if content:
            lines.append(str(content))
            lines.append("")
        if include_tool_details and message.get('thinking'):
            lines.append("<details><summary>Thinking</summary>")
            lines.append("")
            lines.append(str(message['thinking']))
            lines.append("")
            lines.append("</details>")
            lines.append("")
        tool_rounds = message.get('toolRounds') or []
        if tool_rounds:
            lines.append(_format_tool_rounds(tool_rounds, include_tool_details))
            lines.append("")
        return lines

    lines.append(f"## {str(role).title()} Message #{index + 1}")
    lines.append("")
    text = _extract_text(content)
    if text:
        lines.append(text)
        lines.append("")
    return lines


def _read_all_messages(conversation_id: str, *, user_id: int) -> tuple[dict, list]:
    """Page the authority newest-first, then return one ascending message list.

    Pages are stitched only across one revision: the authority returns the
    conversation revision with every window, and a mismatch means the record
    changed under the export, which must fail loudly rather than produce a file
    that mixes two versions of the same conversation.
    """
    pages: list[list] = []
    before: int | None = None
    revision: int | None = None
    total: int | None = None
    while True:
        if before is None:
            snapshot = _read_conversation_snapshot(
                conversation_id, user_id=user_id,
                message_window=EXPORT_PAGE_SIZE,
            )
        else:
            snapshot = _read_conversation_snapshot(
                conversation_id, user_id=user_id,
                message_window=min(EXPORT_PAGE_SIZE, before),
                before_sequence=before,
            )
        if snapshot is None:
            raise TranscriptExportError('conversation_missing')
        page = snapshot.get('messages')
        if not isinstance(page, list):
            raise TranscriptExportError('conversation_messages_malformed')
        page_revision = snapshot.get('rev')
        page_total = snapshot.get('msg_count')
        if not isinstance(page_revision, int) or isinstance(page_revision, bool):
            raise TranscriptExportError('conversation_revision_malformed')
        if not isinstance(page_total, int) or isinstance(page_total, bool):
            raise TranscriptExportError('conversation_count_malformed')
        if revision is None:
            revision = page_revision
            total = page_total
            if total > MAX_EXPORT_MESSAGES:
                raise TranscriptExportError('conversation_too_large')
        elif page_revision != revision or page_total != total:
            raise TranscriptExportError('conversation_changed_during_export')
        if not page:
            break
        pages.append(page)
        # ``before`` is a count of messages preceding the window, so the next
        # page ends exactly where this one began.
        before = (total if before is None else before) - len(page)
        if before <= 0:
            break
    messages: list = []
    for page in reversed(pages):
        messages.extend(page)
    if total is not None and len(messages) != total:
        # A short read is a failed export, never a silently shorter file.
        raise TranscriptExportError('conversation_export_incomplete')
    return {'id': conversation_id, 'rev': revision, 'msg_count': total}, messages


def render_full_transcript(
    conversation_id: str,
    *,
    user_id: int,
    include_tool_details: bool = True,
) -> tuple[str, int]:
    """Return ``(markdown, message_count)`` for the complete transcript.

    Raises :class:`TranscriptExportError` when the authority cannot supply one
    consistent, complete snapshot.
    """
    snapshot, messages = _read_all_messages(conversation_id, user_id=user_id)
    header = [
        f"# {snapshot.get('id')}",
        "",
        f"_Exported from Tofu · conversation `{conversation_id}` · "
        f"{len(messages)} message(s)._",
        "",
    ]
    lines = list(header)
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise TranscriptExportError('conversation_message_malformed')
        lines.extend(
            _message_markdown(
                index, message, include_tool_details=include_tool_details,
            )
        )
        lines.append('---')
        lines.append('')
    return '\n'.join(lines).rstrip() + '\n', len(messages)


__all__ = [
    'EXPORT_PAGE_SIZE',
    'MAX_EXPORT_MESSAGES',
    'TranscriptExportError',
    'render_full_transcript',
]
