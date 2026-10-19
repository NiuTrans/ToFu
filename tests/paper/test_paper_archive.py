"""Bounded archive extraction: formats, slip defense, limits, atomicity."""
from __future__ import annotations

import io
import tarfile
import zipfile

import pytest

from lib.paper.archive import extract_archive, stream_to_temp
from lib.paper.builds.errors import BuildError

pytestmark = pytest.mark.unit


def _zip(path, members):
    with zipfile.ZipFile(path, 'w') as bundle:
        for name, data in members:
            bundle.writestr(name, data)


def _tar_gz(path, members):
    with tarfile.open(path, 'w:gz') as bundle:
        for name, data in members:
            info = tarfile.TarInfo(name)
            payload = data.encode() if isinstance(data, str) else data
            info.size = len(payload)
            bundle.addfile(info, io.BytesIO(payload))


def test_zip_extracts_into_deduped_fresh_directory(tmp_path):
    archive = tmp_path / 'paper.zip'
    _zip(archive, [('main.tex', '\\documentclass{article}'),
                   ('chapters/intro.tex', 'intro'),
                   ('refs.bib', '@article{x,title={T}}')])
    dest = tmp_path / 'root'
    dest.mkdir()
    receipt = extract_archive(archive, dest)
    assert receipt.files == 3
    assert (receipt.directory / 'main.tex').read_text().startswith('\\documentclass')
    assert (receipt.directory / 'chapters/intro.tex').is_file()
    second = extract_archive(archive, dest)
    assert second.directory != receipt.directory
    assert second.directory.name.startswith('paper')


def test_tar_gz_roundtrip_and_symlink_members_skipped(tmp_path):
    archive = tmp_path / 'src.tar.gz'
    with tarfile.open(archive, 'w:gz') as bundle:
        for name, payload in [('main.tex', b'body'), ('figs/a.pdf', b'%PDF-1.7')]:
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            bundle.addfile(info, io.BytesIO(payload))
        link = tarfile.TarInfo('evil-link')
        link.type = tarfile.SYMTYPE
        link.linkname = '/etc/passwd'
        bundle.addfile(link)
    receipt = extract_archive(archive, tmp_path)
    assert receipt.files == 2
    assert receipt.skipped and receipt.skipped[0][1] == 'link or special file'
    assert not (receipt.directory / 'evil-link').exists()


def test_zip_slip_members_are_rejected_and_tree_is_clean(tmp_path):
    archive = tmp_path / 'slip.zip'
    _zip(archive, [('../../escape.tex', 'x'), ('ok.tex', 'y')])
    dest = tmp_path / 'root'
    dest.mkdir()
    # '..' components are stripped by the sanitizer; the payload can never
    # land outside the staging tree.
    receipt = extract_archive(archive, dest)
    assert not (tmp_path / 'escape.tex').exists()
    assert (receipt.directory / 'ok.tex').is_file()


def test_absolute_member_names_never_escape(tmp_path):
    archive = tmp_path / 'abs.zip'
    _zip(archive, [('/tmp/abs-escape.tex', 'x'), ('ok.tex', 'y')])
    receipt = extract_archive(archive, tmp_path)
    assert not (tmp_path / 'abs-escape.tex').exists()
    assert receipt.files == 2  # sanitized to a relative member


def test_complete_member_count_beyond_old_limit(tmp_path):
    archive = tmp_path / 'many.zip'
    _zip(archive, [(f'f{i}.tex', 'x') for i in range(1101)])
    dest = tmp_path / 'root'
    dest.mkdir()
    receipt = extract_archive(archive, dest)
    assert receipt.files == 1101
    assert (receipt.directory / 'f1100.tex').read_text() == 'x'


def test_complete_large_member_and_total_bytes(tmp_path):
    archive = tmp_path / 'big.zip'
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as bundle:
        with bundle.open('big.bin', 'w') as output:
            for _ in range(65):
                output.write(b'x' * (1024 * 1024))
    receipt = extract_archive(archive, tmp_path)
    assert receipt.bytes_written == 65 * 1024 * 1024
    assert (receipt.directory / 'big.bin').stat().st_size == receipt.bytes_written


def test_unknown_format_and_empty_archive_are_typed(tmp_path):
    junk = tmp_path / 'junk.txt'
    junk.write_text('not an archive')
    with pytest.raises(BuildError) as excinfo:
        extract_archive(junk, tmp_path)
    assert excinfo.value.code == 'archive_invalid'
    empty = tmp_path / 'empty.zip'
    _zip(empty, [])
    with pytest.raises(BuildError) as excinfo:
        extract_archive(empty, tmp_path)
    assert excinfo.value.code == 'archive_empty'


def test_spool_is_complete_and_private(tmp_path):
    path = stream_to_temp(io.BytesIO(b'x' * 100))
    try:
        assert path.stat().st_size == 100
        assert path.name.startswith('tofu-archive-')
    finally:
        path.unlink()


def test_default_spool_receives_source_beyond_old_128_mib_limit():
    import hashlib
    class Source:
        remaining = 129 * 1024 * 1024
        def read(self, size):
            count = min(size, self.remaining)
            self.remaining -= count
            return b'x' * count
    path = stream_to_temp(Source())
    try:
        assert path.stat().st_size == 129 * 1024 * 1024
        digest = hashlib.sha256()
        with path.open('rb') as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        expected = hashlib.sha256()
        for _ in range(129):
            expected.update(b'x' * (1024 * 1024))
        assert digest.digest() == expected.digest()
    finally:
        path.unlink()


def test_premature_member_eof_rolls_back_complete_staging_tree(tmp_path, monkeypatch):
    import lib.paper.archive as archive_module
    archive = tmp_path / 'source.zip'
    _zip(archive, [('ok.tex', 'complete'), ('broken.bin', 'required')])
    copy = archive_module._copy_stream
    def short_read(source, target, size):
        if target.name == 'broken.bin':
            source = io.BytesIO(b'x')
        return copy(source, target, size)
    monkeypatch.setattr(archive_module, '_copy_stream', short_read)
    destination = tmp_path / 'root'
    destination.mkdir()
    with pytest.raises(BuildError) as error:
        extract_archive(archive, destination)
    assert error.value.code == 'archive_invalid'
    assert list(destination.iterdir()) == []


def test_directory_collision_has_no_private_count_gate(tmp_path):
    archive = tmp_path / 'source.zip'
    name = 'main.tex'
    _zip(archive, [(name, 'complete')])
    destination = tmp_path / 'root'
    destination.mkdir()
    (destination / 'source').mkdir()
    for index in range(1, 71):
        (destination / f'source ({index})').mkdir()
    result = extract_archive(archive, destination)
    assert result.directory.name == 'source (71)'
    assert (result.directory / name).read_text() == 'complete'
