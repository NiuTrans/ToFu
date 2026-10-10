"""Paper quality gate: source rules, PDF probes, dossier, scaffold, handler."""
from __future__ import annotations

import contextlib
import io

import pytest

from lib.paper.checks import (_parse_pdffonts, _parse_pdfimages,
                              format_report, run_pdf_checks, run_source_checks)
from lib.paper.review_dossier import build_review_dossier, build_scaffold

pytestmark = pytest.mark.unit


def _tree(tmp_path, files: dict[str, str]):
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return tmp_path


def _codes(rows):
    return {row['code'] for row in rows}


_CLEAN = (
    '\\documentclass{article}\n'
    '\\begin{document}\n'
    '\\section{Introduction}\n'
    'As shown by Doe~\\cite{doe2024} and in Figure~\\ref{fig:curve}.\n'
    '\\begin{figure}\n\\includegraphics{curve}\n'
    '\\caption{A nice plot.}\n\\label{fig:curve}\n\\end{figure}\n'
    '\\subsection{Background}\n\\subsection{Setup}\n'
    'More text~\\ref{fig:curve}.\n'
    '\\bibliography{refs}\n\\end{document}\n')


class TestSourceRules:
    def test_figure_without_label(self, tmp_path):
        bad = _tree(tmp_path / 'bad', {'main.tex':
                    '\\begin{figure}\n\\caption{Plot.}\n\\end{figure}\n'})
        rows = run_source_checks(bad, 'main.tex')
        assert 'figure_without_label' in _codes(rows)
        row = next(r for r in rows if r['code'] == 'figure_without_label')
        assert (row['file'], row['line'], row['severity']) == ('main.tex', 1, 'warning')

        good = _tree(tmp_path / 'good', {'main.tex':
                     '\\begin{figure}\n\\caption{Plot.}\n\\label{fig:p}\n'
                     '\\end{figure}\nSee \\ref{fig:p}.\n'})
        assert 'figure_without_label' not in _codes(
            run_source_checks(good, 'main.tex'))

    def test_figure_not_referenced(self, tmp_path):
        bad = _tree(tmp_path / 'bad', {'main.tex':
                    '\\begin{figure}\\label{fig:orphan}\\end{figure}\n'})
        assert 'figure_not_referenced' in _codes(run_source_checks(bad, 'main.tex'))
        good = _tree(tmp_path / 'good', {'main.tex':
                     '\\begin{figure}\\label{fig:used}\\end{figure}\n'
                     'See \\autoref{fig:used} and \\cref{fig:used}.\n'})
        assert 'figure_not_referenced' not in _codes(
            run_source_checks(good, 'main.tex'))

    def test_hardcoded_ref_number(self, tmp_path):
        bad = _tree(tmp_path / 'bad', {'main.tex':
                    'See Figure~3 and Table~2; Section~4 and Fig. 5 agree.\n'})
        rows = [r for r in run_source_checks(bad, 'main.tex')
                if r['code'] == 'hardcoded_ref_number']
        assert len(rows) == 4
        good = _tree(tmp_path / 'good', {'main.tex':
                     'See Figure~\\ref{fig:a} and Section~\\ref{sec:b}.\n'})
        assert 'hardcoded_ref_number' not in _codes(
            run_source_checks(good, 'main.tex'))

    def test_caption_missing_period(self, tmp_path):
        bad = _tree(tmp_path / 'bad', {'main.tex':
                    '\\caption{A nice plot}\n\\caption{Also bad\\relax}\n'})
        rows = [r for r in run_source_checks(bad, 'main.tex')
                if r['code'] == 'caption_missing_period']
        assert len(rows) == 2
        good = _tree(tmp_path / 'good', {'main.tex':
                     '\\caption{A nice plot.}\n\\caption{Really?}\n'
                     '\\caption{Wow!}\n\\caption{Curve of \\vec{f}}\n'})
        assert 'caption_missing_period' not in _codes(
            run_source_checks(good, 'main.tex'))

    def test_missing_nbsp_before_cite(self, tmp_path):
        bad = _tree(tmp_path / 'bad', {'main.tex':
                    'Shown by Smith\\cite{x} and Table\\ref{t:y}.\n'
                    '\\begin{table}\\label{t:y}\\end{table}\n'
                    '\\bibliography{refs}\n\\end{document}\n',
                    'refs.bib': '@article{x, title={X}}\n'})
        rows = [r for r in run_source_checks(bad, 'main.tex')
                if r['code'] == 'missing_nbsp_before_cite']
        assert len(rows) == 2
        assert all(r['severity'] == 'info' for r in rows)
        good = _tree(tmp_path / 'good', {'main.tex':
                     'Shown by Smith~\\cite{x} and Table~\\ref{t:y}.\n'
                     '\\begin{table}\\label{t:y}\\end{table}\n'
                     '\\bibliography{refs}\n\\end{document}\n',
                     'refs.bib': '@article{x, title={X}}\n'})
        assert 'missing_nbsp_before_cite' not in _codes(
            run_source_checks(good, 'main.tex'))

    def test_single_subsection(self, tmp_path):
        bad = _tree(tmp_path / 'bad', {'main.tex':
                    '\\section{Method}\n\\subsection{Only child}\nText.\n'
                    '\\section{Next}\nText.\n'})
        rows = [r for r in run_source_checks(bad, 'main.tex')
                if r['code'] == 'single_subsection']
        assert len(rows) == 1 and rows[0]['line'] == 1
        good = _tree(tmp_path / 'good', {'main.tex':
                     '\\section{Method}\n\\subsection{A}\n\\subsection{B}\n'})
        assert 'single_subsection' not in _codes(
            run_source_checks(good, 'main.tex'))
        delegated = _tree(tmp_path / 'in', {'main.tex':
                          '\\section{Method}\n\\input{method_body}\n'})
        assert 'single_subsection' not in _codes(
            run_source_checks(delegated, 'main.tex'))

    def test_heading_level_skip(self, tmp_path):
        bad = _tree(tmp_path / 'bad', {'main.tex':
                    '\\section{Method}\n\\subsubsection{Too deep}\n'})
        rows = [r for r in run_source_checks(bad, 'main.tex')
                if r['code'] == 'heading_level_skip']
        assert len(rows) == 1 and rows[0]['line'] == 2
        good = _tree(tmp_path / 'good', {'main.tex':
                     '\\section{Method}\n\\subsection{Fine}\n'
                     '\\subsubsection{Deep}\n'})
        assert 'heading_level_skip' not in _codes(
            run_source_checks(good, 'main.tex'))

    def test_duplicate_label(self, tmp_path):
        bad = _tree(tmp_path / 'bad', {
            'main.tex': '\\section{A}\\label{sec:x}\n\\input{other}\n',
            'other.tex': '\\section{B}\\label{sec:x}\n'})
        rows = [r for r in run_source_checks(bad, 'main.tex')
                if r['code'] == 'duplicate_label']
        assert len(rows) == 1
        assert rows[0]['severity'] == 'error'
        assert rows[0]['file'] == 'other.tex' and rows[0]['line'] == 1
        good = _tree(tmp_path / 'good', {'main.tex':
                     '\\section{A}\\label{sec:a}\n\\section{B}\\label{sec:b}\n'})
        assert 'duplicate_label' not in _codes(run_source_checks(good, 'main.tex'))

    def test_undefined_citation_and_reference_delegate_to_audit(self, tmp_path):
        bad = _tree(tmp_path, {
            'main.tex': 'See \\cite{ghost} and \\ref{fig:nope}.\n'
                        '\\bibliography{refs}\n\\end{document}\n',
            'refs.bib': '@article{known, title={K}}\n'})
        rows = run_source_checks(bad, 'main.tex')
        codes = _codes(rows)
        assert 'undefined_citation' in codes and 'undefined_reference' in codes

    def test_includegraphics_missing_file(self, tmp_path):
        bad = _tree(tmp_path / 'bad', {'main.tex':
                    '\\includegraphics{figures/curve}\n'})
        rows = [r for r in run_source_checks(bad, 'main.tex')
                if r['code'] == 'includegraphics_missing_file']
        assert len(rows) == 1 and rows[0]['severity'] == 'error'
        assert 'figures/curve' in rows[0]['message']

        good = _tree(tmp_path / 'good', {'main.tex':
                     '\\includegraphics{figures/curve}\n',
                     'figures/curve.png': 'PNG'})
        assert 'includegraphics_missing_file' not in _codes(
            run_source_checks(good, 'main.tex'))

        pathed = _tree(tmp_path / 'path', {'main.tex':
                       '\\graphicspath{{assets/}}\n\\includegraphics{logo}\n',
                       'assets/logo.pdf': 'PDF'})
        assert 'includegraphics_missing_file' not in _codes(
            run_source_checks(pathed, 'main.tex'))

    def test_todo_markers(self, tmp_path):
        bad = _tree(tmp_path / 'bad', {'main.tex':
                    'Text. % TODO: cite properly\n\\todo{fix intro}\nFIXME note\n'})
        rows = [r for r in run_source_checks(bad, 'main.tex')
                if r['code'] == 'todo_markers']
        assert len(rows) == 3 and all(r['severity'] == 'info' for r in rows)
        good = _tree(tmp_path / 'good', {'main.tex': 'All done here.\n'})
        assert 'todo_markers' not in _codes(run_source_checks(good, 'main.tex'))

    def test_comments_are_stripped_for_structure_rules(self, tmp_path):
        tree = _tree(tmp_path, {'main.tex':
                     '% \\begin{figure}\\end{figure}\n% See Figure~9\nClean.\n'})
        rows = run_source_checks(tree, 'main.tex')
        assert 'figure_without_label' not in _codes(rows)
        assert 'hardcoded_ref_number' not in _codes(rows)

    def test_rows_bounded_and_clean_tree_passes(self, tmp_path):
        messy = '\\section{S}\n' + ''.join(
            '\\begin{figure}\n\\caption{P%d.}\n\\end{figure}\n' % i
            for i in range(40))
        rows = run_source_checks(_tree(tmp_path / 'messy', {'main.tex': messy}),
                                 'main.tex')
        assert len(rows) <= 64
        assert sum(1 for r in rows if r['code'] == 'figure_without_label') == 16

        clean = _tree(tmp_path / 'clean', {'main.tex': _CLEAN,
                      'refs.bib': '@article{doe2024, title={D}}\n',
                      'curve.pdf': 'PDF'})
        rows = run_source_checks(clean, 'main.tex')
        assert not [r for r in rows if r['severity'] in ('error', 'warning')]


