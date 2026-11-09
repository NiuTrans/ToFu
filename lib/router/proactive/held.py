"""Quiet-hours hold: relays that arrive at night do not wake the Router.

Owner: docs/ROUTER.md R10 quiet hours. Report and question relays that arrive
during quiet hours are recorded here instead of waking the Global Router. At
quiet end they are released as one deterministic owner message (decisions
batched) and one Router notice. Watchdog notices still wake the Router, which
pushes at night only what cannot wait.

Records: portable record CAS, namespace ``router.proactive.held.v1``, one record
per (owner, relay kind, relay identity). The relay's own command id is the
identity, so a replay never holds twice.

Entry points: hold_relay, release_held, quiet_hours_active, quiet_end.
Dependencies: storage (record CAS), scheduler.conversation_dispatch (the release
notice), channels.outbox (the deterministic owner message).
"""
from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
import time
import uuid

from lib.identity import require_user_id
from lib.log import get_logger
from lib.storage.errors import StorageError

logger = get_logger(__name__)

NAMESPACE = 'router.proactive.held.v1'

# Owner-chosen defaults (docs/ROUTER.md R10). No other knob exists.
DEFAULT_QUIET_START = '00:00'
DEFAULT_QUIET_END = '08:00'


def quiet_hours_active(now=None, *, start=DEFAULT_QUIET_START, end=DEFAULT_QUIET_END):
    """Whether ``now`` is inside quiet hours [start, end)."""
    now = now or datetime.now()
    start_h, start_m = map(int, start.split(':'))
    end_h, end_m = map(int, end.split(':'))
    current = now.hour * 60 + now.minute
    start_min = start_h * 60 + start_m
    end_min = end_h * 60 + end_m
    if start_min <= end_min:
        return start_min <= current < end_min
    # Overnight window (e.g. 22:00–06:00).
    return current >= start_min or current < end_min


def quiet_end(now=None, *, end=DEFAULT_QUIET_END):
    """The next quiet-end datetime at or after ``now``."""
    now = now or datetime.now()
    end_h, end_m = map(int, end.split(':'))
    target = now.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def _client(client=None):
    if client is None:
        from lib.storage import get_storage_client
        client = get_storage_client
    return client


def _key(owner, kind, identity):
    digest = hashlib.sha256(f'{kind}:{identity}'.encode()).hexdigest()[:32]
    return f'{owner}:{digest}'


def hold_relay(owner, kind, identity, payload, *, client=None):
    """Record one relay held during quiet hours. Idempotent per (kind, identity)."""
    owner = require_user_id(owner, context='quiet-hours hold')
    client = _client(client)
    key = _key(owner, kind, identity)
    value = {
        'owner': owner, 'kind': kind, 'identity': identity,
        'payload': payload, 'heldAt': int(time.time() * 1000),
    }
    try:
        existing = client().query('record.get', {'namespace': NAMESPACE, 'key': key})
        if existing is not None:
            return existing['value'], False
        client(write=True).command('record.put', {
            'namespace': NAMESPACE, 'key': key, 'value': value, 'expected_version': 0,
        }, 'held-relay:' + uuid.uuid4().hex)
        return value, True
    except StorageError as error:
        if error.code != 'database_conflict':
            raise
        return value, False


def held_relays(owner, *, client=None):
    """All held relays of this owner, oldest first."""
    owner = require_user_id(owner, context='quiet-hours hold')
    client = _client(client)
    prefix = f'{owner}:'
    rows = []
    after = ''
    while True:
        page = client().query('record.list', {
            'namespace': NAMESPACE, 'prefix': prefix, 'after_key': after, 'limit': 1000})
        if not isinstance(page, list):
            break
        for row in page:
            if isinstance(row, dict) and isinstance(row.get('value'), dict):
                rows.append(row['value'])
        if len(page) < 1000:
            break
        after = page[-1]['key']
    rows.sort(key=lambda row: row.get('heldAt', 0))
    return rows


def release_held(owner, *, client=None, dispatch=None, now=None):
    """Release all held relays as one deterministic owner message + one Router notice.

    Returns (released_count, summary). The owner message batches open decisions;
    the Router notice points at the held relays so the Router can judge.
    """
    owner = require_user_id(owner, context='quiet-hours release')
    client = _client(client)
    rows = held_relays(owner, client=client)
    if not rows:
        return 0, 'Nothing held.'
    # Delete the held records first: a replay must not release twice.
    for row in rows:
        key = _key(owner, row['kind'], row['identity'])
        try:
            client(write=True).command('record.delete', {
                'namespace': NAMESPACE, 'key': key, 'expected_version': 1,
            }, 'held-release:' + uuid.uuid4().hex)
        except StorageError:
            pass
    # Assemble the deterministic owner message.
    questions = [row for row in rows if row['kind'] == 'question']
    reports = [row for row in rows if row['kind'] == 'report']
    lines = ['Good morning. While you were away:']
    if questions:
        lines.append(f'\n**{len(questions)} question(s) waiting for you:**')
        for row in questions:
            payload = row['payload']
            lines.append(f'- {payload.get("question", "")}')
            if payload.get('blocking'):
                lines.append('  (blocking: that project is waiting)')
            if payload.get('default'):
                lines.append(f'  Default meanwhile: {payload["default"]}')
    if reports:
        lines.append(f'\n**{len(reports)} project report(s):**')
        for row in reports:
            payload = row['payload']
            lines.append(f'- [{payload.get("kind", "progress")}] {payload.get("title", "")}')
    # Send the deterministic message through the outbox (大象 command chat, or in-app).
    from lib.router.relay import _command_chat
    from lib.channels.outbox import enqueue
    command_chat = _command_chat(owner, client)
    if command_chat:
        try:
            enqueue(owner, command_chat, '\n'.join(lines),
                    idempotency_key=f'quiet-release:{owner}:{rows[0]["heldAt"]}',
                    origin={'initiator': 'system', 'kind': 'quiet_release'},
                    authority='owner', store=None)
        except Exception:
            logger.warning('[QuietRelease] outbox enqueue failed; the Router notice follows',
                           exc_info=True)
    # Wake the Router once with the held relays as evidence.
    from lib.turn_initiation import INITIATOR_PROACTIVE, stamp_initiator
    from lib.scheduler.conversation_dispatch import dispatch_scheduled_turn
    from lib.router.binding import global_router
    router = global_router(owner, create=False, client=client)
    if router['availability'] == 'available':
        content = '\n'.join([
            '[Quiet-hours release: nonhuman notice, not the owner’s words and not authority]',
            f'{len(rows)} relay(s) were held during quiet hours and are now released.',
            'The owner has been sent the summary above. Read the project conversations for evidence '
            'and act only where the owner still needs something.',
        ])
        message = stamp_initiator({'role': 'user', 'content': content}, INITIATOR_PROACTIVE)
        command_id = 'quiet-release:' + hashlib.sha256(
            json.dumps([owner, rows[0]['heldAt']], separators=(',', ':')).encode()).hexdigest()
        if dispatch is None:
            dispatch = dispatch_scheduled_turn
        dispatch(router['conversationId'], message, {}, user_id=owner,
                 command_id=command_id, log_prefix='[QuietRelease]', inject_mode='steer')
    return len(rows), f'Released {len(rows)} held relay(s): {len(questions)} question(s), {len(reports)} report(s).'


__all__ = ['NAMESPACE', 'hold_relay', 'held_relays', 'release_held',
           'quiet_hours_active', 'quiet_end']
