"""Debian metadata, closure, unpacking and dpkg registration (README §5)."""
import hashlib
import shutil
import subprocess
import tarfile
import textwrap

import pytest

from runtime_builder import debs
from runtime_builder.tree import BlobStore, Tree

NOBLE_EXCLUDES = textwrap.dedent("""\
    path-exclude=/usr/share/man/*
    path-exclude=/usr/share/locale/*/LC_MESSAGES/*.mo
    path-exclude=/usr/share/doc/*
    path-include=/usr/share/doc/*/copyright
    path-include=/usr/share/doc/*/changelog.*
    """)

VERSION_PAIRS = [
    ('1.0~rc1', '1.0'), ('1.0', '1.0+b1'), ('2.0', '1:1.0'), ('1.0-1', '1.0-1ubuntu1'),
    ('5.38.2-3.2ubuntu0.4', '5.38.2-3.2ubuntu0.6'), ('2.39-0ubuntu8.8', '2.39-0ubuntu8.9'),
    ('1:2.43.0-1ubuntu7.3', '1:2.43.0-1ubuntu7.4'), ('1.0a', '1.0b'), ('1.0', '1.0.1'),
    ('0.17029-2', '0.17029-10'), ('2.4+20151223.gitfa8646d.1-2build7', '2.4+20151224-1'),
    ('1.0~~', '1.0~'), ('9', '10'), ('1.0-0', '1.0-1'),
]


@pytest.mark.parametrize('lower,higher', VERSION_PAIRS)
def test_version_order(lower, higher):
    assert debs.compare_versions(lower, higher) == -1
    assert debs.compare_versions(higher, lower) == 1
    assert debs.compare_versions(lower, lower) == 0


@pytest.mark.skipif(shutil.which('dpkg') is None, reason='needs dpkg')
@pytest.mark.parametrize('lower,higher', VERSION_PAIRS)
def test_version_order_agrees_with_dpkg(lower, higher):
    assert subprocess.run(['dpkg', '--compare-versions', lower, 'lt', higher]).returncode == 0


def test_relations_parse_alternatives_versions_and_arch_qualifiers():
    groups = debs.parse_relations('libc6 (>= 2.38), perl:any, debconf (>= 0.5) | debconf-2.0')
    assert groups == [
        [debs.Relation('libc6', '>=', '2.38')],
        [debs.Relation('perl')],
        [debs.Relation('debconf', '>=', '0.5'), debs.Relation('debconf-2.0')],
    ]


def _stanza(name, version, depends='', provides=''):
    stanza = {'Package': name, 'Version': version, 'Architecture': 'arm64'}
    if depends:
        stanza['Depends'] = depends
    if provides:
        stanza['Provides'] = provides
    return stanza


def test_closure_adds_missing_and_upgrades_a_base_package_pinned_exactly():
    installed = debs.Installed()
    installed.add(_stanza('perl-base', '5.38.2-3.2ubuntu0.4'))
    installed.add(_stanza('libc6', '2.39-0ubuntu8.8'))
    candidates = {stanza['Package']: debs.Candidate(stanza, 'noble-security') for stanza in [
        _stanza('git', '1:2.43.0-1', 'libc6 (>= 2.38), perl, missing-virtual | liberror-perl'),
        _stanza('perl', '5.38.2-3.2ubuntu0.6', 'perl-base (= 5.38.2-3.2ubuntu0.6)'),
        _stanza('perl-base', '5.38.2-3.2ubuntu0.6'),
        _stanza('liberror-perl', '0.17029-2', 'perl:any'),
        _stanza('libc6', '2.39-0ubuntu8.9'),
    ]}
    closure = [candidate.name for candidate in debs.resolve_closure(['git'], installed, candidates)]
    assert closure == ['git', 'liberror-perl', 'perl', 'perl-base']


def test_closure_reports_an_unsatisfiable_relation():
    with pytest.raises(debs.ArchiveError):
        debs.resolve_closure(['a'], debs.Installed(), {
            'a': debs.Candidate(_stanza('a', '1', 'b (>= 2)'), 'noble'),
            'b': debs.Candidate(_stanza('b', '1'), 'noble')})