class TestFormatReport:
    def test_grouped_counts_and_footer(self):
        source_rows = [
            {'severity': 'error', 'code': 'duplicate_label',
             'message': 'dup', 'file': 'main.tex', 'line': 3},
            {'severity': 'warning', 'code': 'figure_without_label',
             'message': 'no label', 'file': 'main.tex', 'line': 9},
            {'severity': 'info', 'code': 'todo_markers',
             'message': 'TODO', 'file': 'main.tex', 'line': 12}]
        report = format_report(source_rows, [])
        assert report.startswith('✗ 投稿就绪检查未通过：错误 1 · 警告 1 · 提示 1')
        assert '源码检查：' in report and 'main.tex:3' in report
        assert 'PDF 检查' in report
        assert report.endswith('先修 error，再处理 warning；改完用 paper_build 复验。')

    def test_clean_report(self):
        report = format_report([], [])
        assert report.startswith('✓ 投稿就绪检查通过：错误 0 · 警告 0 · 提示 0')
        assert '无问题' in report


_PDF_FONTS = (
    'name                                 type              encoding         '
    'emb sub uni object ID\n'
    '------------------------------------ ----------------- ---------------- '
    '--- --- --- ---------\n'
    'ZLVXEH+CMR10                         Type 1C           Builtin          '
    'yes yes no       4  0\n'
    'Helvetica                            Type 1            Standard         '
    'no  no  no       8  0\n')

