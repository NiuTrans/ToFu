"""New paper endpoints: inverse/layout/source/arxiv-import HTTP semantics."""
from __future__ import annotations

import asyncio
import gzip
import json
import uuid

import pytest
from quart import Quart, g, request

from lib.api_keys import AuthContext
from lib.paper.builds.compiler import CompiledBuild, artifact_metadata
from lib.paper.builds.errors import BuildError
from tests.support import paper_builds as build_fixtures

pytestmark = pytest.mark.unit
scheduler = build_fixtures.scheduler

_PT = 72.27 / 72


def _sp(bp: float) -> int:
    return int(bp * 65536 * _PT)


def _synctex_bytes() -> bytes:
    raw = ('SyncTeX Version:1\n'
           'Input:1:main.tex\n'
           '{1\n'
           f'(1,5:{_sp(100)},{_sp(190)}:{_sp(300)},{_sp(10)},0\n'
           '}\n'
           '{2\n'
           f'(1,9:{_sp(100)},{_sp(130)}:{_sp(300)},{_sp(12)},0\n'
           '}\n'
           '{3\n'
           f'(1,30:{_sp(100)},{_sp(700)}:{_sp(300)},{_sp(10)},0\n'
           '}\n')
    return gzip.compress(raw.encode())


def _pdf_bytes() -> bytes:
    return b'%%PDF-1.7\n1 0 obj<</MediaBox[0 0 595 842]>>endobj' + b'0' * 64


@pytest.fixture
def app(scheduler, monkeypatch):
    from routes.api_v1 import paper_builds
    from lib.paper.builds import artifact_repository
    from lib.storage.errors import StorageError

    class EmptyRetainedStore:
        def command(self, *args):
            raise StorageError('database_not_found', 'cache-only fixture')

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
    monkeypatch.setattr(paper_builds, 'get_scheduler', lambda: scheduler)
    return application


def _stored_full(scheduler, tmp_path, *, document_id='d' * 64):
    directory = tmp_path / uuid.uuid4().hex
    directory.mkdir()
    files = {'document.pdf': _pdf_bytes(),
             'document.synctex.gz': _synctex_bytes(),
             'sources.json': json.dumps([{'path': 'main.tex', 'sha256': 'a' * 64, 'size_bytes': 3}]).encode(),
             'build.log': b'fixture log'}
    receipt = {'schema_version': 1, 'build_id': uuid.uuid4().hex, 'document_id': document_id,
               'owner_user_id': 7, 'target': 'host', 'source_digest': 'a' * 64,
               'version_verified': True, 'main_file': 'main.tex', 'engine': 'pdflatex',
               'toolchain': 'fixture', 'status': 'success', 'started_at': 1,
               'completed_at': 2, 'artifacts': [], 'diagnostics': []}
    for name, data in files.items():
        path = directory / name
        path.write_bytes(data)
        receipt['artifacts'].append(artifact_metadata(path))
    scheduler.cache.publish(CompiledBuild(receipt, directory))
    return receipt


def test_inverse_location_owner_isolation_and_validation(app, scheduler, tmp_path):
    receipt = _stored_full(scheduler, tmp_path)
    url = f"/api/v1/paper/builds/{receipt['document_id']}/{receipt['build_id']}/location/inverse"

    async def run():
        client = app.test_client()
        assert (await client.post(url, json={'page': 1, 'h': 150, 'v': 195})).status_code == 401
        assert (await client.post(url, json={'page': 1, 'h': 150, 'v': 195},
                                  headers={'X-Test-Owner': '8'})).status_code == 404
        ok = await client.post(url, json={'page': 1, 'h': 150, 'v': 195},
                               headers={'X-Test-Owner': '7'})
        assert ok.status_code == 200
        body = await ok.get_json()
        assert body['file'] == 'main.tex' and body['line'] == 5
        miss = await client.post(url, json={'page': 9, 'h': 1, 'v': 1},
                                 headers={'X-Test-Owner': '7'})
        assert (await miss.get_json())['file'] == ''
        bad = await client.post(url, json={'page': 0}, headers={'X-Test-Owner': '7'})
        assert bad.status_code == 400
    asyncio.run(run())


