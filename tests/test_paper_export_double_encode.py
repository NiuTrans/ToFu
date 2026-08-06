#!/usr/bin/env python3
"""Report/review EXPORT must tolerate a double-encoded ``lang`` query param.

The bug: a reverse proxy (e.g. the VS Code web proxy) double-encodes
percent-escapes in the query string. The frontend correctly sends the
composite Review-Mode key ``review:neurips:en`` as ``review%3Aneurips%3Aen``;
the proxy re-encodes the ``%`` → ``review%253Aneurips%253Aen``; Quart decodes
only once, so ``GET /api/v1/paper/report/export`` sees the literal
``review%3Aneurips%3Aen`` (with the escapes still in it), which matches no
``paper_reports`` row → ``404 report not found``.

Plain-language report exports (``lang=en``/``zh``) have no reserved chars, so
nothing gets double-encoded and they always worked — this is exactly why
"many exports fail but not all": only REVIEW exports (composite colon key) 404'd.

Fix (routes/paper.py::export_report): when ``lang`` still carries ``%XX``
escapes, ``unquote`` it once before the DB lookup.

BITING NC: revert the unquote (simulate the pre-fix handler by NOT decoding)
and the double-encoded review key 404s again — the exact production failure.

Run standalone: ``python tests/test_paper_export_double_encode.py``
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
    bootstrapped schema. Cached across tests.

    Under pytest, conftest has ALREADY imported ``server`` with the
    per-worker isolated DB — re-executing server.py here would double-run
    the async bootstrap in-process, and its background stages can flip DB
    state mid-test (CI-only 404s on a committed seed row). Reuse the live
    module; the standalone path (``python tests/...``) keeps the explicit
    boot below.
    """
    global _APP
    if _APP is not None:
        return _APP
    existing = sys.modules.get('server')
    if existing is not None and getattr(existing, 'app', None) is not None:
        try:
            from lib.database import init_db
            init_db()  # idempotent — guarantees the schema even when no
            # flask_app-fixtured suite ran first in this worker
        except Exception as e:
            print(f'[paper_export_test] init_db: {e}')
        _APP = existing.app
        return _APP
    import tempfile
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
    try:
        from lib.database import init_db
        init_db()
    except Exception as e:
        print(f'[paper_export_test] init_db: {e}')
    _APP = mod.app
    return _APP


# 64-hex paper hash (must pass _safe_hash_dir).
_PHASH = 'e' * 64
_REVIEW_LANG = 'review:neurips:en'
_REPORT_BODY = '# Test Paper\n\nThis is a stored **review** report body.\n'


_SEED_INO = None


def _seed_report(paper_hash, lang, report):
    """Insert a paper_reports row DIRECTLY so the export endpoint has something
    to find under the (paper_hash, lang) key."""
    global _SEED_INO
    from lib.database._core import _pool_get, _pool_put
    from lib.database._core_schema import PAPER_REPORTS, upsert
    db = _pool_get()
    try:
        upsert(db, PAPER_REPORTS, {
            'paper_hash': paper_hash, 'lang': lang, 'report': report,
            'model': 'test-model', 'meta': '{}', 'created_at': int(time.time()),
        }, retry=True)
    finally:
        _pool_put(db)
    # CI-only-404 forensics: pin the file identity at seed time so a mid-test
    # replacement (a leaked suite's tmpdir cleanup deleting+recreating the
    # file the handler later reads) is provable from the failure line.
    try:
        import lib.database._core as _c
        _SEED_INO = os.stat(_c.DB_PATH).st_ino
    except Exception:
        _SEED_INO = None


def _diag(paper_hash):
    """CI-only-404 diagnostics (ad9a7b1): the handler 404s a committed seed
    row ONLY in the full CI lane, never locally — report the DB identity, a
    fresh-connection read-back, AND the same query through the aio facade the
    handler uses, so the next CI log says WHICH side moved."""
    import lib.database._core as core
    _ino_now = None
    try:
        _ino_now = os.stat(core.DB_PATH).st_ino
    except Exception:
        pass
    out = [f'ino seed={_SEED_INO} now={_ino_now}',
           f'DB_PATH={core.DB_PATH!r}',
           f'BACKEND={core._BACKEND!r}',
           f'env TOFU_DB_PATH={os.environ.get("TOFU_DB_PATH")!r}',
           f'server module={id(sys.modules.get("server")):x}']
    try:
        from lib.database._core import _new_connection
        c = _new_connection()
        try:
            rows = c.execute(
                'SELECT lang, LENGTH(report) FROM paper_reports WHERE paper_hash=?',
                (paper_hash,)).fetchall()
            out.append(f'fresh-conn rows={[(r[0], r[1]) for r in rows]!r}')
        finally:
            c.close()
    except Exception as e:
        out.append(f'fresh-conn read failed: {type(e).__name__}: {e}')
    try:
        out.append('pool stamps=%r' % [
            getattr(c, '_pool_path', None) for c in list(core._sqlite_pool)])
    except Exception as e:
        out.append(f'pool-stamp read failed: {e}')
    return '; '.join(out)


