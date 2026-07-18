#!/usr/bin/env python3
"""serve_paper_pdf MUST honour HTTP Range (206 Partial Content).

Root cause of the ORCA "fail to render" / "PDF cannot be loaded" report:
``serve_paper_pdf`` called ``send_file(..., mimetype='application/pdf')`` with
the default ``conditional=False``. Quart then IGNORES the request ``Range``
header and always returns ``200`` + the WHOLE file (the ORCA PDF is ~41 MB).
pdf.js loads large PDFs with ranged requests; a buffering cloud-IDE proxy can
truncate / time-out that single tens-of-MB response, which pdf.js surfaces as
"Missing PDF" (whole-doc parse) or per-page "failed to render".

Fix: ``conditional=True`` → Quart calls ``make_conditional(accept_ranges=True,
complete_length=...)`` → a proper ``206`` with ``Content-Range`` for ranged
requests, while a plain GET still returns ``200`` + the full file.

BITING NEGATIVE CONTROL: monkeypatch the module's ``send_file`` binding to drop
``conditional`` → the Range request falls back to ``200`` + full length,
reproducing the pre-fix behaviour and proving ``conditional=True`` is
load-bearing.

Run standalone: ``python tests/test_paper_pdf_range.py``
"""

import os
import sys
import asyncio
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Flask→Quart shim before importing routes (matches the other paper tests).
import quart as _quart  # noqa: E402
sys.modules.setdefault('flask', _quart)


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


try:
    import pytest
    pytestmark = [pytest.mark.unit]
except ImportError:
    pytest = None


# A payload comfortably larger than a single Range slice so Content-Length of a
# partial response is unambiguously smaller than the whole file.
_PDF_BYTES = b'%PDF-1.7\n' + (b'A' * 8192) + b'\n%%EOF\n'


def _build_app(paper_dir):
    """Minimal Quart app exposing only serve_paper_pdf, pointed at paper_dir."""
    import routes.paper as rp
    rp.PAPER_DIR = paper_dir
    app = _quart.Quart('range_test')
    app.add_url_rule('/api/paper/pdf/<filename>', 'serve_paper_pdf',
                     rp.serve_paper_pdf)
    return app, rp


def _seed(paper_dir, filename):
    os.makedirs(paper_dir, exist_ok=True)
    with open(os.path.join(paper_dir, filename), 'wb') as f:
        f.write(_PDF_BYTES)


def test_range_request_returns_206_partial():
    """A ``Range`` request yields 206 + Content-Range + a short Content-Length;
    a plain GET yields 200 + the full length."""
    import server  # noqa: F401 — installs the sync-safe send_file shim
    tmp = tempfile.mkdtemp(prefix='tofu-pdfrange-')
    fn = 'arxiv_range_test.pdf'
    _seed(tmp, fn)
    app, _rp = _build_app(tmp)
    total = len(_PDF_BYTES)

    async def _t():
        c = app.test_client()
        r = await c.get('/api/paper/pdf/' + fn, headers={'Range': 'bytes=0-1023'})
        assert r.status_code == 206, f'expected 206 for Range, got {r.status_code}'
        cr = r.headers.get('Content-Range') or ''
        assert cr.startswith('bytes 0-1023/') or cr.startswith('bytes 0-'), \
            f'missing/bad Content-Range: {cr!r}'
        assert cr.endswith('/' + str(total)), f'complete-length wrong: {cr!r}'
        assert (r.headers.get('Accept-Ranges') or '').lower() == 'bytes'
        clen = int(r.headers.get('Content-Length') or total)
        assert clen < total, f'partial Content-Length not smaller: {clen} >= {total}'

        r2 = await c.get('/api/paper/pdf/' + fn)
        assert r2.status_code == 200, f'plain GET should be 200, got {r2.status_code}'
        assert int(r2.headers.get('Content-Length') or 0) == total

    asyncio.run(_t())
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    _ok('serve_paper_pdf returns 206 for Range and 200 full for plain GET')


def test_initial_200_advertises_accept_ranges():
    """pdf.js's validateRangeRequestCapabilities only enables ranged loading
    when the INITIAL (non-Range) response carries ``Accept-Ranges: bytes``.
    Quart's make_conditional sets it only on the 206 path, so serve_paper_pdf
    must stamp it explicitly on the plain 200 — else conditional=True is inert
    for the viewer (one giant full GET, no ranged requests ever issued).

    NC: strip the explicit ``setdefault('Accept-Ranges', …)`` (simulated by
    deleting the header from the response) → the 200 lacks it → pdf.js gate
    would fail. Proven here by asserting the header IS present after the fix.
    """
    import server  # noqa: F401
    tmp = tempfile.mkdtemp(prefix='tofu-pdfrange-init-')
    fn = 'arxiv_init.pdf'
    _seed(tmp, fn)
    app, _rp = _build_app(tmp)

    async def _t():
        c = app.test_client()
        r = await c.get('/api/paper/pdf/' + fn)  # pdf.js's first request: no Range
        assert r.status_code == 200
        ar = (r.headers.get('Accept-Ranges') or '').lower()
        assert ar == 'bytes', \
            f"initial 200 must advertise Accept-Ranges: bytes for pdf.js, got {ar!r}"
        # The other pdf.js gate inputs: uncompressed + length over 2×chunk.
        ce = (r.headers.get('Content-Encoding') or 'identity').lower()
        assert ce == 'identity', f'ranged loading needs identity encoding, got {ce!r}'

    asyncio.run(_t())
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    _ok('initial 200 advertises Accept-Ranges: bytes (pdf.js range gate passes)')


