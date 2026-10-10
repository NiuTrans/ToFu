"""Location must use the same build, exact source label and PDF-point units."""
from __future__ import annotations

import gzip

import pytest

from lib.paper.builds.synctex import locate, normalize

pytestmark = pytest.mark.unit


def test_normalized_labels_survive_staging_removal_and_do_not_match_packages(tmp_path):
    root = tmp_path / 'snapshot'
    root.mkdir()
    (root / 'main.tex').write_text('source')
    compressed = tmp_path / 'raw.gz'
    raw = ('SyncTeX Version:1\n'
           f'Input:1:{root}/main.tex\n'
           'Input:2:/system/tex/main.tex\n'
           '{1\n(2,5:6553600,13107200:655360,655360,0\n}\n'
           '{2\n(1,5:6553600,13107200:655360,655360,0\n}\n')
    compressed.write_bytes(gzip.compress(raw.encode()))
    output = tmp_path / 'document.synctex.gz'
    normalize(compressed, output, source_root=root, source_files=({'path': 'main.tex'},))
    (root / 'main.tex').unlink()
    root.rmdir()
    result = locate(output, build_id='a' * 32, file='main.tex', line=5)
    assert result['build_id'] == 'a' * 32
    assert result['page'] == 2
    assert result['rects'][0]['h'] == pytest.approx(100 * 72 / 72.27)
    assert result['rects'][0]['v'] == pytest.approx(190 * 72 / 72.27)
    assert str(root) not in gzip.decompress(output.read_bytes()).decode()
    assert '/system/' not in gzip.decompress(output.read_bytes()).decode()


def test_missing_mapping_is_explicit_and_does_not_invent_a_page(tmp_path):
    result = locate(tmp_path / 'missing', build_id='b' * 32, file='intro.tex', line=3)
    assert result['page'] is None and result['rects'] == []


_TEX = '\\section{Intro}\nHello.\n\\section{Method}\n  \\subsection{A}\n'


def test_needle_to_line_exact_then_whitespace_tolerant():
    from lib.paper.builds.location import needle_to_line
    assert needle_to_line(_TEX, 'Hello.') == 2
    assert needle_to_line(_TEX, '\\section{Method}\n  \\subsection{A}') == 3
    # Block drifted but the first line still identifies the edit position.
    assert needle_to_line(_TEX, '\\section{Method}\nparagraph the file no longer has') == 3
    # The agent's new_string lost the leading indent the file carries.
    assert needle_to_line(_TEX, '\\subsection{A}\nnew') == 4


def test_needle_to_line_no_match_is_explicit():
    from lib.paper.builds.location import needle_to_line
    assert needle_to_line(_TEX, 'never appears') is None
    assert needle_to_line(_TEX, '') is None
    assert needle_to_line(_TEX, '   \n  ') is None
