"""Selected-store acceptance, invalidation, concurrent edits and owner boundaries."""
import asyncio
from types import SimpleNamespace

import pytest

from tests.turn_lifecycle.test_task_event_delivery import storage as storage, turn_service as turn_service
from lib.artifacts import core, review
from lib.identity import PrincipalContext

pytestmark = pytest.mark.unit
PRINCIPAL = PrincipalContext.user(subject_id='reviewer', owner_user_id=1)


@pytest.fixture
def artifact(storage, turn_service, monkeypatch):
    monkeypatch.setattr(review, 'get_storage_client', lambda write=False: storage.client)
    turn_service.create_turn_pair('review-conv', command_id='review-pair',
                                 input_projection={'content': 'Produce a report'},
                                 config={'model': 'fixture'}, user_id=1,
                                 conversation_defaults={'allowCreate': True, 'title': 'Review', 'settings': {}})
    return core.create_artifact(conv_id='review-conv', content='# Report 1', format='markdown',
                                source='write_file', source_ref={'path': 'report.md'})


def read(artifact):
    return review.read_review(artifact['id'], principal=PRINCIPAL)


def requirements(artifact, previous=None, **changes):
    current = previous or read(artifact)
    command = {'action': 'requirements', 'requirementsRevision': current['requirementsRevision'],
               'objective': 'A useful report', 'criteria': [
                   {'id': 'accuracy', 'text': 'Totals agree with source', 'kind': 'requirement'},
                   {'id': 'format', 'text': 'Readable headings', 'kind': 'requirement'},
               ], **changes}
    return review.update_review(artifact['id'], command, principal=PRINCIPAL)


def decide(artifact, previous=None, index=0, **changes):
    current = previous or read(artifact)
    command = {'action': 'decision', 'requirementsRevision': current['requirementsRevision'],
               'decisionsRevision': current['decisionsRevision'], 'artifactDigest': current['artifactDigest'],
               'criterionId': current['criteria'][index]['id'], 'criterionDigest': current['criteria'][index]['digest'],
               'verdict': 'pass', 'note': 'Compared the report with the source ledger', **changes}
    return review.update_review(artifact['id'], command, principal=PRINCIPAL)


def test_acceptance_is_version_bound_and_survives_restart(artifact, storage):
    assert read(artifact)['status'] == 'unconfigured'
    requirements(artifact)
    assert decide(artifact)['status'] == 'pending'
    accepted = decide(artifact, index=1)
    assert accepted['status'] == 'accepted'
    second = core.create_artifact(conv_id=artifact['conv_id'], content='# Report 2', format='markdown',
                                  source='write_file', source_ref={'path': 'report.md'})
    fresh = read(second)
    assert fresh['lineageId'] == artifact['id']
    assert fresh['status'] == 'pending'
    assert all(item['state'] == 'unreviewed' for item in fresh['criteria'])
    storage.stop(); storage.start()
    assert read(artifact) == accepted
    assert read(second) == fresh


def test_requirement_edits_invalidate_only_affected_judgments(artifact):
    requirements(artifact); decide(artifact); old = decide(artifact, index=1)
    criteria = [{key: item[key] for key in ('id', 'text', 'kind')} for item in old['criteria']]
    criteria[0]['text'] = 'Totals and tax agree with source'
    updated = requirements(artifact, criteria=criteria)
    assert [item['state'] for item in updated['criteria']] == ['stale', 'pass']
    # Reverting the wording still requires fresh review, as does removing/readding.
    reverted = requirements(artifact)
    assert reverted['criteria'][0]['state'] == 'stale'
    requirements(artifact, criteria=criteria[1:])
    assert requirements(artifact)['criteria'][0]['state'] == 'stale'
    scoped = requirements(artifact, objective='New audience and purpose')
    assert all(item['state'] == 'stale' for item in scoped['criteria'])


def test_constraints_invalidate_requirements_and_failure_prevents_acceptance(artifact):
    requirements(artifact); decide(artifact); old = decide(artifact, index=1)
    criteria = [{key: item[key] for key in ('id', 'text', 'kind')} for item in old['criteria']]
    criteria.append({'id': 'privacy', 'kind': 'constraint', 'text': 'No personal data'})
    changed = requirements(artifact, criteria=criteria)
    assert [item['state'] for item in changed['criteria']] == ['stale', 'stale', 'unreviewed']
    assert decide(artifact, index=2, verdict='fail')['status'] == 'changes_requested'


