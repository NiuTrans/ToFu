"""Watchdog fault signals: durable, idempotent records that wake the Watchdog.

Responsibility: existing owners report faults they already observe — failed
Turns of router-managed conversations (infrastructure error kinds only),
dispatch recovery failures, outbox sends that failed or became unknown,
unhealthy channel accounts, and registered compute jobs whose status a parked
Evolution run can no longer read or verify. Each report becomes one ``watchdogSignal`` record
(contracts/router_v1.schema.json) keyed by a deterministic identity in portable
record CAS; recording an existing identity again is a no-op, so replays never
duplicate a signal or a wake. A new signal wakes the owner's Watchdog through
``dispatch_scheduled_turn`` with the nonhuman ``peer`` initiator and a command
id derived from the signal, so redelivery is idempotent. Open signals keep an
index row for paging; resolving marks the record resolved and removes the row.
The signal is the authority and the wake only a hint: a lost wake surfaces at
the next review. Owners without an available Watchdog record nothing (reporting
never creates a Watchdog), a signal about the Watchdog's own Turn never wakes
it, and reporting never raises into the reporting owner.

Entry points: FaultSignals (record/get/resolve/open_page/open_turn_failures),
report_turn_failure, report_recovery_failure, report_outbox_fault,
report_channel_status, report_compute_stall, resolve_compute_stall,
wake_watchdog, render_signal_notice.
Dependencies: router.contract, router.watchdog.binding, router.home,
turn_initiation; conversations and scheduler dispatch are imported lazily.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import json
import time
import uuid

from lib.identity import require_user_id
from lib.log import get_logger
from lib.storage.errors import StorageError

from ..contract import CONTRACT, NAMESPACES, validate

logger = get_logger(__name__)

SIGNALS = NAMESPACES['watchdogSignals']
OPEN_SIGNALS = NAMESPACES['watchdogOpenSignals']
TURN_FAULT_KINDS = frozenset(CONTRACT['$defs']['watchdogTurnErrorKind']['enum'])
UNHEALTHY_CHANNEL_STATES = frozenset(CONTRACT['$defs']['watchdogUnhealthyChannelState']['enum'])
_ERROR_FIELDS = CONTRACT['$defs']['watchdogSignal']['properties']['error']['properties']


def _now_ms() -> int:
    return int(time.time() * 1000)


def _conflict(error: BaseException) -> bool:
    return isinstance(error, StorageError) and error.code == 'database_conflict'


def _bounded(field: str, text) -> str:
    return str(text or '')[:_ERROR_FIELDS[field]['maxLength']]


class FaultSignals:
    """One owner's signal records and their open index."""

    def __init__(self, owner: int, *, client=None, clock=None):
        self.owner = require_user_id(owner, context='watchdog signals')
        if client is None:
            from lib.storage import get_storage_client
            client = get_storage_client
        self.client = client
        self.clock = clock or _now_ms

    def signal_id(self, source: str, identity) -> str:
        return hashlib.sha256(json.dumps([self.owner, source, identity], separators=(',', ':'),
                                         ensure_ascii=False).encode()).hexdigest()[:32]

    def _key(self, signal_id: str) -> str:
        return f'{self.owner}:{signal_id}'

    def _read(self, namespace: str, key: str):
        row = self.client().query('record.get', {'namespace': namespace, 'key': key})
        if row is None:
            return None, 0
        value = row.get('value') if isinstance(row, Mapping) else None
        if not isinstance(value, Mapping):
            raise StorageError('database_integrity', 'Invalid watchdog signal record')
        return dict(value), int(row['version'])

    def _put(self, namespace: str, key: str, value: dict, expected_version: int) -> None:
        self.client(write=True).command('record.put', {
            'namespace': namespace, 'key': key, 'value': value, 'expected_version': expected_version,
        }, 'router-watchdog:' + uuid.uuid4().hex)

    def get(self, signal_id: str) -> dict | None:
        value, _ = self._read(SIGNALS, self._key(signal_id))
        if value is not None and (value.get('owner') != self.owner or value.get('signalId') != signal_id):
            raise StorageError('database_integrity', 'Watchdog signal escaped its owner')
        return value

    def record(self, source: str, identity, subject: dict, error: dict) -> tuple[dict, bool]:
        """Create the signal once; returns ``(signal, created)``."""
        signal_id = self.signal_id(source, identity)
        signal, created = self.get(signal_id), False
        if signal is None:
            proposed = validate({
                'version': 1, 'owner': self.owner, 'signalId': signal_id, 'source': source,
                'state': 'open', 'subject': subject, 'error': error, 'createdAt': self.clock(),
                'resolvedAt': None, 'resolution': None}, 'watchdogSignal')
            try:
                self._put(SIGNALS, self._key(signal_id), proposed, 0)
                signal, created = proposed, True
            except StorageError as failure:
                if not _conflict(failure):
                    raise
                signal = self.get(signal_id)
                if signal is None:
                    raise
        if signal['state'] == 'open':
            # Also repairs an index row lost between the two writes of a create.
            self._index(signal)
        return signal, created

    def _index(self, signal: dict) -> None:
        key = self._key(signal['signalId'])
        if self._read(OPEN_SIGNALS, key)[0] is not None:
            return
        # The row carries the routing facts so open_turn_failures needs no per-row read.
        row = {'owner': self.owner, 'signalId': signal['signalId'], 'source': signal['source'],
               'conversationId': signal['subject'].get('conversationId', '')}
        try:
            self._put(OPEN_SIGNALS, key, row, 0)
        except StorageError as failure:
            if not _conflict(failure):
                raise

    def resolve(self, signal_ids: list[str], resolution: str) -> list[dict]:
        resolved = []
        for signal_id in signal_ids:
            key = self._key(signal_id)
            while True:
                value, version = self._read(SIGNALS, key)
                if value is None or value.get('owner') != self.owner:
                    raise ValueError(f'Unknown Watchdog signal {signal_id}')
                if value['state'] == 'resolved':
                    break
                updated = validate({**value, 'state': 'resolved', 'resolvedAt': self.clock(),
                                    'resolution': resolution}, 'watchdogSignal')
                try:
                    self._put(SIGNALS, key, updated, version)
                    value = updated
                    break
                except StorageError as failure:
                    if not _conflict(failure):
                        raise
            index, version = self._read(OPEN_SIGNALS, key)
            if index is not None:
                try:
                    self.client(write=True).command('record.delete', {
                        'namespace': OPEN_SIGNALS, 'key': key, 'expected_version': version,
                    }, 'router-watchdog:' + uuid.uuid4().hex)
                except StorageError as failure:
                    if not _conflict(failure):
                        raise
            resolved.append(value)
        return resolved

    def _index_page(self, after_id: str | None) -> tuple[list[dict], str | None]:
        """One page of open index rows, oldest key first; bounded like history pages."""
        from lib.project_dispatch.history import PAGE_ROWS
        prefix = f'{self.owner}:'
        rows = self.client().query('record.list', {
            'namespace': OPEN_SIGNALS, 'prefix': prefix,
            'after_key': prefix + after_id if after_id else '', 'limit': PAGE_ROWS + 1})
        if not isinstance(rows, list) or len(rows) > PAGE_ROWS + 1:
            raise StorageError('database_integrity', 'Invalid watchdog signal page')
        entries = []
        for row in rows[:PAGE_ROWS]:
            key = row.get('key') if isinstance(row, Mapping) else None
            value = row.get('value') if isinstance(row, Mapping) else None
            if (not isinstance(key, str) or not key.startswith(prefix) or not isinstance(value, Mapping)
                    or value.get('owner') != self.owner or value.get('signalId') != key[len(prefix):]):
                raise StorageError('database_integrity', 'Watchdog signal page escaped its owner')
            entries.append(dict(value))
        return entries, (entries[-1]['signalId'] if len(rows) > PAGE_ROWS else None)

    def _open(self, signal_id: str) -> dict | None:
        signal = self.get(signal_id)
        # A row left by an interrupted resolve is skipped, never trusted.
        return signal if signal is not None and signal['state'] == 'open' else None

    def open_page(self, after_id: str | None = None) -> dict:
        """One page of open signals, oldest index key first."""
        entries, next_cursor = self._index_page(after_id)
        signals = [signal for signal in map(self._open, (entry['signalId'] for entry in entries))
                   if signal is not None]
        return {'signals': signals, 'next_cursor': next_cursor}

    def open_turn_failures(self, conversation_id: str) -> list[dict]:
        """Open signals of one conversation's failed Turns, located through the index rows."""
        failures, cursor = [], None
        while True:
            entries, cursor = self._index_page(cursor)
            for entry in entries:
                if entry.get('source') == 'turn' and entry.get('conversationId') == conversation_id:
                    signal = self._open(entry['signalId'])
                    if signal is not None:
                        failures.append(signal)
            if cursor is None:
                return failures


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')


