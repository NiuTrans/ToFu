"""Complete Office traversal and honest caller-requested text truncation."""

import ast
import io
import pathlib

import pytest

openpyxl = pytest.importorskip('openpyxl')

pytestmark = pytest.mark.unit

from lib.doc_parser._office import (  # noqa: E402
    _extract_xlsx,
)

# A generous explicit output budget isolates complete structural traversal.
PROD_CHAR_LIMIT = 50 * 1024 * 1024


def _real_xlsx(fill) -> bytes:
    """Build a genuine .xlsx (real OOXML zip container) and return its bytes."""
    wb = openpyxl.Workbook()
    fill(wb.active)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _sheet_many_rows(ws):
    """5,000 distinct data rows — comfortably past the 1,000-row cap."""
    ws.append(['id', 'name', 'dept', 'amount'])
    for i in range(1, 5000):
        ws.append([i, f'employee_{i}', f'dept_{i % 37}', 1000 + i])


def _sheet_with_blank_gap(ws):
    """Summary block, a blank gap longer than the cap, then a detail block."""
    ws.append(['section', 'value'])
    for i in range(100):
        ws.append([f'summary_{i}', i])
    for _ in range(50 + 10):
        ws.append([None, None])
    for i in range(200):
        ws.append([f'detail_{i}', i * 7])


def _sheet_many_columns(ws):
    ws.append([f'c{i}' for i in range(200 + 50)])
    for r in range(5):
        ws.append([f'v{r}_{c}' for c in range(200 + 50)])


def _sheet_small(ws):
    ws.append(['a', 'b'])
    ws.append([1, 2])


def _digits(text: str) -> list[int]:
    """All integers in a string, with thousands separators folded away."""
    import re
    return [int(m.replace(',', '')) for m in re.findall(r'\d[\d,]*', text)]


def test_complete_rows_gaps_and_columns():
    rows = _extract_xlsx(_real_xlsx(_sheet_many_rows), PROD_CHAR_LIMIT)
    gap = _extract_xlsx(_real_xlsx(_sheet_with_blank_gap), PROD_CHAR_LIMIT)
    cols = _extract_xlsx(_real_xlsx(_sheet_many_columns), PROD_CHAR_LIMIT)
    assert 'employee_4999' in rows['text'] and not rows['warnings']
    assert 'detail_199' in gap['text'] and not gap['warnings']
    assert 'v4_249' in cols['text'] and not cols['warnings']


def test_explicit_spreadsheet_text_budget_is_honest():
    full = _extract_xlsx(_real_xlsx(_sheet_many_rows), PROD_CHAR_LIMIT)
    limited = _extract_xlsx(_real_xlsx(_sheet_many_rows), 128)
    assert limited['text'] == full['text'][:128]
    assert len(full['text']) in _digits(' '.join(limited['warnings']))


def test_an_untruncated_sheet_claims_nothing():
    """The complement: no cut fired, so no warning may be invented.

    Without this, an implementation that always warns would satisfy every
    assertion above while making the signal worthless.
    """
    res = _extract_xlsx(_real_xlsx(_sheet_small), PROD_CHAR_LIMIT)
    assert res['warnings'] == []
    assert res['textLength'] > 0


# ══════════════════════════════════════════════════════════
#  The RATCHET — no site may hand-roll a truncation warning
# ══════════════════════════════════════════════════════════

_PKG = pathlib.Path(__file__).resolve().parent.parent.parent / 'lib' / 'doc_parser'

# Words that mean "content was dropped". A warning containing one of these
# is a truncation announcement and must come from the shared constructor.
_TRUNCATION_WORDS = ('truncat', 'kept ', 'skipped', 'stopped after')


def _warning_append_strings():
    """Every literal string appended to a `warnings` list in the package.

    Returns [(file, lineno, literal_or_fstring_source)]. Scans the AST so a
    reformat cannot hide a site from the ratchet.
    """
    found = []
    for path in sorted(_PKG.glob('*.py')):
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not (isinstance(fn, ast.Attribute) and fn.attr == 'append'):
                continue
            if not (isinstance(fn.value, ast.Name)
                    and fn.value.id == 'warnings'):
                continue
            for arg in node.args:
                # A call argument (truncation_warning(...)) is compliant.
                if isinstance(arg, ast.Call):
                    continue
                try:
                    src = ast.unparse(arg)
                except Exception:                     # pragma: no cover
                    src = '<unparseable>'
                found.append((path.name, node.lineno, src))
    return found


