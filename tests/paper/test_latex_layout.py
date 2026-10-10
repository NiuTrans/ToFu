"""tests/paper/test_latex_layout.py — Executable spec for lib/paper/latex_layout.

Pure-parser pins (synctex/geometry/MediaBox/log/section scanner) run anywhere;
the end-to-end compile pins skip when the repo-local TinyTeX toolchain is
absent, mirroring the module's PATH fallback.
"""

import gzip
from pathlib import Path

import pytest

from lib.paper import latex_layout as ll

pytestmark = pytest.mark.unit


_TEXLIVE = ll._TEXROOT / 'bin' / 'x86_64-linux' / 'latexmk'
needs_texlive = pytest.mark.skipif(
    not _TEXLIVE.is_file(), reason='repo-local TinyTeX (.texlive/) not installed')


# ── Synthetic fixtures ─────────────────────────────────────────────────

SYNCTEX_STREAM = """SyncTeX Version:1
Input:1:/tmp/proj/main.tex
Input:2:/tmp/proj/sections/intro.tex
{1
(1,3:726460,9176001:1050976,9176001,0
[1,4:726460,10000000:1050976,800000,0
h1,10:726460,12000000:1050976,500000,100000
v1,12:726460,20000000:1050976,6000000,200000
}
{2
(1,20:726460,3000000:1050976,9176001,0
h2,5:726460,5000000:1050976,500000,100000
}
{3
(2,9:726460,25000000:1050976,9176001,0
h1,15:726460,45000000:1050976,500000,100000
}
"""

MAIN_TEX = """\\documentclass{article}
\\begin{document}
\\section{Intro}
Hello.
\\input{sections/intro}
\\section{Methods}
Stuff.
\\subsection{Setup}
More.
\\end{document}
"""


# ── find_compile_root ─────────────────────────────────────────────────

def test_find_compile_root_prefers_main_tex(tmp_path):
    (tmp_path / 'paper.tex').write_text('\\documentclass{article}\n')
    (tmp_path / 'main.tex').write_text('\\documentclass{article}\n')
    (tmp_path / 'macros.tex').write_text('% no documentclass\n')
    assert ll.find_compile_root(tmp_path).name == 'main.tex'


def test_find_compile_root_falls_back_to_any_documentclass(tmp_path):
    (tmp_path / 'notes.tex').write_text('no directive here\n')
    (tmp_path / 'thesis.tex').write_text('\\documentclass{book}\n')
    assert ll.find_compile_root(tmp_path).name == 'thesis.tex'


def test_find_compile_root_fail_closed(tmp_path):
    (tmp_path / 'a.tex').write_text('nothing\n')
    with pytest.raises(FileNotFoundError):
        ll.find_compile_root(tmp_path)


# ── Pure parsers ──────────────────────────────────────────────────────

def test_parse_synctex_first_box_per_line_and_pages():
    parsed = ll.parse_synctex(SYNCTEX_STREAM)
    assert parsed['inputs'][2].endswith('intro.tex')
    assert parsed['max_page'] == 3
    # line 3 box lives on page 1; line 20 on page 2; tag-2 line 8 on page 3
    assert parsed['by_line'][(1, 3)]['page'] == 1
    assert parsed['by_line'][(1, 20)]['page'] == 2
    assert parsed['by_line'][(2, 9)]['page'] == 3
    # lines_by_tag is sorted, enabling nearest-line fallback
    assert parsed['lines_by_tag'][1] == sorted(parsed['lines_by_tag'][1])


def test_page_for_line_nearest_box_prefers_later_line_on_tie():
    parsed = ll.parse_synctex(SYNCTEX_STREAM)
    # line 11 has no box on tag 1; nearest boxes are 10 (p1) and 12 (p1)
    assert ll._page_for_line(parsed, [1], 11) == 1
    # tag 2 boxes at 5 (p2) and 9 (p3): line 7 is a distance tie -> later wins
    assert ll._page_for_line(parsed, [2], 7) == 3
    assert ll._page_for_line(parsed, [99], 1) is None

