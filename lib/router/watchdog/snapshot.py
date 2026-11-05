"""Watchdog health snapshot: deterministic, owner-scoped facts for one review.

Responsibility: project what existing owners already record into one bounded
view, with no model call and no side effect: the Global Router and a page of
Project Routers (availability, last activity, open signals) with their Workers
that are active, failed, or whose delivery is held (objective, state, last
activity time and the settled Turn's error); outbox items in ``failed`` or
``unknown``; channel account and bridge health; and a page of open fault
signals. Pages follow the project dispatch history page bound, like
get_router_projects; Workers come from each project's newest history page.

Entry points: health_snapshot(owner, *, after_id=None, signals_after_id=None,
client=None), needs_review(snapshot).
Dependencies: project_coordination, project_dispatch (history, continuation
summaries), turn_lifecycle, channels (store, each channel's supervisor), router.home,
router.watchdog (binding, signals).
"""
from __future__ import annotations

from collections.abc import Mapping
import time

from lib.storage.errors import StorageError

from .signals import FaultSignals, UNHEALTHY_CHANNEL_STATES, _bounded

_HELD_DELIVERY = frozenset({'blocked', 'failed', 'preserved'})
# The supervisor hosting each contract channel (channels_v1 Channel enum).
CHANNEL_SUPERVISORS = {'dx': 'lib.channels.dx.supervisor', 'github': 'lib.channels.github.supervisor'}
_SETTLED_OK = frozenset({'completed', 'superseded', 'cancelled'})


def _last_activity(owner: int, conversation_id: str, client) -> int | None:
    from lib.conversations.repository import ConversationRepository
    snapshot = ConversationRepository(client).get(conversation_id, user_id=owner, include_messages=False)
    metadata = getattr(snapshot, 'metadata', None) or {}
    value = metadata.get('updated_at')
    return value if isinstance(value, int) else None


def _turn_facts(repository, conversation_id: str, turn_id: str) -> dict:
    turn = repository.turn_get(conversation_id, turn_id, user_id=repository.owner,
                               include_projection=False)
    facts = {'last_activity_at': turn.get('updatedAt')}
    error = (turn.get('settlement') or {}).get('error')
    if isinstance(error, Mapping) and error.get('kind'):
        facts['error'] = {'kind': str(error['kind'])[:64], 'detail': _bounded('detail', error.get('detail')),
                          **({'hint': _bounded('hint', error['hint'])} if error.get('hint') else {})}
    return facts


def _workers(owner: int, project: str, binding: dict, client) -> dict:
    """Workers of this binding epoch that need judgment, from the newest history page."""
    from lib.project_dispatch.continuation import worker_summaries
    from lib.project_dispatch.repository import ProjectDispatchRepository
    repository = (ProjectDispatchRepository(owner, project) if client is None
                  else ProjectDispatchRepository(owner, project, client=client))
    page = repository.history_page(None)
    rows = [row for row in page['rows']
            if row['request']['coordinatorId'] == binding['conversationId']
            and row['grant'].get('bindingRevision') == binding['revision']
            and row['request'].get('profile', {}).get('mode') != 'coordinator']
    workers = []
    for row, summary in zip(rows, worker_summaries(repository, rows)):
        delivery = summary.get('delivery') or {}
        held = delivery.get('status') in _HELD_DELIVERY
        if summary['status'] in _SETTLED_OK and not held:
            continue
        entry = {'assignment_id': summary['assignment_id'], 'conversation_id': summary['conversation_id'],
                 'objective': summary['title'], 'status': summary['status'],
                 'request_turn_id': row['request']['authorizationSource']['turnId']}
        if held:
            entry['delivery'] = {'status': delivery['status'], 'reason': delivery.get('reason') or ''}
        execution = row.get('execution')
        if execution:
            entry.update(_turn_facts(repository, summary['conversation_id'], execution['turnId']))
        workers.append(entry)
    return {'workers': workers,
            'workers_coverage': 'complete' if page.get('olderCursor') is None else 'newest_page'}


def _router(owner: int, project: str, binding: dict, signals: list[dict], client) -> dict:
    entry = {'project_path': project, 'conversation_id': binding['conversationId'],
             'binding_revision': binding['revision'], 'availability': binding['availability'],
             'open_signal_ids': [signal['signalId'] for signal in signals
                                 if signal['subject'].get('conversationId') == binding['conversationId']]}
    if binding['availability'] != 'available':
        return entry
    try:
        entry['last_activity_at'] = _last_activity(owner, binding['conversationId'], client)
        entry.update(_workers(owner, project, binding, client))
    except (StorageError, ValueError, PermissionError) as error:
        # One damaged project must not hide the others; the gap is explicit.
        entry['activity_error'] = str(error)
    return entry


