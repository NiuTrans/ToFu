"""Passive bounded assignment history over the selected semantic authority.

Ordering follows immutable admission sequence, not task completion time. Cursors
retain the owned storage witness and work through legacy cutover because copied
assignments keep their original array order. This module creates no index or task.
"""
from copy import deepcopy

from lib.storage.errors import StorageError
from .repository import CONTRACT, LIMITS, ProjectDispatchError
from .segmented import SegmentedDispatch, Snapshot, selection

PAGE_ROWS = CONTRACT['$defs']['historyPage']['properties']['assignments']['maxItems']


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
            return {'rows': [], 'olderCursor': None,
                    'newerCursor': 'newer/' + after if after and reverse else None}
        return {'rows': [deepcopy(item['row']) for item in rows],
                'olderCursor': 'older/' + rows[-1]['key'] if older_available else None,
                'newerCursor': 'newer/' + rows[0]['key'] if newer_available else None}
    raise ProjectDispatchError('Concurrent history changes; retry the same page')
