"""lib/agent_inbox/_format.py — XML payload helpers.

Pure formatters with no shared state — keep formatting consistent across
callers of the inbox.
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════
#  XML payload helpers — keep formatting consistent across callers
# ═══════════════════════════════════════════════════════════

def format_swarm_update(*,
                         agent_id: str,
                         role: str,
                         status: str,
                         elapsed_seconds: float,
                         tokens: int,
                         preview: str,
                         output_file: str = '',
                         remaining_running: int = 0,
                         remaining_pending: int = 0,
                         error: str = '') -> str:
    """Build a ``<swarm-update>`` XML payload for a single agent completion.

    Mirrors Claude Code's ``<task-notification>`` shape. The 200-char preview
    cap matches what we agreed in the design doc.
    """
    preview_clean = (preview or '').replace('\r', ' ').strip()
    if len(preview_clean) > 200:
        preview_clean = preview_clean[:200].rstrip() + '…'

    parts = [
        '<swarm-update>',
        f'  <agent-id>{_escape(agent_id)}</agent-id>',
        f'  <role>{_escape(role)}</role>',
        f'  <status>{_escape(status)}</status>',
        f'  <elapsed-seconds>{elapsed_seconds:.1f}</elapsed-seconds>',
        f'  <tokens>{int(tokens)}</tokens>',
    ]
    if output_file:
        parts.append(f'  <output-file>{_escape(output_file)}</output-file>')
    if error:
        parts.append(f'  <error>{_escape(error[:300])}</error>')
    if preview_clean:
        parts.append(f'  <preview>{_escape(preview_clean)}</preview>')
    if remaining_running or remaining_pending:
        parts.append(
            f'  <remaining running="{remaining_running}" '
            f'pending="{remaining_pending}"/>'
        )
    parts.append('</swarm-update>')
    return '\n'.join(parts)


def _escape(text: str) -> str:
    """Minimal XML escape for inline values."""
    if not text:
        return ''
    return (text
            .replace('&', '&amp;')
            .replace('<', '&lt;')
            .replace('>', '&gt;'))
