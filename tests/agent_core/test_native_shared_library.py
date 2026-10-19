"""Optional release ABIs share admitted bytes without sharing call lifetimes."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import fcntl
import hashlib
import json
import os
import threading
from unittest.mock import AsyncMock, patch

import pytest

from lib.agent_core import native_artifact
from lib.agent_core.native_image import NativeArchiveImage
from lib.agent_core.native_process import NativeConfigurationError, _GET_SEALS, _ALL_SEALS
from lib.agent_core.native_token_count_contract import TOKEN_COUNT_SPEC
from lib.orchestration.native_flow_contract import SPEC as FLOW_SPEC
from scripts.build_native_runtime import write_archive
from tests.agent_core.test_native_process import budget
from tests.release.test_native_runtime_archive import members

pytestmark = pytest.mark.unit


def release(tmp_path, extensions=('optional_token_count', 'optional_flow')):
    files = members()
    for name in extensions:
        extension = native_artifact.SPEC[name]
        files[extension['member']] = b'\x7fELFshared-library'
        contract = TOKEN_COUNT_SPEC if name == 'optional_token_count' else FLOW_SPEC
        files[extension['contract_member']] = json.dumps(contract).encode()
    manifest = json.loads(files['manifest.json'])
    manifest['files'] = {name: {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
                         for name, raw in files.items() if name != 'manifest.json'}
    files['manifest.json'] = json.dumps(manifest).encode()
    archive = tmp_path / 'native.tar.gz'
    write_archive(files, archive)
    return NativeArchiveImage(archive=archive, sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
                              application_version='fixture', budget=budget())


def test_concurrent_abis_share_exact_sealed_bytes_with_independent_descriptors(tmp_path):
    owner = release(tmp_path)
    loans = []
    try:
        with patch.object(native_artifact, 'inspect_archive', wraps=native_artifact.inspect_archive) as inspect, \
                patch.object(native_artifact, '_self_check') as execute:
            with ThreadPoolExecutor(max_workers=8) as workers:
                loans = list(workers.map(owner.duplicate_library,
                                        ['optional_token_count', 'optional_flow'] * 4))
            assert inspect.call_count == 1
            execute.assert_not_called()
        assert len({os.fstat(loan.fileno()).st_ino for loan in loans}) == 1
        assert len({loan.fileno() for loan in loans}) == 8
        assert all(fcntl.fcntl(loan.fileno(), _GET_SEALS) == _ALL_SEALS for loan in loans)
        assert all(not os.get_inheritable(loan.fileno()) for loan in loans)
        owner.archive.unlink()
        loans[0].close()
        asyncio.run(owner.close())
        assert not owner._libraries
        assert os.pread(loans[1].fileno(), 64, 0) == b'\x7fELFshared-library'
        with pytest.raises(NativeConfigurationError, match='closed'):
            owner.duplicate_library('optional_flow')
    finally:
        for loan in loans:
            loan.close()


@pytest.mark.parametrize('present,absent', [('optional_token_count', 'optional_flow'),
                                         ('optional_flow', 'optional_token_count')])
def test_cached_member_does_not_grant_absent_abi(tmp_path, present, absent):
    owner = release(tmp_path, (present,))
    try:
        with owner.duplicate_library(present):
            owner.archive.unlink()
            with pytest.raises(NativeConfigurationError, match='extension missing'):
                owner.duplicate_library(absent)
            with owner.duplicate_library(present) as loan:
                assert os.pread(loan.fileno(), 4, 0) == b'\x7fELF'
    finally:
        asyncio.run(owner.close())


@pytest.mark.parametrize('fault', ['pin', 'version', 'capacity', 'target'])
def test_failed_library_admission_retains_no_image(tmp_path, fault):
    owner = release(tmp_path)
    if fault == 'pin': owner.sha256 = '0' * 64
    if fault == 'version': owner.application_version = 'different'
    if fault == 'capacity':
        from dataclasses import replace
        owner.budget = replace(owner.budget, binary_bytes=4)
    machine = 'aarch64' if fault == 'target' else 'x86_64'
    try:
        with patch.object(native_artifact.platform, 'machine', return_value=machine):
            with pytest.raises(NativeConfigurationError):
                owner.duplicate_library('optional_flow')
        assert not owner._libraries
    finally:
        asyncio.run(owner.close())


def test_close_joins_running_inspection_and_withdraws_unpublished_loan(tmp_path):
    owner = release(tmp_path)
    entered, finish = threading.Event(), threading.Event()
    original = native_artifact._inspect_library_archive
    admitted = []
    def held(*args):
        value = original(*args)
        admitted.append(value[1])
        entered.set()
        assert finish.wait(5)
        return value
    async def run():
        with patch.object(native_artifact, '_inspect_library_archive', side_effect=held):
            acquisition = asyncio.create_task(asyncio.to_thread(owner.duplicate_library, 'optional_flow'))
            assert await asyncio.to_thread(entered.wait, 3)
            closing = asyncio.create_task(owner.close())
            await asyncio.sleep(.01)
            assert not closing.done()  # Loop remains available while worker owns admission.
            closing.cancel()
            await asyncio.sleep(0)
            assert not closing.done()
            finish.set()
            with pytest.raises(NativeConfigurationError, match='closing'):
                await acquisition
            await closing
        assert admitted[0].closed and not owner._libraries
    try:
        asyncio.run(run())
    finally:
        finish.set()


def test_unused_optional_close_is_lazy_and_unknown_extension_never_opens(tmp_path):
    owner = release(tmp_path)
    with patch.object(native_artifact, 'inspect_archive') as inspect:
        with pytest.raises(NativeConfigurationError, match='unsupported'):
            owner.duplicate_library('arbitrary')
        asyncio.run(owner.close())
        inspect.assert_not_called()


def test_failed_descriptor_wrapper_does_not_revoke_existing_library(tmp_path):
    owner = release(tmp_path)
    try:
        with owner.duplicate_library('optional_flow') as loan:
            descriptor = os.dup(loan.fileno())
            with patch('lib.agent_core.native_image.os.dup', return_value=descriptor), \
                    patch('lib.agent_core.native_image.os.fdopen', side_effect=OSError('wrapper fault')):
                with pytest.raises(OSError, match='wrapper fault'):
                    owner.duplicate_library('optional_token_count')
            with pytest.raises(OSError): os.fstat(descriptor)
            assert os.pread(loan.fileno(), 4, 0) == b'\x7fELF'
    finally:
        asyncio.run(owner.close())


def test_shutdown_preserves_executable_and_library_failures_and_attempts_every_image(tmp_path):
    from types import SimpleNamespace
    owner = release(tmp_path)
    attempted = []
    def close_bad():
        attempted.append('bad')
        raise OSError('library close fault')
    owner._libraries = {
        'bad': ({}, SimpleNamespace(close=close_bad)),
        'good': ({}, SimpleNamespace(close=lambda: attempted.append('good'))),
    }
    async def run():
        with patch('lib.agent_core.native_image._NativeImage._close',
                   new=AsyncMock(side_effect=OSError('executable close fault'))):
            with pytest.raises(BaseExceptionGroup) as caught:
                await owner.close()
            assert str(caught.value.exceptions[0]) == 'executable close fault'
            assert str(caught.value.exceptions[1].exceptions[0]) == 'library close fault'
            assert attempted == ['bad', 'good'] and set(owner._libraries) == {'bad'}
            with pytest.raises(BaseExceptionGroup) as repeated:
                await owner.close()
            assert repeated.value is caught.value
    asyncio.run(run())
