"""Executable file policy over a disposable real artifact Sidecar."""
import asyncio
import hashlib
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import uuid
import zipfile

import pytest
from quart import Quart, g, request
from jsonschema import Draft202012Validator
from lib import response_assets
from lib.artifacts import response_files as service
from lib.artifacts import core
from routes.artifacts import artifacts_bp
from routes.api_v1.artifacts import api_v1_artifacts_bp
from routes.response_assets import response_assets_bp

pytestmark = pytest.mark.unit
pytest_plugins = ('tests._artifact_sidecar',)


@pytest.fixture
def owned(artifact_storage, monkeypatch):
    live = {'owned': 7, 'other': 8}
    monkeypatch.setattr(response_assets, 'get_conversation', lambda conv, user_id, **kw:
                        {'id': conv} if live.get(conv) == user_id else None)
    return live


@pytest.fixture
def app(owned):
    app = Quart(__name__)
    app.register_blueprint(artifacts_bp)
    app.register_blueprint(api_v1_artifacts_bp)
    app.register_blueprint(response_assets_bp)
    @app.before_request
    async def identity():
        owner = request.headers.get('X-Test-Owner')
        if owner:
            g.auth_ctx = SimpleNamespace(is_authenticated=True, owner_user_id=int(owner),
                                         key_id='test-owner', tenant_id=None, scopes=[])
    return app


def publish(tmp_path, name, data, **kwargs):
    path = tmp_path / name
    path.write_bytes(data)
    return service.publish_file(str(path), '', conv_id='owned', user_id=7, roots=[str(tmp_path)], **kwargs)


def test_large_self_contained_prototype_receipt_and_source_removal(owned, tmp_path):
    data = (b'<!doctype html><html><head><style>body{color:#123}</style></head><body>'
            + b'<p>prototype</p>' * 220200 + b'</body></html>')
    assert 3 * 1024 * 1024 < len(data)
    a = publish(tmp_path, 'prototype.html', data, task_id='task-file')
    b = service.publish_response_file(str(tmp_path / 'prototype.html'), 'new title', conv_id='owned', user_id=7, roots=[str(tmp_path)])
    assert a['id'] == b['id'] == a['artifactId']
    assert a['format'] == 'html' and a['artifact']['task_id'] == 'task-file'
    assert a['sizeBytes'] == len(data) and a['sha256'] == hashlib.sha256(data).hexdigest()
    assert a['rawUrl'] == a['url'] == f'/api/artifacts/{a["id"]}/raw'
    assert a['downloadUrl'] == f'/api/artifacts/{a["id"]}/download'
    assert 'ownerUserId' not in json.dumps(a) and str(tmp_path) not in json.dumps(a)
    Draft202012Validator(service.contract()).validate(a)

    assert len(json.dumps(a).encode('utf-8')) < 4096
    assert 'content' not in a['artifact'] and 'prototype</p>' not in json.dumps(a)
    (tmp_path / 'prototype.html').unlink()
    actual, info = service.load_file(a['id'], user_id=7)
    assert actual == data and info['sha256'] == a['sha256']
    from lib.artifacts.events import _build_event
    event = _build_event(a['artifact'])
    assert event['id'] == a['artifactId'] and event['task_id'] == 'task-file'
    assert 'content' not in event