_PDF_IMAGES = (
    'page   num  type   width height color comp bpc  enc interp  object ID '
    'x-ppi y-ppi size ratio\n'
    '   1     0 image     600    80  rgb     3   8  jpeg  no       10  0    '
    '96    96  45K  12%\n'
    '   2     1 image    1200   900  gray    1   8  jpeg  no       12  0   '
    '300   300 200K  31%\n'
    '   1     2 smask     600    80  gray    1   8  image no       11  0    '
    '96    96  10K   3%\n')


class TestPdfProbes:
    def test_pdffonts_parsing(self):
        missing = _parse_pdffonts(_PDF_FONTS)
        assert [f['name'] for f in missing] == ['Helvetica']
        assert _parse_pdffonts('garbage\nno structure\n') == []

    def test_pdfimages_parsing(self):
        images = _parse_pdfimages(_PDF_IMAGES)
        assert len(images) == 2  # smask is not placed raster art
        assert (images[0]['page'], images[0]['x_ppi']) == (1, 96)
        assert (images[1]['x_ppi'], images[1]['y_ppi']) == (300, 300)
        assert _parse_pdfimages('junk\n') == []

    def test_poppler_missing_degrades_to_info(self, monkeypatch, tmp_path):
        import lib.paper.checks as checks

        class _Cache:
            @contextlib.contextmanager
            def open_artifact(self, owner, document_id, build_id, name):
                data = b'%PDF-1.4 fake-bytes'
                yield io.BytesIO(data), len(data)

        scheduler = type('S', (), {'cache': _Cache()})()
        monkeypatch.setattr(checks, 'get_scheduler', lambda: scheduler)
        monkeypatch.setattr(checks.shutil, 'which', lambda name: None)
        receipt = {'document_id': 'd' * 64, 'build_id': 'b' * 32,
                   'owner_user_id': 1,
                   'artifacts': [{'name': 'document.pdf', 'size_bytes': 18}]}
        rows = run_pdf_checks({'document_id': 'd' * 64}, receipt,
                              owner_user_id=1)
        codes = _codes(rows)
        assert 'pdf_size_pages' in codes
        assert 'poppler_unavailable' in codes
        assert all(r['severity'] == 'info' for r in rows)


