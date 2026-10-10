"""Paper build HTTP auth, accepted tasks, versioned reads and streaming leases."""
from __future__ import annotations

import asyncio
import uuid

import pytest
from quart import Quart, g, request

from lib.api_keys import AuthContext
from lib.api_v4 import attach_v4_envelope, copy_blueprint_to_v4
from lib.paper.builds.compiler import CompiledBuild, artifact_metadata
from lib.paper.builds.errors import BuildError
from tests.support import paper_builds as build_fixtures

pytestmark = pytest.mark.unit
scheduler = build_fixtures.scheduler


@pytest.fixture
def app(scheduler, monkeypatch):
    from routes.api_v1 import paper_builds
    from lib.paper.builds import artifact_repository
    from lib.storage.errors import StorageError
    class EmptyRetainedStore:
        def command(self, *args):
            raise StorageError('database_not_found', 'No retained artifact in this cache-only fixture')
    monkeypatch.setattr(artifact_repository, '_storage', lambda **_: EmptyRetainedStore())
    application = Quart(__name__, static_folder=None)
    application.config['TESTING'] = True
    @application.before_request
    def principal_fixture():
        owner = request.headers.get('X-Test-Owner')
        if owner:
            g.auth_ctx = AuthContext(key_id='fixture', owner_user_id=int(owner),
                                     scopes=frozenset(request.headers.get('X-Test-Scopes', 'tasks').split(',')))
    application.register_blueprint(paper_builds.api_v1_paper_builds_bp)
    # Downstream retained-stream suites (test_paper_build_retention*,
    # test_paper_build_retained_streams) share this fixture but were migrated
    # to /api/v4 paths; production mounts the v4 projection, so mirror it here.
    application.register_blueprint(attach_v4_envelope(copy_blueprint_to_v4(
        paper_builds.api_v1_paper_builds_bp, name='api_v1_paper_builds_bp_v4')))
    monkeypatch.setattr(paper_builds, 'get_scheduler', lambda: scheduler)
    return application


def _stored(scheduler, tmp_path, *, extra_artifact=None):
    directory = tmp_path / uuid.uuid4().hex
    directory.mkdir()
    pdf = directory / 'document.pdf'
    data = b'%PDF-1.4\n' + b'paper fixture ' * 2000 + b'\n%%EOF\n'
    pdf.write_bytes(data)
    artifacts = [artifact_metadata(pdf)]
    if extra_artifact is not None:
        name, raw = extra_artifact
        extra = directory / name
        extra.write_bytes(raw)
        artifacts.append(artifact_metadata(extra))
    receipt = {'schema_version': 1, 'owner_user_id': 7, 'document_id': 'a' * 64,
               'build_id': uuid.uuid4().hex, 'target': 'host', 'source_digest': 'b' * 64,
               'version_verified': True, 'main_file': 'main.tex', 'engine': 'pdflatex',
               'toolchain': 'fixture', 'status': 'success', 'started_at': 1,
               'completed_at': 2, 'artifacts': artifacts, 'diagnostics': []}
    scheduler.cache.publish(CompiledBuild(receipt, directory))
    return receipt, data


def test_start_requires_auth_scope_and_explicit_owner(app, monkeypatch):
    from routes.api_v1 import paper_builds
    calls = []
    def start(payload, *, owner_user_id):
        calls.append((payload, owner_user_id))
        return {'task_id': 'paper-task', 'document_id': 'a' * 64, 'subscriber_id': 'viewer'}
    monkeypatch.setattr(paper_builds, 'start_preview', start)
    async def run():
        client = app.test_client()
        assert (await client.post('/api/v1/paper/builds', json={})).status_code == 401
        assert (await client.post('/api/v1/paper/builds', json={}, headers={
            'X-Test-Owner': '7', 'X-Test-Scopes': 'read'})).status_code == 403
        response = await client.post('/api/v1/paper/builds', json={'conv_id': 'c'},
                                     headers={'X-Test-Owner': '7'})
        assert response.status_code == 202
        assert (await response.get_json())['task_id'] == 'paper-task'
    asyncio.run(run())
    assert calls == [({'conv_id': 'c'}, 7)]