def test_double_encoded_review_lang_exports_ok():
    """The core fix: a proxy-double-encoded composite review key resolves to
    the stored row (200), same as the single-encoded and raw-colon forms. A
    plain report key is unaffected."""
    import asyncio
    app = _load_app()
    _seed_report(_PHASH, _REVIEW_LANG, _REPORT_BODY)
    _seed_report(_PHASH, 'en', '# Plain report\n\nbody\n')

    # CI-only-404 forensics: spy the handler's OWN DB call so the failure
    # line shows the EXACT (phash, lang) the handler queried with and what
    # came back. (Locally every form passes; on CI the row the fresh conn
    # provably sees is reported missing by the handler.)
    import routes.paper as _rp
    _spy_log = []
    _orig_fetch = _rp.async_fetchone

    async def _spy(sql, params=None, **kw):
        if 'paper_reports' in (sql or ''):
            row = await _orig_fetch(sql, params, **kw)
            _spy_log.append((params, bool(row and row[0])))
            return row
        return await _orig_fetch(sql, params, **kw)
    _rp.async_fetchone = _spy

    base = f'/api/v1/paper/report/export?paper_hash={_PHASH}&format=md&lang='
    cases = [
        ('double-encoded review', 'review%253Aneurips%253Aen', 200),
        ('single-encoded review', 'review%3Aneurips%3Aen', 200),
        ('raw-colon review', 'review:neurips:en', 200),
        ('plain report en', 'en', 200),
        ('genuinely-absent venue', 'review%253Aiclr%253Aen', 404),
    ]

    async def _t():
        # Sample core.DB_PATH at 5ms while the requests run: a leaked
        # background thread flipping the module global mid-request is the
        # prime suspect for the handler reading a different file than the
        # seed wrote (CI-only 404 with the row provably present).
        import lib.database._core as _core
        import threading as _th
        seen = {_core.DB_PATH}
        stop = _th.Event()

        def _sampler():
            while not stop.is_set():
                seen.add(_core.DB_PATH)
                stop.wait(0.005)
        t = _th.Thread(target=_sampler, daemon=True)
        t.start()
        try:
            async with app.test_client() as client:
                for name, langq, want in cases:
                    r = await client.get(base + langq)
                    assert r.status_code == want, \
                        f'{name}: expected {want}, got {r.status_code} ' \
                        f'body={(await r.get_data())[:120]!r} spy={_spy_log!r} | {_diag(_PHASH)}'
                    if want == 200:
                        body = (await r.get_data()).decode('utf-8', 'replace')
                        assert body, f'{name}: empty export body'
        finally:
            stop.set()
            t.join(timeout=2)
            if len(seen) > 1:
                print(f'[paper_export_test] DB_PATH FLIPPED mid-test: {seen}')

    try:
        asyncio.run(_t())
    finally:
        _rp.async_fetchone = _orig_fetch
    _ok('double/single/raw review lang all export 200; plain report ok; absent venue 404')


def test_double_encode_NC_reproduces_404():
    """BITING NEGATIVE CONTROL: monkeypatch urllib.parse.unquote as it is bound
    inside routes.paper to a no-op (the pre-fix behaviour, where the handler
    never peeled the extra encode layer). The double-encoded review key then
    404s — the exact production failure — while the raw-colon form still works
    (proving the row IS present and only the decoding is at fault)."""
    import asyncio
    app = _load_app()
    _seed_report(_PHASH, _REVIEW_LANG, _REPORT_BODY)

    import routes.paper as rp
    orig_unquote = rp.unquote
    rp.unquote = lambda s, *a, **k: s  # neuter the decode

    base = f'/api/v1/paper/report/export?paper_hash={_PHASH}&format=md&lang='

    async def _t():
        async with app.test_client() as client:
            r_bad = await client.get(base + 'review%253Aneurips%253Aen')
            assert r_bad.status_code == 404, \
                f'NC failed: double-encoded should 404 without the decode, got {r_bad.status_code}'
            # Sanity: with the escapes NOT double-encoded the row still resolves,
            # so the 404 above is purely the decoding gap, not a missing row.
            r_ok = await client.get(base + 'review:neurips:en')
            assert r_ok.status_code == 200, \
                f'NC sanity: raw-colon key should still resolve, got {r_ok.status_code} ' \
                f'body={(await r_ok.get_data())[:120]!r} | {_diag(_PHASH)}'

    try:
        asyncio.run(_t())
    finally:
        rp.unquote = orig_unquote
    _ok('NC: neutering unquote reproduces the 404 on the double-encoded review key')


def main():
    print()
    print(_color('═══ Paper Export Double-Encode Tests ═══', '36'))
    print()
    tests = [
        test_double_encoded_review_lang_exports_ok,
        test_double_encode_NC_reproduces_404,
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