@pytest.mark.parametrize('name,fmt,mime,data', [
    ('report.md', 'markdown', 'text/markdown', '# Résumé\n'.encode()),
    ('note.txt', 'markdown', 'text/plain', b'plain text\r\n'),
    ('chart.svg', 'svg', 'image/svg+xml', b'<svg xmlns="http://www.w3.org/2000/svg"><text>chart</text></svg>'),
    ('paper.pdf', 'file', 'application/pdf', b'%PDF-1.7\n%binary\x00\xff\n%%EOF'),
])
def test_mime_bytes_and_safe_public_fields(owned, tmp_path, name, fmt, mime, data):
    receipt = publish(tmp_path, name, data)
    assert receipt['format'] == fmt and receipt['mime'] == mime
    row = core.get_artifact(receipt['id'], file_access=True)
    safe = core.public_meta(row)
    assert safe['meta']['responseFile'] == receipt['meta']['responseFile']
    assert 'ownerUserId' not in json.dumps(safe)
    assert 'content' not in safe
    if fmt == 'file':
        assert not core.get_artifact(receipt['id'])['content']
        assert core.get_artifact_meta(receipt['id'])['format'] == 'file'
        assert 'rawUrl' not in receipt and receipt['url'] == receipt['downloadUrl']
    (tmp_path / name).unlink()
    assert service.load_file(receipt['id'], user_id=7)[0] == data


def package(members):
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as z:
        for name, content in members.items():
            # Fixed ZIP metadata keeps parametrized IDs identical across workers.
            z.writestr(zipfile.ZipInfo(name), content)
    return out.getvalue()


@pytest.mark.parametrize('ext,members', [
    ('zip', {'hello.txt': b'hello'}),
    ('docx', {'[Content_Types].xml': b'<Types/>', 'word/document.xml': b'<document/>'}),
    ('xlsx', {'[Content_Types].xml': b'<Types/>', 'xl/workbook.xml': b'<workbook/>'}),
    ('pptx', {'[Content_Types].xml': b'<Types/>', 'ppt/presentation.xml': b'<presentation/>'}),
    ('odt', {'mimetype': b'application/vnd.oasis.opendocument.text', 'content.xml': b'<content/>'}),
    ('doc', None), ('xls', None), ('ppt', None),
])
def test_common_binary_attachments(owned, tmp_path, ext, members):
    data = package(members) if members else bytes.fromhex('d0cf11e0a1b11ae1') + ext.encode()
    receipt = publish(tmp_path, 'attachment.' + ext, data)
    Draft202012Validator(service.contract()).validate(receipt)
    assert receipt['format'] == 'file'
    assert service.load_file(receipt['id'], user_id=7)[0] == data


@pytest.mark.parametrize('user,conv', [(0, 'owned'), (True, 'owned'), (8, 'owned'), (7, ''), (7, 'missing')])
def test_explicit_owner_and_live_conversation_required(owned, tmp_path, user, conv):
    path = tmp_path / 'x.md'
    path.write_text('x')
    with pytest.raises(PermissionError):
        service.publish_file(str(path), '', conv_id=conv, user_id=user, roots=[str(tmp_path)])


@pytest.mark.parametrize('name,data', [('bad.txt', b'\xff'), ('bad.md', b'\xc0'), ('bad.pdf', b'html'),
                                     ('bad.zip', b'PKbroken'), ('bad.docx', package({'x': b'x'})),
                                     ('bad.png', b'png'), ('bad.py', b'print(1)')])
def test_invalid_utf8_mime_and_unsupported_types(owned, tmp_path, name, data):
    with pytest.raises(ValueError):
        publish(tmp_path, name, data)


def test_no_roots_traversal_symlinks_and_fifo(tmp_path):
    root = tmp_path / 'root'
    root.mkdir()
    file = root / 'file.md'
    file.write_bytes(b'text')
    for roots in ([], ['.']):
        with pytest.raises(PermissionError):
            service.read_project_file(str(file), roots)
    with pytest.raises(PermissionError):
        service.read_project_file(str(tmp_path / 'outside.md'), [str(root)])
    with pytest.raises(ValueError):
        service.read_project_file('../root/file.md', [str(root)])
    (root / 'link.md').symlink_to(file)
    (root / 'dirlink').symlink_to(root, target_is_directory=True)
    os.mkfifo(root / 'fifo.md')
    linkedroot = tmp_path / 'linkedroot'
    linkedroot.symlink_to(root, target_is_directory=True)
    ancestor = tmp_path / 'ancestor'
    ancestor.symlink_to(tmp_path, target_is_directory=True)
    for path, roots in [(root / 'link.md', [root]), (root / 'dirlink/file.md', [root]),
                        (root / 'fifo.md', [root]), (linkedroot / 'file.md', [linkedroot]),
                        (ancestor / 'root/file.md', [ancestor / 'root'])]:
        with pytest.raises((OSError, ValueError)):
            service.read_project_file(str(path), [str(r) for r in roots])
    assert service.read_project_file('file.md', [str(root)]) == b'text'


