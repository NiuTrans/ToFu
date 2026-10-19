"""Execution error contracts remain usable without a storage implementation."""
import os
import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit


def test_storage_adapter_reexports_identical_error_contract():
    from lib.agent_core import storage_errors as contract
    from lib.storage import errors as adapter

    for name in contract.__all__:
        assert getattr(adapter, name) is getattr(contract, name)
    failure = adapter.StorageError('database_busy', 'retry', True, 12)
    with pytest.raises(contract.StorageError) as raised:
        raise failure
    assert raised.value is failure
    assert contract.http_status_for_storage_error(failure) == 503


def test_error_contract_does_not_import_application_storage():
    program = '''
import importlib.abc
import sys

class RefuseStorage(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'lib.storage' or fullname.startswith('lib.storage.'):
            raise AssertionError('execution error contract initialized storage')

sys.meta_path.insert(0, RefuseStorage())
from lib.agent_core.storage_errors import StorageError, describe_storage_failure
failure = StorageError.from_payload({'code': 'database_timeout', 'message': 'deadline',
    'retryable': True, 'request_not_dispatched': 'true'})
assert failure.request_not_dispatched is False
assert 'code=database_timeout' in describe_storage_failure(failure)
assert 'lib.storage' not in sys.modules
'''
    subprocess.run([os.environ.get('TOFU_BENCHMARK_PYTHON', sys.executable),
                    '-c', program], check=True, timeout=15,
                   capture_output=True, text=True)
