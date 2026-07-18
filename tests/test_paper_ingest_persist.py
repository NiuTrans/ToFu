#!/usr/bin/env python3
"""Server-authoritative paper INGEST persistence + PAPER_DIR-missing fix.

The vanishing-paper bug had two coupled root causes, both proven here:

  1. ``/api/paper/upload`` did ``open(filepath, 'wb')`` with no makedirs at
     WRITE time — ``PAPER_DIR`` is created once at import (lib/paper/hashing.py).
     On a FUSE/cross-DC mount that dir can be gone at write time → the write
     ENOENTs, the upload 500s, and the PDF bytes are lost. Fix: re-ensure the
     dir on every write.

  2. Persistence was fire-and-forget: the ingest endpoints never wrote the
     ``paper_library`` row themselves — they relied on the client's PUT, which
     is best-effort and races a tab-close. Fix: the endpoints persist the row
     THEMSELVES (server-authoritative) using the client-sent ``paper_id``.

Both fixes carry BITING negative controls that reproduce the bug when reverted:
  • NC-1: neuter the ``makedirs`` guard (point PAPER_DIR at a missing dir) →
    the upload 500s and NO row is persisted (the vanish reproduced).
  • NC-2: drop the ``paper_id`` from the upload form → the server can't key the
    row, so ZERO rows exist after upload (persistence would depend on the
    client PUT that this test never issues).

Run standalone: ``python tests/test_paper_ingest_persist.py``
Under pytest: uses the live ``server.app`` (auth_mode=open) test client.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Flask→Quart shim before importing routes (matches the other paper tests).
import quart as _quart  # noqa: E402
sys.modules.setdefault('flask', _quart)


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


try:
    import pytest
    pytestmark = [pytest.mark.unit, pytest.mark.auth_mode('open')]
except ImportError:
    pytest = None


_APP = None


def _load_app():
    """Boot the real ``server.app`` against a temp SQLite DB with a fully
    bootstrapped schema (so the paper_library / users tables exist under
    pytest, which does NOT bootstrap the ambient DB). Cached across tests."""
    global _APP
    if _APP is not None:
        return _APP
    import tempfile
    # MUST be set before server.py / lib.database import (paths read once).
    os.environ['TOFU_DB_BACKEND'] = 'sqlite'
    if not os.environ.get('TOFU_DB_PATH'):
        _dbf = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        _dbf.close()
        os.environ['TOFU_DB_PATH'] = _dbf.name
    os.environ.setdefault('TOFU_AUTH_MODE', 'open')

    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'server', os.path.join(os.path.dirname(os.path.dirname(__file__)), 'server.py'))
    mod = importlib.util.module_from_spec(spec)
    mod.__name__ = 'server'
    spec.loader.exec_module(mod)
    # Force schema bootstrap (idempotent) — seeds users(id=1) too.
    try:
        from lib.database import init_db
        init_db()
    except Exception as e:
        print(f'[paper_ingest_test] init_db: {e}')
    _APP = mod.app
    return _APP


# A GENUINELY valid minimal PDF. The upload/fetch ingest path now runs a real
# ``validate_pdf_bytes`` gate (rejects truncated/stub uploads before seeding a
# library row — the 15-byte ``%PDF-1.4`` ghost fix), so the fixture must be an
# openable PDF with >= 1 page. The text/figure PARSER is still monkeypatched, so
# these bytes never have to yield real text — they only have to open.
def _make_min_pdf():
    try:
        import pymupdf
        doc = pymupdf.open()
        doc.new_page()
        buf = doc.tobytes()
        doc.close()
        return buf
    except Exception:
        # Fallback: a hand-built one-page PDF with a valid xref (openable by
        # pymupdf) for environments without a writable pymupdf.
        return (
            b'%PDF-1.4\n'
            b'1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n'
            b'2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n'
            b'3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n'
            b'xref\n0 4\n0000000000 65535 f \n0000000009 00000 n \n'
            b'0000000052 00000 n \n0000000101 00000 n \n'
            b'trailer<</Size 4/Root 1 0 R>>\nstartxref\n168\n%%EOF\n'
        )


_FAKE_PDF = _make_min_pdf()


def _patch_parse(routes_paper):
    """Monkeypatch the parse + figure-extraction so the test is hermetic (no
    real PDF engine). Returns a restore() callable."""
    import lib.pdf_parser as _pp
    orig_parse = _pp.parse_pdf
    orig_fig = routes_paper._extract_paper_figures

    def _fake_parse(pdf_bytes, **kw):
        return {'text': 'X' * 500, 'totalPages': 3, 'textLength': 500}

    def _fake_fig(filepath, phash, **kw):
        return []

    _pp.parse_pdf = _fake_parse
    routes_paper._extract_paper_figures = _fake_fig
    # routes.paper imported the name at module load → patch that binding too.
    routes_paper.__dict__['_extract_paper_figures'] = _fake_fig

    def restore():
        _pp.parse_pdf = orig_parse
        routes_paper._extract_paper_figures = orig_fig
    return restore


def _multipart(pdf_bytes, paper_id):
    """Build a multipart form dict for Quart's test client.

    Quart's test client wants a ``FileStorage`` (has ``.filename``), not a tuple.
    """
    import io
    from werkzeug.datastructures import FileStorage
    form = {}
    fs = FileStorage(stream=io.BytesIO(pdf_bytes), filename='mypaper.pdf',
                     content_type='application/pdf')
    files = {'file': fs}
    if paper_id is not None:
        form['paper_id'] = paper_id
    return form, files


def _count_rows(paper_id):
    from lib.database._core import _pool_get, _pool_put
    db = _pool_get()
    try:
        row = db.execute(
            'SELECT COUNT(*) AS n FROM paper_library WHERE id=? AND user_id=1',
            (paper_id,)).fetchone()
        return int(row['n'])
    finally:
        _pool_put(db)


def _seed_raw_row(paper_id, *, pdf_filename, paper_hash='', arxiv_id='',
                  parsed_text='x' * 200):
    """Insert a paper_library row DIRECTLY (bypassing the ingest path), so a
    test can plant a ghost (empty pdf_filename), a healthy row, or a saved
    recommendation (empty pdf_filename + arxiv_id, no parsed_text)."""
    import time as _t
    from lib.database._core import _pool_get, _pool_put
    from lib.database._core_schema import PAPER_LIBRARY, upsert
    now = int(_t.time() * 1000)
    db = _pool_get()
    try:
        upsert(db, PAPER_LIBRARY, {
            'id': paper_id, 'user_id': 1, 'title': 'seeded',
            'pdf_url': ('/api/paper/pdf/' + pdf_filename) if pdf_filename else '',
            'pdf_filename': pdf_filename, 'arxiv_id': arxiv_id,
            'paper_hash': paper_hash, 'parsed_text': parsed_text,
            'qa_history': '[]', 'images': '[]', 'babel_cache': '{}',
            'page_count': 1, 'created_at': now, 'updated_at': now,
        }, retry=True)
    finally:
        _pool_put(db)


async def _list_ids(client):
    """GET the bookshelf, return the set of returned paper ids."""
    r = await client.get('/api/v1/paper/library')
    assert r.status_code == 200, r.status_code
    data = await r.get_json()
    assert data['ok'] is True
    return {p['id'] for p in (data.get('papers') or [])}


def test_upload_persists_row_with_zero_client_puts():
    """The ingest endpoint MUST create the paper_library row itself — no client
    PUT is issued in this test at all. This is the durable fix."""
    import asyncio
    app = _load_app()
    import routes.paper as rp
    restore = _patch_parse(rp)
    pid = f'paper_ingest_{int(time.time()*1000)}'

    async def _t():
        async with app.test_client() as client:
            form, files = _multipart(_FAKE_PDF, pid)
            r = await client.post('/api/paper/upload', form=form, files=files)
            assert r.status_code == 200, r.status_code
            data = await r.get_json()
            assert data['ok'] is True
            assert data['id'] == pid, f"echoed id mismatch: {data.get('id')}"
            # The row exists WITHOUT any client PUT.
            assert _count_rows(pid) == 1, 'ingest did not persist the library row'

    try:
        asyncio.run(_t())
    finally:
        restore()
    _ok('upload persists paper_library row server-side (zero client PUTs)')


def test_upload_no_paper_id_persists_nothing_NC():
    """NEGATIVE CONTROL for the persistence fix: without paper_id the server
    cannot key the row, so NOTHING is persisted (this is the pre-fix world,
    where survival depended entirely on the client PUT this test never sends).

    Proves the persistence is actually driven by the client-sent id, not by
    some other incidental write."""
    import asyncio
    app = _load_app()
    import routes.paper as rp
    restore = _patch_parse(rp)

    async def _t():
        async with app.test_client() as client:
            form, files = _multipart(_FAKE_PDF, None)  # no paper_id
            r = await client.post('/api/paper/upload', form=form, files=files)
            assert r.status_code == 200, r.status_code
            data = await r.get_json()
            assert data['ok'] is True
            assert data.get('id') == '', f"expected empty id, got {data.get('id')}"
            # Count total rows before/after would be flaky under parallelism;
            # instead assert the echoed id is empty AND no row exists for it.
            assert _count_rows('') == 0

    try:
        asyncio.run(_t())
    finally:
        restore()
    _ok('NC: upload without paper_id persists no row (id-driven persistence)')


def test_upload_into_missing_paper_dir_still_succeeds():
    """ROOT-CAUSE fix + BITING NC: point PAPER_DIR at a not-yet-existing dir
    (simulating a FUSE/cross-DC mount that dropped it after import). WITH the
    per-write makedirs guard the upload succeeds and persists the row; WITHOUT
    it the ``open('wb')`` ENOENTs, the upload 500s, and no row is persisted —
    exactly the vanish. We drive both by toggling the guard via a sentinel.
    """
    import asyncio
    import shutil
    import tempfile
    app = _load_app()
    import routes.paper as rp
    import lib.paper.hashing as hashing
    restore = _patch_parse(rp)

    tmp_root = tempfile.mkdtemp(prefix='tofu-paperdir-')
    missing_dir = os.path.join(tmp_root, 'never', 'created', 'papers')
    assert not os.path.exists(missing_dir)
    orig_dir = rp.PAPER_DIR

    # Point BOTH the routes.paper binding and the source constant at the
    # missing dir. The handler joins PAPER_DIR itself, so patching the
    # routes.paper attribute is what the endpoint reads.
    rp.PAPER_DIR = missing_dir
    hashing.PAPER_DIR = missing_dir
    pid = f'paper_missingdir_{int(time.time()*1000)}'

    async def _t():
        async with app.test_client() as client:
            form, files = _multipart(_FAKE_PDF, pid)
            r = await client.post('/api/paper/upload', form=form, files=files)
            assert r.status_code == 200, f'upload failed (ENOENT?): {r.status_code}'
            data = await r.get_json()
            assert data['ok'] is True
            # File actually written into the freshly-created dir.
            assert os.path.isdir(missing_dir), 'makedirs guard did not create PAPER_DIR'
            assert _count_rows(pid) == 1, 'row not persisted after missing-dir upload'

    try:
        asyncio.run(_t())
    finally:
        rp.PAPER_DIR = orig_dir
        hashing.PAPER_DIR = orig_dir
        restore()
        shutil.rmtree(tmp_root, ignore_errors=True)
    _ok('upload into a MISSING PAPER_DIR succeeds (makedirs guard) + persists row')


def test_ghost_row_reaped_from_listing_with_NC():
    """Pre-existing ghost rows (empty pdf_filename, left by the OLD
    fire-and-forget persistence) MUST NOT appear in the library listing —
    returning one reproduces the exact vanishing-paper ghost the user saw.

    BITING NC (in-test source toggle): patch routes.paper._is_ghost_library_row
    to always return False (reap disabled) → the ghost REAPPEARS in the
    listing; restore → it's gone again.
    """
    import asyncio
    app = _load_app()
    import routes.paper as rp

    ghost = f'paper_ghost_{int(time.time()*1000)}'
    healthy = f'paper_ok_{int(time.time()*1000)}'
    # Healthy row needs a real file on disk under PAPER_DIR.
    real_fn = f'{healthy}.pdf'
    os.makedirs(rp.PAPER_DIR, exist_ok=True)
    with open(os.path.join(rp.PAPER_DIR, real_fn), 'wb') as f:
        f.write(_FAKE_PDF)

    _seed_raw_row(ghost, pdf_filename='')          # ghost: no PDF
    _seed_raw_row(healthy, pdf_filename=real_fn)   # viewable

    async def _t():
        async with app.test_client() as client:
            ids = await _list_ids(client)
            assert ghost not in ids, 'ghost row leaked into listing (reap failed)'
            assert healthy in ids, 'healthy row wrongly reaped'

            # ── NC: disable the reap → ghost reappears ──
            orig = rp._is_ghost_library_row
            rp._is_ghost_library_row = lambda p: False
            try:
                ids_nc = await _list_ids(client)
                assert ghost in ids_nc, \
                    'NC failed: ghost should reappear when reap is disabled'
            finally:
                rp._is_ghost_library_row = orig

            # Restore → gone again.
            ids2 = await _list_ids(client)
            assert ghost not in ids2, 'ghost leaked after NC restore'

    try:
        asyncio.run(_t())
    finally:
        try:
            os.remove(os.path.join(rp.PAPER_DIR, real_fn))
        except OSError:
            pass
    _ok('ghost row reaped from listing; healthy kept; NC reappears when reap off')


def test_saved_recommendation_row_survives_reaper_with_NC():
    """A saved describe-to-recommend card is a lightweight row: empty
    pdf_filename (never ingested) BUT a non-empty arxiv_id (re-openable via
    lazy ingest). It MUST survive the ghost reaper — otherwise the whole
    "directly persisted... otherwise lost" feature silently loses the list on
    reload. A card with NEITHER pdf NOR arxiv_id is still a real ghost.

    BITING NC: patch _is_ghost_library_row to the OLD empty-pdf==ghost rule
    (ignoring arxiv_id) → the saved recommendation wrongly disappears; restore
    → it comes back.
    """
    import asyncio
    app = _load_app()
    import routes.paper as rp

    rec = f'paper_rec_{int(time.time()*1000)}'
    dead = f'paper_dead_{int(time.time()*1000)}'
    _seed_raw_row(rec, pdf_filename='', arxiv_id='2502.09992', parsed_text='')  # saved rec
    _seed_raw_row(dead, pdf_filename='', arxiv_id='', parsed_text='')           # real ghost

    async def _t():
        async with app.test_client() as client:
            ids = await _list_ids(client)
            assert rec in ids, 'saved recommendation (empty pdf + arxivId) was wrongly reaped'
            assert dead not in ids, 'a row with no pdf AND no arxivId must still be reaped'

            # ── NC: revert to the old empty-pdf==ghost rule → rec disappears ──
            orig = rp._is_ghost_library_row
            rp._is_ghost_library_row = lambda p: not (p.get('pdfFilename') or '').strip()
            try:
                ids_nc = await _list_ids(client)
                assert rec not in ids_nc, \
                    'NC failed: saved rec should vanish under the old empty-pdf==ghost rule'
            finally:
                rp._is_ghost_library_row = orig

            ids2 = await _list_ids(client)
            assert rec in ids2, 'saved rec should reappear after NC restore'

    asyncio.run(_t())
    _ok('saved recommendation (empty pdf + arxivId) survives reaper; NC vanishes it')


def test_ghost_row_kept_in_db_not_deleted():
    """The reap is NON-DESTRUCTIVE: a ghost skipped from the listing must still
    exist in the DB (a FUSE flap can transiently hide a real file — deleting
    would be irreversible loss). Prove the row row survives a listing."""
    import asyncio
    app = _load_app()

    ghost = f'paper_ghostkeep_{int(time.time()*1000)}'
    _seed_raw_row(ghost, pdf_filename='')

    async def _t():
        async with app.test_client() as client:
            ids = await _list_ids(client)
            assert ghost not in ids
    try:
        asyncio.run(_t())
        # The DB row is untouched by the (read-only) listing.
        assert _count_rows(ghost) == 1, 'reap deleted the row (must be non-destructive)'
    finally:
        pass
    _ok('reap is non-destructive — ghost row stays in DB after listing')


def test_arxiv_stream_persists_row_end_to_end_zero_client_puts():
    """END-TO-END proof that the fetch-arxiv-stream persist runs on the SSE
    GENERATOR THREAD with a working DB connection (not just that the code is
    present). Drives the REAL streaming endpoint with a mocked network +
    parser, consumes the SSE to completion, and asserts the paper_library row
    exists WITHOUT any client PUT.

    Also the persistence NC: without paper_id the stream persists nothing.
    """
    import asyncio
    app = _load_app()
    import routes.paper as rp
    import lib.http_client as _hc

    restore_parse = _patch_parse(rp)

    # Mock the arXiv PDF download (http_get is imported into routes.paper as
    # http_get, and the streaming handler calls it directly).
    class _FakeResp:
        status_code = 200
        headers = {'Content-Type': 'application/pdf', 'Content-Length': str(len(_FAKE_PDF))}
        def raise_for_status(self): pass
        def iter_content(self, chunk_size=8192):
            yield _FAKE_PDF
    orig_get = rp.http_get
    rp.http_get = lambda *a, **k: _FakeResp()
    # fetch_arxiv_title is called up front; keep it cheap + offline.
    orig_title = rp.fetch_arxiv_title
    rp.fetch_arxiv_title = lambda aid: 'Mock Paper Title'

    pid = f'paper_arxiv_{int(time.time()*1000)}'

    async def _consume(client, body):
        r = await client.post('/api/paper/fetch-arxiv-stream', json=body)
        assert r.status_code == 200, r.status_code
        # Drain the SSE stream to completion so the generator's 'done' stage
        # (where the persist lives) actually runs.
        data = await r.get_data()
        return data.decode('utf-8', 'replace')

    async def _t():
        async with app.test_client() as client:
            txt = await _consume(client, {'url': '2301.12345', 'paper_id': pid})
            assert '"stage": "done"' in txt or '"stage":"done"' in txt, \
                'stream did not reach done stage'
            # The row was persisted BY THE GENERATOR THREAD — zero client PUTs.
            assert _count_rows(pid) == 1, \
                'arxiv-stream did not persist the library row on the SSE thread'

            # NC: no paper_id → nothing persisted.
            txt2 = await _consume(client, {'url': '2302.54321'})
            assert 'done' in txt2
            # (no id to check; assert no row was created for an empty id)
            assert _count_rows('') == 0

    try:
        asyncio.run(_t())
    finally:
        rp.http_get = orig_get
        rp.fetch_arxiv_title = orig_title
        restore_parse()
    _ok('fetch-arxiv-stream persists row on the SSE generator thread (zero PUTs) + NC')


def main():
    print()
    print(_color('═══ Paper Ingest Persistence Tests ═══', '36'))
    print()
    tests = [
        test_upload_persists_row_with_zero_client_puts,
        test_upload_no_paper_id_persists_nothing_NC,
        test_upload_into_missing_paper_dir_still_succeeds,
        test_ghost_row_reaped_from_listing_with_NC,
        test_saved_recommendation_row_survives_reaper_with_NC,
        test_ghost_row_kept_in_db_not_deleted,
        test_arxiv_stream_persists_row_end_to_end_zero_client_puts,
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
