"""Passive bounded assignment history over the selected semantic authority.

Ordering follows immutable admission sequence, not task completion time. Cursors
retain the owned storage witness and work through legacy cutover because copied
assignments keep their original array order. This module creates no index or task.
A page also returns the loaded dispatch value its rows came from (``snapshot``);
it holds every row's reachable prerequisites, so one build evaluates dependency
states without reloading. ``RecipientOrigins`` reads catalog provenance from the
same admission order.
"""
from copy import deepcopy
from dataclasses import dataclass
import threading
from types import MappingProxyType
import weakref

from lib.storage.errors import StorageError
from lib.storage.generated_project_dispatch_contract import SPEC
from .repository import CONTRACT, LIMITS, ProjectDispatchError
from .segmented import SegmentedDispatch, Snapshot, key, selection

PAGE_ROWS = CONTRACT['$defs']['historyPage']['properties']['assignments']['maxItems']
ID_CHARACTERS = CONTRACT['$defs']['id']['maxLength']


def page(repository, cursor=None):
    prefix = repository.key + ':indexes:assignments:order:'
    direction, after = 'older', ''
    if cursor is not None and not isinstance(cursor, str):
        raise ProjectDispatchError('Invalid history cursor')
    if cursor:
        direction, separator, after = cursor.partition('/')
        number = after.removeprefix(prefix)
        if (separator != '/' or direction not in ('older', 'newer') or not after.startswith(prefix)
                or len(number) != 20 or not number.isascii() or not number.isdigit() or int(number) < 1):
            raise ProjectDispatchError('History cursor differs from this owned project')
    reverse = direction == 'older'
    store = SegmentedDispatch(repository)
    for _ in range(LIMITS['casAttempts']):
        head = store.head() if repository.storage_version == 2 else None
        if head is None or head['value']['state']['phase'] == 'copying':
            if repository.storage_version == 2:
                # Read the bounded frozen source directly. A concurrent cutover
                # must never fall through into a full segmented history scan or
                # synthesize array positions for newly allocated sequence gaps.
                value, _, _ = store._legacy()
                if value is None:
                    latest = store.head()
                    if latest is None or latest['value']['state']['phase'] == 'copying':
                        raise StorageError('database_integrity', 'History cutover lost its source')
                    continue
                repository._check_record(value)
            else:
                value, _ = repository._load()
            indexed = [{'key': prefix + f'{index + 1:020d}', 'row': row}
                       for index, row in enumerate(value['assignments'])]
            indexed = [item for item in indexed if not after or
                       (item['key'] < after if reverse else item['key'] > after)]
            indexed.sort(key=lambda item: item['key'], reverse=reverse)
            rows = indexed[:PAGE_ROWS + 1]
        else:
            rows = store.query('list', collection='indexes', prefix='assignments:order:',
                               reverse=reverse, after_key=after, limit=PAGE_ROWS + 1)
            selected = [item['value']['id'] for item in rows[:PAGE_ROWS]]
            value, snapshot = repository._load(selection(assignments=selected, cohort=False, include_policy=False))
            if not isinstance(snapshot, Snapshot) or snapshot.revision != head['version']:
                continue
            documents = {row['id']: row for row in value['assignments']}
            for item in rows[:PAGE_ROWS]:
                ident = item['value']['id']
                stored = snapshot.documents.get(('assignments', ident))
                if not stored or stored['value']['sequence'] != item['value']['sequence']:
                    raise StorageError('database_integrity', 'History index lost assignment evidence')
                item['row'] = documents[ident]
        has_more = len(rows) > PAGE_ROWS
        rows = rows[:PAGE_ROWS]
        if not reverse:
            rows.reverse()
        older_available = has_more if reverse else bool(after)
        newer_available = bool(after) if reverse else has_more
        if not rows:
            # Preserve a route back after following a cursor while history was
            # concurrently imported, or when an owned page has no visible rows.
            return {'rows': [], 'olderCursor': None, 'rowOlderCursors': [],
                    'newerCursor': 'newer/' + after if after and reverse else None, 'snapshot': value}
        # rowOlderCursors[i] continues with the rows older than rows[i], so a
        # reader that shows only part of a page can resume exactly after it.
        return {'rows': [deepcopy(item['row']) for item in rows],
                'olderCursor': 'older/' + rows[-1]['key'] if older_available else None,
                'rowOlderCursors': ['older/' + item['key'] for item in rows],
                'newerCursor': 'newer/' + rows[0]['key'] if newer_available else None,
                'snapshot': value}
    raise ProjectDispatchError('Concurrent history changes; retry the same page')


