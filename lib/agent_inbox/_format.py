"""lib/agent_inbox/_format.py — XML payload helpers.

Pure formatters with no shared state — keep formatting consistent across
callers of the inbox.
"""

from __future__ import annotations

from lib.log import get_logger
from lib.tools.result_surfaces_generated import CONTRACT

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
                         result: str,
                         output_file: str = '',
                         remaining_running: int = 0,
                         remaining_pending: int = 0,
                         error: str = '') -> str:
    """Build a ``<swarm-update>`` XML payload for a single agent completion.

    Mirrors Claude Code's ``<task-notification>`` shape. The agent's final
    answer is carried IN FULL: it is natural text the orchestrator asked
    for, and truncating it would force a second ``get_agent_result`` round
    trip for every non-trivial result. The durable per-agent log remains
    the audit copy.
    """
    result_clean = (result or '').replace('\r', ' ').strip()

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
    if result_clean:
        tag = CONTRACT['notifications']['swarm']['reportTags'][0]
        parts.append(f'  <{tag}>{_escape(result_clean)}</{tag}>')
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