def test_ratchet_scan_surface(capsys):
    """Print the scan surface before asserting on it (charter discipline)."""
    sites = _warning_append_strings()
    print('\n--- ratchet scan surface: non-call warnings.append sites ---')
    for f, ln, src in sites:
        print(f'  {f}:{ln}  {src[:90]}')
    print(f'  total: {len(sites)} literal site(s) across '
          f'{len(list(_PKG.glob("*.py")))} module(s)')
    # The scan must actually find the package; an empty walk would make the
    # ratchet below vacuous.
    assert list(_PKG.glob('*.py')), 'AST walk found no modules to scan'


def test_no_extractor_hand_rolls_a_truncation_warning():
    """Any NEW truncation phrasing must go through truncation_warning().

    Non-truncation warnings (missing backend, lossy decode, binary scan) are
    legitimately literal and stay allowed — the ratchet targets exactly the
    class of message that must carry a denominator.
    """
    offenders = [
        (f, ln, src) for f, ln, src in _warning_append_strings()
        if any(w in src.lower() for w in _TRUNCATION_WORDS)
    ]
    assert not offenders, (
        'These sites announce a truncation with a hand-written string, so '
        'nothing forces them to state a denominator. Route them through '
        'lib/doc_parser/_truncation.py::truncation_warning:\n'
        + '\n'.join(f'  {f}:{ln}  {src}' for f, ln, src in offenders)
    )


# ══════════════════════════════════════════════════════════
#  Per-format coverage of the same contract
# ══════════════════════════════════════════════════════════

def test_pptx_truncation_states_the_deck_size():
    """`Truncated at slide 48` of 200 read like a per-slide fault."""
    pptx = pytest.importorskip('pptx')
    prs = pptx.Presentation()
    for i in range(200):
        slide = prs.slides.add_slide(prs.slide_layouts[5])
        slide.shapes.title.text = f'Slide {i} ' + 'content ' * 50
    buf = io.BytesIO()
    prs.save(buf)

    from lib.doc_parser._office import _extract_pptx
    res = _extract_pptx(buf.getvalue(), 20_000)
    assert res['warnings'], 'slide truncation must warn'
    warn = ' '.join(res['warnings'])
    assert 200 in _digits(warn), (
        f'warning must state the total slide count; got {warn!r}')
    assert 'NOT read' in warn


def test_docx_char_truncation_states_the_original_length():
    docx = pytest.importorskip('docx')
    doc = docx.Document()
    for _ in range(3000):
        doc.add_paragraph('lorem ipsum dolor sit amet ' * 40)
    buf = io.BytesIO()
    doc.save(buf)

    from lib.doc_parser._office import _extract_docx
    res = _extract_docx(buf.getvalue(), 200_000)
    assert res['warnings'], 'char truncation must warn'
    warn = ' '.join(res['warnings'])
    nums = _digits(warn)
    assert 200_000 in nums, f'must state what was kept; got {warn!r}'
    assert any(n > 200_000 for n in nums), (
        f'must state the ORIGINAL length (denominator), not just the limit; '
        f'got {warn!r}')


def test_plaintext_truncation_states_the_original_length():
    from lib.doc_parser._plain import _extract_plaintext
    res = _extract_plaintext(b'x' * 5000, 'sample.txt', 1000)
    warn = ' '.join(res['warnings'])
    nums = _digits(warn)
    assert 1000 in nums and 5000 in nums, (
        f'must state kept AND original size; got {warn!r}')


def test_untruncated_document_claims_nothing():
    """Complement across formats: 'always warn' must not satisfy the suite."""
    from lib.doc_parser._plain import _extract_plaintext
    res = _extract_plaintext(b'a short file', 'sample.txt', 1000)
    assert res['warnings'] == []


def test_legacy_xls_retains_all_declared_rows():
    """Legacy .xls traverses its complete declared row range.

    Authoring a genuine .xls needs ``xlwt`` (xlrd 2.x reads that format but
    cannot write it). When it is absent this SKIPS rather than substituting a
    fake container — the ratchet above still covers this call site
    structurally, so the format is not left unguarded, only unexercised.
    """
    xlwt = pytest.importorskip(
        'xlwt', reason='xlwt needed to author a real legacy .xls fixture')
    pytest.importorskip('xlrd')

    wb = xlwt.Workbook()
    ws = wb.add_sheet('S')
    for r in range(1500):
        ws.write(r, 0, f'row_{r}')
        ws.write(r, 1, r)
    buf = io.BytesIO()
    wb.save(buf)

    from lib.doc_parser._legacy import _extract_xls_legacy
    res = _extract_xls_legacy(buf.getvalue(), PROD_CHAR_LIMIT)
    assert 'row_1499' in res['text']
    assert res['warnings'] == []
