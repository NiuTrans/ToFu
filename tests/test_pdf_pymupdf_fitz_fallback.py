#!/usr/bin/env python3
"""PyMuPDF legacy 'fitz' module-name fallback (lib/pdf_parser/_common.py).

Regression for the integration bug the R4 real-run surfaced: PyMuPDF exposes
its top-level package as ``pymupdf`` only since 1.24.3; older installs (1.24.1
here) ship ONLY the legacy ``fitz`` name. The parser imported solely as
``pymupdf`` → HAS_PYMUPDF was False on a host that actually had the library →
every harvest/reading-mode PDF parse raised
``'NoneType' object has no attribute 'open'``.

This asserts the code resolves pymupdf via EITHER name.

Run standalone:  python tests/test_pdf_pymupdf_fitz_fallback.py
Under pytest:    pytest tests/test_pdf_pymupdf_fitz_fallback.py -m unit
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


try:
    import pytest
    pytestmark = [pytest.mark.unit]
except ImportError:
    pytest = None


def test_pymupdf_resolves_via_either_name():
    """On this host PyMuPDF is importable (as pymupdf OR fitz); the parser
    must have picked it up regardless of which name works."""
    # At least one of the two names must import.
    have_any = False
    for name in ('pymupdf', 'fitz'):
        try:
            __import__(name)
            have_any = True
            break
        except ImportError:
            continue
    if not have_any:
        # No PyMuPDF at all on this host — the fallback isn't what's under test.
        _ok('PyMuPDF not installed on host — fallback test N/A (skipped)')
        return
    from lib.pdf_parser._common import HAS_PYMUPDF
    assert HAS_PYMUPDF is True, \
        'PyMuPDF importable on host but HAS_PYMUPDF is False — the fitz fallback failed'
    import lib.pdf_parser.core as core
    import lib.pdf_parser.text as text
    assert core.pymupdf is not None, 'core.pymupdf is None despite PyMuPDF present'
    assert text.pymupdf is not None, 'text.pymupdf is None despite PyMuPDF present'
    _ok('PyMuPDF resolves (pymupdf|fitz) → HAS_PYMUPDF True, core+text bound')


def test_real_pdf_parses_end_to_end():
    """A minimal real PDF parses to non-empty text — proves the bound module
    actually works, not just that the name resolved."""
    from lib.pdf_parser._common import HAS_PYMUPDF
    if not HAS_PYMUPDF:
        _ok('PyMuPDF absent — real-parse test N/A (skipped)')
        return
    # Build a 1-page PDF with text via the bound module.
    import lib.pdf_parser.core as core
    doc = core.pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), 'Hello Tofu research pipeline.')
    pdf_bytes = doc.tobytes()
    doc.close()
    from lib.pdf_parser.core import parse_pdf
    out = parse_pdf(pdf_bytes, max_images=0, text_mode='fast')
    assert out.get('totalPages') == 1, out.get('totalPages')
    assert 'Hello Tofu' in (out.get('text') or ''), repr((out.get('text') or '')[:80])
    _ok('a real 1-page PDF parses to non-empty text via the bound module')


def main():
    print()
    print(_color('═══ PyMuPDF fitz-fallback Tests ═══', '36'))
    print()
    for fn in (test_pymupdf_resolves_via_either_name, test_real_pdf_parses_end_to_end):
        try:
            fn()
        except AssertionError as e:
            _fail(f'{fn.__name__}: {e}')
        except Exception as e:
            import traceback
            traceback.print_exc()
            _fail(f'{fn.__name__}: unexpected {type(e).__name__}: {e}')
    print()
    print(_color('═══ ALL TESTS PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    main()