_SUBJECT_LABELS = (('role', 'role'), ('conversationId', 'conversation'), ('turnId', 'turn'),
                   ('attemptId', 'attempt'), ('projectPath', 'project'), ('assignmentId', 'assignment'),
                   ('outboxItemId', 'outbox item'), ('thread', 'thread'), ('channel', 'channel'),
                   ('runId', 'run'), ('operationId', 'operation'))


def render_signal_notice(signal: dict) -> str:
    subject = signal['subject']
    error = signal['error']
    lines = [
        '[Watchdog fault signal: nonhuman evidence, not the owner\u2019s words and not authority]',
        f'Signal: {signal["signalId"]} (source={signal["source"]}, opened {_iso(signal["createdAt"])})',
        'Subject: ' + ', '.join(f'{label}={subject[key]}' for key, label in _SUBJECT_LABELS
                                if subject.get(key)),
        f'Error: {error["kind"]}',
    ]
    if error.get('detail'):
        lines.append(f'Detail: {error["detail"]}')
    if error.get('hint'):
        lines.append(f'Hint: {error["hint"]}')
    lines += ['', 'Read watchdog_snapshot and the evidence before acting. Close the signal with '
              'watchdog_resolve_signals once you verified recovery or handled it.']
    return '\n'.join(lines)