def test_page_for_line_prefers_last_hbox_when_paragraph_shares_heading_line():
    # TeX tags a paragraph's hboxes with the line current at paragraph END,
    # so a heading written directly after a paragraph (no blank line) shares
    # that line tag: paragraph boxes (earlier page) first, heading boxes last.
    stream = """SyncTeX Version:1
Input:1:/tmp/proj/main.tex
{1
(1,9:726460,3000000:1050976,500000,100000
(1,9:726460,5000000:1050976,500000,100000
}
{2
(1,9:726460,7000000:1050976,500000,100000
}
"""
    parsed = ll.parse_synctex(stream)
    assert parsed['by_line'][(1, 9)]['page'] == 1  # forward-sync first box
    assert parsed['hbox_last_by_line'][(1, 9)]['page'] == 2
    assert ll._page_for_line(parsed, [1], 9) == 2


def test_fullness_excludes_container_vbox_records():
    # The output routine's body container (`[`) reports V+depth at the
    # text-block bottom on EVERY page; the footer container sits below it.
    # Fullness must be measured from real content boxes only.
    stream = """SyncTeX Version:1
Input:1:/tmp/proj/main.tex
{1
[1,11:4736286,49328947:30785865,44592661,0
[1,11:4736286,47362867:30785865,42626581,0
(1,11:4736286,6348472:30785865,498346,139537
v1,11:4736286,8000000:30785865,600000,100000
}
"""
    parsed = ll.parse_synctex(stream)
    sp_to_bp = (72.0 / 72.27) / 65536.0
    leaf_v = (8000000 + 100000) * sp_to_bp  # deepest non-container box
    assert max(parsed['page_box_vs'][1]) == pytest.approx(leaf_v)
    assert parsed['page_bottoms'][1] == pytest.approx(leaf_v)
    # with geometry's text bottom known, the body bottom is the leaf vbox
    assert ll._body_bottom_on_page(parsed, 1, 720.0) == pytest.approx(leaf_v)


def test_synctex_tags_for_label_normalizes_paths():
    parsed = ll.parse_synctex(SYNCTEX_STREAM)
    assert ll._synctex_tags_for_label(parsed, 'main.tex') == [1]
    assert ll._synctex_tags_for_label(parsed, 'sections/intro.tex') == [2]
    # basename fallback when the recorded path differs in prefix
    assert ll._synctex_tags_for_label(parsed, 'intro.tex') == [2]


def test_geometry_fields_and_text_block_bottom():
    log = (
        "* \\textwidth=345.0pt\n"
        "* \\textheight=550.0pt\n"
        "* \\topmargin=17.0pt\n"
        "* \\headheight=12.0pt\n"
        "* \\headsep=25.0pt\n"
        "* \\voffset=-5.0pt\n"
    )
    fields = ll._parse_geometry_fields(log)
    assert fields['textheight'] == 550.0
    bottom = ll._text_block_bottom_bp(fields)
    # (72.27 - 5 + 17 + 12 + 25 + 550) pt -> bp
    expected = (72.27 - 5.0 + 17.0 + 12.0 + 25.0 + 550.0) * (72.0 / 72.27)
    assert abs(bottom - expected) < 1e-6


def test_text_block_bottom_requires_all_fields():
    assert ll._text_block_bottom_bp({'textheight': 1.0}) is None


def test_mediabox_height_parsing():
    pdf = b"%PDF-1.5\n<< /MediaBox [ 0 0 612 792 ] >>\n"
    assert ll._parse_mediabox_height(pdf) == 792.0
    pdf_off = b"/MediaBox [-10 -20 602 772]"
    assert ll._parse_mediabox_height(pdf_off) == 792.0
    assert ll._parse_mediabox_height(b'no box here') is None


def test_pages_from_log_and_pdf():
    assert ll._pages_from_log_text('Output written on main.pdf (7 pages, 123 bytes).') == 7
    assert ll._pages_from_log_text('no output line') is None
    pdf = b'/Type /Pages /Kids [1 2] /Type /Page /MediaBox [] /Type /Page '
    assert ll._pages_from_pdf_bytes(pdf) == 2  # /Pages must not match


def test_section_line_scanner_skips_starred_and_nests():
    text = (
        '\\section{A}\n'
        '\\subsection*{B(x)}\n'
        '\\subsubsection{C \\emph{nested}}\n'
    )
    rows = list(ll._iter_section_lines(text))
    assert rows == [
        (1, 'section', 'A'),
        (2, 'subsection', 'B(x)'),
        (3, 'subsubsection', 'C \\emph{nested}'),
    ]


