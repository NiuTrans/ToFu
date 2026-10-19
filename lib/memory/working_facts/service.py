"""Versioned working claims and targeted dependency invalidation.

Entry points: query_facts, record_fact, owner_decision. All state belongs to one
explicit owner/tenant/conversation aggregate. Freshness is projected from its
sources; source failure never erases evidence or promotes a model inference.
"""
from copy import deepcopy
import time
import uuid

from lib.identity import PrincipalContext
from .contract import FactInputError, limit, validate
from .repository import FactConflict, FactNotFound, FactRepository
from .sources import SourceResolver


def _assert_acyclic(facts):
    visited, visiting = set(), set()
    def visit(identifier):
        if identifier in visiting:
            raise ValueError('Working fact dependencies must be acyclic')
        if identifier in visited:
            return
        if identifier not in facts:
            raise ValueError('Working fact dependency is unavailable')
        visiting.add(identifier)
        for source in facts[identifier]['sources']:
            if source['kind'] == 'fact':
                visit(source['id'])
        visiting.remove(identifier); visited.add(identifier)
    for identifier in facts:
        visit(identifier)


class _FactView:
    def __init__(self, conversation_id, principal, document, witnesses=None, tool_witnesses=None):
        self.facts = document.get('facts', {})
        self.sources = SourceResolver(conversation_id, principal, document, witnesses, tool_witnesses)
        self.memo = {}
        _assert_acyclic(self.facts)

    def binding(self, source, *, capture=False):
        self.sources.check_deadline()
        if source['kind'] != 'fact':
            return self.sources.resolve(source, capture=capture)
        fact = self.facts.get(source['id'])
        if fact is None:
            return 'unavailable', 'dependency_unavailable', ''
        if fact['revision'] != source['version']:
            return 'stale', 'dependency_version_changed', ''
        view = self.fact(source['id'])
        return view['status'], '' if view['status'] in {'observed', 'owner_asserted'} else 'dependency_not_current', fact['text']

    def fact(self, identifier):
        if identifier in self.memo:
            return self.memo[identifier]
        fact = self.facts[identifier]
        bindings = [self.binding(source) for source in fact['sources']] if fact['active'] else []
        states = [binding[0] for binding in bindings]
        if not fact['active']:
            status = 'withdrawn'
        elif 'unavailable' in states:
            status = 'unavailable'
        elif any(state in {'stale', 'withdrawn'} for state in states):
            status = 'stale'
        elif fact['kind'] == 'derived' or 'needs_review' in states:
            status = 'needs_review'
        else:
            status = 'observed' if fact['kind'] == 'observation' else 'owner_asserted'
        value = {**deepcopy(fact), 'status': status, 'sourceStates': states,
                 'reasons': sorted({binding[1] for binding in bindings if binding[1]})}
        self.memo[identifier] = value
        return value


def query_facts(conversation_id: str, *, principal: PrincipalContext, ids=None):
    validate({'ids': ids} if ids is not None else {}, 'WorkingFactQuery')
    return _query_facts(conversation_id, principal, ids, missing_is_error=True)


def query_dependency_facts(conversation_id: str, *, principal: PrincipalContext, ids):
    """Read only selected dependencies through one graph; missing IDs stay absent.

    Internal evidence readers accept the bounded full fact population, whereas
    the public query keeps its smaller page contract. No revision is fabricated.
    """
    selected = list(dict.fromkeys(ids))
    if (len(selected) > limit('maxFacts') or any(not isinstance(identifier, str)
            or not 1 <= len(identifier) <= 128 for identifier in selected)):
        raise ValueError('Invalid working fact dependency population')
    return _query_facts(conversation_id, principal, selected, missing_is_error=False)


def _query_facts(conversation_id, principal, ids, *, missing_is_error):
    repository = FactRepository(conversation_id, principal)
    document, revision = repository.read()
    view = _FactView(conversation_id, principal, document)
    selected = list(dict.fromkeys(ids)) if ids is not None else list(view.facts)
    if missing_is_error and any(identifier not in view.facts for identifier in selected):
        raise FactNotFound('Working fact not found in this conversation')
    # This owned projection is schema-checked by the boundary tests. Revalidating
    # every output node each model round costs more than its source acquisition;
    # untrusted commands still pass through the canonical validators above.
    return {'schemaVersion': 1, 'conversationId': conversation_id, 'revision': revision,
            'facts': [view.fact(identifier) for identifier in selected if identifier in view.facts],
            'semantics': 'source_binding_and_owner_assessment_not_automatic_truth'}