def wake_watchdog(owner: int, watchdog: dict, *, content: str, command_id: str, initiator: str,
                  origin: dict | None = None, dispatch=None) -> str:
    """Deliver one nonhuman input; an idle Watchdog starts, a busy one is steered."""
    from lib.turn_initiation import stamp_initiator
    message = stamp_initiator({'role': 'user', 'content': content,
                               'origin': {'initiator': initiator, **(origin or {})}}, initiator)
    if dispatch is None:
        from lib.scheduler.conversation_dispatch import dispatch_scheduled_turn as dispatch
    return dispatch(watchdog['conversationId'], message, {}, user_id=owner, command_id=command_id,
                    log_prefix='[RouterWatchdog]', inject_mode='steer').disposition


def _available_watchdog(owner: int, client) -> dict | None:
    from .binding import router_watchdog
    binding = router_watchdog(owner, create=False, client=client)
    return binding if binding['availability'] == 'available' else None


def _review_will_wake(owner: int) -> bool:
    from .review import review_state
    return review_state(owner)['enabled']


def _record_and_wake(owner: int, watchdog: dict, *, source: str, identity, subject: dict, error: dict,
                     client, dispatch) -> dict:
    signals = FaultSignals(owner, client=client)
    signal, created = signals.record(source, identity, subject, error)
    # A Watchdog whose own Turn failed is not restarted by every new fault while
    # its periodic review will wake it anyway; reviews and re-checks always wake
    # it, and its first successful snapshot closes its own failure (tools.py).
    # Without an enabled review nothing else would, so each new fault does.
    if (created and subject.get('conversationId') != watchdog['conversationId']
            and not (signals.open_turn_failures(watchdog['conversationId']) and _review_will_wake(owner))):
        from lib.turn_initiation import INITIATOR_PEER
        origin = ({'sourceConversationId': subject['conversationId']}
                  if subject.get('conversationId') else {})
        disposition = wake_watchdog(owner, watchdog, content=render_signal_notice(signal),
                                    command_id='router-watchdog-signal:' + signal['signalId'],
                                    initiator=INITIATOR_PEER, origin=origin, dispatch=dispatch)
        if disposition not in ('started', 'steered', 'queued'):
            logger.warning('[RouterWatchdog] signal %s recorded; wake not accepted (%s)',
                           signal['signalId'], disposition)
    return signal


