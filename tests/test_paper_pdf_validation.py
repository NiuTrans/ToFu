"""Backend regression: PDF validity gate for the paper upload/fetch write path.

Root cause (2026-07): the upload + fetch-arxiv handlers committed a file and
seeded a paper_library row whenever `os.path.getsize > 0`. A 15-byte
`%PDF-1.4`-header-only stub (a truncated / aborted upload, or a client that
POSTed an empty body) passed that check — the row became a permanent GHOST that
the reader dead-ended on ("No paper text available. Load a PDF first."). On this
box 89 of 116 PDFs under PAPER_DIR were exactly such 15-byte stubs.

The fix is a shared validity gate, `lib.pdf_parser.validate_pdf_bytes`, that
returns (ok, page_count, error): a PDF is real only if pymupdf can OPEN it AND
it has >= 1 page. The ingest paths call it BEFORE seeding a library row; the
ghost reaper treats a present-but-unopenable file as a ghost.

This suite drives the REAL parser (NOT a jsdom spy):
  • a genuine multi-page text PDF validates (ok, pages>=1) and parse_pdf
    recovers non-empty text — proving reparse works for real papers;
  • a 15-byte `%PDF-1.4` stub, an empty body, and a truncated PDF are each
    detected as INVALID (ok is False) — proving the gate rejects what used to be
    silently stored;
  • `_is_ghost_library_row` flags a row whose on-disk PDF is a stub.

DB-free except the ghost-row check, which only stats a temp file.
"""

from __future__ import annotations

import io
import os
import tempfile

import pytest

pytestmark = pytest.mark.unit


def _make_real_pdf(pages: int = 2, text: str = 'Hello world this is a real paper. ') -> bytes:
    """Build a genuine multi-page text PDF via pymupdf. Skips if unavailable."""
    pymupdf = pytest.importorskip('pymupdf')
    doc = pymupdf.open()
    for _ in range(pages):
        page = doc.new_page()
        page.insert_text((72, 72), text * 20)
    buf = doc.tobytes()
    doc.close()
    return buf


# ── The validity gate ────────────────────────────────────────────────────

def test_validate_accepts_a_real_text_pdf():
    from lib.pdf_parser import validate_pdf_bytes
    pdf = _make_real_pdf(pages=3)
    ok, pages, err = validate_pdf_bytes(pdf)
    assert ok is True, f'real PDF rejected: {err}'
    assert pages >= 1
    assert not err


def test_validate_rejects_the_15_byte_pdf_header_stub():
    """The exact shape of the 89 ghosts on disk: `%PDF-1.4` and nothing else."""
    from lib.pdf_parser import validate_pdf_bytes
    stub = b'%PDF-1.4\n'  # header line only — no objects, no xref, no body
    assert len(stub) < 20
    ok, pages, err = validate_pdf_bytes(stub)
    assert ok is False, 'a header-only stub must NOT validate'
    assert pages == 0
    assert err  # a human-readable reason is surfaced, not silently swallowed


def test_validate_rejects_empty_and_truncated():
    from lib.pdf_parser import validate_pdf_bytes
    for bad in (b'', b'   ', b'not a pdf at all', b'%PDF-1.5\n%\xe2\xe3\xcf\xd3\n1 0 obj'):
        ok, pages, err = validate_pdf_bytes(bad)
        assert ok is False, f'invalid bytes validated: {bad[:20]!r}'
        assert err


def test_real_pdf_parses_to_nonempty_text():
    """The recovery objective: reparse of a genuine PDF yields real text."""
    from lib.pdf_parser import parse_pdf
    pdf = _make_real_pdf(pages=2)
    result = parse_pdf(pdf, max_text_chars=0, max_images=0)
    text = result.get('text') or ''
    assert len(text) > 50, f'real PDF parsed to near-empty text ({len(text)} chars)'
    assert result.get('totalPages', 0) >= 1


def test_validate_is_the_gate_between_them():
    """Cross-check: exactly the bytes that fail validation are the ones that
    parse to empty — so gating on validity is equivalent to gating on
    recoverability, which is what the ingest path needs."""
    from lib.pdf_parser import validate_pdf_bytes, parse_pdf
    real = _make_real_pdf(pages=1)
    stub = b'%PDF-1.4\n'

    ok_real, _, _ = validate_pdf_bytes(real)
    ok_stub, _, _ = validate_pdf_bytes(stub)
    assert ok_real and not ok_stub

    # The real one parses to real text.
    real_len = len(parse_pdf(real, max_text_chars=0, max_images=0).get('text') or '')
    assert real_len > 50
    # The stub is unusable: parse_pdf either RAISES (unopenable stream) or yields
    # near-empty text. Both outcomes confirm the validity gate and the parser
    # agree that a stub is not recoverable.
    stub_len = None
    parse_error = None
    try:
        stub_len = len(
            parse_pdf(stub, max_text_chars=0, max_images=0).get('text') or '')
    except Exception as exc:
        parse_error = exc
    assert parse_error is not None or (stub_len is not None and stub_len < 50), (
        f'stub unexpectedly parsed to recoverable text ({stub_len} chars)')


# ── Ghost reaper now catches a present-but-invalid PDF ─────────────────────

