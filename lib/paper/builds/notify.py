"""Return failed build verdicts to a live agent turn.

Browser preview builds settle out of the model's sight: the panel renders
diagnostics, but an agent that just edited sources cannot see them. When a
build fails for a conversation with a running turn, this module enqueues ONE
bounded ``<paper-build-update>`` into the conversation inbox (drained at the
next round boundary, like ``<swarm-update>``). Delivery is deliberately
volatile-only: with no live turn the preview panel is already the user's
surface, and a durable queue row would burn an unattended model turn.

Dedupe is per (owner, document, source digest): editing sources mints a new
digest, so every DISTINCT broken state is reported once while repeated
builds of the same broken tree stay silent.
"""
from __future__ import annotations

import threading

from lib.log import get_logger

logger = get_logger(__name__)

_MAX_ROWS = 8
_MAX_MESSAGE = 280
_SEEN_CAP = 256
_seen_lock = threading.Lock()
_seen: dict[tuple[int, str, str], None] = {}


def _claim(owner_user_id: int, document_id: str, digest: str) -> bool:
    key = (owner_user_id, document_id, digest)
    with _seen_lock:
        if key in _seen:
            return False
        if len(_seen) >= _SEEN_CAP:
            _seen.pop(next(iter(_seen)))
        _seen[key] = None
        return True


def _payload(receipt: dict) -> str:
    diagnostics = receipt.get('diagnostics') or []
    errors = [row for row in diagnostics if row.get('severity') == 'error']
    warnings = [row for row in diagnostics if row.get('severity') == 'warning']
    rows = ['<paper-build-update>',
            '  <status>failed</status>',
            f'  <main-file>{receipt.get("main_file") or "?"}</main-file>',
            f'  <engine>{receipt.get("engine") or "?"}</engine>',
            f'  <errors>{len(errors)}</errors>',
            f'  <warnings>{len(warnings)}</warnings>']
    for row in (errors + warnings)[:_MAX_ROWS]:
        where = row.get('file') or '?'
        if row.get('line'):
            where += ':%d' % row['line']
        message = (row.get('message') or '').replace('\n', ' ')[:_MAX_MESSAGE]
        rows.append(f'  <diagnostic severity="{row.get("severity")}" '
                    f'code="{row.get("code")}" where="{where}">{message}</diagnostic>')
    rows.append('  <action>Fix every error at its reported file:line, then call '
                'paper_build to verify; paper_build(action=diagnostics) reports '
                'the latest build without recompiling.</action>')
    rows.append('</paper-build-update>')
    return '\n'.join(rows)


def notify_build_failed(*, owner_user_id: int, conversation_id: str,
                        receipt: dict) -> bool:
    """Best-effort inbox delivery of one failed-build verdict."""
    if not conversation_id or receipt.get('status') != 'failed':
        return False
    if not _claim(owner_user_id, receipt.get('document_id') or '',
                  receipt.get('source_digest') or ''):
        return False
    try:
        from lib.agent_inbox import enqueue
        return enqueue(conversation_id, _payload(receipt),
                       mode='paper-build', priority='next')
    except Exception as exc:  # delivery is advisory; the panel stays authoritative
        logger.debug('[paper-build] failed-build inbox delivery skipped: %s', exc)
        return False