def _report(owner, source: str, build, *, client=None, dispatch=None) -> dict | None:
    """Shared best-effort boundary: the reporting owner never fails because of us."""
    try:
        owner = require_user_id(owner, context='watchdog fault report')
        watchdog = _available_watchdog(owner, client)
        if watchdog is None:
            return None
        built = build(owner, watchdog)
        if built is None:
            return None
        identity, subject, error = built
        return _record_and_wake(owner, watchdog, source=source, identity=identity, subject=subject,
                                error=error, client=client, dispatch=dispatch)
    except Exception:
        logger.warning('[RouterWatchdog] %s fault report failed owner=%s', source, owner, exc_info=True)
        return None


def _conversation_role(owner: int, conversation_id: str, watchdog: dict, client) -> tuple[str, str] | None:
    """Router-managed role of a conversation from durable metadata, or None."""
    if conversation_id == watchdog['conversationId']:
        return 'watchdog', watchdog['projectPath']
    from lib.conversations.project_coordination import ProjectCoordinationRepository
    from lib.conversations.project_identity import normalize_project_path
    from lib.conversations.repository import ConversationRepository
    from lib.router.home import is_router_home
    snapshot = ConversationRepository(client).get(conversation_id, user_id=owner, include_messages=False)
    settings = snapshot.metadata.get('settings') if snapshot is not None else None
    if not isinstance(settings, Mapping):
        return None
    project = normalize_project_path(settings.get('projectPath') or '')
    if settings.get('_coordinatorSource'):
        return 'worker', project
    if not project:
        return None
    bindings = (ProjectCoordinationRepository(owner) if client is None
                else ProjectCoordinationRepository(owner, client=client))
    if bindings.read(project)['conversationId'] != conversation_id:
        return None
    return ('global_router' if is_router_home(owner, project) else 'project_router'), project


def report_turn_failure(owner: int, *, conversation_id: str, turn_id: str, attempt_id: str,
                        error: Mapping | None, client=None, dispatch=None) -> dict | None:
    """A failed Turn of a router-managed conversation with an infrastructure error kind."""
    kind = error.get('kind') if isinstance(error, Mapping) else None
    if kind not in TURN_FAULT_KINDS or not conversation_id or not turn_id or not attempt_id:
        return None

    def build(owner, watchdog):
        role = _conversation_role(owner, conversation_id, watchdog, client)
        if role is None:
            return None
        subject = {'role': role[0], 'conversationId': conversation_id, 'turnId': turn_id,
                   'attemptId': attempt_id, **({'projectPath': role[1]} if role[1] else {})}
        detail = {'kind': kind, 'detail': _bounded('detail', error.get('detail'))}
        if error.get('hint'):
            detail['hint'] = _bounded('hint', error['hint'])
        return [conversation_id, turn_id, attempt_id], subject, detail
    return _report(owner, 'turn', build, client=client, dispatch=dispatch)


def report_recovery_failure(owner: int, project: str, error: BaseException, *,
                            assignment: Mapping | None = None, episode=None,
                            client=None, dispatch=None) -> dict | None:
    """Dispatch recovery could not retry a grant (or a whole project at startup).

    Authority refusals of one assignment (revoked, rebound, stale or invalid
    grants: PermissionError/ValueError) are the recovery pass's ordinary skips,
    not faults; storage and unexpected failures are. One fault is one signal
    per grant revision and error class (``episode`` distinguishes startup
    passes), never one per retry, so messages with volatile text cannot storm.
    """
    if assignment is not None and isinstance(error, (PermissionError, ValueError)):
        return None

    def build(owner, watchdog):
        subject = {'role': 'dispatch', 'projectPath': project}
        identity = [project, '', type(error).__name__, str(getattr(error, 'code', '') or ''), episode]
        if assignment is not None:
            subject['assignmentId'] = str(assignment['id'])
            recipient = (assignment.get('request') or {}).get('recipientId')
            if recipient:
                subject['conversationId'] = recipient
            identity[1] = [str(assignment['id']), (assignment.get('grant') or {}).get('revision')]
        return identity, subject, {
            'kind': 'recovery_failed', 'detail': _bounded('detail', f'{type(error).__name__}: {error}')}
    return _report(owner, 'recovery', build, client=client, dispatch=dispatch)