@pytest.mark.parametrize('mutation', ['grow', 'replace', 'rename_root', 'chmod'])
def test_changed_file_and_path_race_refused(tmp_path, monkeypatch, mutation):
    root = tmp_path / 'race'
    root.mkdir()
    path = root / 'file.md'
    path.write_bytes(b'original')
    read = os.read
    changed = False
    def racing_read(fd, size):
        nonlocal changed
        data = read(fd, size)
        if not changed:
            changed = True
            if mutation == 'grow':
                path.write_bytes(b'changed and grown')
            elif mutation == 'chmod':
                path.chmod(path.stat().st_mode ^ 0o100)
            elif mutation == 'replace':
                new = root / 'new.md'
                new.write_bytes(b'original')
                os.replace(new, path)
            else:
                root.rename(tmp_path / 'old')
                root.mkdir()
        return data
    monkeypatch.setattr(service.os, 'read', racing_read)
    with pytest.raises(ValueError):
        service.read_project_file(str(path), [str(root)])


def test_large_binary_envelope_and_document_survive_source_removal(owned, tmp_path):
    from io import BytesIO
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, NameObject
    writer = PdfWriter()
    page = writer.add_blank_page(width=100, height=100)
    stream = DecodedStreamObject()
    stream.set_data(b'q\n' + b' ' * (9 * 1024 * 1024) + b'\nQ')
    page[NameObject('/Contents')] = writer._add_object(stream)
    buffer = BytesIO();writer.write(buffer);pdf = buffer.getvalue()
    for name, data in [('large.pdf', pdf), ('large.md', b'# Full document\n' + b'x' * (9 * 1024 * 1024) + b'\nlate')]:
        receipt = publish(tmp_path, name, data)
        (tmp_path / name).unlink()
        row = core.get_artifact(receipt['id'])
        recovered, info = service.load_file(receipt['id'], user_id=7)
        assert recovered == data
        assert receipt['sizeBytes'] == len(data)
        assert receipt['sha256'] == hashlib.sha256(data).hexdigest()


def test_repeat_dedupe_retains_first_filename_and_refuses_collisions(owned, tmp_path):
    data = ('# unique ' + str(uuid.uuid4())).encode()
    a = publish(tmp_path, 'first.md', data)
    b = publish(tmp_path, 'renamed.md', data)
    assert a['id'] == b['id'] and b['filename'] == 'first.md'
    with pytest.raises(ValueError, match='incompatible'):
        publish(tmp_path, 'different.txt', data)
    with pytest.raises(ValueError, match='incompatible'):
        core.create_artifact(conv_id='owned', content=data.decode(), format='markdown', source='write_file')
    legacy = '# legacy ' + str(uuid.uuid4())
    row = core.create_artifact(conv_id='owned', content=legacy, format='markdown', source='write_file')
    with pytest.raises(ValueError, match='incompatible'):
        publish(tmp_path, 'legacy.md', legacy.encode())
    assert core.get_artifact(row['id'])['source'] == 'write_file'
    binary = b'%PDF-' + str(uuid.uuid4()).encode()
    legacy_binary = core.create_artifact(conv_id='owned', content=service.envelope(binary), format='markdown', source='inline_fence')
    with pytest.raises(ValueError, match='incompatible'):
        publish(tmp_path, 'collision.pdf', binary)
    assert core.get_artifact(legacy_binary['id'])['source'] == 'inline_fence'


