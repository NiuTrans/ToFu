"""Contract checks over built artifacts (README §1-§6, §8).

Run with ``--runtime-out <out>/arm64-v8a`` (and ``--runtime-out-repeat`` for
the byte-identity check of a second build from the same inputs).
"""
import gzip
import hashlib
import json
from pathlib import Path
import posixpath
import re
import shutil
import subprocess
import tarfile

import pytest

from runtime_builder import debs
from runtime_builder.payload import EXCLUDED_SOURCE_TREES
from runtime_builder.tree import MANIFEST_NAME, canonical_json

REPO = Path(__file__).resolve().parents[3]

VERSION = re.compile(r'^[0-9A-Za-z.-]+$')
PYC_UNCHECKED_HASH = 0b01  # PEP 552 flags: hash-based, do not check the source


def _assets(out: Path) -> Path:
    return out / 'assets' / 'kernel'


@pytest.fixture(scope='module')
def index(runtime_out):
    return json.loads((_assets(runtime_out) / 'index.json').read_text())


@pytest.fixture(scope='module')
def archives(runtime_out, index):
    """kind -> (manifest, {path: member}, archive path), read once."""
    loaded = {}
    for kind in ('rootfs', 'payload'):
        path = _assets(runtime_out) / index[kind]['file']
        with tarfile.open(path) as archive:
            members = archive.getmembers()
            manifest = json.loads(archive.extractfile(members[0]).read())
            loaded[kind] = (manifest, members, path)
    return loaded


def test_index_shape(runtime_out, index):
    assert set(index) == {'abi', 'format', 'payload', 'rootfs'}
    assert index['format'] == 1 and index['abi'] == 'arm64-v8a'
    for kind in ('rootfs', 'payload'):
        row = index[kind]
        assert set(row) == {'file', 'sha256', 'size', 'version'}
        assert VERSION.match(row['version'])
        assert row['file'] == f'{kind}-{row["version"]}.tgz'
        data = (_assets(runtime_out) / row['file']).read_bytes()
        assert len(data) == row['size']
        assert hashlib.sha256(data).hexdigest() == row['sha256']
    text = (_assets(runtime_out) / 'index.json').read_text()
    assert text == json.dumps(index, indent=2, sort_keys=True) + '\n'
    assert sorted(path.name for path in _assets(runtime_out).iterdir()) == sorted(
        ['index.json', index['rootfs']['file'], index['payload']['file']])


def test_no_output_name_ends_in_gz(runtime_out):
    # The Android Gradle Plugin un-gzips *.gz assets at merge time (contract v1.1).
    names = [path.name for path in runtime_out.rglob('*') if path.is_file()]
    assert names and not [name for name in names if name.endswith('.gz')]


@pytest.mark.parametrize('kind', ['rootfs', 'payload'])
def test_archive_is_deterministic_tar_with_manifest_first(archives, index, kind):
    manifest, members, path = archives[kind]
    raw = path.read_bytes()
    assert raw[3] & 0x08 == 0 and raw[4:8] == b'\0\0\0\0', 'gzip -n: no name, mtime 0'
    assert members[0].name == MANIFEST_NAME
    assert manifest['kind'] == kind and manifest['abi'] == 'arm64-v8a'
    assert manifest['version'] == index[kind]['version']
    rows = manifest['entries']
    body = {key: value for key, value in manifest.items() if key != 'version'}
    digest = hashlib.sha256(canonical_json(body).encode()).hexdigest()[:12]
    assert manifest['version'].endswith('-' + digest)
    names = [member.name.rstrip('/') for member in members[1:]]
    assert names == [row['path'] for row in rows]
    assert names == sorted(names, key=str.encode)
    seen_dirs, seen_files = set(), set()
    for member, row in zip(members[1:], rows):
        assert (member.uid, member.gid, member.uname, member.gname, member.mtime) == (0, 0, '', '', 0)
        assert f'{member.mode:04o}' == row['mode']
        parent = row['path'].rpartition('/')[0]
        assert not parent or parent in seen_dirs, f'{row["path"]}: parent not an earlier dir'
        if row['type'] == 'dir':
            assert member.isdir()
            seen_dirs.add(row['path'])
        elif row['type'] == 'file':
            assert member.isreg() and member.size == row['size']
            seen_files.add(row['path'])
        else:
            assert row['type'] == 'symlink', 'no hard links: Android apps cannot create them'
            assert member.issym() and member.linkname == row['target']


