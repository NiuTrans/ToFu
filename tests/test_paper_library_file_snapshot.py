"""Paper bookshelf visibility uses one fault-safe directory snapshot."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from lib.paper.library_repository import PaperLibraryEntry


pytestmark = pytest.mark.unit


class _Scandir:
    def __init__(self, entries):
        self._entries = iter(entries)

    def __enter__(self):
        return self._entries

    def __exit__(self, *_args):
        return False


def _entry(paper_id: str, filename: str, *, arxiv_id: str = ''):
    return PaperLibraryEntry(
        paper_id=paper_id,
        title=paper_id,
        pdf_filename=filename,
        arxiv_id=arxiv_id,
    )


def test_large_bookshelf_uses_one_scan_and_one_stat_per_unique_pdf(monkeypatch):
    import routes.paper_pkg._library as library

    calls = {'scandir': 0, 'stat': 0}

    class DirectoryEntry:
        def __init__(self, name):
            self.name = name

        def stat(self):
            calls['stat'] += 1
            return SimpleNamespace(st_size=4096)

    entries = [_entry(f'paper-{index}', f'{index}.pdf') for index in range(1000)]

    def scandir(_path):
        calls['scandir'] += 1
        return _Scandir(DirectoryEntry(f'{index}.pdf') for index in range(1000))

    monkeypatch.setattr(library.os, 'scandir', scandir)
    monkeypatch.setattr(
        library.os.path,
        'getsize',
        lambda _path: pytest.fail('batch listing must not stat by path per row'),
    )

    visible = library._viewable_library_papers(entries, summaries=True)

    assert len(visible) == 1000
    assert calls == {'scandir': 1, 'stat': 1000}


def test_complete_snapshot_distinguishes_missing_from_uncertain_stat(monkeypatch):
    import routes.paper_pkg._library as library

    class DirectoryEntry:
        def __init__(self, name, size=None):
            self.name = name
            self._size = size

        def stat(self):
            if self._size is None:
                raise OSError('transient FUSE stat failure')
            return SimpleNamespace(st_size=self._size)

    monkeypatch.setattr(
        library.os,
        'scandir',
        lambda _path: _Scandir([
            DirectoryEntry('healthy.pdf', 4096),
            DirectoryEntry('uncertain.pdf'),
        ]),
    )
    rows = [
        _entry('healthy', 'healthy.pdf'),
        _entry('uncertain', 'uncertain.pdf'),
        _entry('missing', 'missing.pdf'),
        _entry('empty', ''),
        _entry('recommendation', '', arxiv_id='2608.00001'),
    ]

    visible = library._viewable_library_papers(rows, summaries=True)

    assert [row['id'] for row in visible] == [
        'healthy', 'uncertain', 'recommendation']


def test_failed_directory_snapshot_fails_open_without_reviving_empty_rows(
    monkeypatch,
):
    import routes.paper_pkg._library as library

    def unavailable(_path):
        raise OSError('mount unavailable')

    monkeypatch.setattr(library.os, 'scandir', unavailable)
    visible = library._viewable_library_papers([
        _entry('uncertain', 'uncertain.pdf'),
        _entry('empty', ''),
        _entry('recommendation', '', arxiv_id='2608.00001'),
    ], summaries=True)

    assert [row['id'] for row in visible] == ['uncertain', 'recommendation']


def test_duplicate_small_stub_is_opened_and_validated_once(monkeypatch, tmp_path):
    import lib.pdf_parser.text as pdf_text
    import routes.paper_pkg._library as library

    stub = tmp_path / 'stub.pdf'
    stub.write_bytes(b'%PDF-1.4\n')
    calls = {'validate': 0}

    def invalid(_data):
        calls['validate'] += 1
        return False, 0, 'truncated'

    monkeypatch.setattr(library, 'PAPER_DIR', str(tmp_path))
    monkeypatch.setattr(pdf_text, 'validate_pdf_bytes', invalid)

    visible = library._viewable_library_papers([
        _entry('stub-one', stub.name),
        _entry('stub-two', stub.name),
    ], summaries=True)

    assert visible == []
    assert calls['validate'] == 1
