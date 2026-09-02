#!/usr/bin/env python3
"""R1 — batch harvest + parse-once ingest (lib/paper/harvest.py).

The owner's #1 acceptance criterion: the harvest ingest path MUST produce the
byte-identical ``phash`` reading-mode ingest produces, or the whole parse-once
cost story silently collapses (same paper hashed two ways → every "reuse"
actually re-parses at full cost while looking like a hit).

Proven here (all hermetic — the network download and the PDF parser are
monkeypatched, so no real arXiv fetch and no real PDF engine are needed):

  1. PHASH IDENTITY — harvest computes the same phash as the canonical
     ``_paper_hash(parse_pdf(bytes)['text'])`` chain reading-mode ingest uses.
     Driven through the real ``harvest_arxiv_id`` so it exercises the actual
     code path, not a re-derivation.
       ↳ NEUTER: inject a text-normalization step into harvest's hashing seam
         (the exact parse-once-breaking bug) → the identity assertion goes red.

  2. CACHE HIT SKIPS REPARSE — a second harvest of a paper already in the
     library (non-empty parsed_text) returns status='cache_hit' and does NOT
     call the parser again (asserted via a parse-call counter).
       ↳ NEUTER: a library row with EMPTY parsed_text is NOT a hit (we still
         parse to fill it in).

  3. BATCH SECOND-RUN ZERO REPARSE — harvest 3 ids twice; the first run parses
     all 3, the second run parses ZERO (reparse_count==0, all cache_hits).

  4. CROSS-MODE REUSE — a paper first landed by *reading-mode* ingest
     (_persist_ingested_library_row) is a cache-hit when harvested later under
     the same arxiv_id, and vice-versa — the shared bookshelf works both ways.

Run standalone:  python tests/test_paper_harvest.py
Under pytest:    pytest tests/test_paper_harvest.py -m unit
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
    pytest_plugins = ('tests._artifact_sidecar',)
    pytestmark = [pytest.mark.unit, pytest.mark.auth_mode('open')]
except ImportError:
    pytest = None


_APP = None


def _load_app():
    """Start isolated storage and return the shared native application."""
    global _APP
    if _APP is not None:
        return _APP
    os.environ.setdefault('TOFU_AUTH_MODE', 'open')
    from lib.storage import start_storage
    import server
    start_storage()
    _APP = server.app
    return _APP


# A distinctive body of "parsed text" with leading/trailing whitespace, so the
# strip-canonicalization inside _paper_hash is actually exercised: if harvest
# ever re-normalized differently, the whitespace is where it would diverge.
_FAKE_TEXT = '  Attention Is All You Need\n\nThe Transformer architecture …  \n\n'


class _ParseCounter:
    """A fake parse_pdf that counts calls, so 'did we reparse?' is a hard
    assertion, not an inference."""

    def __init__(self, text=_FAKE_TEXT, pages=7, extractor=None):
        self.calls = 0
        self.text = text
        self.pages = pages
        # The extractor tag the fake reports. Default matches the env's
        # expected winner so cache-hit tests hold on boxes with OR without
        # pymupdf4llm; pass explicitly (e.g. 'pymupdf-raw') to simulate a
        # degraded per-document fallback.
        if extractor is None:
            from lib.pdf_parser._common import HAS_PYMUPDF4LLM
            extractor = 'pymupdf4llm' if HAS_PYMUPDF4LLM else 'pymupdf-raw'
        self.extractor = extractor

    def __call__(self, pdf_bytes, **kw):
        self.calls += 1
        return {'text': self.text, 'totalPages': self.pages,
                'textLength': len(self.text), 'extractor': self.extractor}


def _patch_harvest(*, parse=None, text=_FAKE_TEXT, pages=7):
    """Monkeypatch harvest's network + parser + title seams so the test is
    hermetic. Returns (parse_counter, restore)."""
    import lib.paper.harvest as h
    counter = parse or _ParseCounter(text=text, pages=pages)
    orig_dl = h._download_pdf_bytes
    orig_parse = h.parse_pdf
    orig_title = None
    h._download_pdf_bytes = lambda arxiv_id, **kw: b'%PDF-1.4 fake bytes'
    h.parse_pdf = counter
    # Title lookup goes through lib.paper.arxiv.fetch_arxiv_title (imported
    # inside the function) — patch it at the source module.
    import lib.paper.arxiv as _ax
    orig_title = _ax.fetch_arxiv_title
    _ax.fetch_arxiv_title = lambda aid: f'Title of {aid}'

    def restore():
        h._download_pdf_bytes = orig_dl
        h.parse_pdf = orig_parse
        _ax.fetch_arxiv_title = orig_title
    return counter, restore


def _count_rows_for_arxiv(arxiv_id, user_id=1):
    from lib.paper.library_repository import PaperLibraryRepository
    return sum(
        entry.arxiv_id == arxiv_id
        for entry in PaperLibraryRepository(user_id).list_entries()
    )


def _phash_row_for_arxiv(arxiv_id, user_id=1):
    from lib.paper.library_repository import PaperLibraryRepository
    rows = [
        entry for entry in PaperLibraryRepository(user_id).list_entries()
        if entry.arxiv_id == arxiv_id
    ]
    return max(rows, key=lambda entry: entry.updated_at).paper_hash if rows else ''


def _seed_library_row(
        row_id, arxiv_id, *, parsed_text, parser_version,
        paper_hash='seedhash', user_id=1):
    """Persist a library row with an exact parser-version stamp."""
    import uuid
    from lib.paper.library_repository import (
        PaperLibraryEntry,
        PaperLibraryRepository,
    )
    now = int(time.time() * 1000)
    assert PaperLibraryRepository(user_id).put(
        PaperLibraryEntry(
            paper_id=row_id,
            title='seeded',
            arxiv_id=arxiv_id,
            paper_hash=paper_hash,
            parsed_text=parsed_text,
            parser_version=parser_version,
            page_count=3,
            created_at=now,
            updated_at=now,
        ),
        command_id=f'harvest-seed:{row_id}:{uuid.uuid4().hex}',
    )


def _parser_version_for_arxiv(arxiv_id, user_id=1):
    from lib.paper.library_repository import PaperLibraryRepository
    rows = [
        entry for entry in PaperLibraryRepository(user_id).list_entries()
        if entry.arxiv_id == arxiv_id
    ]
    return (
        max(rows, key=lambda entry: entry.updated_at).parser_version
        if rows else None
    )


# ── Test 1: phash byte-identity (the load-bearing one) + NEUTER ───────────

def test_harvest_phash_identical_to_reading_mode():
    """Harvest's stored phash == the canonical _paper_hash(parse_text) that
    reading-mode ingest would compute. Driven through the REAL harvest path."""
    _load_app()
    from lib.paper_identity import _paper_hash
    counter, restore = _patch_harvest()
    aid = f'2301.{int(time.time()) % 100000:05d}'
    try:
        import lib.paper.harvest as h
        res = h.harvest_arxiv_id(aid, folder_id='research-x', user_id=1)
        assert res.status == 'parsed', f'expected parsed, got {res.status}: {res.error}'
        # The canonical identity reading-mode would mint from the same text:
        canonical = _paper_hash(_FAKE_TEXT)
        assert res.phash == canonical, \
            f'harvest phash {res.phash!r} != canonical {canonical!r}'
        # And it is what actually landed in the row.
        assert _phash_row_for_arxiv(aid) == canonical, 'stored row phash diverged'
    finally:
        restore()
    _ok('harvest phash is byte-identical to reading-mode _paper_hash(parse_text)')


def test_harvest_phash_identity_NEUTER():
    """BITING NEUTER: inject a text-normalization step into harvest's hashing
    seam (the exact parse-once-breaking bug the design warns about) → the
    harvested phash no longer matches the canonical one."""
    _load_app()
    from lib.paper_identity import _paper_hash
    counter, restore = _patch_harvest()
    import lib.paper.harvest as h
    orig_ph = h._paper_hash
    # Neuter: harvest normalizes the text before hashing (collapse whitespace).
    import re as _re
    h._paper_hash = lambda text: orig_ph(_re.sub(r'\s+', ' ', text or ''))
    aid = f'2302.{int(time.time()) % 100000:05d}'
    try:
        res = h.harvest_arxiv_id(aid, user_id=1)
        canonical = _paper_hash(_FAKE_TEXT)
        assert res.phash != canonical, \
            'NEUTER FAILED: normalization did not change the phash (identity ' \
            'is not actually guarded by the shared _paper_hash seam)'
    finally:
        h._paper_hash = orig_ph
        restore()
    _ok('NEUTER: a normalization step in harvest breaks phash identity (guard bites)')


# ── Test 2: cache hit skips reparse + NEUTER ──────────────────────────────

def test_cache_hit_skips_reparse():
    """Second harvest of an already-parsed paper → cache_hit, parser NOT called
    the second time."""
    _load_app()
    counter, restore = _patch_harvest()
    aid = f'2303.{int(time.time()) % 100000:05d}'
    try:
        import lib.paper.harvest as h
        r1 = h.harvest_arxiv_id(aid, user_id=1)
        assert r1.status == 'parsed', r1.error
        assert counter.calls == 1, f'first harvest should parse once, got {counter.calls}'
        r2 = h.harvest_arxiv_id(aid, user_id=1)
        assert r2.status == 'cache_hit', f'second harvest should hit, got {r2.status}'
        assert counter.calls == 1, \
            f'cache hit MUST NOT reparse — parser called {counter.calls}x'
        assert r2.phash == r1.phash, 'cache hit returned a different phash'
    finally:
        restore()
    _ok('cache hit reuses the parsed row and does NOT reparse')


def test_empty_parsed_text_is_not_a_cache_hit_NEUTER():
    """A library row with EMPTY parsed_text (e.g. a saved recommendation card)
    is NOT a hit — harvest still downloads+parses to fill it in."""
    _load_app()
    counter, restore = _patch_harvest()
    aid = f'2304.{int(time.time()) % 100000:05d}'
    _seed_library_row(
        f'rec_{aid}', aid, parsed_text='', parser_version='', paper_hash='')
    try:
        import lib.paper.harvest as h
        res = h.harvest_arxiv_id(aid, user_id=1)
        assert res.status == 'parsed', \
            f'empty-text row must NOT be a hit; expected parsed, got {res.status}'
        assert counter.calls == 1, 'should have parsed to fill the empty row'
    finally:
        restore()
    _ok('a row with empty parsed_text is not a cache hit (harvest fills it)')


# ── Test 3: batch second-run zero reparse ─────────────────────────────────

def test_batch_second_run_zero_reparse():
    """Harvest a batch of 3 ids twice: first run parses all 3, second run
    parses ZERO."""
    _load_app()
    counter, restore = _patch_harvest()
    base = int(time.time()) % 90000
    ids = [f'2305.{base:05d}', f'2305.{base+1:05d}', f'2305.{base+2:05d}']
    try:
        import lib.paper.harvest as h
        out1 = h.harvest_arxiv_batch(ids, folder_id='research-y', user_id=1)
        assert out1['total'] == 3, out1
        assert out1['parsed'] == 3, f"first run should parse 3, got {out1['parsed']}"
        assert out1['reparse_count'] == 3
        assert counter.calls == 3, f'parser should run 3x, got {counter.calls}'

        out2 = h.harvest_arxiv_batch(ids, folder_id='research-y', user_id=1)
        assert out2['cache_hits'] == 3, \
            f"second run should be all cache hits, got {out2['cache_hits']}"
        assert out2['reparse_count'] == 0, \
            f"second run MUST NOT reparse, reparse_count={out2['reparse_count']}"
        assert counter.calls == 3, \
            f'parser must NOT be called again — total calls {counter.calls}'
    finally:
        restore()
    _ok('batch second run is zero-reparse (all cache hits)')


def test_transient_download_failure_is_retried():
    """R2/R3 seam v2: a TRANSIENT download failure gets one retry before giving
    up — a real paper must not be permanently dropped and starve the survey."""
    _load_app()
    counter, restore = _patch_harvest()
    aid = f'2308.{int(time.time()) % 100000:05d}'
    import lib.paper.harvest as h
    # First download attempt raises (transient), second succeeds.
    calls = {'n': 0}
    orig_dl = h._download_pdf_bytes
    def _flaky(arxiv_id, **kw):
        calls['n'] += 1
        if calls['n'] == 1:
            raise TimeoutError('simulated transient arXiv timeout')
        return b'%PDF-1.4 fake bytes'
    h._download_pdf_bytes = _flaky
    # Neutralize the retry backoff sleep so the test is fast.
    orig_sleep = h.time.sleep
    h.time.sleep = lambda s: None
    try:
        res = h.harvest_arxiv_id(aid, user_id=1)
        assert res.status == 'parsed', f'transient failure should recover, got {res.status}: {res.error}'
        assert calls['n'] == 2, f'should have retried the download once, got {calls["n"]} attempts'
    finally:
        h._download_pdf_bytes = orig_dl
        h.time.sleep = orig_sleep
        restore()
    _ok('a transient download failure is retried once and recovers (paper not dropped)')


def test_persistent_download_failure_gives_up_NEUTER():
    """Counter: an ALWAYS-failing download exhausts the retry budget and returns
    an error result (best-effort, never raises)."""
    _load_app()
    counter, restore = _patch_harvest()
    aid = f'2309.{int(time.time()) % 100000:05d}'
    import lib.paper.harvest as h
    calls = {'n': 0}
    orig_dl = h._download_pdf_bytes
    def _always_fail(arxiv_id, **kw):
        calls['n'] += 1
        raise TimeoutError('down')
    h._download_pdf_bytes = _always_fail
    orig_sleep = h.time.sleep
    h.time.sleep = lambda s: None
    try:
        res = h.harvest_arxiv_id(aid, user_id=1)
        assert res.status == 'error', res.status
        assert calls['n'] == h._HARVEST_FETCH_ATTEMPTS, \
            f'should exhaust exactly {h._HARVEST_FETCH_ATTEMPTS} attempts, got {calls["n"]}'
    finally:
        h._download_pdf_bytes = orig_dl
        h.time.sleep = orig_sleep
        restore()
    _ok('a persistent download failure exhausts the retry budget and errors (no raise)')


def test_batch_dedups_input_ids():
    """Duplicate ids in the batch input are collapsed to one parse."""
    _load_app()
    counter, restore = _patch_harvest()
    aid = f'2306.{int(time.time()) % 100000:05d}'
    try:
        import lib.paper.harvest as h
        out = h.harvest_arxiv_batch([aid, aid, aid], user_id=1)
        assert out['total'] == 1, f'input should dedup to 1, got {out["total"]}'
        assert counter.calls == 1
    finally:
        restore()
    _ok('batch dedups duplicate input ids (one parse)')


# ── Test 4: cross-mode reuse (reading-mode ↔ harvest share the bookshelf) ──

def test_reading_mode_ingest_then_harvest_is_cache_hit():
    """A paper landed by reading-mode ingest is a cache hit when harvested
    later — the shared paper_library + phash makes the two paths reuse one
    parsed copy (the '降低开销' integration the design promises)."""
    _load_app()
    from routes.paper_pkg._library import _persist_ingested_library_row
    from lib.paper_identity import _paper_hash
    counter, restore = _patch_harvest()
    aid = f'2307.{int(time.time()) % 100000:05d}'
    # Reading-mode ingest persists a row directly (server-authoritative path),
    # computing phash the canonical way from the SAME text.
    canonical = _paper_hash(_FAKE_TEXT)
    try:
        from lib.pdf_parser._common import expected_parser_version
        assert _persist_ingested_library_row(
            f'paper_read_{aid}', user_id=1, title='Read in reading mode',
            pdf_url=f'/api/paper/pdf/arxiv_{aid}.pdf',
            pdf_filename=f'arxiv_{aid}.pdf', arxiv_id=aid,
            paper_hash=canonical, parsed_text=_FAKE_TEXT, images=[], page_count=7,
            parser_version=expected_parser_version())
        # Now harvest the same arxiv_id → must be a cache hit, no parse.
        import lib.paper.harvest as h
        res = h.harvest_arxiv_id(aid, user_id=1)
        assert res.status == 'cache_hit', \
            f'harvest of a reading-mode paper should hit, got {res.status}'
        assert counter.calls == 0, 'harvest must NOT reparse a reading-mode paper'
        assert res.phash == canonical, 'cross-mode phash mismatch'
    finally:
        restore()
    _ok('reading-mode ingest → harvest is a cache hit (shared bookshelf, no reparse)')


# ── Test 5: parser_version is the parse-once cache key (owner R1 fix) ─────
#
# The probe's version predicate is INLINE SQL, so it is not monkeypatchable —
# these four tests are the biting guard BY CONSTRUCTION: every one of them
# goes red the moment the `AND parser_version = ?` predicate (or the writer
# stamping) is removed, because the seeded row would then hit unconditionally.

def test_stale_parser_version_reparses():
    """A row written by an OLDER parser stack is NOT a cache hit — a parser
    upgrade naturally invalidates and re-parses, and the row's version stamp
    is refreshed to the current one."""
    _load_app()
    counter, restore = _patch_harvest()
    aid = f'2310.{int(time.time()) % 100000:05d}'
    _seed_library_row(f'stale_{aid}', aid, parsed_text=_FAKE_TEXT,
                      parser_version='pymupdf4llm-0.0.0-stale')
    try:
        import lib.paper.harvest as h
        res = h.harvest_arxiv_id(aid, user_id=1)
        assert res.status == 'parsed', \
            f'a stale-version row must NOT hit; expected parsed, got {res.status}'
        assert counter.calls == 1, 'should have re-parsed the stale row'
        from lib.pdf_parser._common import expected_parser_version
        assert _parser_version_for_arxiv(aid) == expected_parser_version(), \
            're-parse must refresh the row to the current parser_version'
    finally:
        restore()
    _ok('a stale parser_version invalidates → re-parse + version refresh')


def test_raw_tagged_row_is_not_a_cache_hit():
    """A row stamped with the OTHER extractor's version (e.g. raw-fallback text
    in a markdown env) is NOT a hit — degraded text never serves as a
    markdown-corpus hit."""
    _load_app()
    counter, restore = _patch_harvest()
    aid = f'2311.{int(time.time()) % 100000:05d}'
    from lib.pdf_parser._common import (current_parser_version,
                                        expected_parser_version, HAS_PYMUPDF4LLM)
    # The version a DEGRADED write would carry in this env: raw when the
    # markdown pipeline is healthy, 4llm when it is not (either way it is NOT
    # the expected one).
    wrong = (current_parser_version('pymupdf-raw') if HAS_PYMUPDF4LLM
             else current_parser_version('pymupdf4llm'))
    assert wrong and wrong != expected_parser_version(), 'test premise broken'
    _seed_library_row(f'raw_{aid}', aid, parsed_text=_FAKE_TEXT,
                      parser_version=wrong)
    try:
        import lib.paper.harvest as h
        res = h.harvest_arxiv_id(aid, user_id=1)
        assert res.status == 'parsed', \
            f'a degraded-tagged row must NOT hit; expected parsed, got {res.status}'
        assert counter.calls == 1, 'should have re-parsed the degraded-tagged row'
    finally:
        restore()
    _ok('a raw/degraded-tagged row is not a markdown-corpus cache hit')


def test_null_legacy_row_is_not_a_cache_hit():
    """A legacy row with EMPTY parser_version (written before the column
    contract) is NOT a hit — the already-poisoned corpus self-heals on next
    touch with no backfill script."""
    _load_app()
    counter, restore = _patch_harvest()
    aid = f'2312.{int(time.time()) % 100000:05d}'
    _seed_library_row(f'legacy_{aid}', aid, parsed_text=_FAKE_TEXT,
                      parser_version='')
    try:
        import lib.paper.harvest as h
        res = h.harvest_arxiv_id(aid, user_id=1)
        assert res.status == 'parsed', \
            f'a legacy NULL-version row must NOT hit; expected parsed, got {res.status}'
        assert counter.calls == 1, 'should have re-parsed the legacy row'
        from lib.pdf_parser._common import expected_parser_version
        assert _parser_version_for_arxiv(aid) == expected_parser_version(), \
            'self-heal must stamp the current parser_version on the row'
    finally:
        restore()
    _ok('a legacy empty parser_version misses and self-heals on next harvest')


def test_degraded_parse_is_tagged_flagged_and_reparsed():
    """THE owner scenario: the markdown pipeline is available but the document
    falls back to raw at parse time. The write must be (a) tagged with the raw
    parser_version, (b) flagged degraded on the result, and (c) re-parsed on
    the next harvest — a degraded row must never freeze into the corpus."""
    _load_app()
    from lib.pdf_parser._common import HAS_PYMUPDF4LLM
    if not HAS_PYMUPDF4LLM:
        _ok('skipped (no pymupdf4llm in this env — degraded path unreachable)')
        return
    # Fake parse reports the RAW winner (simulates the per-document 4llm→raw
    # fallback) while the env expects the markdown pipeline.
    counter, restore = _patch_harvest(
        parse=_ParseCounter(extractor='pymupdf-raw'))
    aid = f'2313.{int(time.time()) % 100000:05d}'
    try:
        import lib.paper.harvest as h
        r1 = h.harvest_arxiv_id(aid, user_id=1)
        assert r1.status == 'parsed', r1.error
        assert r1.degraded is True, \
            'a raw-fallback parse in a markdown env must be FLAGGED degraded'
        from lib.pdf_parser._common import current_parser_version
        assert _parser_version_for_arxiv(aid) == current_parser_version('pymupdf-raw'), \
            'the row must carry the RAW parser_version, not the expected one'
        # The very next harvest must NOT treat the degraded row as a hit —
        # it re-parses (self-heal), which is exactly what un-poisons a corpus
        # after the extractor is fixed.
        r2 = h.harvest_arxiv_id(aid, user_id=1)
        assert r2.status == 'parsed', \
            f'a degraded row must NOT freeze: second harvest should re-parse, got {r2.status}'
        assert counter.calls == 2, \
            f'degraded row froze the corpus — parser called {counter.calls}x total, want 2'
    finally:
        restore()
    _ok('degraded parse: raw-tagged + flagged + re-parsed next harvest (never frozen)')


def main():
    print()
    print(_color('═══ R1 Harvest / Parse-Once Tests ═══', '36'))
    print()
    tests = [
        test_harvest_phash_identical_to_reading_mode,
        test_harvest_phash_identity_NEUTER,
        test_cache_hit_skips_reparse,
        test_empty_parsed_text_is_not_a_cache_hit_NEUTER,
        test_batch_second_run_zero_reparse,
        test_transient_download_failure_is_retried,
        test_persistent_download_failure_gives_up_NEUTER,
        test_batch_dedups_input_ids,
        test_reading_mode_ingest_then_harvest_is_cache_hit,
        test_stale_parser_version_reparses,
        test_raw_tagged_row_is_not_a_cache_hit,
        test_null_legacy_row_is_not_a_cache_hit,
        test_degraded_parse_is_tagged_flagged_and_reparsed,
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
    from tests._standalone_guard import guard_standalone_storage
    guard_standalone_storage('test_paper_harvest.standalone')
    main()