def test_http_owner_auth_missing_conv_raw_and_download(app, owned, tmp_path):
    html = publish(tmp_path, '交付.html', b'<html><script>alert(1)</script><body>prototype</body></html>')
    binary = publish(tmp_path, 'receipt.pdf', b'%PDF-1.7\x00\xff%%EOF')
    legacy = core.create_artifact(conv_id='owned', content='<html>legacy-secret</html>', format='html', source='write_file')
    h7, h8 = {'X-Test-Owner': '7'}, {'X-Test-Owner': '8'}
    async def exercise():
        client = app.test_client()
        for aid in (html['id'], binary['id'], legacy['id']):
            for suffix in ('raw', 'view', 'download', 'export'):
                url = f'/api/artifacts/{aid}/{suffix}'
                assert (await client.get(url)).status_code == 401
                assert (await client.get(url, headers=h8)).status_code == 404
            assert (await client.get(f'/api/v1/artifacts/{aid}', headers=h8)).status_code == 404
            assert (await client.get(f'/api/v1/artifacts/{aid}/versions', headers=h8)).status_code == 404
            assert (await client.post(f'/api/v1/artifacts/{aid}/pin', headers=h8, json={'pinned': True})).status_code == 404
            assert (await client.delete(f'/api/v1/artifacts/{aid}', headers=h8)).status_code == 404
        assert (await client.get('/api/v1/artifacts?conv=owned', headers=h8)).status_code == 404
        library = await client.get('/api/v1/artifacts', headers=h8)
        assert library.status_code == 200 and b'legacy-secret' not in await library.get_data()
        for suffix in ('raw', 'view', 'export'):
            assert (await client.get(f'/api/artifacts/{binary["id"]}/{suffix}', headers=h7)).status_code == 404
        meta = await client.get(f'/api/v1/artifacts/{binary["id"]}', headers=h7)
        assert meta.status_code == 200
        assert b'base64' not in await meta.get_data() and b'ownerUserId' not in await meta.get_data()
        raw = await client.get(html['rawUrl'], headers=h7)
        assert raw.status_code == 200 and raw.content_type.startswith('text/html')
        assert 'sandbox' in raw.headers['Content-Security-Policy']
        assert 'allow-scripts' not in raw.headers['Content-Security-Policy']

        export = await client.get(f'/api/artifacts/{html["id"]}/export', headers=h7)
        assert export.status_code == 400
        assert b'response_file_markup_pdf_export_unavailable' in await export.get_data()
        inert = await client.get(html['downloadUrl'], headers=h7)
        assert inert.status_code == 200 and inert.content_type == 'text/plain'
        assert '.html.txt' in inert.headers['Content-Disposition']
        active = await client.get(html['interactiveDownloadUrl'], headers=h7)
        assert active.content_type == 'text/html' and 'execute scripts' in active.headers['Warning']
        assert await active.get_data() == await inert.get_data() == await raw.get_data()
        (tmp_path / 'receipt.pdf').unlink()
        download = await client.get(binary['downloadUrl'], headers=h7)
        assert download.status_code == 200 and download.content_type == 'application/pdf'
        assert await download.get_data() == b'%PDF-1.7\x00\xff%%EOF'
        assert download.headers['X-Content-Type-Options'] == 'nosniff'
        assert download.headers['Cache-Control'] == 'private, no-store'
        assert download.headers['Content-Disposition'].startswith('attachment;')
        action = f'/api/response-assets/artifacts/{binary["id"]}?conversationId=owned&download=1'
        assert await (await client.get(action, headers=h7)).get_data() == b'%PDF-1.7\x00\xff%%EOF'
        assert (await client.get(action.replace('owned', 'other'), headers=h7)).status_code == 404
        owned.pop('owned')
        for aid in (html['id'], binary['id'], legacy['id']):
            assert (await client.get(f'/api/artifacts/{aid}/download', headers=h7)).status_code == 404
            assert (await client.get(f'/api/artifacts/{aid}/raw', headers=h7)).status_code == 404
            assert (await client.get(f'/api/v1/artifacts/{aid}', headers=h7)).status_code == 404
    asyncio.run(exercise())


