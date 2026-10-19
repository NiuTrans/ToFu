"""Common review mechanics, independent of artifact or Turn lifecycle.

ReviewRecords owns owner/tenant CAS and lost-ACK reconciliation. Requirement
and evidence projection are shared; target adapters provide output identity.
No tool execution, provider loop or automatic correctness assertion lives here.
"""
from copy import deepcopy
import hashlib
import json
import time
import uuid

from lib.storage import StorageError
from .contract import contract
from .dependencies import current_facts, project_dependencies, capture_bindings


class ReviewConflict(Exception):
    """Reload changed requirements, outputs or decisions before deciding."""


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        allow_nan=False, separators=(',', ':')).encode()).hexdigest()


class ReviewRecords:
    def __init__(self, principal, namespace, targets, client, *, operation_prefix=None):
        self.principal = principal
        self.owner = principal.require_owner(context='owner review')
        self.namespace = namespace
        self.client = client
        self.operation_prefix = operation_prefix or namespace
        self.keys = {kind: digest([self.owner, principal.tenant_id, kind, target])
                     for kind, target in targets.items()}

    def payload(self, kind):
        return {'namespace': self.namespace, 'key': self.keys[kind], 'user_id': self.owner}

    def read(self, kind):
        row = self.client().query('record.get', self.payload(kind))
        if not row:
            return {}, 0
        value = row['value']
        if value.get('owner') != self.owner or value.get('tenant') != self.principal.tenant_id:
            raise PermissionError('Invalid review owner')
        return deepcopy(value), row['version']

    def put(self, kind, value, revision):
        value.update(owner=self.owner, tenant=self.principal.tenant_id)
        commit = digest([self.keys[kind], revision, value])
        value['_commitDigest'] = commit
        if len(json.dumps(value, ensure_ascii=False).encode()) > contract()['x-limits']['maxRecordBytes']:
            raise ValueError('Review record is full; existing evidence was preserved')
        try:
            result = self.client(write=True).command('record.put', {
                **self.payload(kind), 'value': value, 'expected_version': revision,
            }, self.operation_prefix + ':' + commit)
        except StorageError as exc:
            if exc.code == 'database_conflict':
                raise ReviewConflict('Review changed; reload before saving') from exc
            if exc.code not in {'database_timeout', 'database_busy', 'database_unavailable'}:
                raise
            # Lost ACK is only success when the exact submitted record is visible.
            committed, version = self.read(kind)
            if version != revision + 1 or committed.get('_commitDigest') != commit:
                raise
            return
        if not isinstance(result, dict) or result.get('version') != revision + 1:
            raise ValueError('Review write was not acknowledged')

    def criteria(self):
        value, revision = self.read('requirements')
        return project_criteria(value, revision)


def project_criteria(requirements, revision):
    criteria = requirements.get('criteria', [])
    scope = [requirements.get('objectiveGeneration'),
             sorted((item for item in criteria if item['kind'] == 'constraint'), key=lambda item: item['id'])]
    projected = []
    for item in criteria:
        version = digest([item, scope if item['kind'] == 'requirement'
                          else requirements.get('objectiveGeneration')])
        projected.append({**{key: item[key] for key in ('id', 'text', 'kind')}, 'digest': version})
    return {'requirementsRevision': revision, 'objective': requirements.get('objective', ''),
            'criteria': projected}


def generate_requirements(command, previous):
    ids = [item['id'] for item in command['criteria']]
    if len(ids) != len(set(ids)) or any(not item['text'].strip() for item in command['criteria']):
        raise ValueError('Requirements need unique IDs and nonempty text')
    old_items = {item['id']: item for item in previous.get('criteria', [])}
    criteria = []
    for item in command['criteria']:
        old = old_items.get(item['id'], {})
        unchanged = all(old.get(key) == value for key, value in item.items())
        criteria.append({**item, 'generation': old['generation'] if unchanged else uuid.uuid4().hex})
    objective = command['objective']
    generation = (previous.get('objectiveGeneration') if objective == previous.get('objective')
                  else uuid.uuid4().hex)
    return {'objective': objective, 'objectiveGeneration': generation, 'criteria': criteria}


def project_decisions(criteria, decisions, conversation_id, principal, output_current):
    bindings = [binding for item in criteria
                for binding in decisions.get('items', {}).get(item['id'], {}).get('factBindings', [])]
    facts = current_facts(conversation_id, principal, bindings)
    projected = []
    for item in criteria:
        evidence = decisions.get('items', {}).get(item['id'])
        dependencies = project_dependencies((evidence or {}).get('factBindings', []), facts)
        state = 'unreviewed'
        if evidence:
            valid = (evidence['criterionDigest'] == item['digest'] and output_current(item, evidence)
                     and all(row['status'] == 'current' for row in dependencies))
            state = evidence['verdict'] if valid else 'stale'
        projected.append({**item, 'state': state, 'evidence': evidence, 'factDependencies': dependencies})
    return projected


def review_status(criteria, *, complete=True):
    return ('unconfigured' if not criteria else
            'changes_requested' if any(item['state'] == 'fail' for item in criteria) else
            'accepted' if complete and all(item['state'] == 'pass' for item in criteria) else 'pending')


def save_decision(review, command, criterion, evidence_fields, conversation_id):
    """Admit one owner judgment through the common evidence/CAS boundary."""
    if not command['note'].strip():
        raise ValueError('A review note describing the evidence is required')
    decisions, revision = review.read('decisions')
    if revision != command['decisionsRevision']:
        raise ReviewConflict('Decisions changed; reload before deciding')
    previous = decisions.get('items', {}).get(criterion['id'], {})
    if previous.get('factBindings') and 'factBindings' not in command:
        raise ValueError('Explicit supporting fact bindings are required for this review')
    facts = capture_bindings(conversation_id, review.principal, command.get('factBindings', []))
    decisions.setdefault('items', {})[criterion['id']] = {
        **evidence_fields, 'criterionDigest': criterion['digest'], 'verdict': command['verdict'],
        'note': command['note'], 'reviewer': review.principal.subject_id,
        'reviewedAt': int(time.time() * 1000), 'factBindings': facts,
    }
    decisions.pop('_commitDigest', None)
    review.put('decisions', decisions, revision)