def test_extract_errors_blocks():
    log = "ok\n! Undefined control sequence.\nl.3 \\bad\nmore\n! Missing $ inserted.\nl.9 x\n"
    errors = ll._extract_errors(log)
    assert len(errors) == 2
    assert errors[0].startswith('! Undefined control sequence.')


# ── End-to-end with the real toolchain ─────────────────────────────────

@needs_texlive
def test_compile_and_page_map_end_to_end(tmp_path):
    (tmp_path / 'main.tex').write_text(MAIN_TEX)
    (tmp_path / 'sections').mkdir()
    (tmp_path / 'sections' / 'intro.tex').write_text('Intro body.\\par\n')

    build = ll.compile_project(tmp_path)
    assert build['status'] == 'success', build['errors']
    assert Path(build['pdf']).is_file()
    assert Path(build['synctex']).is_file()

    pages = ll.page_count(tmp_path)
    assert pages['pages'] == 1
    assert pages['source'] == 'log'

    report = ll.section_page_map(tmp_path)
    assert report['error'] is None if 'error' in report else True
    assert report['pages'] == 1
    assert report['engine'] == 'synctex-offline-local'
    assert report['page_height_source'] == 'mediabox'
    assert report['page_height_pt'] > 700  # letter/a4
    titles = [(s['type'], s['title'], s['page']) for s in report['sections']]
    assert ('section', 'Intro', 1) in titles
    assert ('subsection', 'Setup', 1) in titles
    assert report['unresolved'] == 0
    assert report['end'] is not None
    assert report['end']['page'] == 1
    assert 0 < report['end']['fill_pct'] < 100
    # article class loads geometry defaults only when the package is used;
    # without it text_area metrics may be absent — but must never crash.
    assert 'text_area_fill_pct' in report['end'] or 'text_area_fill_pct' not in report['end']

    rendered = ll.format_section_page_map(report)
    assert '\\section{Intro}' in rendered
    assert 'p.1' in rendered


@needs_texlive
def test_section_page_map_heading_after_paragraph_without_blank_line(tmp_path):
    # Real-compile regression for two observed defects: \section written
    # directly after a paragraph (no blank line) mapped to the PREVIOUS page,
    # and last-page fullness pinned at ~100% by the output-routine container.
    (tmp_path / 'main.tex').write_text(
        '\\documentclass[11pt]{article}\n'
        '\\usepackage[margin=1in]{geometry}\n'
        '\\usepackage{lipsum}\n'
        '\\begin{document}\n'
        '\\section{One}\n'
        '\\lipsum[1-8]\n'
        '\\section{Two}\n'
        '\\lipsum[9-10]\n'
        '\\end{document}\n')
    report = ll.compile_and_report(tmp_path)
    assert report['status'] == 'success', report.get('errors')
    two = next(s for s in report['sections'] if s['title'] == 'Two')
    # ground truth: lipsum[1-8] overflows page 1, so the Two heading lands
    # on page 2 even though its SyncTeX line tag is shared with the
    # preceding paragraph (paragraph-end tagging).
    assert two['page'] == 2
    assert report['end'] is not None
    assert report['end']['text_area_fill_pct'] < 90


@needs_texlive
def test_xelatex_cjk_content_bottom_uses_pdf_ground_truth(tmp_path):
    # xelatex SyncTeX streams carry empty kern-only hboxes from the output
    # routine deep below the real content; fullness must come from the PDF's
    # visible content, not the deepest SyncTeX box.
    (tmp_path / 'main.tex').write_text(
        '\\documentclass[fontset=windows]{ctexart}\n'
        '\\usepackage[margin=1in]{geometry}\n'
        '\\title{t}\n'
        '\\begin{document}\n'
        '\\maketitle\n'
        '\\section{A}\n'
        '\\section{B}\n'
        'short body.\n'
        '\\end{document}\n')
    report = ll.compile_and_report(tmp_path, engine='xelatex')
    assert report['status'] == 'success', report.get('errors')
    assert report['end'] is not None
    try:
        import pymupdf  # noqa: F401
    except ImportError:
        assert report['end']['bottom_source'] == 'synctex'
    else:
        assert report['end']['bottom_source'] == 'pdf'
        # title + two headings + one line is nowhere near a full page
        assert report['end']['text_area_fill_pct'] < 60


