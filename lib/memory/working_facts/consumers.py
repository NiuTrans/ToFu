"""Durable task dependencies in the existing owner-scoped record authority.

prepare/confirm retain revision extrema, not prompt bodies or request history.
Prepared evidence survives uncertain provider/ACK loss; only the existing
verified-return boundary can confirm it. query projects current source changes.
"""
from copy import deepcopy
import time

from lib.storage import get_storage_client
from .contract import limit, validate
from .repository import FactConflict, FactRepository, FactsUnavailable, digest
from .dependencies import assess_dependency

NAMESPACE = 'working_fact_consumers_v1'


class ConsumerRepository(FactRepository):
    def __init__(self, conversation_id, principal, task_id):
        if not isinstance(task_id, str) or not 1 <= len(task_id) <= 128:
            raise ValueError('Invalid working fact consumer task identity')
        super().__init__(conversation_id, principal)
        self.key += ':' + digest(task_id)
        self.task_id = task_id

    def payload(self):
        return {**super().payload(), 'namespace': NAMESPACE}

    def read(self):
        value, revision = super().read()
        if revision and value.get('taskId') != self.task_id:
            raise FactsUnavailable('Working fact consumer unavailable')
        return value, revision


def _references(facts):
    if not isinstance(facts, list) or not 1 <= len(facts) <= limit('maxFacts'):
        raise ValueError('Invalid working fact dependency population')
    result = {}
    for row in facts:
        identifier, revision = row.get('id'), row.get('revision')
        if (not isinstance(identifier, str) or not 1 <= len(identifier) <= 128
                or type(revision) is not int or revision < 1 or identifier in result):
            raise ValueError('Invalid working fact dependency identity')
        result[identifier] = revision
    return result


def _extend(target, references):
    for identifier, revision in references.items():
        previous = target.get(identifier, [revision, revision])
        target[identifier] = [min(previous[0], revision), max(previous[1], revision)]


def prepare(conversation_id, *, principal, task_id, facts, turn_id='', attempt_id=''):
    """Persist potential consumption before dispatch; source changes remain valid history."""
    references = _references(facts)
    for identity in (turn_id, attempt_id):
        if not isinstance(identity, str) or len(identity) > 128:
            raise ValueError('Invalid working fact execution identity')
    repository = ConsumerRepository(conversation_id, principal, task_id)
    document, version = repository.read()
    if not version:
        document = {'taskId': task_id, 'turnId': turn_id, 'attemptId': attempt_id,
                    'prepared': {}, 'confirmed': {}, 'uncertain': {}, 'pending': {},
                    'lastConfirmedDigest': ''}
    elif (document.get('turnId'), document.get('attemptId')) != (turn_id, attempt_id):
        raise FactConflict('Working fact consumer execution identity changed')
    previous = deepcopy(document)
    if document['pending'] and document['pending'] != references:
        _extend(document['uncertain'], document['pending'])
    document['pending'] = references
    _extend(document['prepared'], references)
    if len(document['prepared']) > limit('maxFacts'):
        raise ValueError('Working fact consumer capacity reached; existing evidence was preserved')
    if document != previous:
        document['updatedAt'] = int(time.time() * 1000)
        repository.put(document, version)


def confirm(conversation_id, *, principal, task_id, facts):
    """Confirm only previously prepared revisions; unknown delivery never self-promotes."""
    references = _references(facts)
    repository = ConsumerRepository(conversation_id, principal, task_id)
    document, version = repository.read()
    if version and not document['pending'] and document['lastConfirmedDigest'] == digest(references):
        return
    if not version or document['pending'] != references:
        raise FactConflict('Working fact consumption was not prepared')
    _extend(document['confirmed'], references)
    document['pending'] = {}
    document['lastConfirmedDigest'] = digest(references)
    document['updatedAt'] = int(time.time() * 1000)
    repository.put(document, version)


def query(conversation_id, *, principal, cursor='', page_size=None):
    from .service import query_facts
    page_size = limit('consumerPageSize') if page_size is None else page_size
    validate({'cursor': cursor, 'limit': page_size}, 'WorkingFactConsumersQuery')
    scope = FactRepository(conversation_id, principal)
    prefix = scope.key + ':'
    rows = get_storage_client().query('record.list', {
        'namespace': NAMESPACE, 'prefix': prefix, 'after_key': prefix + cursor if cursor else '',
        'limit': page_size + 1, 'user_id': scope.owner})
    view = query_facts(conversation_id, principal=principal)
    current = {row['id']: row for row in view['facts']}
    consumers = []
    for row in rows[:page_size]:
        document = row['value']
        if (document.get('owner') != scope.owner or document.get('tenant') != scope.tenant
                or document.get('conversationId') != conversation_id
                or row['key'] != prefix + digest(document.get('taskId'))):
            raise FactsUnavailable('Working fact consumer unavailable')
        dependencies = []
        uncertain = False
        for identifier, prepared in sorted(document['prepared'].items()):
            confirmed = document['confirmed'].get(identifier)
            fact = current.get(identifier)
            reasons = []
            unconfirmed = (identifier in document['pending'] or identifier in document['uncertain']
                           or confirmed != prepared)
            if unconfirmed:
                uncertain = True
                reasons.append('provider_consumption_not_fully_confirmed')
            status, source_reasons = assess_dependency(fact, *prepared)
            reasons.extend(source_reasons)
            if status == 'current':
                status = 'unconfirmed' if unconfirmed else 'current'
            dependencies.append({'id': identifier, 'preparedFirstRevision': prepared[0],
                'preparedLastRevision': prepared[1], 'confirmedFirstRevision': confirmed[0] if confirmed else None,
                'confirmedLastRevision': confirmed[1] if confirmed else None,
                'currentRevision': fact['revision'] if fact else None, 'status': status, 'reasons': reasons})
        consumers.append({'taskId': document['taskId'], 'turnId': document['turnId'],
            'attemptId': document['attemptId'], 'dependencies': dependencies,
            'requiresRevalidation': any(row['status'] in {'changed', 'unavailable', 'needs_review'} for row in dependencies),
            'consumptionUncertain': uncertain, 'updatedAt': document['updatedAt']})
    return {'schemaVersion': 1, 'conversationId': conversation_id, 'factsRevision': view['revision'],
        'coverage': 'admitted_working_fact_context_since_enablement',
        'consumers': consumers, 'nextCursor': rows[page_size - 1]['key'][len(prefix):] if len(rows) > page_size else '',
        'semantics': 'prepared_and_provider_confirmed_dependencies_not_task_acceptance'}
