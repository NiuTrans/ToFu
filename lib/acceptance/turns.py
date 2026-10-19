"""Owner acceptance of an ordinary assistant Turn and declared output versions.

Entry: read_review, update_review, read_requirements, read_execution_requirements.
Turn lifecycle and
artifact ownership are read through their facades; shared review mechanics own
generations, fact dependencies and CAS. Output membership is an explicit owner
declaration, never inferred from a browser attachment or a task's final claim.
"""
import uuid

from lib.identity import PrincipalContext
from lib.storage import get_storage_client, StorageError
from lib.turn_lifecycle import get_turn, get_attempt, LifecycleNotFound
from lib.artifacts.core import get_artifact_meta, ArtifactNotFoundError
from .contract import validate
from .review import (ReviewRecords, ReviewConflict, digest, generate_requirements,
                     project_criteria, project_decisions, review_status, save_decision)


def _owned_turn(conversation_id, turn_id, principal):
    try:
        turn = get_turn(conversation_id, turn_id, user_id=principal.require_owner(context='Turn review'))
    except StorageError as exc:
        if exc.code in {'database_forbidden', 'database_not_found'}:
            raise LifecycleNotFound('Turn unavailable') from exc
        raise
    if turn['actor'] != 'assistant' or turn['laneId'] != 'main':
        raise LifecycleNotFound('Ordinary assistant Turn unavailable')
    return turn


class TurnReview(ReviewRecords):
    def __init__(self, conversation_id, turn_id, principal, *, turn=None, attempt=None):
        self.conversation_id = conversation_id
        self.turn_id = turn_id
        from .continuity import review_source
        turn = turn if turn is not None else _owned_turn(conversation_id, turn_id, principal)
        source = review_source(turn, principal, attempt=attempt)
        self.source = source
        self.turn = turn
        record_turn_id = source['turn'] if source is not None else turn_id
        self.bound_attempt = turn.get('currentAttemptId')
        super().__init__(principal, 'turn_review_v1', {
            'requirements': [conversation_id, record_turn_id], 'decisions': [conversation_id, record_turn_id],
        }, get_storage_client)

    def artifact(self, artifact_id):
        try:
            meta = get_artifact_meta(artifact_id, user_id=self.owner)
        except StorageError as exc:
            if exc.code == 'database_forbidden':
                raise ArtifactNotFoundError('Unavailable') from exc
            raise
        if meta['conv_id'] != self.conversation_id:
            raise ArtifactNotFoundError('Unavailable')
        return meta

    def outputs(self, requirements):
        # Authority revisions prevent A→B→A text changes and retry attempts from
        # resurrecting prior acceptance. Display-only enrichment is excluded by
        # the Turn owner's existing modelContextRevision when available.
        version = digest([self.turn_id, self.turn.get('currentAttemptId'),
                          self.turn.get('modelContextRevision', self.turn['projectionRevision'])])
        rows = [{'id': 'answer', 'label': 'Answer', 'kind': 'answer', 'artifactId': None,
                 'version': version, 'available': True, 'covered': False}]
        for slot in requirements.get('outputs', []):
            try:
                meta = self.artifact(slot['artifactId'])
                version = digest([slot['generation'], meta['id'], meta['content_sha256']])
            except ArtifactNotFoundError:
                version = None
            rows.append({key: slot[key] for key in ('id', 'label', 'artifactId')} | {
                'kind': 'artifact', 'version': version, 'available': version is not None, 'covered': False})
        return rows

    def view(self, *, refresh=True):
        # A final reread after a write observes newer output revisions as stale.
        if refresh:
            self.turn = _owned_turn(self.conversation_id, self.turn_id, self.principal)
        if self.turn.get('currentAttemptId') != self.bound_attempt:
            raise ReviewConflict('Turn execution changed; reload before review')
        if refresh and self.source is not None:
            from .continuity import review_source
            if review_source(self.turn, self.principal) != self.source:
                raise ReviewConflict('Turn review source changed; reload before review')
        requirements, revision = self.read('requirements')
        criteria = project_criteria(requirements, revision)
        decisions, decisions_revision = self.read('decisions')
        outputs = self.outputs(requirements)
        by_id = {row['id']: row for row in outputs}
        def current(item, evidence):
            bindings = evidence['outputBindings']
            return (all(row['id'] in by_id and by_id[row['id']]['available']
                        and row['version'] == by_id[row['id']]['version'] for row in bindings)
                    and (item['kind'] != 'constraint' or {row['id'] for row in bindings} == set(by_id)))
        projected = project_decisions(criteria['criteria'], decisions, self.conversation_id,
                                      self.principal, current)
        covered = {binding['id'] for item in projected if item['state'] == 'pass'
                   for binding in item['evidence']['outputBindings']}
        for row in outputs:
            row['covered'] = row['available'] and row['id'] in covered
        complete = self.turn['status'] == 'completed' and all(row['covered'] for row in outputs)
        return validate({
            'schemaVersion': 1, 'conversationId': self.conversation_id, 'turnId': self.turn_id,
            'requirementsRevision': revision, 'decisionsRevision': decisions_revision,
            'objective': criteria['objective'], 'criteria': projected, 'outputs': outputs,
            'executionStatus': self.turn['status'], 'status': review_status(projected, complete=complete),
            'basis': 'owner_attestation',
        }, 'TurnReviewView')