class TestReviewDossier:
    def test_contains_rubric_and_claims_protocol(self, tmp_path):
        _tree(tmp_path, {
            'main.tex':
                '\\documentclass{article}\n\\begin{document}\n'
                '\\begin{abstract}\nWe prove things.\n\\end{abstract}\n'
                '\\section{Introduction}\nIntro text here.\n'
                '\\section{Method}\nMethod text.\n'
                '\\section{Conclusion}\nWe conclude.\n\\end{document}\n',
            'refs.bib': '@article{doe2024, title={D}}\n'})
        dossier = build_review_dossier(tmp_path, 'main.tex')
        assert '## Review rubric' in dossier
        assert '**Soundness**' in dossier and '**Reproducibility**' in dossier
        assert '## Claims audit' in dossier
        assert 'supported' in dossier and 'overstated' in dossier
        assert 'prioritized revision plan' in dossier
        assert 'We prove things.' in dossier        # abstract in full
        assert 'Intro text here.' in dossier         # introduction in full
        assert '### Bibliography entries (1)' in dossier
        assert 'doe2024' in dossier

    def test_respects_max_chars(self, tmp_path):
        _tree(tmp_path, {'main.tex':
              '\\section{Introduction}\n' + 'Long paragraph. ' * 4000})
        dossier = build_review_dossier(tmp_path, 'main.tex', max_chars=6000)
        assert len(dossier) <= 6000


class TestScaffold:
    def test_skeleton_contains_all_sections(self):
        scaffold = build_scaffold('My Great Paper',
                                  ['Introduction', 'Related Work', 'Method'])
        assert '\\title{My Great Paper}' in scaffold['main_tex']
        for slug in ('introduction', 'related-work', 'method'):
            assert '\\input{sections/%s}' % slug in scaffold['main_tex']
        files = {f['path']: f['content'] for f in scaffold['files']}
        assert files['sections/related-work.tex'].startswith(
            '\\section{Related Work}')
        assert all('% TODO' in content for content in files.values())

    def test_duplicate_titles_get_unique_slugs_and_defaults(self):
        scaffold = build_scaffold('', ['Results', 'Results'])
        paths = [f['path'] for f in scaffold['files']]
        assert len(set(paths)) == 2
        default = build_scaffold('T', [])
        assert default['files']  # falls back to a sane four-section default