def test_build_download_range_and_cross_owner_isolation(app, scheduler, tmp_path):
    from lib.paper.builds.service import artifact_url
    receipt, data = _stored(scheduler, tmp_path)
    url = artifact_url(receipt['document_id'], receipt['build_id'], 'document.pdf')
    async def run():
        client = app.test_client()
        assert (await client.get(url)).status_code == 401
        assert (await client.get(url, headers={'X-Test-Owner': '8'})).status_code == 404
        result = await client.get(url, headers={'X-Test-Owner': '7', 'Range': 'bytes=0-99'})
        assert result.status_code == 206
        assert await result.get_data() == data[:100]
        assert result.headers['Content-Range'] == f'bytes 0-99/{len(data)}'
        assert 'private' in result.headers['Cache-Control']
        assert 'public' not in result.headers['Cache-Control']
        one = await client.get(url, headers={'X-Test-Owner': '7', 'Range': 'bytes=0-0'})
        assert one.status_code == 206
        assert await one.get_data() == data[:1]
        assert one.headers['Content-Range'] == f'bytes 0-0/{len(data)}'
        missing = await client.get(url.replace('document.pdf', 'unknown.pdf'), headers={'X-Test-Owner': '7'})
        assert missing.status_code == 404
        invalid = await client.get(url, headers={'X-Test-Owner': '7', 'Range': 'bytes=9999999-'})
        assert invalid.status_code == 416
    asyncio.run(run())


def test_stream_lease_survives_read_and_releases_on_disconnect(app, scheduler, tmp_path):
    from lib.file_serving import send_leased_artifact
    from lib.paper.builds.cache import _file_lock
    receipt, _ = _stored(scheduler, tmp_path)
    path = scheduler.cache._build(7, receipt['document_id'], receipt['build_id']) / '.lease'
    async def run():
        async with app.test_request_context('/download'):
            lease = scheduler.cache.open_artifact(7, receipt['document_id'], receipt['build_id'], 'document.pdf')
            response = await asyncio.to_thread(send_leased_artifact, lease, mimetype='application/pdf')
            with _file_lock(path, wait=False) as acquired:
                assert not acquired
            with pytest.raises(asyncio.CancelledError):
                async with response.response as stream:
                    assert (await anext(stream)).startswith(b'%PDF-')
                    raise asyncio.CancelledError()
            with _file_lock(path, wait=False) as acquired:
                assert acquired
    asyncio.run(run())


def test_reader_reservation_route_requires_owner_scope_and_cannot_revive_a_released_reader(app, scheduler, tmp_path):
    receipt, _ = _stored(scheduler, tmp_path)
    url = f"/api/v1/paper/builds/{receipt['document_id']}/{receipt['build_id']}/viewers"
    payload = {'viewer_id': uuid.uuid4().hex, 'operation': 'retain'}
    async def run():
        client = app.test_client()
        assert (await client.post(url, json=payload)).status_code == 401
        assert (await client.post(url, json=payload, headers={
            'X-Test-Owner': '7', 'X-Test-Scopes': 'read'})).status_code == 403
        assert (await client.post(url, json=payload, headers={'X-Test-Owner': '8'})).status_code == 404
        for operation, retained in [('retain', True), ('renew', True), ('release', False), ('renew', False)]:
            response = await client.post(url, json={**payload, 'operation': operation}, headers={'X-Test-Owner': '7'})
            assert response.status_code == 200
            result = await response.get_json()
            assert result['retained'] is retained
            assert bool(result['expires_in_seconds']) is retained
        assert (await client.post(url, json={**payload, 'viewer_id': '../escape'},
                                  headers={'X-Test-Owner': '7'})).status_code == 400
    asyncio.run(run())


