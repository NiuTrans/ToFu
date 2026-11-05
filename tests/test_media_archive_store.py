"""POST /api/v1/media/attachments/archive: chat-drop archives are stored verbatim.

Unlike the project-browser archive drop, this needs NO pre-attached root — the
archive lands, unextracted, in a fresh directory under the managed chat
workspace (``uploads/workspaces/``) so a 'drop a zip on the composer' works with
no project set and the model unpacks it with its own tools. The workspace root
is monkeypatched to a tmp dir in these tests.
"""
from __future__ import annotations

import asyncio
import io
import zipfile

import pytest
from quart import Quart, g

import lib.chat_workspace as chat_ws
from lib.api_keys import AuthContext

pytestmark = pytest.mark.unit


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / 'workspaces'
    root.mkdir()
    monkeypatch.setattr(chat_ws, 'chat_workspaces_root', lambda: str(root))
    return root


@pytest.fixture
def app(workspace):
    from routes.api_v1 import media
    application = Quart(__name__, static_folder=None)
    application.config['TESTING'] = True

    @application.before_request
    def principal_fixture():
        g.auth_ctx = AuthContext(key_id='fixture', owner_user_id=7,
                                 scopes=frozenset({'tasks'}))

    application.register_blueprint(media.api_v1_media_bp)
    return application


def _zip_bytes(members) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as bundle:
        for name, data in members:
            bundle.writestr(name, data)
    return buffer.getvalue()


async def _post(app, filename, payload):
    from werkzeug.datastructures import FileStorage
    files = {'file': FileStorage(stream=io.BytesIO(payload), filename=filename)}
    response = await app.test_client().post(
        '/api/v1/media/attachments/archive', files=files)
    body = await response.get_json()
    return response.status_code, body.get('result') or body


def test_archive_is_stored_verbatim_without_extraction(app, workspace):
    archive = _zip_bytes([
        ('main.tex', '\\documentclass{article}\n\\begin{document}\nHi\n\\end{document}'),
        ('chapters/intro.tex', 'intro body'),
    ])
    status, payload = asyncio.run(_post(app, 'paper.zip', archive))
    assert status == 200, payload
    assert payload['ok'] is True and payload['action'] == 'store_archive'
    tree = workspace / payload['directory']
    assert payload['path'] == str(tree)
    assert payload['archivePath'] == str(tree / 'paper.zip')
    assert payload['bytes'] == len(archive)
    # Exactly the uploaded bytes and nothing unpacked beside them.
    assert [entry.name for entry in tree.iterdir()] == ['paper.zip']
    assert (tree / 'paper.zip').read_bytes() == archive


def test_repeated_drops_land_in_fresh_directories(app, workspace):
    first = asyncio.run(_post(app, 'paper.tar.gz', b'first bytes'))[1]
    second = asyncio.run(_post(app, 'paper.tar.gz', b'second bytes'))[1]
    assert first['directory'] == 'paper'
    assert second['directory'] == 'paper (1)'
    assert (workspace / 'paper' / 'paper.tar.gz').read_bytes() == b'first bytes'
    assert (workspace / 'paper (1)' / 'paper.tar.gz').read_bytes() == b'second bytes'


def test_client_name_cannot_choose_a_path(app, workspace, tmp_path):
    status, payload = asyncio.run(_post(app, '../../escape.zip', b'zip bytes'))
    assert status == 200, payload
    assert not (tmp_path / 'escape.zip').exists()
    assert (workspace / 'escape' / 'escape.zip').read_bytes() == b'zip bytes'


@pytest.mark.parametrize(('filename', 'payload', 'kind'), [
    ('notes.txt', b'plain text', 'archive-invalid'),
    ('.zip', b'zip bytes', 'archive-invalid'),
    ('empty.zip', b'', 'archive-empty'),
])
def test_refusals_are_typed_and_leave_no_directory(app, workspace, filename, payload, kind):
    status, body = asyncio.run(_post(app, filename, payload))
    assert status == 400
    assert body['ok'] is False and body['errorKind'] == kind
    assert list(workspace.iterdir()) == []


def test_multi_chunk_archive_is_received_completely(app, workspace):
    big = bytes(range(256)) * (12 * 1024 + 1)  # > 3 copy chunks, odd tail
    status, payload = asyncio.run(_post(app, 'big.zip', big))
    assert status == 200
    assert payload['bytes'] == len(big)
    assert (workspace / payload['directory'] / 'big.zip').read_bytes() == big


def test_missing_file_field_is_a_bad_request(app):
    async def exercise():
        response = await app.test_client().post('/api/v1/media/attachments/archive')
        return response.status_code

    assert asyncio.run(exercise()) == 400
