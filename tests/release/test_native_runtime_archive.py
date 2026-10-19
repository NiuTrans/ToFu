"""Hermetic native archive integrity, deterministic output and publication gates."""
from pathlib import Path
import hashlib
import io
import json
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import pytest

from scripts.build_native_runtime import SPEC, FORMAT, verify, write_archive

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('html_projection', [False, True])
@pytest.mark.parametrize('checkpoint_storage', [False, True])
def test_first_party_embedded_data_notice_is_in_the_verified_archive(tmp_path, monkeypatch, html_projection, checkpoint_storage):
    import tomllib
    from types import SimpleNamespace
    from scripts import build_native_runtime as builder
    version = tomllib.loads((builder.ROOT/'packages/tofu-runtime/Cargo.toml').read_text())['package']['version']
    target = 'x86_64-unknown-linux-gnu'
    binary = tmp_path/'target'/target/'release/tofu-native'
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b'\x7fELFfixture')
    binary.with_name('libtofu_runtime.so').write_bytes(b'\x7fELFhtml-library')
    info = dict(format=SPEC['build_format'],runtime_version=version,
        stdio_contract=json.loads((builder.ROOT/'contracts/native_agent_stdio_v1.schema.json').read_text())['$id'],
        application_version=(builder.ROOT/'VERSION').read_text().strip(),target_os='linux',target_arch='x86_64')
    package = dict(name='tofu-runtime',version=version,license='MIT',source=None,
                   manifest_path=str(builder.ROOT/'packages/tofu-runtime/Cargo.toml'))
    def command(argv, **kwargs):
        if argv[:2] == ['cargo','metadata']:
            return SimpleNamespace(stdout=json.dumps(dict(packages=[package])).encode())
        if argv[:2] == ['rustc','--version']:
            return SimpleNamespace(stdout=b'rustc fixture')
        assert argv[:2] == ['cargo','build']
        features = argv[argv.index('--features')+1].split(',') if '--features' in argv else []
        assert ('html-projection-abi' in features) is html_projection
        assert ('checkpoint-storage-abi' in features) is checkpoint_storage
        return SimpleNamespace(stdout=b'')
    monkeypatch.setattr(builder.subprocess,'run',command)
    monkeypatch.setattr(builder,'execute',lambda *args: info)
    monkeypatch.setattr(builder,'native_target',lambda: target)
    original_verify = builder.verify
    monkeypatch.setattr(builder,'verify',lambda path: original_verify(path,run=False))
    output = builder.build(tmp_path/'dist',tmp_path/'target',html_projection=html_projection, checkpoint_storage=checkpoint_storage)
    manifest = original_verify(output,run=False)
    name = f'licenses/tofu-runtime-{version}/LICENSE-PSF'
    expected = (builder.ROOT/'packages/tofu-runtime'/name).read_bytes()
    with tarfile.open(output) as archive:
        assert archive.extractfile(name).read() == expected
    assert manifest['files'][name]['sha256'] == hashlib.sha256(expected).hexdigest()
    assert manifest['dependency_inventory'] == []
    assert (SPEC['optional_html_projection']['contract_member'] in manifest['files']) is html_projection
    assert (SPEC['optional_checkpoint_storage']['contract_member'] in manifest['files']) is checkpoint_storage


def members():
    files = {name: b'fixture' for name in SPEC['required_members']}
    files['bin/tofu-native'] = b'\x7fELFfixture'
    files['contracts/native_agent_stdio_v1.schema.json']=json.dumps({'$id':'tofu.native-agent-stdio/v1'}).encode()
    files['contracts/native_runtime_artifact_v1.json']=json.dumps(SPEC).encode()
    manifest = {'format': FORMAT, 'target':'x86_64-unknown-linux-gnu', 'build':{'format':SPEC['build_format'],'target_os':'linux','target_arch':'x86_64','stdio_contract':'tofu.native-agent-stdio/v1','application_version':'fixture'}, 'files': {
        name: {'sha256': hashlib.sha256(content).hexdigest(), 'bytes': len(content)}
        for name, content in files.items()}}
    files['manifest.json'] = json.dumps(manifest).encode()
    return files