# ── handler ───────────────────────────────────────────────────────────────

_LOCAL_DOC = {'source': 'local', 'path': 'main.tex', 'main_file': 'main.tex',
              'label': 'main.tex', 'document_id': 'd' * 64}


def _task(**over):
    task = {'id': 'task_paper_quality_test', 'aborted': False,
            '_userId': 1, 'convId': 'conv-1'}
    task.update(over)
    return task


def _invoke(handler_args, task=None, fn_name='paper_check'):
    from lib.tasks_pkg.handlers.paper_quality import _handle_paper_quality_tool
    task = task or _task()
    round_entry = {'toolCallId': 'tc_1', 'status': 'running', 'query': ''}
    return _handle_paper_quality_tool(
        task, None, fn_name, 'tc_1', handler_args, 1, round_entry,
        {}, None, True)


@pytest.fixture
def discovery(monkeypatch, tmp_path):
    import lib.paper.builds.documents as documents
    doc = dict(_LOCAL_DOC, project_root=str(tmp_path))
    state = {'documents': [doc], 'selected': doc}

    def fake(payload, *, owner_user_id):
        assert owner_user_id == 1
        return {'documents': list(state['documents']),
                'selected': state['selected'], 'diagnostics': [],
                'overleaf_servers': []}

    monkeypatch.setattr(documents, 'discover_documents', fake)
    import lib.tasks_pkg.handlers.paper_quality as handler
    monkeypatch.setattr(handler, '_latest_receipt', lambda d, o: (None, None))
    state['root'] = tmp_path
    return state


class TestHandler:
    def test_requires_conversation_context(self):
        _tc, content, _ = _invoke({}, task=_task(convId=None, conv_id=None))
        assert '会话' in content

    def test_paper_check_reports_rules_and_pdf_skip_note(self, discovery):
        root = discovery['root']
        _tree(root, {'main.tex':
                     '\\documentclass{article}\n\\begin{document}\n'
                     '\\begin{figure}\n\\caption{Unlabeled.}\n\\end{figure}\n'
                     'See Figure~2.\n\\end{document}\n'})
        tc_id, content, _ = _invoke({})
        assert tc_id == 'tc_1'
        assert content.startswith('✗ 投稿就绪检查未通过') or \
            content.startswith('△ 投稿就绪检查有警告')
        assert 'figure_without_label' in content
        assert 'hardcoded_ref_number' in content
        assert 'paper_build' in content  # PDF-skip guidance

    def test_paper_check_clean_tree_passes(self, discovery):
        root = discovery['root']
        _tree(root, {'main.tex': _CLEAN, 'curve.pdf': 'PDF',
                     'refs.bib': '@article{doe2024, title={D}}\n'})
        _tc, content, _ = _invoke({'scope': 'source'})
        assert content.startswith('✓ 投稿就绪检查通过')
        assert 'scope=source' in content

    def test_paper_review_returns_dossier_with_focus(self, discovery):
        root = discovery['root']
        _tree(root, {'main.tex':
                     '\\documentclass{article}\n\\begin{document}\n'
                     '\\begin{abstract}\nAbs.\n\\end{abstract}\n'
                     '\\section{Introduction}\nText.\n\\end{document}\n'})
        _tc, content, _ = _invoke({'focus': 'related work'},
                                  fn_name='paper_review')
        assert '用户指定的审稿重点：related work' in content
        assert '## Review rubric' in content and '## Claims audit' in content

    def test_paper_scaffold_needs_no_conversation(self):
        _tc, content, _ = _invoke(
            {'title': 'Fresh Paper', 'sections': ['Introduction', 'Method']},
            task=_task(convId=None, conv_id=None), fn_name='paper_scaffold')
        assert '论文骨架提案' in content
        assert 'sections/introduction.tex' in content
        assert 'sections/method.tex' in content
        assert '\\title{Fresh Paper}' in content

    def test_paper_scaffold_requires_title(self):
        _tc, content, _ = _invoke({}, fn_name='paper_scaffold')
        assert 'title' in content
