"""Deterministic archives and their manifests (README §2), on synthetic trees."""
import gzip
import hashlib
import json
import tarfile

import pytest

from runtime_builder.tree import (
    MANIFEST_NAME, BlobStore, Tree, TreeError, canonical_json, write_archive)


def _tree(tmp_path, content=b'payload\n'):
    blobs = BlobStore(tmp_path / 'blobs')
    tree = Tree(blobs)
    tree.add_dir('usr', 0o755)
    tree.add_dir('usr/bin', 0o755)
    tree.add_symlink('bin', 'usr/bin')
    tree.add_bytes('usr/bin/tool', content, 0o755)
    tree.add_bytes('usr/bin/su', b'setuid\n', 0o4755)
    tree.add_hardlink('usr/bin/tool-alias', 'usr/bin/tool')
    tree.add_dir('tmp', 0o1777)
    tree.add_bytes('etc/' + 'x' * 120, b'long name\n')
    tree.add_symlink('etc/localtime', '/usr/share/zoneinfo/Etc/UTC')
    return tree


def _write(tmp_path, name, tree):
    return write_archive(tree, kind='payload', abi='arm64-v8a', label='1.0-abc',
                         inputs={'test': True}, out_dir=tmp_path / name)


def test_two_writes_are_byte_identical(tmp_path):
    first = _write(tmp_path, 'a', _tree(tmp_path / 'one'))
    second = _write(tmp_path, 'b', _tree(tmp_path / 'two'))
    assert first.path.read_bytes() == second.path.read_bytes()
    assert first.version == second.version
    assert first.path.name == f'payload-{first.version}.tgz'
    assert not first.path.name.endswith('.gz'), 'AGP un-gzips *.gz assets (contract v1.1)'


def test_version_follows_content(tmp_path):
    first = _write(tmp_path, 'a', _tree(tmp_path / 'one'))
    changed = _write(tmp_path, 'b', _tree(tmp_path / 'two', content=b'other\n'))
    assert first.version != changed.version
    assert first.version.startswith('1.0-abc-') and len(first.version.rsplit('-', 1)[1]) == 12


def test_gzip_header_has_no_name_and_no_time(tmp_path):
    data = _write(tmp_path, 'a', _tree(tmp_path / 'one')).path.read_bytes()
    assert data[:3] == b'\x1f\x8b\x08'
    flags, mtime = data[3], int.from_bytes(data[4:8], 'little')
    assert flags & 0x08 == 0, 'FNAME must be absent (gzip -n)'
    assert mtime == 0


def test_manifest_first_and_matches_every_member(tmp_path):
    result = _write(tmp_path, 'a', _tree(tmp_path / 'one'))
    with tarfile.open(result.path) as archive:
        members = archive.getmembers()
        assert members[0].name == MANIFEST_NAME
        manifest = json.loads(archive.extractfile(members[0]).read())
        assert manifest == result.manifest
        rows = manifest['entries']
        names = [member.name.rstrip('/') for member in members[1:]]
        assert names == [row['path'] for row in rows]
        assert names == sorted(names, key=lambda name: name.encode('utf-8'))
        assert MANIFEST_NAME not in names
        for member, row in zip(members[1:], rows):
            assert (member.uid, member.gid, member.uname, member.gname, member.mtime) == (0, 0, '', '', 0)
            assert f'{member.mode:04o}' == row['mode']
            kind = {tarfile.REGTYPE: 'file', tarfile.DIRTYPE: 'dir', tarfile.SYMTYPE: 'symlink',
                    tarfile.LNKTYPE: 'hardlink'}[member.type]
            assert kind == row['type']
            if kind == 'file':
                content = archive.extractfile(member).read()
                assert len(content) == row['size']
                assert hashlib.sha256(content).hexdigest() == row['sha256']
            if kind in ('symlink', 'hardlink'):
                assert member.linkname == row['target']
    body = {key: value for key, value in manifest.items() if key != 'version'}
    digest = hashlib.sha256(canonical_json(body).encode('utf-8')).hexdigest()
    assert manifest['version'] == f'1.0-abc-{digest[:12]}'
    assert set(manifest) == {'abi', 'entries', 'format', 'inputs', 'kind', 'version'}


def test_modes_types_and_links_survive(tmp_path):
    rows = {row['path']: row for row in _write(tmp_path, 'a', _tree(tmp_path / 'one')).manifest['entries']}
    assert rows['usr/bin/su']['mode'] == '4755'
    assert rows['tmp']['mode'] == '1777'
    assert rows['bin'] == {'mode': '0777', 'path': 'bin', 'target': 'usr/bin', 'type': 'symlink'}
    alias = rows['usr/bin/tool-alias']
    assert alias == {**rows['usr/bin/tool'], 'path': 'usr/bin/tool-alias'}, \
        'a hard link is written as its own regular file'
    assert rows['etc/localtime']['target'] == '/usr/share/zoneinfo/Etc/UTC'
    assert rows['etc']['type'] == 'dir', 'implicit parents become directory entries'


def test_archives_carry_no_hard_links(tmp_path):
    # Android apps cannot create hard links (contract v1.1).
    tree = Tree(BlobStore(tmp_path / 'blobs'))
    tree.add_bytes('z/original', b'same\n', 0o755)
    tree.add_hardlink('a/link', 'z/original')
    result = _write(tmp_path, 'a', tree)
    rows = {row['path']: row for row in result.manifest['entries']}
    assert rows['a/link'] == {'mode': '0755', 'path': 'a/link', 'sha256': rows['z/original']['sha256'],
                              'size': 5, 'type': 'file'}
    with tarfile.open(result.path) as archive:
        assert not [member.name for member in archive if member.islnk()]
        assert archive.extractfile('a/link').read() == b'same\n'


def test_parent_resolution_follows_directory_symlinks(tmp_path):
    tree = _tree(tmp_path)
    tree.add_symlink('lib', 'usr/lib')
    tree.add_dir('usr/lib')
    tree.add_symlink('usr/lib/alias', '../bin')
    tree.add_symlink('opt-link', '/usr')
    assert tree.resolve_parent('lib/aarch64-linux-gnu/libz.so.1') == 'usr/lib/aarch64-linux-gnu/libz.so.1'
    assert tree.resolve_parent('lib/alias/tool') == 'usr/bin/tool'
    assert tree.resolve_parent('opt-link/bin/x') == 'usr/bin/x'
    assert tree.resolve_parent('bin') == 'bin', 'the last component is not followed'


@pytest.mark.parametrize('path', ['../escape', 'a/../../b', '/', '.'])
def test_unsafe_paths_are_refused(tmp_path, path):
    with pytest.raises(TreeError):
        Tree(BlobStore(tmp_path / 'blobs')).add_bytes(path, b'x')


def test_conflicting_entries_are_refused(tmp_path):
    tree = _tree(tmp_path)
    with pytest.raises(TreeError):
        tree.add_bytes('usr/bin/tool', b'again\n')
    with pytest.raises(TreeError):
        tree.add_bytes('usr/bin/tool/child', b'x')


def test_streamed_gzip_member_is_one_stream(tmp_path):
    data = _write(tmp_path, 'a', _tree(tmp_path / 'one')).path.read_bytes()
    assert gzip.decompress(data)[-1024:] == b'\0' * 1024
