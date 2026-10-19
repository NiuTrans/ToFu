"""SQL checkpoint extension admission and exclusive installed image publication."""
import hashlib
import json
import os

import pytest

from lib.native_binary import NativeConfigurationError
from lib.native_binary.generated_checkpoint_storage_contract import SPEC as CHECKPOINT
from lib.orchestration.native_flow_contract import SPEC as FLOW
from lib.agent_core.native_artifact import seal_checkpoint_storage_archive, inspect_archive
from scripts.build_native_runtime import SPEC, write_archive, install_checkpoint_library
from tests.release.test_native_runtime_archive import members

pytestmark = pytest.mark.unit


def archive(tmp_path, *, checkpoint=True, flow=False, mutation=None):
    files = members()
    for selected, name, contract in ((checkpoint, 'optional_checkpoint_storage', CHECKPOINT), (flow, 'optional_flow', FLOW)):
        if selected:
            extension = SPEC[name]
            files[extension['member']] = b'\x7fELFcheckpoint-fixture'
            files[extension['contract_member']] = json.dumps(contract).encode()
    if mutation:
        mutation(files)
    manifest = json.loads(files['manifest.json'])
    manifest['files'] = {name:dict(sha256=hashlib.sha256(body).hexdigest(),bytes=len(body))
                         for name,body in files.items() if name != 'manifest.json'}
    files['manifest.json'] = json.dumps(manifest).encode()
    path = tmp_path/'checkpoint.tar.gz'
    write_archive(files, path)
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize('checkpoint,flow', [(False, False), (False, True), (True, False), (True, True)])
def test_checkpoint_extension_requires_its_own_declared_contract(tmp_path, checkpoint, flow):
    path, digest = archive(tmp_path, checkpoint=checkpoint, flow=flow)
    inspect_archive(path)
    if checkpoint:
        with seal_checkpoint_storage_archive(path, digest, 'fixture', 4096) as image:
            assert image.read() == b'\x7fELFcheckpoint-fixture'
            with pytest.raises(PermissionError):
                os.write(image.fileno(), b'x')
    else:
        with pytest.raises(NativeConfigurationError, match='missing'):
            seal_checkpoint_storage_archive(path, digest, 'fixture', 4096)


@pytest.mark.parametrize('damage', ['library', 'contract', 'version', 'elf'])
def test_bad_checkpoint_archive_refuses_before_loading(tmp_path, damage):
    extension = SPEC['optional_checkpoint_storage']
    def mutate(files):
        if damage == 'library':
            files.pop(extension['member'])
        elif damage == 'contract':
            files.pop(extension['contract_member'])
        elif damage == 'version':
            files[extension['contract_member']] = json.dumps({**CHECKPOINT, 'abi_version': 999}).encode()
        else:
            files[extension['member']] = b'not ELF'
    path, digest = archive(tmp_path, mutation=mutate)
    with pytest.raises(NativeConfigurationError):
        seal_checkpoint_storage_archive(path, digest, 'fixture', 4096)


def test_checkpoint_installation_is_pinned_and_exclusive(tmp_path):
    path, digest = archive(tmp_path)
    destination = tmp_path/'checkpoint.so'
    result = install_checkpoint_library(path, destination, archive_sha256=digest, application_version='fixture')
    assert destination.read_bytes() == b'\x7fELFcheckpoint-fixture'
    assert result['checkpoint_library_sha256'] == hashlib.sha256(destination.read_bytes()).hexdigest()
    assert result['bytes'] == destination.stat().st_size
    with pytest.raises(FileExistsError):
        install_checkpoint_library(path, destination, archive_sha256=digest, application_version='fixture')