def test_direct_cli_checks_archive_from_unrelated_directory_without_pythonpath(tmp_path):
    import os
    import subprocess
    import sys
    from scripts.build_native_runtime import ROOT
    archive = tmp_path/'tampered.tar.gz'
    files = members()
    files['bin/tofu-native'] = b'\x7fELFchanged'
    write_archive(files, archive)
    environment = dict(os.environ)
    environment.pop('PYTHONPATH', None)
    result = subprocess.run([sys.executable, '-I', str(ROOT/'scripts/build_native_runtime.py'),
                             '--check', str(archive)], cwd=tmp_path, env=environment,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    assert 'member checksum differs' in result.stderr, result.stderr
    assert archive.is_file()


class NativeArchiveTests(unittest.TestCase):
    def test_local_profile_has_exact_members_reproducible_bytes_and_exclusive_publication(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            distribution,local,repeat=(root/name for name in ('distribution.tar.gz','local.tar.gz','repeat.tar.gz'))
            files=members()
            write_archive(files,distribution)
            write_archive(files,local,compression_profile='local')
            write_archive(files,repeat,compression_profile='local')
            self.assertEqual(local.read_bytes(),repeat.read_bytes())
            self.assertEqual(verify(local,run=False),verify(distribution,run=False))
            for path in (distribution,local):
                with tarfile.open(path) as archive:
                    self.assertEqual({member.name:archive.extractfile(member).read() for member in archive},files)
            with self.assertRaises(FileExistsError):write_archive(files,local,compression_profile='local')
            self.assertEqual(local.read_bytes(),repeat.read_bytes())
            self.assertEqual(len(list(root.iterdir())),3)

    def test_invalid_profile_refuses_before_build_or_directory_creation(self):
        from scripts.build_native_runtime import build
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)/'uncreated'
            for profile in ('unknown',None,True,[]):
                with self.subTest(profile=profile),patch('scripts.build_native_runtime.subprocess.run') as cargo:
                    with self.assertRaises(ValueError):write_archive(members(),root/'archive',compression_profile=profile)
                    with self.assertRaises(ValueError):build(root,root,compression_profile=profile)
                    cargo.assert_not_called()
                    self.assertFalse(root.exists())

    def test_stored_tar_overhead_refuses_before_publication(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'over-budget.tar.gz'
            with patch('scripts.build_native_runtime.MAX_BYTES',10240):
                with self.assertRaisesRegex(ValueError,'compressed artifact byte bound'):
                    write_archive(members(),path,compression_profile='local')
            self.assertEqual(list(path.parent.iterdir()),[])

    def test_reproducible_bytes_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            first = Path(temporary) / 'one.tar.gz'
            second = Path(temporary) / 'two.tar.gz'
            write_archive(members(), first)
            write_archive(members(), second)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(verify(first, run=False)['format'], FORMAT)
            original = first.read_bytes()
            with self.assertRaises(FileExistsError):
                write_archive(members(), first)
            self.assertEqual(first.read_bytes(), original)
            self.assertEqual({p.name for p in first.parent.iterdir()},
                             {'one.tar.gz', 'two.tar.gz'})

    def test_tampered_binary_and_missing_required_member_fail_before_execution(self):
        for mode in ('tamper', 'missing'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / 'bad.tar.gz'
                files = members()
                if mode == 'tamper':
                    files['bin/tofu-native'] += b'altered'
                else:
                    files.pop('README.txt')
                write_archive(files, path)
                with self.assertRaises(ValueError):
                    verify(path, run=False)

    def test_traversal_links_and_duplicate_members_are_rejected(self):
        for mode in ('traversal', 'link', 'duplicate'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / 'bad.tar.gz'
                with tarfile.open(path, 'w:gz') as archive:
                    name = '../escape' if mode == 'traversal' else 'repeated'
                    entry = tarfile.TarInfo(name)
                    if mode == 'link':
                        entry.type = tarfile.SYMTYPE
                        entry.linkname = '/outside'
                    else:
                        entry.size = 1
                    archive.addfile(entry, None if mode == 'link' else io.BytesIO(b'x'))
                    if mode == 'duplicate':
                        archive.addfile(entry, io.BytesIO(b'x'))
                with self.assertRaises(ValueError):
                    verify(path, run=False)
                self.assertFalse((path.parent / 'escape').exists())

    def test_missing_manifest_inventory_cannot_admit_extra_members(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'extra.tar.gz'
            files = members()
            files['unexpected'] = b'unlisted'
            write_archive(files, path)
            with self.assertRaises(ValueError):
                verify(path, run=False)


if __name__ == '__main__':
    unittest.main()


@pytest.mark.parametrize('mode', ['valid', 'missing-contract', 'bad-abi', 'bad-status', 'bad-elf'])
def test_optional_token_library_extension_is_all_or_nothing(tmp_path, mode):
    from lib.agent_core.native_artifact import inspect_archive, seal_token_count_archive
    from lib.agent_core.native_process import NativeConfigurationError
    import os
    files = members()
    extension = SPEC['optional_token_count']
    files[extension['member']] = b'\x7fELFtoken-library'
    from lib.agent_core.native_token_count_contract import TOKEN_COUNT_SPEC
    contract = dict(TOKEN_COUNT_SPEC)
    if mode == 'bad-abi':
        contract['abi_version'] = 99
    if mode == 'bad-status':
        contract['status'] = dict(contract['status'], OK=99)
    files[extension['contract_member']] = json.dumps(contract).encode()
    if mode == 'missing-contract':
        del files[extension['contract_member']]
    if mode == 'bad-elf':
        files[extension['member']] = b'not-elf'
    manifest = json.loads(files['manifest.json'])
    manifest['files'] = {name: {'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}
                         for name, raw in files.items() if name != 'manifest.json'}
    files['manifest.json'] = json.dumps(manifest).encode()
    archive = tmp_path / 'extension.tar.gz'
    write_archive(files, archive)
    if mode != 'valid':
        with pytest.raises(NativeConfigurationError):
            inspect_archive(archive)
        return
    pin = hashlib.sha256(archive.read_bytes()).hexdigest()
    with pytest.raises(NativeConfigurationError, match='checksum'):
        seal_token_count_archive(archive, '0' * 64, 'fixture', 1024)
    with pytest.raises(NativeConfigurationError, match='byte budget'):
        seal_token_count_archive(archive, pin, 'fixture', 4)
    with patch('lib.agent_core.native_artifact.platform.machine', return_value='unsupported'):
        with pytest.raises(NativeConfigurationError, match='host'):
            seal_token_count_archive(archive, pin, 'fixture', 1024)
    with seal_token_count_archive(archive, pin, 'fixture', 1024) as image:
        assert image.read() == files[extension['member']]
        with pytest.raises(PermissionError):
            os.write(image.fileno(), b'tamper')
        archive.write_bytes(b'replaced')
        image.seek(0)
        assert image.read() == files[extension['member']]


def test_old_archive_still_valid_but_cannot_select_token_library(tmp_path):
    from lib.agent_core.native_artifact import seal_token_count_archive
    from lib.agent_core.native_process import NativeConfigurationError
    archive = tmp_path / 'legacy.tar.gz'
    write_archive(members(), archive)
    verify(archive, run=False)
    with pytest.raises(NativeConfigurationError, match='selected image member missing'):
        seal_token_count_archive(archive, hashlib.sha256(archive.read_bytes()).hexdigest(), 'fixture', 1024)


@pytest.mark.parametrize('pin,version,limit', [(None, 'v', 1024), ('0'*64, '', 1024),
                                              ('0'*64, 'v', True), ('0'*64, 'v', 0)])
def test_token_library_requires_trusted_inputs_before_open(pin, version, limit):
    from lib.agent_core.native_artifact import seal_token_count_archive
    from lib.agent_core.native_process import NativeConfigurationError
    with patch('lib.agent_core.native_artifact.inspect_archive') as inspect:
        with pytest.raises(NativeConfigurationError):
            seal_token_count_archive(Path('/missing'), pin, version, limit)
        inspect.assert_not_called()
