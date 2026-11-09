"""Digest: a deterministic daily summary for the owner, zero model requests.

Owner: docs/ROUTER.md R10 reporting rules. At the owner's digest time (default
21:00), assemble from durable records: open decisions (blocking first, then
oldest), overdue commitments, open faults, verified deliveries since the last
digest, projects active but silent, and daily figures. Skipped when empty.
Delivered through the outbox (大象 command chat) or as an in-app report Turn
in the Global Router conversation.

Entry points: assemble_digest, run_digest.
Dependencies: router.proactive.questions, conversations.coordinator_progress,
router.watchdog.snapshot, channels.outbox, conversations.coordinator_reports.
"""
from __future__ import annotations

from datetime import datetime
import time

from lib.identity import require_user_id
from lib.log import get_logger

logger = get_logger(__name__)


def assemble_digest(owner, *, client=None, now=None):
    """Build the digest text from durable records. Returns (text, counts) or (None, {})."""
    owner = require_user_id(owner, context='digest')
    from lib.router.proactive.questions import open_questions
    from lib.conversations.coordinator_progress import CoordinatorTaskProgress
    from lib.router.projects import router_projects

    now = now or datetime.now()
    sections = []

    # 1. Open decisions (blocking first, then oldest).
    questions = open_questions(owner, client=client)
    if questions:
        blocking = [q for q in questions if q.get('blocking')]
        nonblocking = [q for q in questions if not q.get('blocking')]
        ordered = blocking + nonblocking
        lines = [f'**{len(questions)} decision(s) waiting for you:**']
        for q in ordered:
            age_h = int((time.time() * 1000 - q.get('createdAt', 0)) / 3_600_000)
            age = f'{age_h}h' if age_h < 48 else f'{age_h // 24}d'
            prefix = '⏸ ' if q.get('blocking') else '  '
            lines.append(f'{prefix}{q["question"]} ({age})')
            if q.get('default'):
                lines.append(f'   Default: {q["default"]}')
        sections.append('\n'.join(lines))

    # 2. Overdue commitments.
    overdue = []
    try:
        projects = router_projects(owner, {}, client=client)
        for entry in projects.get('projects', []):
            if entry.get('availability') != 'available':
                continue
            progress = CoordinatorTaskProgress(owner, entry['project_path'], client=client)
            row = progress._head()
            if row is None:
                continue
            head = row['value']
            for receipt in head.get('commitments', []):
                if receipt['state'] != 'open':
                    continue
                if receipt['dueAt'] < time.time() * 1000 and 'wake' in receipt:
                    overdue.append({'title': receipt['title'], 'project': entry['project_path'],
                                    'dueAt': receipt['dueAt'], 'slips': receipt.get('slips', 0)})
    except Exception:
        logger.warning('[Digest] commitment scan failed', exc_info=True)
    if overdue:
        lines = [f'**{len(overdue)} overdue promise(s):**']
        for item in overdue:
            from lib.conversations.coordinator_commitments import iso_ms
            lines.append(f'- "{item["title"]}" — was due {iso_ms(item["dueAt"])}'
                         + (f' (moved {item["slips"]}x)' if item['slips'] else ''))
        sections.append('\n'.join(lines))

    # 3. Open faults (Watchdog signals).
    try:
        from lib.router.watchdog.snapshot import health_snapshot
        from lib.router.watchdog.binding import router_watchdog
        watchdog = router_watchdog(owner, create=False)
        if watchdog['availability'] == 'available':
            snapshot = health_snapshot(owner)
            open_signals = snapshot.get('open_signals', [])
            if open_signals:
                lines = [f'**{len(open_signals)} open fault(s):**']
                for signal in open_signals[:10]:
                    lines.append(f'- {signal.get("source", "?")}: {signal.get("detail", "")[:120]}')
                sections.append('\n'.join(lines))
    except Exception:
        logger.debug('[Digest] fault scan unavailable', exc_info=True)

    # 4. Verified deliveries since the last digest.
    deliveries = []
    try:
        for entry in projects.get('projects', []):
            last_report = entry.get('last_report')
            if last_report and last_report.get('kind') == 'delivery' and last_report.get('unread'):
                deliveries.append({'title': last_report['title'], 'project': entry['project_path']})
    except Exception as error:
        logger.debug('[Digest] verified-delivery scan unavailable: %s', error)
    if deliveries:
        lines = [f'**{len(deliveries)} verified delivery(ies):**']
        for item in deliveries:
            lines.append(f'- {item["title"]}')
        sections.append('\n'.join(lines))

    # 5. Projects active but silent (no report in the last 24h).
    silent = []
    try:
        for entry in projects.get('projects', []):
            if entry.get('active_workers', 0) > 0 and not entry.get('last_report'):
                silent.append(entry['project_path'].rsplit('/', 1)[-1])
    except Exception as error:
        logger.debug('[Digest] active-but-silent scan unavailable: %s', error)
    if silent:
        sections.append(f'**Active but silent:** {", ".join(silent)}')

    if not sections:
        return None, {}
    text = f'📋 Daily digest — {now.strftime("%Y-%m-%d %H:%M")}\n\n' + '\n\n'.join(sections)
    counts = {'questions': len(questions), 'overdue': len(overdue),
              'deliveries': len(deliveries), 'silent_projects': len(silent)}
    return text, counts


def run_digest(task, *, client=None, now=None):
    """Scheduler handler for the daily digest. Returns (ok, result)."""
    owner = require_user_id(task.get('user_id'), context='digest')
    text, counts = assemble_digest(owner, client=client, now=now)
    if text is None:
        return True, 'Nothing to digest; skipped.'
    # Deliver through the outbox (大象 command chat) or in-app.
    from lib.router.relay import _command_chat
    command_chat = _command_chat(owner, client)
    if command_chat:
        from lib.channels.outbox import enqueue
        day = (now or datetime.now()).strftime('%Y-%m-%d')
        try:
            enqueue(owner, command_chat, text,
                    idempotency_key=f'digest:{owner}:{day}',
                    origin={'initiator': 'system', 'kind': 'digest'},
                    authority='owner')
            return True, f'Digest sent to 大象: {counts}'
        except Exception:
            logger.warning('[Digest] outbox enqueue failed; in-app fallback', exc_info=True)
    # In-app fallback: a report Turn in the Global Router conversation.
    from lib.router.binding import global_router
    from lib.conversations.coordinator_reports import CoordinatorReports
    from lib.router.home import router_home_path
    router = global_router(owner, create=False, client=client)
    if router['availability'] != 'available':
        return False, 'No Global Router and no 大象; digest not delivered.'
    home = router_home_path(owner)
    reports = CoordinatorReports(owner, home, client=client)
    scope = {'owner': owner, 'projectPath': home,
             'coordinatorId': router['conversationId'], 'bindingRevision': router['revision']}
    day = (now or datetime.now()).strftime('%Y-%m-%d')
    reports.publish(scope, {'kind': 'progress', 'title': f'Daily digest {day}',
                            'message': text, 'idempotency_key': f'digest:{owner}:{day}'})
    return True, f'Digest published in-app: {counts}'


__all__ = ['assemble_digest', 'run_digest']