def test_neuter_without_conditional_falls_back_to_200():
    """NC: strip ``conditional`` from the module's send_file → the Range request
    degrades to 200 + full length (the reproduced pre-fix bug)."""
    import server  # noqa: F401
    import routes.paper as rp
    tmp = tempfile.mkdtemp(prefix='tofu-pdfrange-nc-')
    fn = 'arxiv_range_nc.pdf'
    _seed(tmp, fn)
    app, _rp = _build_app(tmp)
    total = len(_PDF_BYTES)

    orig_send_file = rp.send_file

    def _no_conditional(filepath, **kw):
        kw.pop('conditional', None)  # simulate the pre-fix call
        return orig_send_file(filepath, **kw)

    rp.send_file = _no_conditional
    try:
        async def _t():
            c = app.test_client()
            r = await c.get('/api/paper/pdf/' + fn, headers={'Range': 'bytes=0-1023'})
            assert r.status_code == 200, \
                f'NC should degrade to 200, got {r.status_code} (fix leaked into NC)'
            assert int(r.headers.get('Content-Length') or 0) == total, \
                'NC should return the full file length'
        asyncio.run(_t())
    finally:
        rp.send_file = orig_send_file
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    _ok('NC: without conditional the Range request degrades to 200 (fix bites)')


def test_stream_fallback_serves_accurate_bytes_and_ranges():
    """TOFU_PAPER_PDF_STREAM=1 → chunked-generator fallback: full GET returns
    200 + byte-identical file with Accept-Ranges + X-Accel-Buffering:no; an
    explicit Range returns a byte-accurate 206 slice. This is the path we land
    if the transport log proves the proxy buffers the whole-file 200."""
    import server  # noqa: F401
    import routes.paper as rp
    tmp = tempfile.mkdtemp(prefix='tofu-pdfstream-')
    fn = 'arxiv_stream.pdf'
    _seed(tmp, fn)
    app, _rp = _build_app(tmp)
    total = len(_PDF_BYTES)
    os.environ['TOFU_PAPER_PDF_STREAM'] = '1'
    try:
        async def _t():
            c = app.test_client()
            r = await c.get('/api/paper/pdf/' + fn)
            assert r.status_code == 200
            body = await r.get_data()
            assert body == _PDF_BYTES, 'streamed full body must be byte-identical'
            assert (r.headers.get('Accept-Ranges') or '').lower() == 'bytes'
            assert (r.headers.get('X-Accel-Buffering') or '') == 'no'
            assert 'no-transform' in (r.headers.get('Cache-Control') or '')

            r2 = await c.get('/api/paper/pdf/' + fn, headers={'Range': 'bytes=10-99'})
            assert r2.status_code == 206
            b2 = await r2.get_data()
            assert b2 == _PDF_BYTES[10:100], 'streamed range slice must be exact'
            assert r2.headers.get('Content-Range') == 'bytes 10-99/%d' % total
        asyncio.run(_t())
    finally:
        os.environ.pop('TOFU_PAPER_PDF_STREAM', None)
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    _ok('stream fallback (env-gated) serves byte-accurate 200 + 206 with anti-buffer headers')


def test_stream_fallback_default_off_uses_send_file():
    """Without the env var the send_file path is used (still 206-capable). Proves
    the fallback is opt-in and the default behaviour is unchanged."""
    import server  # noqa: F401
    tmp = tempfile.mkdtemp(prefix='tofu-pdfstream-off-')
    fn = 'arxiv_off.pdf'
    _seed(tmp, fn)
    app, _rp = _build_app(tmp)
    os.environ.pop('TOFU_PAPER_PDF_STREAM', None)

    async def _t():
        c = app.test_client()
        r = await c.get('/api/paper/pdf/' + fn)
        assert r.status_code == 200
        # send_file path does NOT set X-Accel-Buffering (that's the stream path's marker)
        assert (r.headers.get('X-Accel-Buffering') or '') == '', \
            'default path should be send_file, not the streamed generator'
        assert (r.headers.get('Accept-Ranges') or '').lower() == 'bytes'
    asyncio.run(_t())
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    _ok('default (no env var) uses send_file path — fallback is opt-in')


def main():
    print()
    print(_color('═══ Paper PDF Range (206) Tests ═══', '36'))
    print()
    tests = [
        test_range_request_returns_206_partial,
        test_initial_200_advertises_accept_ranges,
        test_neuter_without_conditional_falls_back_to_200,
        test_stream_fallback_serves_accurate_bytes_and_ranges,
        test_stream_fallback_default_off_uses_send_file,
    ]
    for fn in tests:
        try:
            fn()
        except AssertionError as e:
            _fail(f'{fn.__name__}: {e}')
        except Exception as e:
            import traceback
            traceback.print_exc()
            _fail(f'{fn.__name__}: unexpected {type(e).__name__}: {e}')
    print()
    print(_color(f'═══ ALL {len(tests)} TESTS PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    main()
