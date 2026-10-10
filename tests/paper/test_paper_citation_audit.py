"""Citation audit: undefined/duplicate/unused bib keys, refs, budget, dedupe."""
from __future__ import annotations

import pytest

from lib.paper.bib import parse_entries
from lib.paper.builds.citation_audit import audit_citations

pytestmark = pytest.mark.unit


class TestBibParser:
    def test_entries_and_duplicates(self):
        entries, duplicates = parse_entries(
            '@article{doe2024, author={Doe}, title={A}}\n'
            '@book{doe2024, title={Dup}}\n'
            '@inproceedings(lee2023, title={Parens})\n'
            '@string{foo = "bar"}\n'
            '@comment{ignored}\n')
        keys = [entry.key for entry in entries]
        assert keys == ['doe2024', 'lee2023']
        assert duplicates == ['doe2024']
        assert entries[0].entry_type == 'article'
        assert entries[0].line == 1 and entries[1].line == 3

    def test_malformed_input_never_raises(self):
        entries, duplicates = parse_entries('@article{broken\n@article{ok, title={T}}')
        assert duplicates == []
        assert any(entry.key == 'ok' for entry in entries)


def _tree(tmp_path, files: dict[str, str]):
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return tuple({'path': name} for name in files)


def test_undefined_citation_and_reference_are_located(tmp_path):
    files = _tree(tmp_path, {
        'main.tex': ('\\documentclass{article}\n\\begin{document}\n'
                     'See \\cite{known,ghost2024} and \\ref{fig:nope}.\n'
                     '\\bibliography{refs}\n\\end{document}'),
        'refs.bib': '@article{known, title={K}}',
    })
    rows = audit_citations(tmp_path, files)
    codes = {row['code']: row for row in rows}
    assert 'undefined_citation' in codes
    assert codes['undefined_citation']['file'] == 'main.tex'
    assert codes['undefined_citation']['line'] == 3
    assert "ghost2024" in codes['undefined_citation']['message']
    assert 'undefined_reference' in codes
    assert codes['undefined_reference']['line'] == 3
    assert 'unused_bib_entries' not in codes  # the one entry IS cited


def test_duplicate_keys_and_unused_pool_and_missing_bib(tmp_path):
    files = _tree(tmp_path, {
        'main.tex': 'Text \\cite{a}.\\end{document}',
        'refs.bib': '@article{a, title={A}}\n@article{a, title={A2}}\n@article{idle, title={I}}',
    })
    rows = audit_citations(tmp_path, files)
    codes = [row['code'] for row in rows]
    assert 'duplicate_bib_key' in codes
    assert 'unused_bib_entries' in codes
    unused = next(row for row in rows if row['code'] == 'unused_bib_entries')
    assert '1 of 2' in unused['message']  # duplicate keys count once

    nobib = _tree(tmp_path / 'nb', {'main.tex': 'Text \\cite{x}.'})
    rows = audit_citations(tmp_path / 'nb', nobib)
    assert [row['code'] for row in rows] == ['missing_bibliography']


def test_log_reported_keys_are_not_repeated(tmp_path):
    files = _tree(tmp_path, {
        'main.tex': 'Text \\cite{ghost2024}.\\bibliography{refs}',
        'refs.bib': '',
    })
    existing = [{'severity': 'warning', 'code': 'undefined_citation',
                 'message': "Citation `ghost2024' on page 1 undefined on input line 1.",
                 'file': '', 'line': None}]
    rows = audit_citations(tmp_path, files, existing=existing)
    assert not any(row['code'] == 'undefined_citation' for row in rows)


def test_nocite_star_suppresses_nothing_and_everything_counts(tmp_path):
    files = _tree(tmp_path, {
        'main.tex': 'Text \\nocite{*}.\\bibliography{refs}',
        'refs.bib': '@article{a, title={A}}',
    })
    rows = audit_citations(tmp_path, files)
    assert not any(row['code'] == 'undefined_citation' for row in rows)
    assert not any(row['code'] == 'unused_bib_entries' for row in rows)


def test_comments_are_ignored(tmp_path):
    files = _tree(tmp_path, {
        'main.tex': '% \\cite{commented}\nText.\n\\bibliography{refs}',
        'refs.bib': '',
    })
    rows = audit_citations(tmp_path, files)
    assert rows == []