def _channels(owner: int, client) -> tuple[list[dict], list[dict]]:
    import importlib
    from lib.channels.model import CHANNELS
    from lib.channels.store import ChannelStore
    store = ChannelStore(owner) if client is None else ChannelStore(owner, client=client)
    channels = []
    for channel in CHANNELS:
        account = store.account(channel)
        if not account:
            continue
        status = account.get('status') or {}
        supervisor = importlib.import_module(CHANNEL_SUPERVISORS[channel])
        channels.append({'channel': channel, 'enabled': bool(account.get('enabled')),
                         'state': status.get('state') or 'disabled', 'detail': status.get('detail') or '',
                         'since_ms': status.get('updatedAtMs'), 'connected_at_ms': status.get('connectedAtMs'),
                         'bridge': supervisor.bridge_state(owner)})
    outbox = [{'item_id': item.id, 'thread': item.thread, 'state': item.state, 'error': item.error or '',
               'attempt_at_ms': item.attempt_at_ms, 'queued_at_ms': item.queued_at_ms}
              for item in store.active_outbox() if item.state in ('failed', 'unknown')] if channels else []
    return channels, outbox


def health_snapshot(owner: int, *, after_id: str | None = None, signals_after_id: str | None = None,
                    client=None) -> dict:
    """One page of the owner's router health; the first page also carries the Global Router."""
    from lib.conversations.project_coordination import NAMESPACE, ProjectCoordinationRepository
    from lib.project_dispatch.history import PAGE_ROWS
    from lib.router.home import is_router_home, router_home_location
    from .binding import is_watchdog_location
    bindings = (ProjectCoordinationRepository(owner) if client is None
                else ProjectCoordinationRepository(owner, client=client))
    owner = bindings.user_id
    storage = client
    if storage is None:
        from lib.storage import get_storage_client
        storage = get_storage_client
    signal_page = FaultSignals(owner, client=client).open_page(signals_after_id)
    signals = signal_page['signals']
    snapshot = {'generated_at': int(time.time() * 1000), 'global_router': None}
    if after_id is None:
        home = router_home_location(owner)
        router = bindings.read(home)
        if router['availability'] != 'unbound':
            snapshot['global_router'] = _router(owner, home, router, signals, client)
    prefix = f'{owner}:'
    rows = storage().query('record.list', {'namespace': NAMESPACE, 'prefix': prefix,
                                           'after_key': prefix + after_id if after_id else '',
                                           'limit': PAGE_ROWS + 1})
    if not isinstance(rows, list) or len(rows) > PAGE_ROWS + 1:
        raise StorageError('database_integrity', 'Invalid coordination binding page')
    projects = []
    for row in rows[:PAGE_ROWS]:
        value = row.get('value') if isinstance(row, Mapping) else None
        if (not isinstance(row.get('key'), str) or not row['key'].startswith(prefix)
                or not isinstance(value, Mapping) or value.get('user_id') != owner
                or not isinstance(value.get('projectPath'), str)):
            raise StorageError('database_integrity', 'Coordination binding page escaped its owner')
        project = value['projectPath']
        if is_router_home(owner, project) or is_watchdog_location(owner, project):
            continue
        projects.append(_router(owner, project, bindings.read(project), signals, client))
    snapshot['projects'] = projects
    snapshot['next_cursor'] = rows[PAGE_ROWS - 1]['key'][len(prefix):] if len(rows) > PAGE_ROWS else None
    snapshot['channels'], snapshot['outbox'] = _channels(owner, client)
    snapshot['open_signals'] = signals
    snapshot['open_signals_next_cursor'] = signal_page['next_cursor']
    return snapshot


def needs_review(snapshot: dict) -> dict:
    """Counts that make a periodic review worth a model turn; all zero means skip."""
    routers = ([snapshot['global_router']] if snapshot.get('global_router') else []) + snapshot['projects']
    workers = [worker for router in routers for worker in router.get('workers', [])]
    from lib.project_dispatch.continuation import TERMINAL
    return {
        'active_workers': sum(worker['status'] not in TERMINAL for worker in workers),
        'failed_workers': sum(worker['status'] in TERMINAL and 'delivery' not in worker for worker in workers),
        'held_deliveries': sum('delivery' in worker for worker in workers),
        'open_signals': len(snapshot['open_signals']) + bool(snapshot['open_signals_next_cursor']),
        'outbox_attention': len(snapshot['outbox']),
        'unhealthy_channels': sum(channel['enabled'] and channel['state'] in UNHEALTHY_CHANNEL_STATES
                                  for channel in snapshot['channels']),
        'unreadable_routers': sum('activity_error' in router for router in routers),
    }


__all__ = ['CHANNEL_SUPERVISORS', 'health_snapshot', 'needs_review']