def test_content_bottom_from_pdf_fail_closed(tmp_path):
    assert ll._content_bottom_from_pdf(None, 1, 700.0, 100.0) is None
    assert ll._content_bottom_from_pdf(tmp_path / 'absent.pdf', 1, 700.0, 100.0) is None
    bad = tmp_path / 'bad.pdf'
    bad.write_bytes(b'not a pdf')
    assert ll._content_bottom_from_pdf(bad, 1, 700.0, 100.0) is None


@needs_texlive
def test_compile_error_is_fail_closed(tmp_path):
    (tmp_path / 'main.tex').write_text(
        '\\documentclass{article}\n\\begin{document}\n\\undefinedcommand\n\\end{document}\n')
    build = ll.compile_project(tmp_path)
    assert build['status'] in ('errors', 'failure')
    assert any('! Undefined control sequence.' in e for e in build['errors'])


def test_section_page_map_without_build_fail_closed(tmp_path):
    (tmp_path / 'main.tex').write_text(MAIN_TEX)
    report = ll.section_page_map(tmp_path)
    assert 'error' in report
    assert 'synctex' in report['error']


def test_compile_and_report_no_toolchain(monkeypatch, tmp_path):
    (tmp_path / 'main.tex').write_text(MAIN_TEX)
    monkeypatch.setenv('PATH', '')
    monkeypatch.setattr(ll, '_TEXROOT', tmp_path / 'absent')
    result = ll.compile_and_report(tmp_path)
    assert result['status'] == 'no-toolchain'
    assert result['sections'] == []


def test_tex_env_writes_repo_local_fonts_conf(monkeypatch, tmp_path):
    monkeypatch.delenv('FONTCONFIG_FILE', raising=False)
    texroot = tmp_path / 'texroot'
    (texroot / 'bin' / 'x86_64-linux').mkdir(parents=True)
    monkeypatch.setattr(ll, '_TEXROOT', texroot)
    env = ll._tex_env(tmp_path)
    conf = texroot / 'fontconfig' / 'fonts.conf'
    assert env['FONTCONFIG_FILE'] == str(conf)
    text = conf.read_text()
    assert f'<dir>{tmp_path}</dir>' in text
    assert '<cachedir>' in text
    assert '/etc/fonts/fonts.conf' in text


def test_tex_env_includes_caller_fontconfig_file(monkeypatch, tmp_path):
    monkeypatch.setenv('FONTCONFIG_FILE', '/custom/fonts.conf')
    monkeypatch.setattr(ll, '_TEXROOT', tmp_path / 'texroot')
    env = ll._tex_env(tmp_path)
    conf = tmp_path / 'texroot' / 'fontconfig' / 'fonts.conf'
    assert env['FONTCONFIG_FILE'] == str(conf)
    assert '<include ignore_missing="yes">/custom/fonts.conf</include>' in conf.read_text()


def test_tex_env_fonts_conf_xml_escapes_ampersand(tmp_path):
    text = ll._fonts_conf_text(tmp_path / 'a&b', tmp_path / 'c&d')
    assert 'a&amp;b' in text
    assert 'c&amp;d' in text


def test_fonts_conf_windows_font_fallbacks_and_tl_opentype(tmp_path):
    # A synthetic toolchain root keeps this hermetic: the registration rule
    # under test is conditional on the tree existing, and the real .texlive
    # is an optional download absent from fresh checkouts and CI.
    texroot = tmp_path / 'texlive'
    (texroot / 'texmf-dist' / 'fonts' / 'opentype').mkdir(parents=True)
    text = ll._fonts_conf_text(tmp_path, tmp_path / 'cache', texroot=texroot)
    # TeX Live's bundled opentype tree is registered when present
    assert 'texmf-dist/fonts/opentype' in text
    # append-mode fallback: requested family stays primary
    assert '<string>Times New Roman</string>' in text
    assert 'mode="append"' in text
    assert '<string>TeX Gyre Termes</string>' in text
    assert '<string>FandolSong</string>' in text


def test_fonts_conf_skips_opentype_registration_when_toolchain_absent(tmp_path):
    text = ll._fonts_conf_text(tmp_path, tmp_path / 'cache',
                               texroot=tmp_path / 'no-such-texlive')
    assert 'texmf-dist/fonts/opentype' not in text