def _provenance(request):
    """``(recipient, coordinator)`` of a delegated request; None for a Coordinator's own."""
    profile = request.get('profile', {}) if isinstance(request, dict) else None
    if not isinstance(profile, dict):
        raise StorageError('database_integrity', 'Invalid dispatch provenance')
    if profile.get('mode') == 'coordinator':
        return None
    pair = request.get('recipientId'), request.get('coordinatorId')
    if not all(isinstance(value, str) and 0 < len(value) <= ID_CHARACTERS for value in pair):
        raise StorageError('database_integrity', 'Invalid dispatch provenance')
    return pair


@dataclass(frozen=True)
class _Consumed:
    client: weakref.ref
    after: str
    count: int
    origins: MappingProxyType


class RecipientOrigins:
    """Recipient → original Coordinator, reading each admission once.

    A Worker's origin is the Coordinator of its oldest delegated admission.
    Requests are immutable after admission and an order index commits with its
    document, so consumed admissions never change and a read needs no snapshot
    fence: a concurrent writer cannot make it retry or fail. The head's
    assignment count is the freshness witness. Equal to the consumed count, a
    read is that one head query; larger, it reads only the newer admissions;
    smaller (a replaced history), or under another storage client, it starts
    over. A legacy-phase record is bounded by its own contract and read whole.
    Retention is one mapping per owned project read, holding each delegated
    recipient once, replaced when a later read starts over.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._consumed = {}

    def read(self, repository):
        scope = (repository.owner, repository.project)
        client = repository.client()
        with self._lock:
            stored = self._consumed.get(scope)
        store = SegmentedDispatch(repository)
        head = store.head() if repository.storage_version == 2 else None
        if head is None or head['value']['state']['phase'] == 'copying':
            value = store._legacy()[0] if repository.storage_version == 2 else repository._load()[0]
            if value is not None:
                origins = {}
                for row in value['assignments']:
                    pair = _provenance(row.get('request'))
                    if pair:
                        origins.setdefault(*pair)
                return MappingProxyType(origins)
            # Only a completed cutover reclaims the frozen source.
            head = store.head()
            if head is None or head['value']['state']['phase'] == 'copying':
                raise StorageError('database_integrity', 'History cutover lost its source')
        expected = head['value']['state']['counts']['assignments']
        consumed = stored
        if consumed is None or consumed.client() is not client or consumed.count > expected:
            consumed = _Consumed(weakref.ref(client), '', 0, MappingProxyType({}))
        after, count, added = consumed.after, consumed.count, {}
        while count < expected:
            rows = store.query('list', collection='indexes', prefix='assignments:order:',
                               after_key=after, limit=SPEC['limits']['pageRows'])
            if not isinstance(rows, list) or len(rows) > SPEC['limits']['pageRows']:
                raise StorageError('database_integrity', 'Invalid dispatch page')
            if not rows:
                raise StorageError('database_integrity', 'Dispatch history index is incomplete')
            for row in rows:
                index = row.get('value') if isinstance(row, dict) else None
                if (not isinstance(index, dict) or not isinstance(row.get('key'), str)
                        or row['key'] <= after or not isinstance(index.get('id'), str)
                        or type(index.get('sequence')) is not int or index['sequence'] < 1):
                    raise StorageError('database_integrity', 'Invalid dispatch page cursor')
                found = store.query('get', collection='assignments', id=key(index['id']))
                envelope = found.get('value') if isinstance(found, dict) else None
                record = envelope.get('record') if isinstance(envelope, dict) else None
                if (not isinstance(record, dict) or record.get('id') != index['id']
                        or envelope.get('sequence') != index['sequence']):
                    raise StorageError('database_integrity', 'History index lost assignment evidence')
                pair = _provenance(record.get('request'))
                if pair and pair[0] not in consumed.origins:
                    added.setdefault(*pair)
                after, count = row['key'], count + 1
        if consumed is stored and count == consumed.count:
            return consumed.origins
        fresh = _Consumed(weakref.ref(client), after, count,
                          MappingProxyType({**consumed.origins, **added}))
        with self._lock:
            current = self._consumed.get(scope)
            if current is stored or current.client() is not client or current.count < count:
                self._consumed[scope] = fresh
        return fresh.origins


# Shared across requests only where a composition root passes it explicitly.
PROCESS_RECIPIENT_ORIGINS = RecipientOrigins()
