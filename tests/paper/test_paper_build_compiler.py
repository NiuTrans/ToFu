"""Real TeX output/version isolation and compiler failure/cancellation behavior."""
from __future__ import annotations

import os
import shutil
import threading
import uuid
from pathlib import Path

import pytest

from lib.paper.builds import compiler
from lib.paper.builds.contract import validate
from lib.paper.builds.sources import snapshot_sources

pytestmark = pytest.mark.unit


def _snapshot(tmp_path, text=None):
    root = tmp_path / 'source'
    root.mkdir()
    (root / 'main.tex').write_text(text or
        r'\documentclass{article}\begin{document}A reliable preview.\end{document}')
    return snapshot_sources(root, root / 'main.tex', tmp_path / 'snapshot')


def _compile(snapshot, directory, **kwargs):
    return compiler.compile_snapshot(snapshot, directory, owner_user_id=7,
                                     document_id='a' * 64, build_id=uuid.uuid4().hex, **kwargs)


def _require_tex():
    if not shutil.which('latexmk') and not (Path(__file__).resolve().parents[2] / '.texlive/bin').is_dir():
        pytest.skip('TeX toolchain unavailable')


def test_real_compilation_binds_pdf_and_synctex_to_frozen_sources(tmp_path):
    _require_tex()
    snapshot = _snapshot(tmp_path)
    before = {p.name for p in snapshot.root.iterdir()}
    result = _compile(snapshot, tmp_path / 'build')
    assert result.receipt['status'] == 'success', result.receipt
    assert result.receipt['source_digest'] == snapshot.digest
    assert result.receipt['version_verified'] is True
    assert {a['name'] for a in result.receipt['artifacts']} >= {'document.pdf', 'document.synctex.gz'}
    assert result.receipt['toolchain']
    assert {p.name for p in snapshot.root.iterdir()} == before
    assert not (tmp_path / 'source/main.pdf').exists()
    validate(result.receipt)


def test_real_tex_error_never_publishes_a_successful_pdf(tmp_path):
    _require_tex()
    snapshot = _snapshot(tmp_path, r'\documentclass{article}\begin{document}\undefinedCommand\end{document}')
    result = _compile(snapshot, tmp_path / 'build')
    assert result.receipt['status'] == 'failed'
    assert any(row['severity'] == 'error' for row in result.receipt['diagnostics'])
    assert not any(row['name'] == 'document.pdf' for row in result.receipt['artifacts'])


def test_real_bibliography_is_built_and_location_uses_frozen_labels(tmp_path):
    _require_tex()
    root = tmp_path / 'source'
    root.mkdir()
    (root / 'main.tex').write_text(
        '\\documentclass{article}\n\\begin{document}\n'
        'A citation \\cite{example}.\n\\bibliographystyle{plain}\n'
        '\\bibliography{refs}\n\\end{document}\n')
    (root / 'refs.bib').write_text('@article{example,author={A. Author},title={Evidence},journal={Journal},year={2026}}')
    snapshot = snapshot_sources(root, root / 'main.tex', tmp_path / 'snapshot')
    result = _compile(snapshot, tmp_path / 'build')
    assert result.receipt['status'] == 'success', result.receipt
    assert (tmp_path / 'build/output/main.bbl').is_file()
    from lib.paper.builds.synctex import locate
    location = locate(tmp_path / 'build/document.synctex.gz',
                      build_id=result.receipt['build_id'], file='main.tex', line=3)
    assert location['page'] == 1
    assert not any('undefined' in item['message'].lower() for item in result.receipt['diagnostics'])


def test_nested_main_uses_its_directory_and_keeps_manifest_location(tmp_path):
    _require_tex()
    root = tmp_path / 'source'
    (root / 'paper/chapters').mkdir(parents=True)
    main = root / 'paper/main.tex'
    main.write_text('\\documentclass{article}\n\\begin{document}\n'
                    '\\input{chapters/intro}\n\\end{document}\n')
    (root / 'paper/chapters/intro.tex').write_text('A nested input.\n')
    snapshot = snapshot_sources(root, main, tmp_path / 'snapshot')
    result = _compile(snapshot, tmp_path / 'build')
    assert result.receipt['status'] == 'success', result.receipt
    from lib.paper.builds.synctex import locate
    location = locate(result.directory / 'document.synctex.gz', build_id=result.receipt['build_id'],
                      file='paper/chapters/intro.tex', line=1)
    assert location['page'] == 1


def test_existing_build_directory_cannot_be_reused(tmp_path):
    snapshot = _snapshot(tmp_path)
    existing = tmp_path / 'build'
    existing.mkdir()
    (existing / 'document.pdf').write_bytes(b'%PDF-old')
    with pytest.raises(FileExistsError):
        _compile(snapshot, existing)
    assert (existing / 'document.pdf').read_bytes() == b'%PDF-old'


def test_cancelled_before_start_never_launches_compiler(tmp_path, monkeypatch):
    snapshot = _snapshot(tmp_path)
    cancel = threading.Event()
    cancel.set()
    monkeypatch.setattr(compiler, 'prepare_toolchain', lambda *a, **kw: pytest.fail('cancelled build launched'))
    result = _compile(snapshot, tmp_path / 'build', cancel=cancel)
    assert result.receipt['status'] == 'cancelled'
    assert result.receipt['artifacts'] == []


@pytest.mark.skipif(os.name != 'posix', reason='POSIX process-group fixture')
@pytest.mark.parametrize('failure', ['timeout', 'cancel', 'log_limit'])
def test_running_compiler_is_stopped_on_deadline_cancel_and_output_pressure(tmp_path, monkeypatch, failure):
    snapshot = _snapshot(tmp_path)
    fake = tmp_path / 'fake-latexmk'
    fake.write_text('#!/bin/sh\n' + ('head -c 1100000 /dev/zero\n' if failure == 'log_limit' else '')
                    + 'sleep 60\n')
    fake.chmod(0o700)
    monkeypatch.setattr(compiler, 'prepare_toolchain',
                        lambda *a, **kw: (str(fake), 'pdflatex', 'test-engine', dict(os.environ)))
    cancel = threading.Event()
    timer = threading.Timer(0.15, cancel.set) if failure == 'cancel' else None
    if timer:
        timer.start()
    try:
        result = _compile(snapshot, tmp_path / 'build', cancel=cancel,
                          timeout=0.3 if failure == 'timeout' else 3)
    finally:
        if timer:
            timer.cancel()
            timer.join()
    code = {'timeout': 'compile_timeout', 'cancel': 'cancelled', 'log_limit': 'output_limit'}[failure]
    assert result.receipt['diagnostics'][-1]['code'] == code
    assert result.receipt['status'] == ('cancelled' if failure == 'cancel' else 'failed')
    assert (tmp_path / 'build/build.log').stat().st_size <= 1048576