def test_queue_pressure_is_explicit_and_validation_is_bounded(app, monkeypatch):
    from routes.api_v1 import paper_builds
    def busy(*args, **kwargs):
        raise BuildError('build_busy', 'Paper queue is full')
    monkeypatch.setattr(paper_builds, 'start_preview', busy)
    async def run():
        client = app.test_client()
        result = await client.post('/api/v1/paper/builds', json={}, headers={'X-Test-Owner': '7'})
        assert result.status_code == 503
        result = await client.post('/api/v1/paper/builds/tasks/unknown/detach',
                                   json={'subscriber_id': 'x' * 161}, headers={'X-Test-Owner': '7'})
        assert result.status_code == 400
    asyncio.run(run())


def test_failure_carries_specific_code_and_message_not_opaque_marker(app, monkeypatch):
    """Regression: failures must surface their real code + diagnostic message
    (never collapse to the opaque ``paper_build_unavailable`` string) so the
    frontend can render a specific, copyable debug line."""
    from routes.api_v1 import paper_builds
    from lib.storage.errors import StorageError

    def gone(*args, **kwargs):
        raise StorageError('database_not_found', 'Paper document is unavailable')
    monkeypatch.setattr(paper_builds, 'discover_documents', gone)

    def missing(*args, **kwargs):
        raise BuildError('document_not_found', 'no such owned document')
    monkeypatch.setattr(paper_builds, 'select_document', missing)

    async def run():
        client = app.test_client()
        # v4 wire: the frontend reads the RFC 7807 ``code`` + ``detail``.
        storage = await client.post('/api/v4/paper/documents/discover', json={},
                                    headers={'X-Test-Owner': '7'})
        assert storage.status_code == 404
        problem = await storage.get_json()
        assert problem['code'] == 'database_not_found'
        assert 'Paper document is unavailable' in problem['detail']

        build = await client.post('/api/v4/paper/documents/select', json={},
                                  headers={'X-Test-Owner': '7'})
        assert build.status_code == 404
        problem = await build.get_json()
        assert problem['code'] == 'document_not_found'
        assert 'no such owned document' in problem['detail']

        # v1 wire keeps the legacy string body but still surfaces the message.
        legacy = await client.post('/api/v1/paper/documents/discover', json={},
                                   headers={'X-Test-Owner': '7'})
        assert legacy.status_code == 404
        body = await legacy.get_json()
        assert body['ok'] is False
        assert 'Paper document is unavailable' in body['error']
    asyncio.run(run())


def test_v4_json_artifact_keeps_bytes_ranges_and_releases_its_lease(app, scheduler, tmp_path):
    from lib.paper.builds.cache import _file_lock
    body = b'{ "schema_version": 1, "ok": true, "sources": [] }\n'
    receipt, _ = _stored(scheduler, tmp_path, extra_artifact=('sources.json', body))
    url = (f"/api/v4/paper/builds/{receipt['document_id']}/"
           f"{receipt['build_id']}/artifacts/sources.json")
    lock_path = scheduler.cache._build(7, receipt['document_id'], receipt['build_id']) / '.lease'
    async def run():
        client = app.test_client()
        assert (await client.get(url)).status_code == 401
        denied = await client.get(url, headers={'X-Test-Owner': '8'})
        assert denied.status_code == 404
        assert denied.mimetype == 'application/problem+json'
        for headers, status, expected in [
            ({}, 200, body),
            ({'Range': f'bytes=0-{len(body)-1}'}, 206, body),
            ({'Range': 'bytes=0-0'}, 206, body[:1]),
        ]:
            response = await client.get(url, headers={'X-Test-Owner': '7', **headers})
            assert response.status_code == status
            assert response.mimetype == 'application/json'
            assert await response.get_data() == expected
            assert response.headers['Content-Length'] == str(len(expected))
            with _file_lock(lock_path, wait=False) as acquired:
                assert acquired
    asyncio.run(run())
