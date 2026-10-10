"""POST /api/v1/media/attachments/expand-archive: chat-drop archive extraction.

Unlike the project-browser archive drop, this needs NO pre-attached root — the
archive lands in the managed chat workspace (``uploads/workspaces/``) so a
ChatGPT-style 'drop a zip on the composer' works with no project set. The
workspace root is monkeypatched to a tmp dir in these tests.
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


def _post(client, filename, payload):
    from werkzeug.datastructures import FileStorage
    files = {'file': FileStorage(stream=io.BytesIO(payload), filename=filename)}
    return client.post(
        '/api/v1/media/attachments/expand-archive', files=files)


def test_extracts_without_attached_project_and_reports_tex(app, workspace):
    asyncio.run(_test_extracts(app, workspace))


async def _test_extracts(app, workspace):
    archive = _zip_bytes([
        ('main.tex',
         '\\documentclass{article}\n\\begin{document}\nHi\n\\end{document}'),
        ('chapters/intro.tex', 'intro body'),
        ('refs.bib', '@article{x,title={T}}'),
    ])
    response = await _post(app.test_client(), 'paper.zip', archive)
    body = await response.get_json()
    assert response.status_code == 200, body
    payload = body.get('result') or body
    assert payload['ok'] is True
    assert payload['action'] == 'expand_archive'
    assert payload['files'] == 3
    assert payload['texMainCandidates'] == ['main.tex']
    # Lands under the managed workspace root, never merging into an existing dir.
    tree = workspace / payload['directory']
    assert (tree / 'main.tex').is_file()
    assert (tree / 'chapters' / 'intro.tex').is_file()
    assert (tree / 'refs.bib').is_file()
    assert payload['path'] == str(tree)


def test_repeated_drops_never_overwrite(app, workspace):
    asyncio.run(_test_repeated(app, workspace))


async def _test_repeated(app, workspace):
    archive = _zip_bytes([('main.tex', 'x')])
    first = await (await _post(app.test_client(), 'paper.zip', archive)).get_json()
    second = await (await _post(app.test_client(), 'paper.zip', archive)).get_json()
    p1 = (first.get('result') or first)['directory']
    p2 = (second.get('result') or second)['directory']
    assert p1 != p2
    assert (workspace / p1).is_dir()
    assert (workspace / p2).is_dir()


def test_non_archive_is_a_typed_refusal(app, workspace):
    asyncio.run(_test_non_archive(app, workspace))


async def _test_non_archive(app, workspace):
    response = await _post(app.test_client(), 'notes.txt', b'plain text')
    body = await response.get_json()
    assert response.status_code == 400
    assert (body.get('error') or body.get('result', {}).get('error'))
    # Nothing extracted into the workspace on a refusal.
    assert list(workspace.iterdir()) == []


def test_traversal_member_cannot_escape_workspace(app, workspace, tmp_path):
    asyncio.run(_test_traversal(app, workspace, tmp_path))


async def _test_traversal(app, workspace, tmp_path):
    # The extractor neutralizes '..' by dropping the traversal segments, so a
    # '../../evil.tex' member can never escape — it lands sanitized INSIDE the
    # workspace (the whole archive is not refused). This is the zip-slip
    # invariant that matters: nothing is ever written outside the workspace.
    archive = _zip_bytes([('../../evil.tex', 'x'), ('ok.tex', 'y')])
    response = await _post(app.test_client(), 'slip.zip', archive)
    body = await response.get_json()
    assert response.status_code == 200, body
    payload = body.get('result') or body
    tree = workspace / payload['directory']
    assert not (tmp_path / 'evil.tex').exists()          # no escape
    assert (tree / 'evil.tex').is_file()                  # sanitized landing
    assert (tree / 'ok.tex').is_file()


def test_nul_named_member_cannot_escape(app, workspace, tmp_path):
    asyncio.run(_test_nul(app, workspace, tmp_path))


async def _test_nul(app, workspace, tmp_path):
    # Python's zipfile truncates a member name at the first NUL on write, so
    # 'bad\x00name.tex' is stored as plain 'bad' — the extractor never sees the
    # NUL and the (now harmless) member lands safely inside the workspace. The
    # security invariant is simply that nothing escapes the workspace.
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr('good.tex', 'y')
        bundle.writestr(zipfile.ZipInfo('bad\x00name.tex'), 'x')
    response = await _post(app.test_client(), 'nul.zip', buffer.getvalue())
    body = await response.get_json()
    assert response.status_code == 200, body
    payload = body.get('result') or body
    tree = workspace / payload['directory']
    assert not (tmp_path / 'bad').exists()
    assert (tree / 'bad').is_file()


def test_oversized_member_is_refused_without_partial_tree(app, workspace):
    asyncio.run(_test_oversized(app, workspace))


async def _test_oversized(app, workspace):
    from lib.paper.archive import MAX_FILE_BYTES
    big = b'x' * (MAX_FILE_BYTES + 1)
    archive = _zip_bytes([('big.bin', big), ('ok.tex', 'y')])
    response = await _post(app.test_client(), 'big.zip', archive)
    assert response.status_code == 400
    # Atomic extraction: a failed archive leaves no half-written tree.
    assert list(workspace.iterdir()) == []


def test_missing_file_field_is_a_bad_request(app):
    asyncio.run(_test_missing(app))


async def _test_missing(app):
    response = await app.test_client().post(
        '/api/v1/media/attachments/expand-archive')
    assert response.status_code == 400