@pytest.mark.parametrize('when', ['before', 'during', 'before_commit'])
def test_cancelled_publication_never_writes(owned, tmp_path, monkeypatch, when):
    path = tmp_path / 'cancel.md'
    path.write_bytes(b'x' * 200000)
    writes, state = [], {'cancelled': when == 'before'}
    monkeypatch.setattr(core, 'create_artifact', lambda **kw: writes.append(kw))
    if when == 'during':
        read = os.read
        def interrupted(fd, size):
            data = read(fd, size)
            state['cancelled'] = True
            return data
        monkeypatch.setattr(service.os, 'read', interrupted)
    if when == 'before_commit':
        inspect = service.file_info
        def cancel_after_validation(*args):
            result = inspect(*args)
            state['cancelled'] = True
            return result
        monkeypatch.setattr(service, 'file_info', cancel_after_validation)
    with pytest.raises(InterruptedError):
        service.publish_file(str(path), '', conv_id='owned', user_id=7, roots=[str(tmp_path)], cancelled=lambda: state['cancelled'])
    assert writes == []


def test_zip_inconsistent_member_count_before_allocation(monkeypatch):
    import struct
    many = bytearray(package({'ok': b'hello'}))
    end = many.rfind(b'PK\x05\x06')
    struct.pack_into('<HH', many, end + 8, 10001, 10001)
    called = []
    monkeypatch.setattr(service.zipfile, 'ZipFile', lambda *args: called.append(args))
    with pytest.raises(ValueError, match='member count mismatch'):
        service.file_info(bytes(many), 'many.zip')
    assert called == []


@pytest.mark.parametrize('mutation', ['mime', 'hash', 'missing_info', 'info_type', 'kind'])
def test_corrupt_file_provenance_never_served(app, owned, tmp_path, monkeypatch, mutation):
    from lib import storage
    receipt = publish(tmp_path, 'corrupt.html', b'<html>owned body</html>')
    real = storage.get_storage_client()
    row = real.query('artifact.get', {'artifact_id': receipt['id'], 'include_content': True})
    row = json.loads(json.dumps(row))
    if mutation == 'mime':
        row['meta']['responseFile']['mime'] = 'text/html\r\nX-Evil: 1'
    if mutation == 'hash':
        row['meta']['responseFile']['sha256'] = '0' * 64
    if mutation == 'missing_info':
        row['meta'] = {}
    if mutation == 'info_type':
        row['meta']['responseFile'] = 'broken'
    if mutation == 'kind':
        row['meta']['responseFile']['kind'] = 'attachment'
    class ReadAdapter:
        def query(self, method, args):
            if method == 'artifact.get' and args['artifact_id'] == receipt['id']:
                return row
            return real.query(method, args)
    monkeypatch.setattr(storage, 'get_storage_client', lambda **kw: ReadAdapter())
    async def exercise():
        client = app.test_client()
        for suffix in ('raw', 'view', 'download', 'export'):
            response = await client.get(f'/api/artifacts/{receipt["id"]}/{suffix}', headers={'X-Test-Owner': '7'})
            assert response.status_code == 404
            assert b'owned body' not in await response.get_data()
    asyncio.run(exercise())


def test_missing_http_principal_is_denied_even_in_multiuser(owned, tmp_path, monkeypatch):
    from lib import auth_mode
    monkeypatch.setattr(auth_mode, 'is_multi_user', lambda: True)
    row = publish(tmp_path, 'owned.md', b'# owned')
    app = Quart(__name__)
    async def exercise():
        async with app.test_request_context('/api/artifacts/' + row['id']):
            with pytest.raises(core.ArtifactNotFoundError):
                core.get_artifact(row['id'])
            g.auth_ctx = SimpleNamespace(is_authenticated=True, owner_user_id=None, scopes=[])
            with pytest.raises(core.ArtifactNotFoundError):
                core.get_artifact_meta(row['id'])
    asyncio.run(exercise())


