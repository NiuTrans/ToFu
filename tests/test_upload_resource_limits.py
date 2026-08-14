"""Per-image allocation guards under the app-wide large-video body cap."""

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


def test_base64_length_is_rejected_before_decode(flask_client, monkeypatch):
    import routes.upload as upload
    monkeypatch.setattr(upload, '_image_upload_max_bytes', lambda: 1024)
    called = {'decode': False}

    def should_not_decode(_value):
        called['decode'] = True
        raise AssertionError('oversized base64 reached decoder')

    monkeypatch.setattr(upload.base64, 'b64decode', should_not_decode)
    response = flask_client.post('/api/images/upload', json={
        'base64': 'A' * 2000,
        'mediaType': 'image/png',
    })
    assert response.status_code == 413
    assert called['decode'] is False


def test_multipart_reader_stops_at_limit_plus_one(flask_client, monkeypatch):
    import routes.upload as upload
    raw = _png()
    monkeypatch.setattr(upload, '_image_upload_max_bytes', lambda: 32)
    response = flask_client.post(
        '/api/images/upload',
        form={},
        files={'file': FileStorage(
            stream=io.BytesIO(raw), filename='safe.png',
            content_type='image/png')},
    )
    assert response.status_code == 413


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