def _load_for_edit(conversation_id, principal, revision):
    repository = FactRepository(conversation_id, principal)
    document, current_revision = repository.read()
    if current_revision != revision:
        raise FactConflict('Working facts changed; reload before editing')
    return repository, document


def record_fact(conversation_id: str, command: dict, *, principal: PrincipalContext,
                task_id: str = '', file_sources=None, tool_sources=None):
    validate(command, 'WorkingFactRecord')
    if not isinstance(task_id, str) or len(task_id) > 128:
        raise ValueError('Invalid producer task identity')
    if not command['text'].strip():
        raise FactInputError('Working fact text must not be blank')
    repository, document = _load_for_edit(conversation_id, principal, command['revision'])
    identifier = command.get('id') or 'wf_' + uuid.uuid4().hex
    previous = document['facts'].get(identifier)
    if command.get('id') and previous is None:
        raise FactNotFound('Working fact not found in this conversation')
    if previous and previous['actor'] == 'owner':
        raise PermissionError('An agent cannot overwrite an owner-confirmed, corrected or retracted fact')
    if previous is None and len(document['facts']) >= limit('maxFacts'):
        raise ValueError('Working facts capacity reached; existing facts were preserved')
    view = _FactView(conversation_id, principal, document, file_sources, tool_sources)
    bindings = [view.binding(source, capture=True) for source in command['sources']]
    if any(state in {'stale', 'unavailable', 'withdrawn'} for state, _, _ in bindings):
        raise FactConflict('A source changed or is unavailable; acquire its current version')
    if command['kind'] == 'observation':
        if (len(bindings) != 1 or bindings[0][0] not in {'observed', 'owner_asserted'}
                or bindings[0][2] != command['text']):
            raise FactInputError('An observation must quote exactly one current source, with text equal to that quote; use derived for a paraphrase or inference')
    now = int(time.time() * 1000)
    document['facts'][identifier] = {
        'id': identifier, 'revision': (previous['revision'] + 1 if previous else 1),
        'text': command['text'], 'kind': command['kind'], 'sources': deepcopy(command['sources']),
        'active': True, 'actor': 'agent', 'subject': principal.subject_id,
        'producerTaskId': str(task_id), 'note': '',
        'createdAt': previous['createdAt'] if previous else now, 'updatedAt': now,
    }
    _assert_acyclic(document['facts'])
    from .file_sources import prune_file_seals
    prune_file_seals(document)
    from .tool_sources import prune_tool_seals
    prune_tool_seals(document)
    repository.put(document, command['revision'])
    return query_facts(conversation_id, principal=principal, ids=[identifier])


def owner_decision(conversation_id: str, command: dict, *, principal: PrincipalContext):
    validate(command, 'WorkingFactOwnerDecision')
    if principal.kind != 'user':
        raise PermissionError('Owner correction requires a user principal')
    if not command['note'].strip():
        raise ValueError('Describe the basis of the owner decision')
    repository, document = _load_for_edit(conversation_id, principal, command['revision'])
    identifier = command['id']
    fact = document['facts'].get(identifier)
    if fact is None:
        raise FactNotFound('Working fact not found in this conversation')
    if command['action'] == 'correct':
        if not command.get('text', '').strip():
            raise ValueError('A correction requires replacement text')
        fact.update(text=command['text'], kind='owner_assertion', sources=[], active=True)
    elif command['action'] == 'confirm':
        state = _FactView(conversation_id, principal, document).fact(identifier)['status']
        if state in {'stale', 'unavailable', 'withdrawn'}:
            raise FactConflict('Current source bindings are required before confirmation')
        fact.update(kind='reviewed', active=True)
    else:
        fact['active'] = False
    fact.update(revision=fact['revision'] + 1, actor='owner', subject=principal.subject_id,
                note=command['note'], updatedAt=int(time.time() * 1000))
    from .file_sources import prune_file_seals
    prune_file_seals(document)
    from .tool_sources import prune_tool_seals
    prune_tool_seals(document)
    repository.put(document, command['revision'])
    return query_facts(conversation_id, principal=principal, ids=[identifier])
