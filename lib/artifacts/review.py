"""Owner review of immutable artifact versions, independent of task completion.

Entry points: read_review, update_review. Requirements belong to an owned
artifact lineage; latest decisions belong to the exact artifact and criterion
generation. Storage is the selected backend's record CAS, never a local DB.
"""
from lib.identity import PrincipalContext
from lib.storage import StorageError, get_storage_client
from .core import ArtifactNotFoundError, get_artifact_meta
from .review_contract import validate
from lib.acceptance.review import (ReviewRecords, ReviewConflict, generate_requirements,
                                   project_decisions, review_status, save_decision)

NAMESPACE = 'artifact_review_v1'


def _owned_meta(artifact_id, owner):
    try:
        return get_artifact_meta(artifact_id, user_id=owner)
    except StorageError as exc:
        if exc.code == 'database_forbidden':
            raise ArtifactNotFoundError('Unavailable') from exc
        raise


class _Review(ReviewRecords):
    def __init__(self, artifact_id: str, principal: PrincipalContext):
        self.principal = principal
        self.owner = principal.require_owner(context='artifact review')
        self.meta = _owned_meta(artifact_id, self.owner)
        current = self.meta
        seen = {artifact_id}
        while current.get('parent_id'):
            parent_id = current['parent_id']
            if parent_id in seen:
                raise ValueError('Cyclic artifact lineage')
            seen.add(parent_id)
            current = _owned_meta(parent_id, self.owner)
            if current['conv_id'] != self.meta['conv_id']:
                raise ArtifactNotFoundError('Unavailable')
        self.root_id = current['id']
        super().__init__(principal, NAMESPACE, {'requirements': self.root_id, 'decisions': artifact_id},
                         get_storage_client, operation_prefix='artifact-review')

    def view(self):
        requirements = self.criteria()
        decisions, decisions_revision = self.read('decisions')
        projected = project_decisions(requirements['criteria'], decisions, self.meta['conv_id'],
            self.principal, lambda item, evidence: evidence['artifactDigest'] == self.meta['content_sha256']
            and evidence['artifactId'] == self.meta['id'])
        status = review_status(projected)
        return validate({
            'schemaVersion': 1, 'artifactId': self.meta['id'],
            'artifactDigest': self.meta['content_sha256'], 'lineageId': self.root_id,
            'requirementsRevision': requirements['requirementsRevision'], 'decisionsRevision': decisions_revision,
            'objective': requirements.get('objective', ''), 'criteria': projected,
            'status': status, 'basis': 'owner_attestation',
        }, 'ArtifactReviewView')


def read_review(artifact_id: str, *, principal: PrincipalContext) -> dict:
    return _Review(artifact_id, principal).view()


def read_criteria(artifact_id: str, *, principal: PrincipalContext) -> dict:
    """Read requirement generations without evaluating decisions or their sources.

    Working facts may quote requirements; this port prevents review → fact →
    requirement reads from recursively evaluating the same owner decisions.
    """
    return _Review(artifact_id, principal).criteria()


def update_review(artifact_id: str, command: dict, *, principal: PrincipalContext) -> dict:
    validate(command)
    if principal.kind != 'user':
        raise PermissionError('Artifact owner review requires a user principal')
    review = _Review(artifact_id, principal)
    current = review.view()
    if command['requirementsRevision'] != current['requirementsRevision']:
        raise ReviewConflict('Requirements changed; reload before saving')
    if command['action'] == 'requirements':
        previous, revision = review.read('requirements')
        if revision != command['requirementsRevision']:
            raise ReviewConflict('Requirements changed; reload before saving')
        review.put('requirements', generate_requirements(command, previous), revision)
    else:
        item = next((item for item in current['criteria'] if item['id'] == command['criterionId']), None)
        if (item is None or item['digest'] != command['criterionDigest']
                or current['artifactDigest'] != command['artifactDigest']):
            raise ReviewConflict('Artifact or requirement changed; reload before deciding')
        save_decision(review, command, item, {'artifactId': artifact_id,
                      'artifactDigest': current['artifactDigest']}, review.meta['conv_id'])
    # A concurrent requirements edit cannot resurrect a pass: exact generation
    # bindings remain on the decision, and every reader projects fresh criteria.
    return review.view()