def report_outbox_fault(owner: int, item, *, client=None, dispatch=None) -> dict | None:
    """An outbox send settled ``failed`` (provably not sent) or ``unknown``."""
    if item.state not in ('failed', 'unknown'):
        return None

    def build(owner, watchdog):
        # Unknown sends are reconciled and retried; one item is one signal per state.
        return [item.id, item.state], {
            'role': 'outbox', 'outboxItemId': item.id, 'thread': item.thread,
            'channel': item.thread.split(':', 1)[0]}, {
            'kind': 'outbox_' + item.state, 'detail': _bounded('detail', item.error)}
    return _report(owner, 'outbox', build, client=client, dispatch=dispatch)


def report_channel_status(owner: int, channel: str, account: Mapping | None, *,
                          client=None, dispatch=None) -> dict | None:
    """One unhealthy state since the last successful connection (``connectedAtMs``).

    The supervisor restarts a failing bridge on every tick, so the state's own
    start time and its detail text (exit codes, bridge messages) change each
    cycle; the last connection time does not until the channel recovers, which
    makes a persistent failure exactly one signal per state.
    """
    status = (account or {}).get('status') or {}
    state = status.get('state') if isinstance(status, Mapping) else None
    if state not in UNHEALTHY_CHANNEL_STATES or not (account or {}).get('enabled'):
        return None

    def build(owner, watchdog):
        return [channel, state, status.get('connectedAtMs')], {
            'role': 'channel', 'channel': channel}, {
            'kind': 'channel_' + state, 'detail': _bounded('detail', status.get('detail'))}
    return _report(owner, 'channel', build, client=client, dispatch=dispatch)


_COMPUTE_HINT = ('The registered job is never resubmitted by this wait. The run stays parked and re-reads '
                 'the status on every reaper pass; the first successful read closes this signal and resumes '
                 'the executor with the job\u2019s terminal state.')


def report_compute_stall(owner: int, *, run_id: str, operation_id: str, project_path: str, failure: str,
                         since: float, kind: str, detail: str, client=None, dispatch=None) -> dict | None:
    """A parked Evolution run cannot read (or verify) its registered compute job.

    The run reaper re-reads on every pass, so one failure class (``failure``:
    the exception type or verification code) within one failure streak
    (``since``: its first failed read) of one dependency is one signal; volatile
    messages cannot storm, and a later streak is a new signal. Only the
    reporting owner observes recovery, so it closes the signal itself through
    ``resolve_compute_stall``.
    """
    if not run_id or not operation_id or not failure:
        return None

    def build(owner, watchdog):
        subject = {'role': 'compute', 'runId': run_id, 'operationId': operation_id,
                   **({'projectPath': project_path} if project_path else {})}
        return [run_id, operation_id, failure, since], subject, {
            'kind': kind, 'detail': _bounded('detail', detail), 'hint': _bounded('hint', _COMPUTE_HINT)}
    return _report(owner, 'compute', build, client=client, dispatch=dispatch)


def resolve_compute_stall(owner: int, signal_id: str, resolution: str, *, client=None) -> bool:
    """The reporting owner closes its compute signal; never raises into it."""
    try:
        FaultSignals(owner, client=client).resolve([signal_id], resolution)
        return True
    except Exception:
        logger.warning('[RouterWatchdog] compute signal %s resolve failed owner=%s', signal_id, owner,
                       exc_info=True)
        return False


__all__ = ['FaultSignals', 'TURN_FAULT_KINDS', 'UNHEALTHY_CHANNEL_STATES', 'render_signal_notice',
           'report_channel_status', 'report_compute_stall', 'report_outbox_fault',
           'report_recovery_failure', 'report_turn_failure', 'resolve_compute_stall', 'wake_watchdog']
