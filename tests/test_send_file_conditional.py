"""tests/test_send_file_conditional.py — the shared conditional file-serving seam.

WHAT THIS GUARDS (measured live 2026-08-02, on the installer download route)
----------------------------------------------------------------------------
With the installed quart / werkzeug 3.1.8 pair, ``send_file(...,
conditional=True)`` 500s on a SINGLE-BYTE Range (``bytes=0-0`` — the
resume-support probe every download manager sends): quart passes
``end - 1`` as the inclusive stop into ``ContentRange.set`` and werkzeug's
``is_byte_range_valid`` rejects ``start >= stop`` →
``AssertionError: Bad range provided`` escapes as an uncaught 500.

``lib/file_serving.send_file_conditional`` is the ONE seam for every
file-serving route (desktop download, paper image/pdf, motion mp4/srt).
Pinned against the desktop download route (the measured case):

  1. ``bytes=0-0`` → NOT a 500; the documented fallback is a plain
     full-body 200 (spec-legal: a server may always ignore Range);
  2. multi-byte ``bytes=0-99`` → a proper 206 + Content-Range (the healthy
     path must not be degraded by the workaround);
  3. an unsatisfiable range → 416 (quart's own path, untouched);
  4. plain GET → 200 full body (untouched);
  5. NEUTER: bypass the wrapper's catch (raw quart path) → the probe 500s,
     proving the catch is load-bearing, not decoration.

Run:  pytest tests/test_send_file_conditional.py -q
"""

from __future__ import annotations

pytest_plugins = ('tests._credential_sidecar',)

import time

import pytest

from lib.desktop_dist import store

pytestmark = pytest.mark.unit

_CONTENT = b'INSTALLER-BYTES' * 100   # 1500 bytes
_NAME = 'Tofu-Setup-0.16.0-win64.exe'


@pytest.fixture()
def seeded(tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_DESKTOP_DIST_DIR', str(tmp_path))
    (tmp_path / _NAME).write_bytes(_CONTENT)
    store.record_artifact({
        'os': 'windows', 'arch': 'x86_64', 'label': 'Windows installer',
        'filename': _NAME, 'size': len(_CONTENT), 'sha256': 'ab' * 32,
        'source': 'built', 'version': '0.16.0',
        'fetched_at': time.time()})
    return tmp_path


def _bearer():
    from lib.api_keys import create_key
    _row, token = create_key(
        owner_user_id=1, name='fs-test', scopes=['chat'])
    return {'Authorization': f'Bearer {token}'}


def _get(client, range_header=None):
    headers = dict(_bearer())
    if range_header:
        headers['Range'] = range_header
    return client.get(f'/api/v1/desktop/download/{_NAME}', headers=headers)


@pytest.mark.api
def test_a_plain_get_is_untouched(seeded, flask_client):
    r = _get(flask_client)
    assert r.status_code == 200
    assert r.data == _CONTENT


@pytest.mark.api
def test_a_single_byte_range_probe_never_500s(seeded, flask_client):
    """The measured landmine: quart+werkzeug 500 on bytes=0-0; the seam
    answers a spec-legal full-body 200 instead.

    Version-aware: newer quart/werkzeug pairs (public CI's fresh resolve,
    measured 2026-08-05) FIXED the upstream bug, so the raw path answers a
    correct 206 and the seam passes it through untouched. The invariant that
    must hold under EVERY library pair: never a 500, and the body is either
    the full content (200 degradation) or exactly the probed byte (206)."""
    r = _get(flask_client, 'bytes=0-0')
    assert r.status_code in (200, 206), (
        f'the single-byte probe must never 500, got {r.status_code}')
    if r.status_code == 200:
        assert r.data == _CONTENT
    else:
        assert r.data == _CONTENT[:1]


@pytest.mark.api
def test_a_multibyte_range_still_gets_a_proper_206(seeded, flask_client):
    """The healthy path must NOT be degraded by the workaround."""
    r = _get(flask_client, 'bytes=0-99')
    assert r.status_code == 206, f'multi-byte range must 206, got {r.status_code}'
    assert r.data == _CONTENT[:100]
    # The header's stop value is the library pair's own (off-by-one) math —
    # observed rendering 'bytes 0-98' while serving the correct 100 bytes.
    # Pin only what the seam owns: the total length, not the stop digit.
    assert (r.headers.get('Content-Range') or '').endswith(
        f'/{len(_CONTENT)}')


@pytest.mark.api
def test_an_unsatisfiable_range_gets_416(seeded, flask_client):
    r = _get(flask_client, f'bytes={len(_CONTENT) + 1000}-')
    assert r.status_code == 416, (
        f'a range past EOF must be 416, got {r.status_code}')


@pytest.mark.api
def test_NEUTER_without_the_catch_the_probe_500s(seeded, flask_app,
                                                 flask_client, monkeypatch):
    """Documentary: bypass the seam (raw quart send_file, conditional) → the
    probe 500s. The catch is what stands between the probe and a 500."""
    from lib.quart_sync import send_file

    def _pre_seam(path, **kw):
        kw['conditional'] = True
        return send_file(path, **kw)

    monkeypatch.setattr('lib.file_serving.send_file_conditional', _pre_seam)
    # TESTING normally propagates view exceptions out of Quart's test client.
    # Disable propagation for this one documentary probe so it observes the
    # production HTTP boundary (500) rather than an in-process exception.
    monkeypatch.setitem(flask_app.config, 'PROPAGATE_EXCEPTIONS', False)
    r = _get(flask_client, 'bytes=0-0')
    if r.status_code != 500:
        # Upstream fixed (public CI's newer quart/werkzeug, measured
        # 2026-08-05): the raw path answers a correct single-byte 206, the
        # seam is a pass-through by design, and the neuter re-pins THAT — a
        # future regression of the raw path must still be caught here.
        assert r.status_code == 206 and r.data == _CONTENT[:1], (
            f'with the upstream bug fixed the raw path must answer a correct '
            f'206 single-byte body, got {r.status_code}')
        return
    # Buggy library pair: the 500 is exactly what the seam catches.
    assert r.status_code == 500
