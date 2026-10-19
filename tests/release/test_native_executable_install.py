"""Full archive installation publishes only checked bytes, without replacement."""
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from lib.agent_core.native_process import NativeConfigurationError
from scripts.build_native_runtime import install_executable, write_archive
from tests.release.test_native_runtime_archive import members

pytestmark = pytest.mark.unit


def fixture(tmp_path, *, invalid_member=False):
    files = members()
    if invalid_member: files['LICENSE'] += b'changed'
    source = tmp_path/'source.tar.gz'
    write_archive(files, source)
    manifest = json.loads(files['manifest.json'])
    checks = {'build-info': manifest['build'], 'check-session': {'invariants_satisfied': True},
              'check-tool-gateway': {'default_deny': True}}
    return source, hashlib.sha256(source.read_bytes()).hexdigest(), checks


def test_install_preserves_pin_from_checked_snapshot_and_refuses_replacement(tmp_path):
    source, pin, checks = fixture(tmp_path)
    destination = tmp_path/'tofu-native'
    def check(image, command, *_):
        # Replacement after private admission cannot change published bytes.
        source.write_bytes(b'foreign replacement')
        return checks[command]
    with patch('lib.agent_core.native_artifact._self_check', side_effect=check), \
            patch('lib.agent_core.native_artifact.platform.machine', return_value='x86_64'):
        receipt = install_executable(source, destination, archive_sha256=pin, application_version='fixture')
    assert destination.read_bytes() == b'\x7fELFfixture'
    assert receipt['executable_sha256'] == hashlib.sha256(destination.read_bytes()).hexdigest()
    assert receipt['archive_sha256'] == pin != receipt['executable_sha256']
    assert destination.stat().st_mode & 0o777 == 0o500
    source.unlink()
    source, pin, checks = fixture(tmp_path)
    with patch('lib.agent_core.native_artifact._self_check', side_effect=lambda _, name, *a: checks[name]), \
            patch('lib.agent_core.native_artifact.platform.machine', return_value='x86_64'):
        with pytest.raises(FileExistsError):
            install_executable(source, destination, archive_sha256=pin, application_version='fixture')
    assert destination.read_bytes() == b'\x7fELFfixture'
    assert set(tmp_path.iterdir()) == {source, destination}


@pytest.mark.parametrize('failure', ['pin', 'member', 'version', 'self-check', 'write'])
def test_failed_install_never_publishes_and_cleans_owned_files(tmp_path, failure):
    source, pin, checks = fixture(tmp_path, invalid_member=failure == 'member')
    destination = tmp_path/'native'
    if failure == 'pin': pin = '0'*64
    def check(_, command, *args):
        if failure == 'self-check': raise OSError('self-check failed')
        return checks[command]
    with patch('lib.agent_core.native_artifact._self_check', side_effect=check) as execution, \
            patch('lib.agent_core.native_artifact.platform.machine', return_value='x86_64'), \
            patch('scripts.build_native_runtime.os.fsync', side_effect=OSError('write failed') if failure == 'write' else None):
        with pytest.raises((NativeConfigurationError, OSError)):
            install_executable(source, destination, archive_sha256=pin,
                application_version='foreign' if failure == 'version' else 'fixture')
        if failure in {'pin', 'member', 'version'}: execution.assert_not_called()
    assert set(tmp_path.iterdir()) == {source}


def test_install_requires_explicit_destination_and_identity_before_io(tmp_path):
    for pin, version, destination in [(None, 'fixture', tmp_path/'native'),
            ('0'*64, '', tmp_path/'native'), ('0'*64, 'fixture', Path('relative'))]:
        with patch('lib.agent_core.native_artifact.inspect_archive') as inspect:
            with pytest.raises(ValueError):
                install_executable(tmp_path/'missing', destination, archive_sha256=pin, application_version=version)
            inspect.assert_not_called()
