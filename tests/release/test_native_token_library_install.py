"""Installed token capability comes from full pinned release validation."""
import hashlib
from unittest.mock import patch
import pytest
from scripts.build_native_runtime import install_token_library
from tests.release.test_native_flow_archive import archive

pytestmark=pytest.mark.unit


def test_library_install_has_distinct_pin_and_never_executes_code(tmp_path):
    source,pin=archive(tmp_path,token=True)
    destination=tmp_path/'counter.so'
    with patch('lib.agent_core.native_artifact._self_check') as execute:
        receipt=install_token_library(source,destination,archive_sha256=pin,application_version='fixture')
        execute.assert_not_called()
    assert receipt['token_library_sha256']==hashlib.sha256(destination.read_bytes()).hexdigest()
    assert receipt['archive_sha256']==pin!=receipt['token_library_sha256']
    assert destination.read_bytes()==b'\x7fELFlibrary'
    with pytest.raises(FileExistsError):
        install_token_library(source,destination,archive_sha256=pin,application_version='fixture')
    assert set(tmp_path.iterdir())=={source,destination}


@pytest.mark.parametrize('failure',['missing','pin','version','write'])
def test_failed_library_install_does_not_publish(tmp_path,failure):
    source,pin=archive(tmp_path,token=failure!='missing')
    destination=tmp_path/'counter.so'
    with patch('scripts.build_native_runtime.os.fsync',side_effect=OSError('injected') if failure=='write' else None):
        with pytest.raises((ValueError,OSError)):
            install_token_library(source,destination,archive_sha256='0'*64 if failure=='pin' else pin,
                application_version='foreign' if failure=='version' else 'fixture')
    assert set(tmp_path.iterdir())=={source}