def test_stale_tabs_and_wrong_evidence_cannot_overwrite(artifact):
    first = requirements(artifact)
    decide(artifact, first)
    with pytest.raises(review.ReviewConflict):
        decide(artifact, first, verdict='fail')
    requirements(artifact, objective='Changed objective')
    with pytest.raises(review.ReviewConflict):
        requirements(artifact, first)
    with pytest.raises(review.ReviewConflict):
        decide(artifact, artifactDigest='0' * 64)
    with pytest.raises(ValueError):
        decide(artifact, note='  ')


def test_requirements_changed_during_decision_cannot_resurrect_acceptance(artifact, monkeypatch):
    requirements(artifact); decide(artifact)
    original = review._Review.put
    def interleave(self, kind, value, revision):
        if kind == 'decisions':
            requirements(artifact, objective='Changed while reviewing')
        return original(self, kind, value, revision)
    monkeypatch.setattr(review._Review, 'put', interleave)
    result = decide(artifact, index=1)
    assert result['status'] == 'pending'
    assert all(item['state'] == 'stale' for item in result['criteria'])


def test_lost_ack_is_reconciled_by_exact_readback(artifact, storage, monkeypatch):
    from lib.storage import StorageError
    original = storage.client.command
    def lost_ack(*args, **kwargs):
        original(*args, **kwargs)
        raise StorageError('database_timeout', 'ACK lost')
    monkeypatch.setattr(storage.client, 'command', lost_ack)
    assert requirements(artifact)['requirementsRevision'] == 1
    assert read(artifact)['requirementsRevision'] == 1


def test_uncommitted_failure_does_not_create_an_acceptance_record(artifact, storage, monkeypatch):
    from lib.storage import StorageError
    def unavailable(*args, **kwargs):
        raise StorageError('database_unavailable', 'Injected before commit')
    monkeypatch.setattr(storage.client, 'command', unavailable)
    with pytest.raises(StorageError):
        requirements(artifact)
    assert read(artifact)['status'] == 'unconfigured'
    assert read(artifact)['requirementsRevision'] == 0


def test_deleted_ancestor_fails_closed_without_losing_record(artifact, storage):
    requirements(artifact); decide(artifact); decide(artifact, index=1)
    second = core.create_artifact(conv_id=artifact['conv_id'], content='# Descendant', format='markdown',
                                  source='write_file', source_ref={'path': 'report.md'})
    storage.client.command('artifact.delete', {'artifact_id': artifact['id'], 'deleted_at': 123}, 'delete-parent')
    with pytest.raises(core.ArtifactNotFoundError):
        read(second)


def test_http_auth_scope_conflicts_and_wire_contract(artifact):
    from quart import Quart, g, request
    from lib.api_v4 import attach_v4_envelope, copy_blueprint_to_v4
    from routes.api_v1.artifacts import api_v1_artifacts_bp
    app = Quart(__name__)
    from lib.http_transport.error_handlers import register_http_error_handlers
    register_http_error_handlers(app)
    app.register_blueprint(attach_v4_envelope(copy_blueprint_to_v4(api_v1_artifacts_bp, name='review_v4')))
    @app.before_request
    def identity():
        owner = request.headers.get('X-Test-Owner')
        if owner:
            g.auth_ctx = SimpleNamespace(is_authenticated=True, owner_user_id=int(owner),
                key_id='test', name='test owner', tenant_id=None, scopes=request.headers.get('X-Test-Scopes', 'chat').split(','),
                has_scope=lambda scope: scope in request.headers.get('X-Test-Scopes', 'chat').split(','))
            g.principal_context = PrincipalContext.user(subject_id='reviewer', owner_user_id=int(owner))
    async def exercise():
        client = app.test_client(); url = '/api/v4/artifacts/' + artifact['id'] + '/review'
        headers = {'X-Test-Owner': '1'}
        assert (await client.get(url)).status_code == 401
        assert (await client.get(url, headers={'X-Test-Owner': '8'})).status_code == 404
        assert (await client.get(url, headers={**headers, 'X-Test-Scopes': 'tasks'})).status_code == 403
        response = await client.get(url, headers=headers)
        assert response.status_code == 200
        wire = await response.get_json()
        review.validate(wire['data'], 'ArtifactReviewView')
        assert wire['data']['artifactDigest'] == artifact['content_sha256']
        command = {'action': 'requirements', 'requirementsRevision': 0, 'objective': 'Review',
                   'criteria': [{'id': 'a', 'kind': 'requirement', 'text': 'Correct'}]}
        assert (await client.post(url, headers=headers, json=command)).status_code == 200
        assert (await client.post(url, headers=headers, json=command)).status_code == 409
        assert (await client.post(url, headers={'X-Test-Owner': '8'}, json=command)).status_code == 404
        command['extra'] = 'not allowed'
        assert (await client.post(url, headers=headers, json=command)).status_code == 400
    asyncio.run(exercise())
