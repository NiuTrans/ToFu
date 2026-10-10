"""Bounded archive extraction: formats, slip defense, limits, atomicity."""
from __future__ import annotations

import io
import tarfile
import zipfile

import pytest

from lib.paper.archive import (MAX_FILES, MAX_FILE_BYTES, extract_archive,
                               stream_to_temp)
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


def test_member_count_limit_removes_everything(tmp_path):
    archive = tmp_path / 'many.zip'
    _zip(archive, [(f'f{i}.tex', 'x') for i in range(MAX_FILES + 2)])
    dest = tmp_path / 'root'
    dest.mkdir()
    with pytest.raises(BuildError) as excinfo:
        extract_archive(archive, dest)
    assert excinfo.value.code == 'archive_limit'
    assert list(dest.iterdir()) == []


def test_single_file_byte_limit(tmp_path):
    archive = tmp_path / 'big.zip'
    _zip(archive, [('big.bin', b'x' * (MAX_FILE_BYTES + 1))])
    with pytest.raises(BuildError) as excinfo:
        extract_archive(archive, tmp_path)
    assert excinfo.value.code == 'archive_limit'


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


def test_stream_spool_is_bounded_and_private(tmp_path):
    with pytest.raises(BuildError) as excinfo:
        stream_to_temp(io.BytesIO(b'x' * 2048), max_bytes=1024)
    assert excinfo.value.code == 'archive_limit'
    path = stream_to_temp(io.BytesIO(b'x' * 100))
    try:
        assert path.stat().st_size == 100
        assert path.name.startswith('tofu-archive-')
    finally:
        path.unlink()
