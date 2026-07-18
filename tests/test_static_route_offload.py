#!/usr/bin/env python3
"""Tests for the executor-offloaded, FUSE-stall-safe ``/static/<path>`` route.

Background: Quart's built-in static view serves files via a native-async
``send_static_file → send_from_directory`` whose ``is_file()``/``stat()``/
full-file read run DIRECTLY on the event loop. On a FUSE-backed ``static/`` dir
a single stall there wedges the whole server (the proven root cause of the
outage). server.py disables the built-in view (``static_folder=None``) and
registers ``_static_route``, which moves all blocking FS I/O into a worker
thread under a hard ``asyncio.wait_for`` timeout.

These tests assert the three sign-off requirements + the offload being
load-bearing:
  1. Path traversal (``/static/../server.py``) → 404, never a file leak.
  2. 404 (missing file) vs 503 (read timeout) stay DISTINCT — the stale-bundle
     self-heal depends on a genuine 404.
  3. Conditional 304 / ETag + long-cache headers preserved.
  4. NEUTER: degrade the offload to run inline → a slow read wedges a
     concurrent request, proving the executor offload is what keeps the loop
     alive. In-memory monkeypatch only (server.py on disk untouched → no
     ``_NC_GUARDED_SOURCES`` entry needed).
"""

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture(scope='module')
def async_app(flask_app):
    return flask_app


@pytest.fixture
def client(async_app):
    return async_app.test_client()


def _run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ─────────────────────────── requirement #1: traversal ───────────────────────

@pytest.mark.auth_mode("open")
def test_traversal_is_404_not_leak(client):
    """A path-traversal attempt resolves via safe_join → None → 404, and the
    body must NOT contain server.py source (no leak)."""
    async def go():
        for path in ('/static/../server.py',
                     '/static/..%2f..%2fserver.py',
                     '/static/js/../../server.py'):
            resp = await client.get(path)
            assert resp.status_code == 404, '%s → %d' % (path, resp.status_code)
            body = (await resp.get_data()).decode('utf-8', 'replace')
            assert '_install_flask_shim' not in body
            assert 'def _static_route' not in body
    _run_async(go())


@pytest.mark.auth_mode("open")
def test_absolute_path_does_not_leak(client):
    """An absolute-ish path under /static/ must never return /etc/passwd
    contents. (A double slash may normalize to a 3xx before reaching the route;
    what matters is no file leak — safe_join rejects the escape when it does
    reach _load_static_bytes.)"""
    async def go():
        resp = await client.get('/static/%2e%2e%2f%2e%2e%2fetc%2fpasswd')
        assert resp.status_code in (404, 308)
        if resp.status_code == 404:
            body = (await resp.get_data()).decode('utf-8', 'replace')
            assert 'root:' not in body
    _run_async(go())


# ─────────────────── requirement #2: 404 vs 503 stay distinct ─────────────────

@pytest.mark.auth_mode("open")
def test_missing_file_is_404(client):
    """A genuinely-absent file → 404 (so resolve_stale_bundle self-heal fires),
    NOT 503."""
    async def go():
        resp = await client.get('/static/js/does-not-exist-xyz.js')
        assert resp.status_code == 404
    _run_async(go())


@pytest.mark.auth_mode("open")
def test_stale_bundle_selfheal_still_reachable(client, async_app):
    """The 404 handler's stale-bundle redirect depends on a REAL 404 from the
    static route. A stale bundle-<hash>.js → 302 to the current bundle."""
    import server
    from lib.js_bundler import get_bundle_filename
    current = get_bundle_filename()
    if not current:
        pytest.skip('bundler produced no core bundle in this env')

    async def go():
        # A stale (different-hash) but well-formed bundle name → self-heal 302.
        resp = await client.get('/static/js/bundle-deadbeef.js')
        assert resp.status_code == 302
        assert resp.headers['Location'].endswith('/static/js/' + current)
    _run_async(go())


@pytest.mark.auth_mode("open")
def test_read_timeout_is_503(client, monkeypatch):
    """When the offloaded read exceeds the hard timeout (FUSE-wedge signal) the
    route returns 503 — distinct from a 404 — and does NOT block the loop."""
    import server

    async def _slow_offload(loop, filename):
        await asyncio.sleep(5)  # longer than the patched 0.05s timeout
        return (b'x', time.time(), 'e')

    monkeypatch.setattr(server, '_static_offload', _slow_offload)
    monkeypatch.setattr(server, '_STATIC_SEND_TIMEOUT', 0.05)

    async def go():
        t0 = time.monotonic()
        resp = await client.get('/static/js/core.js')
        elapsed = time.monotonic() - t0
        assert resp.status_code == 503, 'expected 503, got %d' % resp.status_code
        assert elapsed < 2.0, 'timeout did not fire fast (%.2fs) — loop blocked?' % elapsed
    _run_async(go())


