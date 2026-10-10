"""Build-artifact layout reports: pages, section map, drift honesty, fullness."""
from __future__ import annotations

import contextlib
import gzip
import hashlib
import io
import json

import pytest

from lib.paper.builds.layout import layout_report

pytestmark = pytest.mark.unit

_PT = 72.27 / 72


def _sp(bp: float) -> int:
    return int(bp * 65536 * _PT)


class _FakeBuild:
    def __init__(self, receipt, artifacts: dict[str, bytes]):
        self.receipt = receipt
        self._artifacts = artifacts

    @contextlib.contextmanager
    def open_artifact(self, name):
        data = self._artifacts[name]
        yield io.BytesIO(data), len(data)


def _receipt(build_id='b' * 32):
    return {'build_id': build_id, 'status': 'success', 'version_verified': True,
            'artifacts': [{'name': name, 'sha256': 'x' * 64, 'size_bytes': 1}
                          for name in ('document.pdf', 'document.synctex.gz',
                                       'sources.json', 'build.log')]}


def _synctex() -> bytes:
    raw = ('SyncTeX Version:1\n'
           'Input:1:main.tex\n'
           '{1\n'
           f'(1,1:{_sp(100)},{_sp(120)}:{_sp(300)},{_sp(12)},0\n'
           f'(1,5:{_sp(100)},{_sp(300)}:{_sp(300)},{_sp(12)},0\n'
           '}\n'
           '{2\n'
           f'(1,9:{_sp(100)},{_sp(130)}:{_sp(300)},{_sp(12)},0\n'
           f'(1,20:{_sp(100)},{_sp(400)}:{_sp(300)},{_sp(10)},0\n'
           '}\n'
           '{3\n'
           f'(1,30:{_sp(100)},{_sp(700)}:{_sp(300)},{_sp(10)},0\n'
           '}\n')
    return gzip.compress(raw.encode())


def _pdf_with_mediabox(height=842.0) -> bytes:
    return b'%%PDF-1.7\n1 0 obj<</MediaBox[0 0 595 ' + str(int(height)).encode() + b']>>endobj'


def _build(manifest_rows):
    artifacts = {
        'document.synctex.gz': _synctex(),
        'document.pdf': _pdf_with_mediabox(),
        'sources.json': json.dumps(manifest_rows).encode(),
        'build.log': b'log without geometry',
    }
    return _FakeBuild(_receipt(), artifacts)


def _source_tree(tmp_path, text=''):
    root = tmp_path / 'root'
    root.mkdir()
    body = text or ('\\documentclass{article}\n\\begin{document}\n'
                    '\\section{Intro}\nText.\n\\section{Method}\nMore.\n'
                    '\\subsection{Setup}\nDetails.\n\\end{document}\n')
    (root / 'main.tex').write_text(body)
    sha = hashlib.sha256(body.encode()).hexdigest()
    return root, [{'path': 'main.tex', 'sha256': sha, 'size_bytes': len(body)}]


def test_pages_sections_and_end_fullness(tmp_path):
    root, manifest = _source_tree(tmp_path)
    report = layout_report(_build(manifest), source_root=root)
    assert report['pages'] == 3
    titles = {row['title']: row['page'] for row in report['sections']}
    assert titles == {'Intro': 1, 'Method': 1, 'Setup': 2}
    assert report['end']['page'] == 3
    # body bottom 700bp of an 842bp page
    assert report['end']['fullness_page'] == pytest.approx(700 / 842, abs=0.01)
    assert report['end']['fullness_text'] is None  # no geometry block in log


def test_drifted_source_yields_pages_without_titles(tmp_path):
    root, manifest = _source_tree(tmp_path)
    (root / 'main.tex').write_text('changed after the build\n')
    report = layout_report(_build(manifest), source_root=root)
    assert report['pages'] == 3
    assert report['sections'] == []


def test_missing_synctex_is_explicit(tmp_path):
    build = _FakeBuild({**_receipt(), 'artifacts': []}, {})
    report = layout_report(build, source_root=tmp_path)
    assert report['pages'] == 0 and report['sections'] == [] and report['end'] is None