def test_ghost_row_flags_present_but_invalid_pdf(monkeypatch):
    """A library row whose on-disk PDF is a 15-byte stub must be classified as a
    ghost (so it's skipped from listings) even though the file EXISTS."""
    import routes.paper as rp

    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setattr(rp, 'PAPER_DIR', d)
        # Write a stub file that "exists" but is not an openable PDF.
        stub_name = 'ghost_stub.pdf'
        with open(os.path.join(d, stub_name), 'wb') as f:
            f.write(b'%PDF-1.4\n')
        # And a real one.
        real_name = 'real.pdf'
        with open(os.path.join(d, real_name), 'wb') as f:
            f.write(_make_real_pdf(pages=1))

        ghost_row = {'id': 'g1', 'pdfFilename': stub_name, 'arxivId': ''}
        real_row = {'id': 'r1', 'pdfFilename': real_name, 'arxivId': ''}
        assert rp._is_ghost_library_row(ghost_row) is True, \
            'present-but-invalid stub PDF must be a ghost'
        assert rp._is_ghost_library_row(real_row) is False, \
            'a valid PDF row must not be a ghost'


def test_missing_and_empty_filename_still_ghosts(monkeypatch):
    """Regression guard: the ORIGINAL ghost conditions still hold."""
    import routes.paper as rp
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setattr(rp, 'PAPER_DIR', d)
        assert rp._is_ghost_library_row({'id': 'e', 'pdfFilename': '', 'arxivId': ''}) is True
        # empty pdfFilename BUT an arxivId → re-openable via lazy ingest, keep.
        assert rp._is_ghost_library_row({'id': 'a', 'pdfFilename': '', 'arxivId': '2501.00001'}) is False
        # present filename, file missing → ghost.
        assert rp._is_ghost_library_row({'id': 'm', 'pdfFilename': 'nope.pdf', 'arxivId': ''}) is True


def test_broken_stub_row_is_narrower_than_ghost(monkeypatch):
    """The prune predicate hard-deletes ONLY a proven stub: present + small +
    unopenable. A MISSING file (possible FUSE hiccup) and a real PDF are NOT
    prune-eligible even though a missing file is a (non-destructive) ghost."""
    import routes.paper as rp
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setattr(rp, 'PAPER_DIR', d)
        with open(os.path.join(d, 'stub.pdf'), 'wb') as f:
            f.write(b'%PDF-1.4\n')
        with open(os.path.join(d, 'real.pdf'), 'wb') as f:
            f.write(_make_real_pdf(pages=1))

        # present stub → prune-eligible.
        assert rp._is_broken_stub_row({'id': 's', 'pdfFilename': 'stub.pdf'}) is True
        # real PDF → never pruned.
        assert rp._is_broken_stub_row({'id': 'r', 'pdfFilename': 'real.pdf'}) is False
        # MISSING file → NOT prune-eligible (could be transient), even though it
        # IS a (non-destructive) ghost.
        assert rp._is_broken_stub_row({'id': 'm', 'pdfFilename': 'gone.pdf'}) is False
        assert rp._is_ghost_library_row({'id': 'm', 'pdfFilename': 'gone.pdf', 'arxivId': ''}) is True
        # empty filename → not a stub.
        assert rp._is_broken_stub_row({'id': 'e', 'pdfFilename': ''}) is False


def test_source_level_negative_control_gate_is_load_bearing():
    """Neuter validate_pdf_bytes to always-True (pre-fix behaviour) in a source
    COPY exec'd in-process, and prove the 15-byte stub then passes the gate.
    The shipped file is never modified."""
    import types
    import lib.pdf_parser.text as tmod

    src = open(tmod.__file__, encoding='utf-8').read()
    marker = 'def validate_pdf_bytes(pdf_bytes):\n    '
    assert marker in src, 'neuter marker not found — test is stale, update the marker'
    broken = src.replace(
        marker,
        'def validate_pdf_bytes(pdf_bytes):\n    return True, 1, \'\'  # NC: gate disabled\n    ',
        1)
    assert broken != src, 'negative-control patch was a no-op'

    mod = types.ModuleType('lib.pdf_parser.text_nc')
    mod.__file__ = tmod.__file__
    mod.__package__ = 'lib.pdf_parser'
    exec(compile(broken, tmod.__file__, 'exec'), mod.__dict__)

    # With the gate neutered, the stub is (wrongly) accepted → the exact bug.
    ok, _pages, _err = mod.validate_pdf_bytes(b'%PDF-1.4\n')
    assert ok is True, 'neuter did not disable the gate — test is wrong'

    # Shipped file untouched.
    assert open(tmod.__file__, encoding='utf-8').read() == src, 'shipped file was modified!'


if __name__ == '__main__':
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith('test_') and callable(v)]

    class _MP:
        def __init__(self): self._u = []
        def setattr(self, o, n, v): self._u.append((o, n, getattr(o, n))); setattr(o, n, v)
        def undo(self):
            for o, n, v in reversed(self._u): setattr(o, n, v)
            self._u = []

    failed = 0
    for fn in fns:
        mp = _MP()
        try:
            import inspect
            if 'monkeypatch' in inspect.signature(fn).parameters:
                fn(mp)
            else:
                fn()
            print('PASS', fn.__name__)
        except Exception as e:
            failed += 1
            print('FAIL', fn.__name__, '-', repr(e))
        finally:
            mp.undo()
    print('ALL PASSED' if not failed else f'{failed} FAILED')
    sys.exit(1 if failed else 0)