# ─────────────────── requirement #3: caching / conditional 304 ────────────────

@pytest.mark.auth_mode("open")
def test_bundle_served_with_etag_and_immutable_cache(client, async_app):
    """A real bundle serves 200 with an ETag and the immutable long-cache
    header (add_cache_headers stamps /bundle-)."""
    import server
    from lib.js_bundler import get_bundle_filename
    current = get_bundle_filename()
    if not current:
        pytest.skip('bundler produced no core bundle in this env')

    async def go():
        resp = await client.get('/static/js/' + current)
        assert resp.status_code == 200
        assert resp.headers.get('ETag')
        cc = resp.headers.get('Cache-Control', '')
        assert 'immutable' in cc and 'max-age=31536000' in cc
        ct = resp.content_type or ''
        assert 'javascript' in ct
        return resp.headers.get('ETag')

    etag = _run_async(go())

    async def go2():
        # Conditional request with the matching ETag → 304, no body.
        resp = await client.get('/static/js/' + current,
                                headers={'If-None-Match': etag})
        assert resp.status_code == 304
        body = await resp.get_data()
        assert body == b''
    _run_async(go2())


@pytest.mark.auth_mode("open")
def test_plain_js_served_with_correct_mime(client):
    """A non-bundle .js still serves 200 with a javascript content-type."""
    async def go():
        resp = await client.get('/static/js/core.js')
        if resp.status_code != 200:
            pytest.skip('core.js absent in test env')
        assert 'javascript' in (resp.content_type or '')
    _run_async(go())


# ─────────────── requirement (reviewer): Range / 206 partial content ─────────

def _first_existing_static(client):
    """Return (path, full_bytes) for a real static file, or (None, None)."""
    async def go():
        from lib.js_bundler import get_bundle_filename
        candidates = []
        b = get_bundle_filename()
        if b:
            candidates.append('/static/js/' + b)
        candidates += ['/static/js/core.js', '/static/styles.css']
        for p in candidates:
            r = await client.get(p)
            if r.status_code == 200:
                return p, await r.get_data()
        return None, None
    return _run_async(go())


@pytest.mark.auth_mode("open")
def test_range_request_returns_206_sliced(client):
    """A ``Range: bytes=0-9`` request must return 206 with the correctly-sliced
    body + an EXACT ``Content-Range`` header — NOT a full 200. The built-in view
    we replaced supported ranges (media seeking / resumable downloads); if this
    regressed to a full 200, those silently break. The end byte must be exactly
    9 (Quart's own make_conditional emits an off-by-one ``0-8`` here — the route
    computes Content-Range itself to be correct)."""
    path, full = _first_existing_static(client)
    if not path:
        pytest.skip('no servable static file in this env')
    if len(full) < 20:
        pytest.skip('static file too small for a meaningful range')

    async def go():
        resp = await client.get(path, headers={'Range': 'bytes=0-9'})
        assert resp.status_code == 206, 'expected 206, got %d' % resp.status_code
        body = await resp.get_data()
        assert body == full[:10], 'sliced body mismatch (len=%d)' % len(body)
        cr = resp.headers.get('Content-Range', '')
        assert cr == 'bytes 0-9/%d' % len(full), 'bad Content-Range: %r' % cr
        assert resp.headers.get('Accept-Ranges') == 'bytes'
        assert resp.headers.get('Content-Length') == '10'
    _run_async(go())


@pytest.mark.auth_mode("open")
def test_range_midfile_slice(client):
    """A mid-file range ``bytes=5-14`` returns exactly those 10 bytes + an exact
    Content-Range, confirming the offset (not just prefix) is honored."""
    path, full = _first_existing_static(client)
    if not path or len(full) < 20:
        pytest.skip('no suitable static file for a mid-file range')

    async def go():
        resp = await client.get(path, headers={'Range': 'bytes=5-14'})
        assert resp.status_code == 206
        body = await resp.get_data()
        assert body == full[5:15], 'mid-file slice mismatch'
        assert resp.headers.get('Content-Range') == 'bytes 5-14/%d' % len(full)
    _run_async(go())


