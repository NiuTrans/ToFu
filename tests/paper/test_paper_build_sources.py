"""Frozen source identity, root discovery and filesystem authority boundaries."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lib.paper.builds.errors import BuildError
from lib.paper.builds.sources import read_source, resolve_main, snapshot_sources

pytestmark = pytest.mark.unit


def paper(tmp_path):
    root = tmp_path / 'paper'
    root.mkdir()
    (root / 'chapters').mkdir()
    main = root / 'main.tex'
    main.write_text(r'\documentclass{article}\begin{document}\input{chapters/intro}\end{document}')
    edited = root / 'chapters/intro.tex'
    edited.write_text('A frozen paragraph.')
    return root, main, edited


def test_nested_chapter_resolves_containing_main(tmp_path):
    root, main, edited = paper(tmp_path)
    assert resolve_main(root, edited) == main
    assert resolve_main(root, 'chapters/intro.tex') == main
    assert resolve_main(root, root) == main


def test_snapshot_cannot_recursively_copy_into_source_tree(tmp_path):
    root, main, _ = paper(tmp_path)
    with pytest.raises(BuildError, match='outside the project source'):
        snapshot_sources(root, main, root / 'snapshot')


def test_include_graph_disambiguates_and_multiple_roots_require_configuration(tmp_path):
    root, main, edited = paper(tmp_path)
    other = root / 'submission.tex'
    other.write_text(r'\documentclass{article}\begin{document}Other\end{document}')
    assert resolve_main(root, edited) == main
    other.write_text(main.read_text())
    with pytest.raises(BuildError, match='Multiple main'):
        resolve_main(root, edited)
    assert resolve_main(root, edited, main_file='submission.tex') == other


def test_tex_root_directive_and_commented_documentclass(tmp_path):
    root, main, edited = paper(tmp_path)
    edited.write_text('% !TEX root = ../main.tex\n% \\documentclass{fake}\nText')
    assert resolve_main(root, edited) == main


def test_outside_symlink_cannot_grant_read_or_compile_authority(tmp_path):
    root, _, edited = paper(tmp_path)
    outside = tmp_path / 'secret.tex'
    outside.write_text(r'\documentclass{article}')
    alias = root / 'outside.tex'
    alias.symlink_to(outside)
    with pytest.raises(BuildError, match='inside the project'):
        read_source(root, alias)
    with pytest.raises(BuildError, match='inside the project'):
        resolve_main(root, edited, main_file='../secret.tex')
    with pytest.raises(BuildError, match='inside the project'):
        snapshot_sources(root, root / 'main.tex', tmp_path / 'snapshot')


def test_snapshot_copies_binary_and_bibliography_and_digest_tracks_them(tmp_path):
    root, main, edited = paper(tmp_path)
    (root / 'figure.png').write_bytes(b'\x89PNG\x00one')
    (root / 'refs.bib').write_text('@article{a,title={First}}')
    (root / 'main.pdf').write_bytes(b'old reconstructible output')
    (root / 'main.aux').write_text('aux')
    first = snapshot_sources(root, main, tmp_path / 'first')
    names = {row['path'] for row in first.files}
    assert names == {'main.tex', 'chapters/intro.tex', 'figure.png', 'refs.bib'}
    assert first.size_bytes == sum(row['size_bytes'] for row in first.files)
    edited.write_text('Changed after snapshot')
    assert (first.root / 'chapters/intro.tex').read_text() == 'A frozen paragraph.'
    second = snapshot_sources(root, main, tmp_path / 'second')
    assert second.digest != first.digest
    (root / 'figure.png').write_bytes(b'\x89PNG\x00two')
    third = snapshot_sources(root, main, tmp_path / 'third')
    assert third.digest != second.digest
    assert json.dumps(first.files)  # binary contents never ride the wire manifest


def test_complete_snapshot_exceeds_old_file_and_byte_quotas(tmp_path):
    import hashlib
    root, main, _ = paper(tmp_path)
    for index in range(1101):
        (root / f'part-{index}.bib').write_text(f'reference {index}')
    large = root / 'figure.png'
    with large.open('wb') as output:
        for _ in range(65):
            output.write(b'x' * (1024 * 1024))
    frozen = snapshot_sources(root, main, tmp_path / 'complete')
    assert len(frozen.files) == 1104
    assert frozen.size_bytes > 64 * 1024 * 1024
    assert (frozen.root / 'figure.png').stat().st_size == 65 * 1024 * 1024
    digest = hashlib.sha256()
    with (frozen.root / 'figure.png').open('rb') as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    expected = hashlib.sha256()
    for _ in range(65):
        expected.update(b'x' * (1024 * 1024))
    assert digest.digest() == expected.digest()
    assert not (frozen.root / 'figure.png').stat().st_mode & 0o222
    print(f'paper_source_demand: {len(frozen.files)} files, {frozen.size_bytes} frozen bytes')


def test_snapshot_retries_content_change_and_returns_the_settled_revision(tmp_path, monkeypatch):
    root, main, edited = paper(tmp_path)
    from lib.paper.builds import sources
    original = sources.read_source
    calls = 0

    def changing(*args, **kwargs):
        nonlocal calls
        data = original(*args, **kwargs)
        calls += 1
        if calls == 2:
            edited.write_text('Concurrent mutation')
        return data

    monkeypatch.setattr(sources, 'read_source', changing)
    snapshot = snapshot_sources(root, main, tmp_path / 'snapshot')
    assert (snapshot.root / 'chapters/intro.tex').read_text() == 'Concurrent mutation'
    assert calls <= 8


@pytest.mark.parametrize('mutation', ['add_file', 'add_directory', 'remove', 'replace'])
def test_snapshot_retries_membership_and_identity_changes(tmp_path, monkeypatch, mutation):
    import hashlib
    from lib.paper.builds import sources
    root, main, edited = paper(tmp_path)
    original = sources.read_source
    changed = False

    def changing(*args, **kwargs):
        nonlocal changed
        data = original(*args, **kwargs)
        if not changed:
            changed = True
            if mutation == 'add_file':
                (root / 'new.bib').write_text('New bibliography')
            elif mutation == 'add_directory':
                (root / 'new').mkdir()
                (root / 'new/figure.png').write_bytes(b'New figure')
            elif mutation == 'remove':
                edited.unlink()
            else:
                edited.unlink()
                edited.write_text('Replaced chapter')
        return data

    monkeypatch.setattr(sources, 'read_source', changing)
    snapshot = snapshot_sources(root, main, tmp_path / 'snapshot')
    actual = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    assert {row['path'] for row in snapshot.files} == set(actual)
    for row in snapshot.files:
        assert row['sha256'] == hashlib.sha256(actual[row['path']]).hexdigest()
        assert (snapshot.root / row['path']).read_bytes() == actual[row['path']]


def test_snapshot_repeated_mutation_exhausts_its_finite_retry_budget(tmp_path, monkeypatch):
    from lib.paper.builds import sources
    root, main, edited = paper(tmp_path)
    original = sources.read_source
    reads = 0

    def changing(*args, **kwargs):
        nonlocal reads
        data = original(*args, **kwargs)
        reads += 1
        edited.write_text(f'Continuously changing {reads}')
        return data

    monkeypatch.setattr(sources, 'read_source', changing)
    with pytest.raises(BuildError) as failure:
        snapshot_sources(root, main, tmp_path / 'snapshot')
    assert failure.value.code == 'source_changed'
    assert reads <= sources.limit('source_snapshot_attempts') * 4


def test_snapshot_scan_keeps_evidence_beyond_old_entry_limit(tmp_path):
    root, main, _ = paper(tmp_path)
    for index in range(4101):
        (root / f'ignored-{index}.aux').write_bytes(b'')
    (root / 'late.bib').write_text('late bibliography')
    frozen = snapshot_sources(root, main, tmp_path / 'snapshot')
    assert 'late.bib' in {row['path'] for row in frozen.files}


def test_snapshot_scan_error_is_not_silently_an_empty_directory(tmp_path, monkeypatch):
    from lib.paper.builds import sources
    root, main, _ = paper(tmp_path)
    original = sources.os.scandir

    def inaccessible(path):
        if Path(path) == root / 'chapters':
            raise PermissionError('Cannot enumerate chapter dependencies')
        return original(path)

    monkeypatch.setattr(sources.os, 'scandir', inaccessible)
    with pytest.raises(BuildError) as failure:
        snapshot_sources(root, main, tmp_path / 'snapshot')
    assert failure.value.code == 'source_path'


def test_snapshot_detects_an_addition_behind_the_final_directory_scan(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from lib.paper.builds import sources
    root, main, _ = paper(tmp_path)
    original = sources.os.scandir
    root_scans = 0

    @contextmanager
    def changing(path):
        nonlocal root_scans
        is_root = not isinstance(path, int) and Path(path) == root
        if is_root:
            root_scans += 1
        with original(path) as children:
            yield children
        if is_root and root_scans == 2:
            (root / 'late.bib').write_text('Added after the final scanner passed')

    monkeypatch.setattr(sources.os, 'scandir', changing)
    snapshot = snapshot_sources(root, main, tmp_path / 'snapshot')
    assert 'late.bib' in {row['path'] for row in snapshot.files}


def test_unrelated_generated_output_does_not_invalidate_the_source_snapshot(tmp_path, monkeypatch):
    from lib.paper.builds import sources
    root, main, _ = paper(tmp_path)
    original = sources.read_source

    def generated(*args, **kwargs):
        data = original(*args, **kwargs)
        (root / 'main.pdf').write_bytes(b'Another compiler output')
        return data

    monkeypatch.setattr(sources, 'read_source', generated)
    snapshot = snapshot_sources(root, main, tmp_path / 'snapshot')
    assert {row['path'] for row in snapshot.files} == {'main.tex', 'chapters/intro.tex'}


def test_special_tex_file_is_rejected_without_blocking(tmp_path):
    import os
    import subprocess
    import sys
    if not hasattr(os, 'mkfifo'):
        pytest.skip('POSIX FIFO required')
    fifo = tmp_path / 'main.tex'
    os.mkfifo(fifo)
    script = '''
import sys
from pathlib import Path
from lib.paper.builds.sources import read_source
from lib.paper.builds.errors import BuildError
try:
    read_source(Path(sys.argv[1]), Path(sys.argv[1]) / 'main.tex')
except BuildError as exc:
    assert exc.code == 'source_path'
else:
    raise AssertionError('FIFO was accepted')
'''
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path)],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
