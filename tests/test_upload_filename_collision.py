"""tests/test_upload_filename_collision.py — parallel image uploads must NOT
collide on one on-disk filename.

Root cause of the "select two images, only one uploads" bug: the frontend now
fires image uploads in PARALLEL (handleFileUpload → Promise.allSettled). The
base64 upload route minted ``filename = f"{int(time.time()*1000)}{ext}"``. Two
images that hit the SAME millisecond generated the SAME filename → the second
write overwrote the first file, and because the persisted/reloaded message
keeps only ``url`` (base64 is stripped server-side, re-hydrated from url — see
static/js/core/conversations.js:_hydrateImageBase64), BOTH message rows ended
up pointing at the one surviving file → the user saw a single image for two
attachments.

The fix (routes/upload.py) appends ``os.urandom(4).hex()`` to the filename so a
same-millisecond collision is astronomically unlikely.

This test FREEZES time.time() to a constant so both uploads produce the SAME
millisecond prefix — the exact collision condition — and asserts the two
returned URLs are still distinct. A NEUTER control (suffix removed) proves the
random suffix is what prevents the collision.
"""

from __future__ import annotations

import base64
import io
import re

import pytest

pytestmark = pytest.mark.unit


def _tiny_png_bytes() -> bytes:
    """A minimal valid 1x1 PNG (passes the route's magic-bytes check)."""
    try:
        from PIL import Image
    except Exception:
        pytest.skip('Pillow not installed')
    buf = io.BytesIO()
    Image.new('RGB', (1, 1), (123, 222, 64)).save(buf, format='PNG')
    return buf.getvalue()


def _upload(flask_client, b64: str):
    resp = flask_client.post('/api/images/upload',
                             json={'base64': b64, 'mediaType': 'image/png'})
    return resp


@pytest.mark.usefixtures('flask_client')
def test_same_millisecond_uploads_get_distinct_filenames(flask_client, monkeypatch):
    b64 = base64.b64encode(_tiny_png_bytes()).decode()

    # Freeze the clock so both uploads share the SAME millisecond prefix —
    # the precise condition that used to collide.
    import routes.upload as upload_mod
    monkeypatch.setattr(upload_mod.time, 'time', lambda: 1_700_000_000.123)

    r1 = _upload(flask_client, b64)
    r2 = _upload(flask_client, b64)

    assert r1.status_code == 200, r1.get_data(as_text=True)
    assert r2.status_code == 200, r2.get_data(as_text=True)
    j1, j2 = r1.get_json(), r2.get_json()
    assert j1.get('ok') and j2.get('ok')
    # Same-ms prefix, but the random suffix must make the URLs differ.
    assert j1['url'] != j2['url'], (
        f'collision NOT prevented: both uploads got {j1["url"]}')
    # Sanity: both carry the frozen millisecond prefix (1700000000123).
    assert '1700000000123_' in j1['filename']
    assert '1700000000123_' in j2['filename']


@pytest.mark.usefixtures('flask_client')
def test_NC_without_random_suffix_would_collide(flask_client, monkeypatch):
    """NEUTER: strip the random suffix (revert to the buggy pattern) and prove
    two same-millisecond uploads DO collide on one filename — establishing that
    the os.urandom suffix is the load-bearing fix."""
    b64 = base64.b64encode(_tiny_png_bytes()).decode()

    import routes.upload as upload_mod
    monkeypatch.setattr(upload_mod.time, 'time', lambda: 1_700_000_000.123)
    # Neuter the entropy source so the suffix is constant → reproduces the
    # pre-fix single-filename behaviour.
    monkeypatch.setattr(upload_mod.os, 'urandom', lambda n: b'\x00' * n)

    r1 = _upload(flask_client, b64)
    r2 = _upload(flask_client, b64)
    assert r1.status_code == 200 and r2.status_code == 200
    j1, j2 = r1.get_json(), r2.get_json()
    # With entropy neutered, the two filenames are identical — the collision.
    assert j1['url'] == j2['url'], (
        'expected a collision with entropy neutered — the guard must depend '
        'on os.urandom')