def test_layout_reports_pages_and_end(app, scheduler, tmp_path):
    from lib.paper.builds.document_repository import PaperDocumentRepository
    root = tmp_path / 'root'
    root.mkdir()
    (root / 'main.tex').write_text('\\documentclass{article}\nHi.\n')
    registered = PaperDocumentRepository(7).register(
        {'source': 'local', 'project_root': str(root), 'path': 'main.tex',
         'main_file': 'main.tex', 'label': 'main.tex'})
    receipt = _stored_full(scheduler, tmp_path,
                           document_id=registered['document_id'])
    url = f"/api/v1/paper/builds/{receipt['document_id']}/{receipt['build_id']}/layout"

    async def run():
        client = app.test_client()
        assert (await client.post(url)).status_code == 401
        ok = await client.post(url, headers={'X-Test-Owner': '7'})
        assert ok.status_code == 200
        body = await ok.get_json()
        assert body['pages'] == 3
        assert body['end']['page'] == 3
        assert body['end']['fullness_page'] is not None
        other = await client.post(url, headers={'X-Test-Owner': '8'})
        assert other.status_code == 404
    asyncio.run(run())


def test_source_read_route_envelope_and_typed_failures(app, monkeypatch):
    import lib.paper.builds.source_read as source_read
    from lib.paper.builds.contract import validate

    def fake(payload, *, owner_user_id):
        validate(payload, 'PaperDocumentSourceRequestV1')
        if payload['path'] == 'missing.tex':
            raise BuildError('document_not_found', 'unavailable')
        if owner_user_id != 7:
            raise BuildError('document_not_found', 'unavailable')
        return {'path': payload['path'], 'content': 'body', 'truncated': False,
                    'size_bytes': 4, 'main_file': 'main.tex'}

    monkeypatch.setattr(source_read, 'read_document_source', fake)

    async def run():
        client = app.test_client()
        url = '/api/v1/paper/documents/source'
        assert (await client.post(url, json={'document_id': 'd' * 64, 'path': 'main.tex'})).status_code == 401
        ok = await client.post(url, json={'document_id': 'd' * 64, 'path': 'main.tex'},
                               headers={'X-Test-Owner': '7'})
        assert ok.status_code == 200
        assert (await ok.get_json())['content'] == 'body'
        missing = await client.post(url, json={'document_id': 'd' * 64, 'path': 'missing.tex'},
                                    headers={'X-Test-Owner': '7'})
        assert missing.status_code == 404
        other = await client.post(url, json={'document_id': 'd' * 64, 'path': 'main.tex'},
                                  headers={'X-Test-Owner': '8'})
        assert other.status_code == 404
        bad = await client.post(url, json={'document_id': 'short', 'path': 'x'},
                                headers={'X-Test-Owner': '7'})
        assert bad.status_code == 400
    asyncio.run(run())


def test_arxiv_import_route_auth_and_typed_errors(app, monkeypatch):
    import lib.paper.builds.imports as imports
    calls = []

    def fake(payload, *, owner_user_id):
        calls.append((payload, owner_user_id))
        if payload['arxiv'] == '0000.00000':
            raise BuildError('arxiv_unavailable', 'arXiv returned HTTP 404')
        return {'directory': 'arxiv-2301.12345', 'files': 2, 'bytes': 100,
                'skipped': [], 'candidates': ['arxiv-2301.12345/main.tex'],
                'document': None}

    monkeypatch.setattr(imports, 'import_arxiv_source', fake)

    async def run():
        client = app.test_client()
        url = '/api/v1/paper/documents/arxiv/import'
        assert (await client.post(url, json={'conv_id': 'c', 'arxiv': '2301.12345'})).status_code == 401
        assert (await client.post(url, json={'conv_id': 'c', 'arxiv': '2301.12345'}, headers={
            'X-Test-Owner': '7', 'X-Test-Scopes': 'read'})).status_code == 403
        ok = await client.post(url, json={'conv_id': 'c', 'arxiv': '2301.12345'},
                               headers={'X-Test-Owner': '7'})
        assert ok.status_code == 200
        assert (await ok.get_json())['files'] == 2
        missing = await client.post(url, json={'conv_id': 'c', 'arxiv': '0000.00000'},
                                    headers={'X-Test-Owner': '7'})
        assert missing.status_code == 400
        assert '404' in (await missing.get_json())['error']
    asyncio.run(run())
    assert calls[0][1] == 7