@pytest.mark.parametrize('kind', ['rootfs', 'payload'])
def test_every_file_matches_its_manifest_digest(archives, kind):
    manifest, _members, path = archives[kind]
    expected = {row['path']: row for row in manifest['entries'] if row['type'] == 'file'}
    checked = 0
    with tarfile.open(path, 'r|gz') as archive:
        for member in archive:
            if member.isreg() and member.name != MANIFEST_NAME:
                digest = hashlib.sha256()
                handle = archive.extractfile(member)
                for chunk in iter(lambda: handle.read(1 << 20), b''):
                    digest.update(chunk)
                assert digest.hexdigest() == expected[member.name]['sha256'], member.name
                checked += 1
    assert checked == len(expected)


def _rows(archives, kind):
    return {row['path']: row for row in archives[kind][0]['entries']}


def test_rootfs_layout(archives):
    rows = _rows(archives, 'rootfs')
    for directory in ('opt/tofu', 'var/lib/tofu', 'root', 'tmp', 'dev', 'proc', 'sys'):
        assert rows[directory]['type'] == 'dir', directory
    assert rows['usr/bin/git']['type'] == 'file' and rows['usr/bin/git']['mode'] == '0755'
    assert rows['etc/resolv.conf'] == {**rows['etc/resolv.conf'], 'type': 'file', 'size': 0}
    assert rows['etc/hosts']['size'] > 0
    assert rows['etc/ssl/certs/ca-certificates.crt']['size'] > 100_000
    assert any(re.match(r'etc/ssl/certs/[0-9a-f]{8}\.0$', path) for path in rows)
    assert rows['bin'] == {'mode': '0777', 'path': 'bin', 'target': 'usr/bin', 'type': 'symlink'}


def test_rootfs_dpkg_database_lists_git(archives):
    _manifest, _members, path = archives['rootfs']
    with tarfile.open(path) as archive:
        status = archive.extractfile('var/lib/dpkg/status').read().decode()
    stanzas = debs.parse_deb822(status)
    assert {stanza['Status'] for stanza in stanzas} == {'install ok installed'}
    names = [stanza['Package'] for stanza in stanzas]
    assert {'git', 'ca-certificates', 'perl', 'perl-base', 'libcurl3t64-gnutls'} <= set(names)
    assert len(names) == len(set(names)) and names == sorted(names)


def test_payload_layout(archives):
    rows = _rows(archives, 'payload')
    for path in ('bin/tofu-local-kernel', 'bin/uv', 'bin/uvx'):
        assert rows[path]['type'] == 'file' and rows[path]['mode'] == '0755', path
    assert rows['python/bin/python3']['type'] == 'symlink'
    assert rows['python/lib/python3.12/site-packages']['type'] == 'dir'
    for path in ('tofu/local_kernel.py', 'tofu/server.py', 'tofu/static/vite/manifest.json',
                 'tofu/frontend/authoring-inputs.json', 'tofu/uv.lock'):
        assert rows[path]['type'] == 'file', path
    for tree in EXCLUDED_SOURCE_TREES:
        assert not any(path == f'tofu/{tree}' or path.startswith(f'tofu/{tree}/') for path in rows), tree
    assert not any(path.startswith('python/lib/python3.12/site-packages/bin/') for path in rows)
    assert not any('/.git/' in path or path.endswith('/.git') for path in rows)


def test_source_tree_is_exactly_the_commit(archives, runtime_out):
    """tofu/ = the commit's tracked files minus the excluded trees, plus static/vite and bytecode."""
    commit = json.loads((runtime_out / 'report.json').read_text())['commit']
    listing = subprocess.run(['git', '-C', str(REPO), 'ls-tree', '-r', '-z', commit],
                             capture_output=True, check=True).stdout.decode('utf-8')
    expected = set()
    for record in filter(None, listing.split('\0')):
        meta, path = record.split('\t', 1)
        if meta.split()[1] != 'commit' and not any(
                path == tree or path.startswith(tree + '/') for tree in EXCLUDED_SOURCE_TREES):
            expected.add(f'tofu/{path}')
    shipped = {path for path, row in _rows(archives, 'payload').items()
               if path.startswith('tofu/') and row['type'] != 'dir'
               and not path.startswith('tofu/static/vite/') and '/__pycache__/' not in path}
    assert sorted(shipped - expected) == [], 'files the commit does not track (build leftovers?)'
    assert sorted(expected - shipped) == [], 'tracked runtime files missing from the payload'


