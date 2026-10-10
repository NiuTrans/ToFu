"""POST /api/v1/project/upload-archive: bounded extraction into attached roots."""
from __future__ import annotations

import asyncio
import io
import zipfile

import pytest
from quart import Quart, g, request

from lib.api_keys import AuthContext

pytestmark = pytest.mark.unit


@pytest.fixture
def workspace(tmp_path):
    from lib.project_mod import config as cfg
    from lib.project_mod.scanner import clear_project, set_project_paths
    root = tmp_path / 'root'
    root.mkdir()
    set_project_paths([str(root)])
    yield root
    clear_project()
    cfg._conv_roots.clear()
    cfg._conv_primary.clear()


@pytest.fixture
def app(workspace):
    from routes.api_v1 import project
    application = Quart(__name__, static_folder=None)
    application.config['TESTING'] = True

    @application.before_request
    def principal_fixture():
        g.auth_ctx = AuthContext(key_id='fixture', owner_user_id=7,
                                 scopes=frozenset({'tasks'}))

    application.register_blueprint(project.api_v1_project_bp)
    return application


def _zip_bytes(members) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as bundle:
        for name, data in members:
            bundle.writestr(name, data)
    return buffer.getvalue()



def _post(client, path, form, filename, payload):
    from werkzeug.datastructures import FileStorage
    files = {'file': FileStorage(stream=io.BytesIO(payload), filename=filename)}
    return client.post(path, form=form, files=files)



def test_archive_extracts_and_reports_tex_candidates(app, workspace):
    asyncio.run(_test_archive_extracts(app, workspace))


async def _test_archive_extracts(app, workspace):
    archive = _zip_bytes([('main.tex', '\\documentclass{article}\n\\begin{document}\nHi\n\\end{document}'),
                          ('refs.bib', '@article{x,title={T}}')])
    response = await _post(app.test_client(), '/api/v1/project/upload-archive', {'dir': str(workspace)}, 'paper.zip', archive)
    body = await response.get_json()
    assert response.status_code == 200, body
    payload = body.get('result') or body
    assert payload['ok'] is True
    assert payload['files'] == 2
    assert payload['texMainCandidates'] == ['main.tex']
    tree = workspace / payload['directory']
    assert (tree / 'main.tex').is_file()
    assert (tree / 'refs.bib').is_file()


def test_archive_outside_attached_roots_is_refused(app, tmp_path):
    asyncio.run(_test_outside_roots(app, tmp_path))


async def _test_outside_roots(app, tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    archive = _zip_bytes([('main.tex', 'x')])
    response = await _post(app.test_client(), '/api/v1/project/upload-archive', {'dir': str(outside)}, 'paper.zip', archive)
    body = await response.get_json()
    assert response.status_code == 400
    assert (body.get('error') or body.get('result', {}).get('error'))
    assert list(outside.iterdir()) == []


def test_non_archive_upload_is_a_typed_refusal(app, workspace):
    asyncio.run(_test_non_archive(app, workspace))


async def _test_non_archive(app, workspace):
    response = await _post(app.test_client(), '/api/v1/project/upload-archive',
                           {'dir': str(workspace)}, 'notes.txt', b'plain text')
    body = await response.get_json()
    assert response.status_code == 400
    assert list(workspace.iterdir()) == []


def test_oversized_member_is_refused_without_partial_tree(app, workspace):
    asyncio.run(_test_oversized(app, workspace))


async def _test_oversized(app, workspace):
    from lib.paper.archive import MAX_FILE_BYTES
    big = b'x' * (MAX_FILE_BYTES + 1)
    archive = _zip_bytes([('big.bin', big), ('ok.tex', 'y')])
    response = await _post(app.test_client(), '/api/v1/project/upload-archive', {'dir': str(workspace)}, 'big.zip', archive)
    assert response.status_code == 400
    assert list(workspace.iterdir()) == []
