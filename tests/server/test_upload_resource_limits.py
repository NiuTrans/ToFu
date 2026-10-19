"""Image source admission, streamed persistence and decode validation."""

from __future__ import annotations

import base64
import io

import pytest
from werkzeug.datastructures import FileStorage

pytestmark = pytest.mark.unit


def _png(width=10, height=10):
    Image = pytest.importorskip('PIL.Image')
    buf = io.BytesIO()
    Image.new('RGB', (width, height), (1, 2, 3)).save(buf, format='PNG')
    return buf.getvalue()


def test_large_base64_and_multipart_sources_preserve_complete_bmp_bytes(flask_client,monkeypatch,tmp_path):
    import hashlib
    import routes.upload as upload
    Image = pytest.importorskip('PIL.Image')
    buffer = io.BytesIO()
    Image.new('RGB',(3500,3500),(1,2,3)).save(buffer,format='BMP')
    raw = buffer.getvalue()
    assert len(raw)>32*1024*1024
    monkeypatch.setattr(upload,'UPLOAD_DIR',str(tmp_path))
    response = flask_client.post('/api/images/upload',json={
        'base64':base64.b64encode(raw).decode('ascii'),'mediaType':'image/bmp'})
    assert response.status_code==200
    first = response.get_json()['filename']
    response = flask_client.post('/api/images/upload',form={},files={'file':FileStorage(
        stream=io.BytesIO(raw),filename='large.bmp',content_type='image/bmp')})
    assert response.status_code==200
    second = response.get_json()['filename']
    assert first!=second
    expected=hashlib.sha256(raw).digest()
    for name in [first,second]:
        assert hashlib.sha256((tmp_path/name).read_bytes()).digest()==expected
    print(f'image_source_demand: {len(raw)} bytes, exact JSON and Multipart BMP persistence')


def test_preview_decode_uses_seekable_source_without_whole_source_read(monkeypatch):
    import routes.upload as upload
    class BoundedReads(io.BytesIO):
        def read(self,size=-1):
            assert size!=-1,'whole source materialization is forbidden'
            return super().read(size)
    source=BoundedReads(_png()+b'x'*(33*1024*1024))
    assert upload._upload_image_dimension_error(source) is None
    preview,extension,info=upload._shrink_upload_image(source,'png')
    assert info['shrunk'] is True and isinstance(preview,bytes)
    assert len(preview)<33*1024*1024


def test_invalid_base64_and_svg_still_fail_closed(flask_client):
    response=flask_client.post('/api/images/upload',json={'base64':'%%%%','mediaType':'image/png'})
    assert response.status_code==400
    response=flask_client.post('/api/images/upload',json={'base64':'eA==','mediaType':'image/svg+xml'})
    assert response.status_code==400


def test_pixel_bomb_is_rejected_before_full_decode(flask_client, monkeypatch):
    import routes.upload as upload
    raw = _png(20, 20)
    # Helper clamps operator config to >=1M, so patch its resolved policy seam.
    monkeypatch.setattr(upload, '_image_upload_max_pixels', lambda: 100)
    response = flask_client.post('/api/images/upload', json={
        'base64': base64.b64encode(raw).decode('ascii'),
        'mediaType': 'image/png',
    })
    assert response.status_code == 413


def test_multipart_filename_is_sanitized(flask_client, monkeypatch, tmp_path):
    import routes.upload as upload
    monkeypatch.setattr(upload, 'UPLOAD_DIR', str(tmp_path))
    response = flask_client.post(
        '/api/images/upload',
        form={},
        files={'file': FileStorage(
            stream=io.BytesIO(_png()), filename='../../unsafe name.png',
            content_type='image/png')},
    )
    assert response.status_code == 200
    filename = response.get_json()['filename']
    assert '/' not in filename and '\\' not in filename and '..' not in filename
    assert (tmp_path / filename).is_file()


def test_partial_image_write_failure_returns_error_and_removes_output(flask_client, monkeypatch, tmp_path):
    import routes.upload as upload
    monkeypatch.setattr(upload, 'UPLOAD_DIR', str(tmp_path))
    def failed_write(source, output):
        output.write(b'partial image')
        raise OSError('injected storage failure')
    monkeypatch.setattr(upload, '_write_image_source', failed_write)
    response = flask_client.post('/api/images/upload', json={
        'base64': base64.b64encode(_png()).decode('ascii'), 'mediaType': 'image/png'})
    assert response.status_code == 500
    assert list(tmp_path.iterdir()) == []