def test_every_record_row_names_a_shipped_file(archives):
    """RECORD lists exactly what is installed: no dropped scripts or uv cache files."""
    manifest, _members, path = archives['payload']
    rows = {row['path'] for row in manifest['entries'] if row['type'] == 'file'}
    site = 'python/lib/python3.12/site-packages'
    records = sorted(row for row in rows if row.startswith(site) and row.endswith('.dist-info/RECORD'))
    assert len(records) > 100
    missing = []
    with tarfile.open(path) as archive:
        for record in records:
            for line in archive.extractfile(record).read().decode().splitlines():
                listed = line.split(',', 1)[0]
                if listed and posixpath.normpath(f'{site}/{listed}') not in rows:
                    missing.append(f'{record}: {listed}')
    assert missing == []
    assert not [row for row in rows if row.endswith('/uv_cache.json')]


def test_tofudb_engine_ships_with_its_library_when_supplied(archives, runtime_out):
    engine = json.loads((runtime_out / 'report.json').read_text())['payload']['tofudb']
    if not engine:
        pytest.skip('built without --tofudb')
    rows = _rows(archives, 'payload')
    for name, sha256 in engine.items():
        assert rows[f'bin/{name}']['sha256'] == sha256 and rows[f'bin/{name}']['mode'] == '0755'


def test_payload_bytecode_is_unchecked_hash_for_every_module(archives, runtime_out):
    rows = _rows(archives, 'payload')
    report = json.loads((runtime_out / 'report.json').read_text())
    failures = set(report['payload']['bytecode_failures'])
    sources = [path for path in rows if path.endswith('.py') and path not in failures]
    for source in sources:
        directory, _, name = source.rpartition('/')
        cache = f'{directory}/__pycache__/{name[:-3]}.cpython-312.pyc'
        assert cache in rows, source
    _manifest, _members, path = archives['payload']
    with tarfile.open(path) as archive:
        header = archive.extractfile('tofu/__pycache__/local_kernel.cpython-312.pyc').read(16)
    assert int.from_bytes(header[4:8], 'little') == PYC_UNCHECKED_HASH


def test_every_elf_is_aarch64(runtime_out):
    report = json.loads((runtime_out / 'report.json').read_text())
    for key in ('rootfs_elf', 'payload_elf'):
        assert report[key]['machine'] == 'aarch64'
        assert report[key]['elf_files'] > 0
        assert report[key]['foreign_machine'] == [], key
        assert all(row['machine'] == 'aarch64' for row in report[key]['files'])
        assert all(row['min_load_align'] is None or row['min_load_align'] > 0
                   for row in report[key]['files'])


def test_size_report_covers_every_component(runtime_out):
    sizes = json.loads((runtime_out / 'report.json').read_text())['sizes']
    assert {'python', 'site-packages', 'tofu-source', 'frontend', 'uv', 'launcher'} <= set(sizes['payload'])
    assert set(sizes['rootfs']) == {'ubuntu-base', 'added'}
    components = [*sizes['payload'].values(), *sizes['rootfs'].values()]
    for component in components:
        for key in ('content_bytes', 'tar_bytes', 'gzip_bytes', 'xz_bytes'):
            assert component[key] >= 0
    if shutil.which('zstd') is None:
        pytest.skip('zstd is not on PATH, so the size report records -1 for its zstd '
                    'column: a measurement only, the archives ship as gzip (README §8)')
    for component in components:
        assert component['zstd_bytes'] >= 0


def test_repeat_build_is_byte_identical(runtime_out, runtime_out_repeat):
    first, second = _assets(runtime_out), _assets(runtime_out_repeat)
    names = sorted(path.name for path in first.iterdir())
    assert names == sorted(path.name for path in second.iterdir())
    for name in names:
        assert (first / name).read_bytes() == (second / name).read_bytes(), name


def test_gzip_streams_decompress_cleanly(archives):
    for kind in ('rootfs', 'payload'):
        with gzip.open(archives[kind][2]) as stream:
            while stream.read(1 << 24):
                pass