def read_review(conversation_id: str, turn_id: str, *, principal: PrincipalContext) -> dict:
    return TurnReview(conversation_id, turn_id, principal).view(refresh=False)


def read_requirements(conversation_id: str, turn_id: str, *, principal: PrincipalContext) -> dict:
    """Requirement-only port: no decisions, fact graph or artifact body reads."""
    turn = _owned_turn(conversation_id, turn_id, principal)
    return _requirements(conversation_id, turn_id, principal, turn=turn)


def _requirements(conversation_id, turn_id, principal, *, turn=None, attempt=None):
    review = TurnReview(conversation_id, turn_id, principal, turn=turn, attempt=attempt)
    requirements, revision = review.read('requirements')
    return {**project_criteria(requirements, revision),
            'outputs': [{key: slot[key] for key in ('id', 'label', 'artifactId')}
                        for slot in requirements.get('outputs', [])]}


def read_execution_requirements(conversation_id: str, turn_id: str, attempt_id: str,
                                task_id: str, *, principal: PrincipalContext) -> dict | None:
    """Read one requirement snapshot for the currently bound ordinary execution.

    Task-supplied identifiers are correlations, never ownership evidence. The
    lifecycle facade checks owner, binding and current attempt before access.
    Other actors/lanes have their own policy and receive no ordinary review.
    """
    owner = principal.require_owner(context='Turn execution requirements')
    attempt = get_attempt(attempt_id, user_id=owner, include_execution_config=True)
    if (attempt['conversationId'], attempt['turnId'], attempt['taskId']) != (
            conversation_id, turn_id, task_id):
        raise LifecycleNotFound('Turn execution binding unavailable')
    turn = get_turn(conversation_id, turn_id, user_id=owner)
    if turn['currentAttemptId'] != attempt_id:
        raise LifecycleNotFound('Turn execution was superseded')
    if turn['actor'] != 'assistant' or turn['laneId'] != 'main':
        return None
    return _requirements(conversation_id, turn_id, principal, turn=turn, attempt=attempt)


def update_review(conversation_id: str, turn_id: str, command: dict, *, principal: PrincipalContext) -> dict:
    validate(command, 'TurnReviewCommand')
    if principal.kind != 'user':
        raise PermissionError('Turn owner review requires a user principal')
    review = TurnReview(conversation_id, turn_id, principal)
    if command['action'] == 'requirements':
        _owned_turn(conversation_id, turn_id, principal)
        previous, revision = review.read('requirements')
        if revision != command['requirementsRevision']:
            raise ReviewConflict('Requirements changed; reload before saving')
        slots = command['outputs']
        ids = [slot['id'] for slot in slots]
        if ('answer' in ids or len(ids) != len(set(ids))
                or len({slot['artifactId'] for slot in slots}) != len(slots)
                or any(not slot['label'].strip() for slot in slots)):
            raise ValueError('Output slots require unique IDs, unique artifacts and nonempty labels')
        old = {slot['id']: slot for slot in previous.get('outputs', [])}
        outputs = []
        for slot in slots:
            review.artifact(slot['artifactId'])
            before = old.get(slot['id'], {})
            generation = (before['generation'] if before.get('artifactId') == slot['artifactId']
                          else uuid.uuid4().hex)
            outputs.append({**slot, 'generation': generation})
        review.put('requirements', {**generate_requirements(command, previous), 'outputs': outputs}, revision)
    else:
        current = review.view()
        item = next((row for row in current['criteria'] if row['id'] == command['criterionId']), None)
        if (command['requirementsRevision'] != current['requirementsRevision']
                or item is None or item['digest'] != command['criterionDigest']):
            raise ReviewConflict('Requirements changed; reload before deciding')
        bindings = command['outputBindings']
        by_id = {row['id']: row for row in current['outputs']}
        if len({row['id'] for row in bindings}) != len(bindings):
            raise ValueError('Output bindings require unique IDs')
        if any(row['id'] not in by_id or not by_id[row['id']]['available']
               or row['version'] != by_id[row['id']]['version'] for row in bindings):
            raise ReviewConflict('Outputs changed or unavailable; reload before deciding')
        if item['kind'] == 'constraint' and {row['id'] for row in bindings} != set(by_id):
            raise ValueError('A global constraint must assess every declared output')
        captured = [{**binding, **{key: by_id[binding['id']][key]
                     for key in ('label', 'kind', 'artifactId')}} for binding in bindings]
        save_decision(review, command, item, {'outputBindings': captured}, conversation_id)
    return review.view()
