"""Version-pinned prerequisite policy over one bounded owner/project snapshot.

check_graph rejects malformed persisted edges. states evaluates reachable evidence
once per node, in durable insertion order, without recursive loads or execution.
Repository ports own authority, turns/attempts and existing workspace receipts.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy

from lib.log import get_logger

logger = get_logger(__name__)


def check_graph(assignments, limit):
    from .repository import ProjectDispatchError
    if len(assignments) > limit:
        raise ProjectDispatchError('Dependency graph capacity exceeded')
    earlier = {}
    for row in assignments:
        references = row['request'].get('dependencies', [])
        ids = [ref['assignmentId'] for ref in references]
        if len(ids) != len(set(ids)):
            raise ProjectDispatchError('Duplicate dependency identity')
        for ref in references:
            predecessor = earlier.get(ref['assignmentId'])
            if (predecessor is None or predecessor['grant']['owner'] != row['grant']['owner']
                    or predecessor['grant']['tenant'] != row['grant']['tenant']
                    or predecessor['request']['projectPath'] != row['request']['projectPath']):
                raise ProjectDispatchError('Dependency must be an earlier same owner/project/tenant assignment')
            files = predecessor['request'].get('profile', {}).get('mode') in ('files', 'project')
            if files != ('checkpointSha' in ref):
                raise ProjectDispatchError('File dependency requires checkpointSha; consult forbids it')
        earlier[row['id']] = row


def _evidence(repository, row):
    """Historical deadline is not revocation of completed accepted evidence."""
    from .repository import CONTRACT, ProjectDispatchError, evidence_owner_matches
    if row['grant']['revoked']:
        return 'invalid', 'revoked', None, None
    try:
        repository._check_stops(row)
    except (ProjectDispatchError, PermissionError):
        return 'invalid', 'stopped', None, None
    try:
        repository.validate_grant_binding(row)
        repository._live_request(row['request'], check_deadline=False)
    except (ProjectDispatchError, PermissionError):
        return 'invalid', 'scope_changed', None, None
    association = row['execution']
    if association is None:
        return 'invalid', 'result_changed', None, None
    attempt = repository.get_attempt(association['attemptId'])
    turn = repository.turn_get(row['request']['recipientId'], association['turnId'],
                               user_id=repository.owner)
    if not isinstance(attempt, Mapping) or not isinstance(turn, Mapping):
        return 'unknown', 'evidence_unavailable', None, None
    if (attempt.get('attemptId') != association['attemptId']
            or attempt.get('turnId') != association['turnId']
            or attempt.get('conversationId') != row['request']['recipientId']
            or not attempt.get('taskId') or attempt.get('status') != 'completed'
            or turn.get('turnId') != association['turnId']
            or turn.get('conversationId') != row['request']['recipientId']
            or turn.get('actor') != 'assistant'
            or type(turn.get('projectionRevision')) is not int
            or not all(evidence_owner_matches(item, repository.owner) for item in (turn, attempt))):
        return 'invalid', 'result_changed', None, None
    if (any(item['phase'] == 'dispatch_intent' or not item['usage_complete']
            for item in row['modelAttempts'])
            or any(item['phase'] not in CONTRACT['terminalToolPhases']
                   for item in row.get('toolAttempts', []))):
        return 'unknown', 'evidence_unavailable', None, None
    return 'ready', 'ready', attempt, turn


def _edge(repository, row, reference, evidence):
    status, reason, attempt, turn = evidence
    if row['grant']['revoked']:
        return 'invalid', 'revoked'
    if row['grant']['revision'] != reference['grantRevision']:
        return 'invalid', 'dependency_changed'
    if status != 'ready':
        return status, reason
    if turn['projectionRevision'] != reference['resultRevision']:
        return 'invalid', 'result_changed'
    review = row['review']
    if review is not None and review['resultRevision'] != reference['resultRevision']:
        return 'invalid', 'result_changed'
    if review is not None and review['decision'] != 'accept':
        return 'invalid', 'review_rejected'
    files = row['request'].get('profile', {}).get('mode') in ('files', 'project')
    if files:
        from .publication import delivery_view
        observed = []
        def workspace_get(*args, **kwargs):
            value = repository.get_workspace(*args, **kwargs)
            observed.append(value)
            return value
        delivery = delivery_view(row, attempt, workspace_get)
        if delivery is None or not observed or not isinstance(observed[0], Mapping):
            return 'unknown', 'evidence_unavailable'
        workspace = observed[0]

        if (type(workspace.get('user_id')) is not int
                or workspace['user_id'] != repository.owner
                or workspace.get('state') == 'discarded'):
            return 'invalid', 'dependency_changed'
        origin = workspace.get('origin') or {}
        from .admission import KEY
        if origin.get(KEY) != repository._admission(row):
            return 'invalid', 'dependency_changed'
        checkpoint = reference['checkpointSha']
        if workspace.get('checkpoint_sha') != checkpoint or delivery['checkpointSha'] != checkpoint:
            return 'invalid', 'checkpoint_changed'
        publication = row.get('publication')
        if publication is not None and (publication['grantRevision'] != reference['grantRevision']
                or publication['resultRevision'] != reference['resultRevision']
                or publication['checkpointSha'] != checkpoint):
            return 'invalid', 'checkpoint_changed'
    if review is None:
        return 'waiting_review', 'review_required'
    if files:
        receipt = origin.get('delivery') or {}
        if (publication is None or delivery['status'] != 'delivered'
                or receipt.get('checkpointSha') != checkpoint):
            return 'waiting_delivery', 'delivery_required'
    return 'ready', 'ready'


def _combine(results):
    # Invalid/unknown descendants cannot hide behind an unreviewed direct result.
    for status in ('invalid', 'unknown', 'waiting_review', 'waiting_delivery'):
        if any(result[0] == status for result in results):
            return next(result for result in results if result[0] == status)
    return 'ready', 'ready'


def states(repository, assignment, value=None):
    """Project direct references; any uncertain storage evidence is never ready."""
    references = assignment['request'].get('dependencies', [])
    if not references:
        return []
    def project(results):
        return [{'reference': deepcopy(ref), 'status': status, 'reason': reason}
                for ref, (status, reason) in zip(references, results)]
    try:
        value = repository._load(repository._selection(assignments=[assignment['id']]))[0] if value is None else value
        rows = {row['id']: row for row in value['assignments']}
        check_graph(value['assignments'], max(len(value['assignments']), repository.dependency_graph_limit)
                    if repository.storage_version == 2 else repository.dependency_graph_limit)
        current = rows[assignment['id']]
        if current['request'] != assignment['request'] or current['grant'] != assignment['grant']:
            return project([('invalid', 'dependency_changed')] * len(references))
        reachable = set()
        pending = [ref['assignmentId'] for ref in references]
        while pending:
            identity = pending.pop()
            if identity in reachable:
                continue
            reachable.add(identity)
            if len(reachable) > repository.dependency_graph_limit:
                raise ValueError('Dependency graph capacity exceeded')
            pending.extend(ref['assignmentId'] for ref in rows[identity]['request'].get('dependencies', []))
        evidence, inherited, edge_cache = {}, {}, {}
        def edge(ref):
            key = (ref['assignmentId'], ref['grantRevision'], ref['resultRevision'], ref.get('checkpointSha'))
            if key not in edge_cache:
                identity = ref['assignmentId']
                try:
                    local = _edge(repository, rows[identity], ref, evidence[identity])
                except Exception as diagnostic_error:
                    logger.debug('edge failure handled: %s', type(diagnostic_error).__name__)
                    local = ('unknown', 'evidence_unavailable')
                edge_cache[key] = _combine([local, inherited[identity]])
            return edge_cache[key]
        for row in value['assignments']:
            if row['id'] not in reachable:
                continue
            try:
                evidence[row['id']] = _evidence(repository, row)
            except Exception as diagnostic_error:
                logger.debug('states failure handled: %s', type(diagnostic_error).__name__)
                evidence[row['id']] = ('unknown', 'evidence_unavailable', None, None)
            inherited[row['id']] = _combine([edge(ref) for ref in row['request'].get('dependencies', [])])
        return project([edge(ref) for ref in references])
    except Exception as diagnostic_error:
        logger.debug('states failure handled: %s', type(diagnostic_error).__name__)
        return project([('unknown', 'evidence_unavailable')] * len(references))
