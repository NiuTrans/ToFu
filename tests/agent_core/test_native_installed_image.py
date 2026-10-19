"""Installed images retain byte, runtime and lifecycle admission boundaries."""
import asyncio
import hashlib
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from lib.agent_core.native_artifact import SPEC, NATIVE_STDIO_CONTRACT, seal_executable
from lib.agent_core.native_image import NativeExecutableImage
from lib.agent_core.native_process import NativeConfigurationError, NativeProcessClient
from lib.tasks_pkg.native_host import NativeHost
from tests.agent_core.test_native_process import budget, principal

pytestmark = pytest.mark.unit


def replies():
    return {'build-info': dict(format=SPEC['build_format'], application_version='fixture',
                stdio_contract=NATIVE_STDIO_CONTRACT, target_os='linux', target_arch='x86_64'),
            'check-session': dict(invariants_satisfied=True),
            'check-tool-gateway': dict(default_deny=True)}


def binary(tmp_path):
    path = tmp_path/'native'
    path.write_bytes(b'\x7fELFfixture')
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize('command,field,value', [
    ('build-info', 'application_version', 'foreign'),
    ('build-info', 'format', 'foreign'),
    ('build-info', 'stdio_contract', 'foreign'),
    ('build-info', 'target_arch', 'aarch64'),
    ('build-info', 'target_os', 'foreign'),
    ('check-session', 'invariants_satisfied', False),
    ('check-tool-gateway', 'default_deny', False),
    ('check-session', None, []),
])
def test_installed_runtime_identity_refuses_and_closes_image(tmp_path, command, field, value):
    path, digest = binary(tmp_path)
    values = replies()
    if field is None: values[command] = value
    else: values[command][field] = value
    images = []
    def check(image, name, *_):
        images.append(image)
        return values[name]
    with patch('lib.agent_core.native_artifact._self_check', side_effect=check), \
            patch('lib.agent_core.native_artifact.platform.machine', return_value='x86_64'):
        with pytest.raises(NativeConfigurationError, match='runtime gate'):
            seal_executable(path, digest, 'fixture', 100, 1, 1024)
    assert images and all(image.closed for image in images)


def test_bad_pin_never_executes_and_runtime_fault_closes_image(tmp_path):
    path, digest = binary(tmp_path)
    with patch('lib.agent_core.native_artifact._self_check') as check:
        with pytest.raises(NativeConfigurationError):
            seal_executable(path, '0'*64, 'fixture', 100, 1, 1024)
        check.assert_not_called()
    images = []
    def fail(image, *_):
        images.append(image)
        raise OSError('injected self-check failure')
    with patch('lib.agent_core.native_artifact._self_check', side_effect=fail):
        with pytest.raises(OSError, match='injected'):
            seal_executable(path, digest, 'fixture', 100, 1, 1024)
    assert images[0].closed


def test_host_borrows_checked_installed_image_without_archive_authority(tmp_path):
    path, digest = binary(tmp_path)
    async def run():
        image = NativeExecutableImage(binary=path, sha256=digest, application_version='fixture', budget=budget())
        with pytest.raises(NativeConfigurationError, match='archive'):
            NativeHost(image, token_count=True)
        host = NativeHost(image)
        with pytest.raises(NativeConfigurationError, match='archive'):
            host.flow_scheduler()
        with pytest.raises(NativeConfigurationError):
            NativeProcessClient.from_shared_archive(image=image, principal=principal(), budget=budget())
        async with host.client(principal(), budget=budget()) as client:
            assert client._shared_image is image and client.binary == path
            descriptor = await image.duplicate(100)
            assert os.pread(descriptor.fileno(), 11, 0) == b'\x7fELFfixture'
            with pytest.raises(OSError): os.write(descriptor.fileno(), b'altered')
            descriptor.close()
        await host.close()
        assert image._image.result().closed and client._closed
    values = replies()
    with patch('lib.agent_core.native_artifact._self_check', side_effect=lambda _, command, *a: values[command]) as check, \
            patch('lib.agent_core.native_artifact.platform.machine', return_value='x86_64'):
        asyncio.run(run())
        assert check.call_count == 3


@pytest.mark.parametrize('library,pin', [
    (None, '0'*64), (Path('relative.so'), '0'*64),
    (Path('/absolute/library.so'), None), (Path('/absolute/library.so'), 'bad'),
])
def test_installed_token_library_requires_its_own_complete_pin(tmp_path, library, pin):
    path, digest = binary(tmp_path)
    with pytest.raises(NativeConfigurationError, match='own path and pin'):
        NativeExecutableImage(binary=path, sha256=digest, application_version='fixture',
            budget=budget(), token_library=library, token_library_sha256=pin)