@pytest.mark.auth_mode("open")
def test_range_suffix_and_open(client):
    """Suffix (``bytes=-N``, last N bytes) and open-ended (``bytes=N-``, from N
    to EOF) ranges resolve correctly — the two forms media players use for
    seek-to-end and resume."""
    path, full = _first_existing_static(client)
    if not path or len(full) < 30:
        pytest.skip('no suitable static file for suffix/open ranges')
    n = len(full)

    async def go():
        # Suffix: last 10 bytes.
        r1 = await client.get(path, headers={'Range': 'bytes=-10'})
        assert r1.status_code == 206
        assert (await r1.get_data()) == full[-10:]
        assert r1.headers.get('Content-Range') == 'bytes %d-%d/%d' % (n - 10, n - 1, n)
        # Open-ended: from byte 20 to EOF.
        r2 = await client.get(path, headers={'Range': 'bytes=20-'})
        assert r2.status_code == 206
        assert (await r2.get_data()) == full[20:]
        assert r2.headers.get('Content-Range') == 'bytes 20-%d/%d' % (n - 1, n)
    _run_async(go())


@pytest.mark.auth_mode("open")
def test_range_unsatisfiable_is_416(client):
    """A range starting past EOF is unsatisfiable → 416 with
    ``Content-Range: bytes */<total>``, not a 200 or a bogus 206."""
    path, full = _first_existing_static(client)
    if not path:
        pytest.skip('no servable static file in this env')
    n = len(full)

    async def go():
        resp = await client.get(path, headers={'Range': 'bytes=%d-%d' % (n + 100, n + 200)})
        assert resp.status_code == 416, 'expected 416, got %d' % resp.status_code
        assert resp.headers.get('Content-Range') == 'bytes */%d' % n
    _run_async(go())


# ─────── reviewer: HEAD + conditional-range (If-Range / If-None-Match) ───────

@pytest.mark.auth_mode("open")
def test_head_returns_correct_headers(client):
    """A HEAD request returns 200 with the same headers a GET would (correct
    Content-Length, ETag, Accept-Ranges). The body is stripped by the ASGI
    layer, so we assert only headers here."""
    path, full = _first_existing_static(client)
    if not path:
        pytest.skip('no servable static file in this env')

    async def go():
        resp = await client.head(path)
        assert resp.status_code == 200
        assert resp.headers.get('Content-Length') == str(len(full))
        assert resp.headers.get('ETag')
        assert resp.headers.get('Accept-Ranges') == 'bytes'
    _run_async(go())


@pytest.mark.auth_mode("open")
def test_head_with_range_returns_206_headers(client):
    """A HEAD carrying a Range still reports 206 + a correct Content-Range."""
    path, full = _first_existing_static(client)
    if not path or len(full) < 20:
        pytest.skip('no suitable static file')

    async def go():
        resp = await client.head(path, headers={'Range': 'bytes=0-9'})
        assert resp.status_code == 206
        assert resp.headers.get('Content-Range') == 'bytes 0-9/%d' % len(full)
    _run_async(go())


@pytest.mark.auth_mode("open")
def test_if_range_match_serves_206(client):
    """If-Range with the CURRENT validator → the range is honored (206)."""
    path, full = _first_existing_static(client)
    if not path or len(full) < 20:
        pytest.skip('no suitable static file')

    async def go():
        etag = (await client.get(path)).headers.get('ETag')
        resp = await client.get(path, headers={'If-Range': etag, 'Range': 'bytes=0-9'})
        assert resp.status_code == 206
        assert (await resp.get_data()) == full[:10]
        assert resp.headers.get('Content-Range') == 'bytes 0-9/%d' % len(full)
    _run_async(go())


@pytest.mark.auth_mode("open")
def test_if_range_mismatch_serves_full_200(client):
    """RFC 9110 §13.1.5: a STALE If-Range validator MUST cause the Range to be
    ignored and the FULL current representation served — otherwise a resuming
    client stitches a slice of the NEW file onto its OLD partial → silent
    corruption. So a mismatching If-Range + Range → 200 full body, no
    Content-Range."""
    path, full = _first_existing_static(client)
    if not path or len(full) < 20:
        pytest.skip('no suitable static file')

    async def go():
        resp = await client.get(path, headers={'If-Range': '"stale-nomatch"',
                                               'Range': 'bytes=0-9'})
        assert resp.status_code == 200, 'expected full 200, got %d' % resp.status_code
        assert (await resp.get_data()) == full, 'must serve the FULL body'
        assert resp.headers.get('Content-Range') is None
    _run_async(go())


