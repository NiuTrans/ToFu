"""Owned segmented dispatch snapshots, delta commits and resumable v1 cutover.

Domain admission stays in ProjectDispatchRepository. Queries are passive. A
mutation first freezes v1 by its existing CAS, then copies bounded batches under
the v2 head CAS. Only a complete copy becomes writable. The frozen transport
copy is reclaimed after all evidence exists in v2; the v1 locator remains as a
small fence against old writers and for existing owner/project discovery.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import uuid

from lib.storage.errors import StorageError
from lib.storage.generated_project_dispatch_contract import SPEC

FORMAT = 'tofu.project-dispatch-cutover/v2'
FIELDS = {'assignments': 'assignments', 'batches': 'batches',
          'coordinatorSettlements': 'settlements', 'requestBudgets': 'requests',
          'scopeChanges': 'scope_changes'}
ID_FIELDS = {'coordinatorSettlements': 'sourceAttemptId', 'scopeChanges': 'changeId'}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def key(identity):
    return hashlib.sha256(identity.encode()).hexdigest()


def identity(field, row):
    return row[ID_FIELDS.get(field, 'id')]


@dataclass
class Snapshot:
    revision: int
    head: dict
    documents: dict


def selection(*, assignments=(), budgets=(), batches=(), sources=(), scopes=(), latest_budget=False, cohort=True, include_policy=True):
    """Explicit bounded point/cohort selection; None means a streamed full read."""
    return {'assignments': list(assignments), 'requestBudgets': list(budgets),
            'batches': list(batches), 'sources': list(sources),
            'scopeChanges': list(scopes), 'latest_budget': latest_budget, 'cohort': cohort,
            'include_policy': include_policy}


class SegmentedDispatch:
    def __init__(self, repository):
        self.repo = repository
        self.scope = {'user_id': repository.owner, 'project_path': repository.project}

    def query(self, operation, **payload):
        return self.repo.client().query('project.dispatch.' + operation, {**self.scope, **payload})

    def head(self):
        row = self.query('get', collection='head', id='root')
        if row is None:
            return None
        value = row.get('value') or {}
        if (value.get('format') != SPEC['format'] or value.get('owner') != self.repo.owner
                or value.get('projectPath') != self.repo.project or not isinstance(value.get('state'), dict)
                or type(row.get('version')) is not int or row['version'] < 1):
            raise StorageError('database_integrity', 'Invalid owned dispatch head')
        state = value['state']
        if (state.get('format') != FORMAT or state.get('phase') not in ('copying', 'copied', 'ready')
                or type(state.get('nextSequence')) is not int or state['nextSequence'] < 0
                or not isinstance(state.get('counts'), dict) or set(state['counts']) != set(FIELDS)
                or any(type(count) is not int or count < 0 for count in state['counts'].values())):
            raise StorageError('database_integrity', 'Invalid dispatch migration state')
        return row

    def pages(self, prefix):
        after = ''
        while True:
            page = self.query('list', collection='indexes', prefix=prefix, after_key=after,
                              limit=SPEC['limits']['pageRows'])
            if not isinstance(page, list) or len(page) > SPEC['limits']['pageRows']:
                raise StorageError('database_integrity', 'Invalid dispatch page')
            if not page:
                return
            for row in page:
                if not isinstance(row.get('key'), str) or row['key'] <= after:
                    raise StorageError('database_integrity', 'Invalid dispatch page cursor')
                after = row['key']
                yield row['value']

    def _legacy(self):
        from .repository import NAMESPACE
        row = self.repo.client().query('record.get', {'namespace': NAMESPACE, 'key': self.repo.key})
        if row is None:
            return {'binding': deepcopy(self.repo.binding), 'assignments': []}, 0, None
        value = row['value']
        if isinstance(value, dict) and value.get('format') == FORMAT:
            if value.get('binding') != self.repo.binding:
                raise StorageError('database_integrity', 'Dispatch cutover owner changed')
            original = value.get('original')
            if original is not None and digest(original) != value.get('digest'):
                raise StorageError('database_integrity', 'Dispatch frozen evidence changed')
            return original, row['version'], value
        self.repo._check_record(value)
        return value, row['version'], None

    def load(self, selected=None):
        head = self.head()
        if head is None or head['value']['state']['phase'] == 'copying':
            value, version, marker = self._legacy()
            if value is None:
                raise StorageError('database_integrity', 'Incomplete dispatch migration lost its source')
            self.repo._check_record(value)
            return deepcopy(value), version
        state = head['value']['state']
        documents = {}
        value = {'binding': deepcopy(self.repo.binding), **{field: [] for field in FIELDS}}

        def fetch(field, ident):
            document_key = (field, ident)
            if document_key in documents:
                return documents[document_key]
            row = self.query('get', collection=FIELDS[field], id=key(ident))
            if row is not None:
                envelope = row.get('value') or {}
                record = envelope.get('record')
                if (not isinstance(record, dict) or identity(field, record) != ident
                        or type(envelope.get('sequence')) is not int or envelope['sequence'] < 1):
                    raise StorageError('database_integrity', 'Dispatch document identity/order changed')
                value[field].append(deepcopy(record))
            documents[document_key] = row
            return row

        def incomplete(message):
            latest = self.head()
            if latest is None or latest['version'] != head['version']:
                raise StorageError('database_conflict', 'Dispatch snapshot advanced', retryable=True)
            raise StorageError('database_integrity', message)

        def indexed(field, prefix, *, budget=None, source=None):
            count = 0
            for item in self.pages(prefix):
                found = fetch(field, item['id'])
                if not found or found['value']['sequence'] != item['sequence']:
                    incomplete('Dispatch index has dangling/changed evidence')
                delegation = found['value']['record'].get('request', {}).get('delegation') or {}
                if (budget is not None and delegation.get('policy', {}).get('requestBudgetId') != budget
                        or source is not None and delegation.get('attemptId') != source):
                    incomplete('Dispatch index belongs to another request')
                count += 1
            return count

        if selected is None:
            for field, collection in FIELDS.items():
                if indexed(field, collection + ':order:') != state['counts'][field]:
                    incomplete('Dispatch history index is incomplete')
        else:
            selected = deepcopy(selected)
            if selected.get('latest_budget') and state.get('latestBudgetId'):
                selected['requestBudgets'].append(state['latestBudgetId'])
            for field in FIELDS:
                for ident in selected.get(field, []):
                    fetch(field, ident)
            if selected.get('cohort', True):
                for ident in selected.get('requestBudgets', []):
                    count = indexed('assignments', 'assignments:budget:' + key(ident) + ':', budget=ident)
                    budget = documents.get(('requestBudgets', ident))
                    expected = budget['value'].get('assignmentCount') if budget else 0
                    if type(expected) is not int or count != expected:
                        incomplete('Request allowance index is incomplete')
            for source in selected.get('sources', []):
                count = indexed('assignments', 'assignments:source:' + key(source) + ':', source=source)
                source_row = self.query('get', collection='source_cohorts', id=key(source))
                expected = source_row['value'].get('assignmentCount') if source_row else 0
                if (type(expected) is not int or count != expected
                        or source_row and source_row['value'].get('sourceAttemptId') != source):
                    incomplete('Source allowance index is incomplete')
                fetch('coordinatorSettlements', source)
            for batch in value['batches']:
                for ident in batch['assignmentIds']:
                    fetch('assignments', ident)

        # Read only reachable historical dependencies, not every old grant.
        pending = list(value['assignments'])
        for row in pending:
            refs = list(row['request'].get('dependencies', []))
            if row['request'].get('reworkOf'):
                refs.append(row['request']['reworkOf'])
            for ref in refs:
                ident = ref['assignmentId']
                if ('assignments', ident) not in documents:
                    found = fetch('assignments', ident)
                    if found:
                        pending.append(found['value']['record'])
            delegation = row['request'].get('delegation') or {}
            if delegation and (selected is None or selected.get('include_policy', True)):
                fetch('coordinatorSettlements', delegation['attemptId'])
                budget = delegation['policy'].get('requestBudgetId')
                if budget:
                    fetch('requestBudgets', budget)
        for field in FIELDS:
            value[field].sort(key=lambda row: documents[field, identity(field, row)]['value']['sequence'])
        # An intervening writer invalidates a multi-query snapshot. Repositories
        # retry from a fresh head; queries never write or change authority.
        current = self.head()
        if current is None or current['version'] != head['version']:
            raise StorageError('database_conflict', 'Dispatch snapshot advanced', retryable=True)
        self.repo._check_record(value, segmented=True)
        return value, Snapshot(head['version'], deepcopy(state), documents)

    def _changes(self, field, row, sequence, *, version=0, new=True, metadata=None):
        ident = identity(field, row)
        collection = FIELDS[field]
        result = [{'collection': collection, 'id': key(ident), 'expected_version': version,
                   'value': {**(metadata or {}), 'sequence': sequence, 'record': deepcopy(row)}}]
        if new:
            prefixes = [collection + ':order:']
            if field == 'assignments':
                delegation = row['request'].get('delegation') or {}
                if delegation:
                    prefixes.append('assignments:source:' + key(delegation['attemptId']) + ':')
                    budget = delegation['policy'].get('requestBudgetId')
                    if budget:
                        prefixes.append('assignments:budget:' + key(budget) + ':')
            result.extend({'collection': 'indexes', 'id': prefix + f'{sequence:020d}',
                'expected_version': 0, 'value': {'id': ident, 'sequence': sequence}} for prefix in prefixes)
        return result

    def command(self, revision, head, changes, command_id=None):
        return self.repo.client(write=True).command('project.dispatch.commit', {**self.scope,
            'expected_revision': revision, 'head': head, 'changes': changes},
            command_id or 'project-dispatch-v2:' + uuid.uuid4().hex)

    def commit(self, original, value, snapshot):
        state = deepcopy(snapshot.head)
        changes = []
        budget_additions, source_additions = {}, {}
        for field in FIELDS:
            old = {identity(field, row): row for row in original.get(field, [])}
            updated = {identity(field, row): row for row in value.get(field, [])}
            if old.keys() - updated.keys():
                raise StorageError('database_integrity', 'Dispatch evidence cannot be removed')
            for ident, row in updated.items():
                if old.get(ident) == row:
                    continue
                stored = snapshot.documents.get((field, ident))
                if stored:
                    sequence, version = stored['value']['sequence'], stored['version']
                else:
                    state['nextSequence'] += 1
                    state['counts'][field] += 1
                    sequence, version = state['nextSequence'], 0
                    if field == 'requestBudgets':
                        state['latestBudgetId'] = ident
                    if field == 'assignments' and (delegation := row['request'].get('delegation')):
                        source = delegation['attemptId']
                        source_additions[source] = source_additions.get(source, 0) + 1
                        budget = delegation['policy'].get('requestBudgetId')
                        if budget:
                            budget_additions[budget] = budget_additions.get(budget, 0) + 1
                metadata = stored['value'] if stored else {'assignmentCount': 0} if field == 'requestBudgets' else {}
                changes.extend(self._changes(field, row, sequence, version=version, new=stored is None, metadata=metadata))
        for budget, count in budget_additions.items():
            old = snapshot.documents.get(('requestBudgets', budget))
            if old is None:
                raise StorageError('database_integrity', 'Delegation has no owned budget document')
            existing = next((item for item in changes if item['collection'] == 'requests' and item['id'] == key(budget)), None)
            if existing is None:
                existing = {'collection': 'requests', 'id': key(budget), 'expected_version': old['version'], 'value': deepcopy(old['value'])}
                changes.append(existing)
            existing['value']['assignmentCount'] = old['value']['assignmentCount'] + count
        for source, count in source_additions.items():
            old = self.query('get', collection='source_cohorts', id=key(source))
            changes.append({'collection': 'source_cohorts', 'id': key(source),
                'expected_version': old['version'] if old else 0,
                'value': {'sourceAttemptId': source, 'assignmentCount': (old['value']['assignmentCount'] if old else 0) + count}})
        self.command(snapshot.revision, state, changes)

    def ensure_ready(self):
        """One explicit mutation resumes each atomic migration prefix on demand."""
        from .repository import LIMITS, NAMESPACE, ProjectDispatchError
        failures = 0
        frozen = None
        while failures < LIMITS['casAttempts']:
            try:
                current = self.head()
                if current and current['value']['state']['phase'] == 'ready':
                    return
                if frozen is None:
                    original, version, marker = self._legacy()
                    if marker is not None:
                        if original is not None:
                            self.repo._check_record(original)
                        frozen = original, version, marker
                else:
                    original, version, marker = frozen
                if marker is None:
                    marker = {'format': FORMAT, 'binding': deepcopy(self.repo.binding),
                              'sourceVersion': version, 'digest': digest(original), 'original': original}
                    self.repo.client(write=True).command('record.put', {'namespace': NAMESPACE,
                        'key': self.repo.key, 'expected_version': version, 'value': marker},
                        'project-dispatch-freeze:' + uuid.uuid4().hex)
                    continue
                if current and current['value']['state']['legacyDigest'] != marker['digest']:
                    raise StorageError('database_integrity', 'Dispatch migration source changed')
                state = (deepcopy(current['value']['state']) if current else {
                    'format': FORMAT, 'phase': 'copying', 'legacyDigest': marker['digest'],
                    'nextSequence': 0, 'copiedRows': 0, 'counts': {field: 0 for field in FIELDS}})
                revision = current['version'] if current else 0
                if state['phase'] == 'copied':
                    # Durable v2 copies exist before the frozen transport copy
                    # is dropped. Restart at either side of this fence is safe.
                    if original is not None:
                        compact = {**marker, 'original': None}
                        self.repo.client(write=True).command('record.put', {'namespace': NAMESPACE,
                            'key': self.repo.key, 'expected_version': version, 'value': compact},
                            'project-dispatch-cutover:' + uuid.uuid4().hex)
                    state['phase'] = 'ready'
                    self.command(revision, state, [])
                    return
                if original is None:
                    raise StorageError('database_integrity', 'Dispatch import lost frozen evidence')
                records = [(field, row) for field in FIELDS for row in original.get(field, [])]
                sources = {}
                for row in original['assignments']:
                    delegation = row['request'].get('delegation') or {}
                    if delegation:
                        source = delegation['attemptId']
                        sources[source] = sources.get(source, 0) + 1
                records.extend(('source_cohorts', {'sourceAttemptId': source, 'assignmentCount': count})
                               for source, count in sources.items())
                cursor = state['copiedRows']
                if type(cursor) is not int or not 0 <= cursor <= len(records):
                    raise StorageError('database_integrity', 'Dispatch import cursor changed')
                changes, byte_count = [], 0
                while cursor < len(records):
                    field, row = records[cursor]
                    if field == 'source_cohorts':
                        proposed = [{'collection': field, 'id': key(row['sourceAttemptId']), 'expected_version': 0, 'value': row}]
                    else:
                        metadata = {}
                        if field == 'requestBudgets':
                            metadata['assignmentCount'] = sum(1 for item in original['assignments']
                                if item['request'].get('delegation', {}).get('policy', {}).get('requestBudgetId') == row['id'])
                        proposed = self._changes(field, row, state['nextSequence'] + 1, metadata=metadata)
                    size = len(json.dumps(proposed, ensure_ascii=False).encode())
                    if changes and (len(changes) + len(proposed) > SPEC['limits']['commitChanges']
                                    or byte_count + size > SPEC['limits']['commitBytes'] - SPEC['limits']['headBytes']):
                        break
                    changes.extend(proposed); byte_count += size
                    state['nextSequence'] += 1; cursor += 1
                    if field in FIELDS:
                        state['counts'][field] += 1
                    if field == 'requestBudgets':
                        state['latestBudgetId'] = row['id']
                state['copiedRows'] = cursor
                if cursor == len(records):
                    state['phase'] = 'copied'
                self.command(revision, state, changes)
            except StorageError as error:
                if error.code != 'database_conflict' and not error.retryable:
                    raise
                frozen = None
                failures += 1
        raise ProjectDispatchError('Concurrent/uncertain dispatch migration; retry the same command')
