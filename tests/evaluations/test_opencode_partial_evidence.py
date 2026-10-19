"""A cancelled baseline retains its available CLI trace before guest teardown."""
import asyncio
import importlib
from types import SimpleNamespace

import pytest
from tests.tools.test_harbor_tofu_runtime_agent import _install_fake_harbor

pytestmark = pytest.mark.unit


def test_cancelled_opencode_preserves_partial_evidence_before_cleanup(tmp_path, monkeypatch):
    _install_fake_harbor(monkeypatch)
    monkeypatch.delitem(__import__('sys').modules, 'rootless_vm.harbor_opencode_agent', raising=False)
    module = importlib.import_module('rootless_vm.harbor_opencode_agent')
    order = []

    class Environment:
        async def upload_file(self, *args):
            pass

        async def exec(self, command, **kwargs):
            if command.startswith('rm -rf '):
                order.append('cleanup')
                return
            raise asyncio.CancelledError()

        async def download_file(self, source, target):
            order.append('download')
            target.write_text('partial trace\n')

    agent = SimpleNamespace(_binary_info={}, _seed_info={},
        _guest_proxy_url='http://10.0.2.101:8765', context_id='test', session_id='test',
        logs_dir=tmp_path, _model='glm-5.3-m17', _context_tokens=200000,
        _output_tokens=65536, _max_continuations=16, _timeout_sec=1)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(module.OpenCodeGuestAgent.run(agent, 'Task', Environment(), SimpleNamespace()))
    assert order == ['download', 'download', 'cleanup']
    assert (tmp_path/'opencode-evidence/opencode-events.jsonl').read_text() == 'partial trace\n'
    assert not (tmp_path/'trajectory.json').exists()