@pytest.mark.auth_mode("open")
def test_if_none_match_with_range_still_serves_slice(client):
    """An explicit Range alongside a MATCHING If-None-Match must NOT collapse to
    304 — a ranged request is a partial fetch, not a cache revalidation. It
    returns the 206 slice (browsers/players send this on resume)."""
    path, full = _first_existing_static(client)
    if not path or len(full) < 20:
        pytest.skip('no suitable static file')

    async def go():
        etag = (await client.get(path)).headers.get('ETag')
        resp = await client.get(path, headers={'If-None-Match': etag, 'Range': 'bytes=0-9'})
        assert resp.status_code == 206
        assert (await resp.get_data()) == full[:10]
    _run_async(go())


# ─────────────────────────────── NEUTER ─────────────────────────────────────

@pytest.mark.auth_mode("open")
def test_offload_yields_loop_but_inline_neuter_starves_it(monkeypatch):
    """Prove the executor offload is load-bearing at the SEAM.

    Real ``_static_offload`` runs the blocking read in a thread, so while it is
    in flight the event loop stays free — a concurrent coroutine (a 1ms-tick
    "loop heartbeat") keeps ticking. The NEUTER runs the same blocking read
    INLINE on the loop (the regression), which starves that heartbeat for the
    whole block. We measure the heartbeat's tick count during a ~0.4s blocking
    read: real → many ticks; neutered → ~zero.

    Driving the seam directly (not two test-client requests) is what makes this
    honest — the Quart test client dispatches requests sequentially, so it
    cannot exhibit real cross-request loop starvation. In-memory monkeypatch
    only; server.py on disk is untouched (no _NC_GUARDED_SOURCES entry needed).
    """
    import server

    BLOCK = 0.4

    def _blocking_read(filename):
        time.sleep(BLOCK)  # simulate a FUSE-slow stat()+read
        return (b'x', time.time(), 'e')

    counter = {'ticks': 0}

    async def _heartbeat(stop):
        while not stop.is_set():
            counter['ticks'] += 1
            await asyncio.sleep(0.001)

    async def run(offload_impl):
        loop = asyncio.get_event_loop()
        stop = asyncio.Event()
        counter['ticks'] = 0
        hb = asyncio.ensure_future(_heartbeat(stop))
        await asyncio.sleep(0.01)  # let the heartbeat settle
        # Snapshot AFTER settling so we count ONLY ticks accrued DURING the read.
        before = counter['ticks']
        await offload_impl(loop, 'x')
        during = counter['ticks'] - before
        stop.set()
        await hb
        return during

    # Real: read runs in a thread → loop free → heartbeat ticks freely.
    monkeypatch.setattr(server, '_load_static_bytes', _blocking_read)
    real_ticks = _run_async(run(server._static_offload))

    # Neuter: read runs INLINE on the loop → heartbeat starved.
    async def _inline_offload(loop, filename):
        return server._load_static_bytes(filename)  # blocks the loop
    neutered_ticks = _run_async(run(_inline_offload))

    assert real_ticks > 20, (
        'real offload starved the loop (%d ticks in %.1fs) — offload broken'
        % (real_ticks, BLOCK))
    assert neutered_ticks < 5, (
        'inline neuter did NOT starve the loop (%d ticks) — the neuter is not '
        'exercising the offload seam' % neutered_ticks)


@pytest.mark.auth_mode("open")
def test_neuter_ifrange_gate_defeated_corrupts_conditional_range(client, monkeypatch):
    """Prove the If-Range gate is load-bearing. Degrade _if_range_allows to
    ALWAYS return True (the pre-fix behaviour — range honoured regardless of a
    stale validator). Then a mismatching If-Range + Range wrongly yields a 206
    slice of the CURRENT file instead of the RFC-mandated full 200 — the exact
    silent-corruption path for a resuming client.

    In-memory monkeypatch of the imported module (server.py on disk untouched →
    no _NC_GUARDED_SOURCES entry needed)."""
    import server

    path, full = _first_existing_static(client)
    if not path or len(full) < 20:
        pytest.skip('no suitable static file')

    monkeypatch.setattr(server, '_if_range_allows', lambda *a, **k: True)

    async def go():
        resp = await client.get(path, headers={'If-Range': '"stale-nomatch"',
                                               'Range': 'bytes=0-9'})
        # With the gate defeated the stale validator is ignored → bogus 206.
        assert resp.status_code == 206, (
            'neuter should defeat the gate → 206; got %d (gate not load-bearing?)'
            % resp.status_code)
        assert resp.headers.get('Content-Range') == 'bytes 0-9/%d' % len(full)
    _run_async(go())
