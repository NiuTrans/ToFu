"""The clone-free runtime imports result capture without application storage."""
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]



def test_packaged_query_contract_is_current():
    result = subprocess.run(
        [sys.executable, 'scripts/gen_tool_result_query_contract.py', '--check'],
        cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_result_capture_import_without_storage_or_repository_contracts():
    probe = r'''
import importlib.abc
import sys
from pathlib import Path
class NoApplicationStorage(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'lib.storage' or fullname.startswith(('lib.storage.', 'lib.storage_sidecar')):
            raise ModuleNotFoundError('Application storage absent: ' + fullname, name=fullname)
sys.meta_path.insert(0, NoApplicationStorage())
original = Path.open
def no_repository_contract(self, *args, **kwargs):
    if self.name == 'tool_result_query_v1.json':
        raise FileNotFoundError('Repository contracts are absent in a wheel')
    return original(self, *args, **kwargs)
Path.open = no_repository_contract
from lib.tools.result_value import capture_result, compose_result, CanonicalReceipt
from lib.tool_result_query_contract import LIMITS
from lib.tasks_pkg.compaction._budget import budget_tool_result_v2
source = capture_result('original')
receipt = CanonicalReceipt(name='read', call_id='call', canonical=source)
result = compose_result('preview', [receipt])
assert result.canonical.text() == 'original'
assert LIMITS['artifactBytes'] == 16 * 1024 * 1024

import json
view = budget_tool_result_v2('execute_tools', result, user_id=0)
assert json.loads(view)['retentionCoverage'] == 'complete'
assert not any(n == 'lib.storage' or n.startswith('lib.storage.') for n in sys.modules)
print('clone-free canonical capture OK')
'''
    result = subprocess.run([sys.executable, '-c', probe], cwd=ROOT,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'clone-free canonical capture OK'