def _member(name, kind=tarfile.REGTYPE):
    member = tarfile.TarInfo(name)
    member.type = kind
    return member


@pytest.mark.parametrize('path,kind,skipped', [
    ('usr/share/doc/git/README', tarfile.REGTYPE, True),
    ('usr/share/doc/git/copyright', tarfile.REGTYPE, False),
    ('usr/share/doc/git/changelog.Debian.gz', tarfile.REGTYPE, False),
    ('usr/share/doc/git', tarfile.DIRTYPE, False),
    ('usr/share/doc/git/contrib', tarfile.DIRTYPE, False),
    ('usr/share/man/man1', tarfile.DIRTYPE, True),
    ('usr/share/man/man1/git.1.gz', tarfile.REGTYPE, True),
    ('usr/share/locale/de/LC_MESSAGES/git.mo', tarfile.REGTYPE, True),
    ('usr/bin/git', tarfile.REGTYPE, False),
])
def test_dpkg_path_filter(path, kind, skipped):
    rules = tuple((option == 'path-include', value) for option, value in
                  (line.split('=', 1) for line in NOBLE_EXCLUDES.splitlines()))
    assert debs.PathFilter(rules).skips(path, _member(path, kind)) is skipped


def _build_deb(tmp_path, name, version, files, *, multi_arch='', conffiles=(), triggers=''):
    root = tmp_path / f'{name}-{version}'
    (root / 'DEBIAN').mkdir(parents=True)
    control = f'Package: {name}\nVersion: {version}\nArchitecture: arm64\n' \
              f'Maintainer: Test <test@example.com>\nDescription: test package\n'
    if multi_arch:
        control += f'Multi-Arch: {multi_arch}\n'
    (root / 'DEBIAN' / 'control').write_text(control)
    if conffiles:
        (root / 'DEBIAN' / 'conffiles').write_text(''.join(f'{path}\n' for path in conffiles))
    if triggers:
        (root / 'DEBIAN' / 'triggers').write_text(triggers)
    (root / 'DEBIAN' / 'postinst').write_text('#!/bin/sh\nexit 0\n')
    (root / 'DEBIAN' / 'postinst').chmod(0o755)
    # dpkg-deb requires a 0755..0775 control directory, whatever the umask.
    (root / 'DEBIAN').chmod(0o755)
    for path, content in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    deb = tmp_path / f'{name}_{version}_arm64.deb'
    result = subprocess.run(['dpkg-deb', '--root-owner-group', '-Zgzip', '--build', str(root),
                             str(deb)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return str(deb)


def _base(tmp_path):
    tree = Tree(BlobStore(tmp_path / 'blobs'))
    tree.add_dir('usr/lib/aarch64-linux-gnu')
    tree.add_symlink('lib', 'usr/lib')
    tree.add_bytes('etc/dpkg/dpkg.cfg.d/excludes', NOBLE_EXCLUDES.encode())
    tree.add_bytes(debs.DPKG_STATUS, b'Package: base-files\nStatus: install ok installed\n'
                                     b'Architecture: arm64\nVersion: 13\nDescription: base\n')
    tree.add_dir(debs.DPKG_INFO)
    tree.add_bytes(f'{debs.DPKG_INFO}/base-files.list', b'/.\n')
    tree.add_dir('var/lib/dpkg/triggers')
    return tree


needs_dpkg_deb = pytest.mark.skipif(shutil.which('dpkg-deb') is None, reason='needs dpkg-deb')


@needs_dpkg_deb
def test_unpack_registers_like_dpkg(tmp_path):
    tree = _base(tmp_path)
    lib = _build_deb(tmp_path, 'demo-lib', '1.0', {'lib/aarch64-linux-gnu/libdemo.so.1': b'elf'},
                     multi_arch='same')
    tool = _build_deb(tmp_path, 'demo-tool', '1.0', {
        'etc/demo.conf': b'setting=1\n',
        'usr/bin/demo': b'#!/bin/sh\n',
        'usr/share/doc/demo-tool/copyright': b'MIT\n',
        'usr/share/doc/demo-tool/README': b'docs\n',
        'usr/share/man/man1/demo.1.gz': b'man',
    }, conffiles=('/etc/demo.conf',), triggers='interest-noawait demo-trigger\nactivate-noawait ldconfig\n')
    debs.unpack_packages(tree, [('demo-lib', lib), ('demo-tool', tool)], 'arm64')

    assert tree.get('usr/lib/aarch64-linux-gnu/libdemo.so.1').type == 'file'
    assert tree.get('lib').type == 'symlink', 'merged /usr symlinks are kept'
    assert tree.get('usr/share/doc/demo-tool/copyright') is not None
    assert tree.get('usr/share/doc/demo-tool/README') is None
    assert tree.get('usr/share/man/man1') is None

    def text(path):
        with open(tree.get(path).source, encoding='utf-8') as handle:
            return handle.read()

    listing = text(f'{debs.DPKG_INFO}/demo-lib:arm64.list').splitlines()
    assert listing[0] == '/.' and '/lib/aarch64-linux-gnu/libdemo.so.1' in listing
    tool_list = text(f'{debs.DPKG_INFO}/demo-tool.list').splitlines()
    assert '/usr/share/doc/demo-tool/README' in tool_list, 'excluded paths stay listed'
    assert tree.get(f'{debs.DPKG_INFO}/demo-tool.postinst').mode == 0o755
    stanzas = {stanza['Package']: stanza for stanza in debs.parse_deb822(text(debs.DPKG_STATUS))}
    assert list(stanzas) == ['base-files', 'demo-lib', 'demo-tool']
    assert stanzas['demo-tool']['Status'] == 'install ok installed'
    assert stanzas['demo-tool']['Conffiles'].split() == [
        '/etc/demo.conf', hashlib.md5(b'setting=1\n').hexdigest()]
    assert text('var/lib/dpkg/triggers/demo-trigger') == 'demo-tool/noawait\n'
    assert tree.get('var/lib/dpkg/triggers/ldconfig') is None, 'activations are not interests'


@needs_dpkg_deb
def test_upgrade_replaces_the_old_version(tmp_path):
    tree = _base(tmp_path)
    old = _build_deb(tmp_path, 'demo', '1.0', {'usr/bin/demo': b'v1', 'usr/share/demo/gone': b'x'})
    debs.unpack_packages(tree, [('demo', old)], 'arm64')
    new = _build_deb(tmp_path, 'demo', '2.0', {'usr/bin/demo': b'v2'})
    debs.unpack_packages(tree, [('demo', new)], 'arm64')
    with open(tree.get('usr/bin/demo').source, 'rb') as handle:
        assert handle.read() == b'v2'
    assert tree.get('usr/share/demo/gone') is None
    with open(tree.get(debs.DPKG_STATUS).source, encoding='utf-8') as handle:
        stanzas = [stanza for stanza in debs.parse_deb822(handle.read()) if stanza['Package'] == 'demo']
    assert [stanza['Version'] for stanza in stanzas] == ['2.0']


def test_inrelease_sha256_table():
    signed = textwrap.dedent("""\
        -----BEGIN PGP SIGNED MESSAGE-----
        Hash: SHA512

        Origin: Ubuntu
        Suite: noble-updates
        SHA256:
         aaaa 10 main/binary-arm64/Packages.xz
         bbbb 20 main/binary-arm64/Release
        -----BEGIN PGP SIGNATURE-----
        sig
        -----END PGP SIGNATURE-----
        """).encode()
    assert debs.inrelease_sha256(signed) == {
        'main/binary-arm64/Packages.xz': ('aaaa', 10), 'main/binary-arm64/Release': ('bbbb', 20)}


def test_deb822_roundtrip_keeps_continuation_lines():
    text = 'Package: a\nDescription: short\n long line\n .\n more\nVersion: 1\n'
    (stanza,) = debs.parse_deb822(text)
    assert debs.format_deb822(stanza) == text