@pytest.mark.parametrize('kind', ['html', 'pdf', 'legacy_html'])
def test_delivery_uses_one_body_query(app, owned, tmp_path, monkeypatch, kind):
    from lib import storage
    data = (b'%PDF-1.7' if kind == 'pdf' else b'<html>') + kind.encode() + b'x' * (3 * 1024 * 1024)
    if kind == 'legacy_html':
        artifact = core.create_artifact(conv_id='owned', content=data.decode(), format='html', source='inline_fence')
    else:
        artifact = publish(tmp_path, 'one-body.' + kind, data)
    real = storage.get_storage_client()
    reads = []
    class MeteredClient:
        def query(self, method, args):
            if method == 'artifact.get' and args.get('include_content'):
                reads.append(args['artifact_id'])
            return real.query(method, args)
    monkeypatch.setattr(storage, 'get_storage_client', lambda **kw: MeteredClient())
    async def exercise():
        client = app.test_client()
        urls = [f'/api/artifacts/{artifact["id"]}/download',
                f'/api/response-assets/artifacts/{artifact["id"]}?conversationId=owned&download=1']
        if kind != 'pdf':
            urls += [f'/api/artifacts/{artifact["id"]}/raw', f'/api/artifacts/{artifact["id"]}/view']
        for url in urls:
            reads.clear()
            response = await client.get(url, headers={'X-Test-Owner': '7'})
            assert response.status_code == 200
            assert reads == [artifact['id']]
        reads.clear()
        action = await client.get(f'/api/response-assets/artifacts/{artifact["id"]}?conversationId=owned', headers={'X-Test-Owner': '7'})
        assert action.status_code == 200 and reads == []
    asyncio.run(exercise())
    reads.clear()
    for owner in (None, 0, True):
        with pytest.raises(core.ArtifactNotFoundError):
            service.download_payload(artifact['id'], user_id=owner)
    assert reads == []


def test_zip_complete_large_directory_publish_and_source_removal(owned, tmp_path):
    # Both retired admission limits are crossed by a real, coherent directory.
    members = {f'{i:05d}-' + 'x' * 180: b'' for i in range(10001)}
    data = package(members)
    import struct
    end = data.rfind(b'PK\x05\x06')
    assert struct.unpack_from('<L', data, end + 12)[0] > 2 * 1024 * 1024
    receipt = publish(tmp_path, 'complete.zip', data)
    (tmp_path / 'complete.zip').unlink()
    restored, info = service.load_file(receipt['id'], user_id=7)
    assert restored == data
    assert info['sha256'] == hashlib.sha256(data).hexdigest()
    with zipfile.ZipFile(io.BytesIO(restored)) as archive:
        assert set(archive.namelist()) == set(members)
        assert archive.read(next(reversed(members))) == b''


def test_zip_member_extent_checked_before_allocation(monkeypatch):
    import struct
    data = bytearray(package({'hello': b'body'}))
    cursor = data.index(b'PK\x01\x02')
    struct.pack_into('<H', data, cursor + 28, 65535)
    monkeypatch.setattr(service.zipfile, 'ZipFile', lambda *a: pytest.fail('unverified allocation'))
    with pytest.raises(ValueError, match='beyond central directory'):
        service.file_info(bytes(data), 'invalid.zip')


@pytest.mark.parametrize('value', [b'application/vnd.oasis.opendocument.textX', b'x' * 1000000], ids=['wrong-suffix', 'large-wrong-type'])
def test_odt_mimetype_requires_exact_declared_type(value):
    with pytest.raises(ValueError, match='type mismatch'):
        service.file_info(package({'mimetype': value, 'content.xml': b'<content/>'}), 'wrong.odt')
