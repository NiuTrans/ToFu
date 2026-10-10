"""Structured build diagnostics: file/line extraction, codes, dedupe, bounds."""
from __future__ import annotations

from pathlib import Path

import pytest

from lib.paper.builds.compiler import _diagnostics

pytestmark = pytest.mark.unit


def test_file_line_error_records_are_located_and_relative(tmp_path):
    root = tmp_path / 'tree'
    (root / 'chapters').mkdir(parents=True)
    (root / 'main.tex').write_text('main')
    (root / 'chapters/intro.tex').write_text('intro')
    log = ('This is pdfTeX, Version 3.14159265\n'
           './main.tex:5: Undefined control sequence.\n'
           'l.5 \\nope\n'
           './chapters/intro.tex:12: LaTeX Error: Missing $ inserted.\n')
    rows = _diagnostics(log, source_root=root, base_dir=root)
    assert [row['severity'] for row in rows] == ['error', 'error']
    assert rows[0]['file'] == 'main.tex' and rows[0]['line'] == 5
    assert rows[0]['code'] == 'tex_error'
    assert rows[1]['file'] == 'chapters/intro.tex' and rows[1]['line'] == 12
    for row in rows:
        assert len(row['message']) <= 2000


def test_absolute_paths_under_root_are_relativized_and_outside_paths_kept(tmp_path):
    root = tmp_path / 'tree'
    root.mkdir()
    (root / 'main.tex').write_text('main')
    outside = tmp_path / 'elsewhere.sty'
    outside.write_text('sty')
    log = (f'{root}/main.tex:3: Undefined control sequence.\n'
           f'{outside}:9: LaTeX Error: Boom.\n')
    rows = _diagnostics(log, source_root=root, base_dir=root)
    assert rows[0]['file'] == 'main.tex'
    assert rows[1]['file'] == str(outside)


def test_classic_error_uses_following_log_line_and_file_context(tmp_path):
    root = tmp_path / 'tree'
    root.mkdir()
    (root / 'main.tex').write_text('main')
    log = ('./main.tex:8: Undefined control sequence.\n'
           'l.8 \\broken\n'
           '! LaTeX Error: Something classic.\n'
           'l.21 \\again\n')
    rows = _diagnostics(log, source_root=root, base_dir=root)
    assert rows[1]['severity'] == 'error'
    assert rows[1]['line'] == 21
    assert rows[1]['file'] == 'main.tex'


def test_warning_position_and_specialized_codes(tmp_path):
    root = tmp_path / 'tree'
    root.mkdir()
    (root / 'main.tex').write_text('main')
    log = ('./main.tex:4: Undefined control sequence.\n'
           "LaTeX Warning: Citation `doe2024' on page 1 undefined on input line 30.\n"
           "LaTeX Warning: Reference `fig:x' on page 2 undefined on input line 41.\n"
           "LaTeX Warning: There were undefined references.\n")
    rows = _diagnostics(log, source_root=root, base_dir=root)
    codes = [row['code'] for row in rows]
    assert codes == ['tex_error', 'undefined_citation', 'undefined_reference', 'tex_warning']
    assert rows[1]['line'] == 30 and rows[1]['file'] == 'main.tex'
    assert rows[2]['line'] == 41
    assert rows[3]['line'] is None


def test_missing_package_has_dedicated_code(tmp_path):
    root = tmp_path / 'tree'
    root.mkdir()
    (root / 'main.tex').write_text('main')
    log = ("! LaTeX Error: File `tofu-missing.sty' not found.\n"
           'l.3 \\usepackage{tofu-missing}\n')
    rows = _diagnostics(log, source_root=root, base_dir=root)
    assert rows[0]['code'] == 'missing_package'
    assert rows[0]['line'] == 3


def test_repeated_pass_records_are_deduped_and_bounded(tmp_path):
    root = tmp_path / 'tree'
    root.mkdir()
    (root / 'main.tex').write_text('main')
    repeated = './main.tex:5: Undefined control sequence.\nl.5 \\nope\n' * 40
    rows = _diagnostics(repeated, source_root=root, base_dir=root)
    assert len(rows) == 1
    flood = ''.join(f'./main.tex:{line}: Error number {line}.\n' for line in range(1, 400))
    rows = _diagnostics(flood, source_root=root, base_dir=root)
    from lib.paper.builds.contract import limit
    assert len(rows) == limit('diagnostics')
    for row in rows:
        from lib.paper.builds.contract import validate
        validate({'severity': row['severity'], 'code': row['code'],
                  'message': row['message'], 'file': row['file'], 'line': row['line']},
                 'PaperBuildDiagnosticV1')


def test_plain_messages_without_location_still_validate():
    rows = _diagnostics('! Undefined control sequence.\nl.1 x\nSome noise\n')
    assert rows[0]['file'] == '' and rows[0]['line'] == 1
    assert rows[0]['severity'] == 'error'
